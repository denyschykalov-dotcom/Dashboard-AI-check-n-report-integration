"""Orchestration for the client report builder: clients, generate, save,
reopen, update. Pure DB + catalog logic, deliberately framework-free so it is
unit-testable the same way ``domain.py`` is.
"""

from __future__ import annotations

import typing

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Client, Report, ReportBlock
from backend.app.report_builder.block_catalog import catalog_as_dicts, get_block
from backend.app.report_builder.data_sources import (
    ahrefs,
    ai_visibility,
    clickup,
    ga4,
    gsc,
    se_ranking,
    static_editorial,
)
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext
from backend.app.utils import compact_error_message, utcnow


logger = logging.getLogger("rankberry.report_builder")

Resolver = typing.Callable[[typing.Any, ResolveContext], BlockResult]

_RESOLVERS: dict[str, Resolver] = {
    "static": static_editorial.resolve,
    "editorial": static_editorial.resolve,
    "ahrefs": ahrefs.resolve,
    "ga4_sheet": ga4.resolve,
    "gsc_sheet": gsc.resolve,
    "se_ranking": se_ranking.resolve,
    "clickup": clickup.resolve,
    "ai_visibility": ai_visibility.resolve,
}


def get_block_catalog() -> list[dict[str, object]]:
    return catalog_as_dicts()


# --- Clients ------------------------------------------------------------------

def list_clients(session: Session) -> list[Client]:
    return list(session.execute(select(Client).order_by(Client.name)).scalars())


def create_client(session: Session, *, name: str, domain: str, created_by: uuid.UUID) -> Client:
    cleaned_name = (name or "").strip()
    cleaned_domain = (domain or "").strip()
    if not cleaned_name:
        raise ValueError("Client name is required.")
    if not cleaned_domain:
        raise ValueError("Client domain is required.")
    client = Client(name=cleaned_name, domain=cleaned_domain, created_by=created_by)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def _get_client(session: Session, client_id: uuid.UUID) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError("Client not found.")
    return client


# --- Generate -----------------------------------------------------------------

def _default_period_label(now) -> str:
    return now.strftime("%Y-%m")


def generate(
    session: Session,
    *,
    client_id: uuid.UUID,
    block_keys: list[str],
) -> dict[str, object]:
    if not block_keys:
        raise ValueError("Select at least one block before generating.")
    client = _get_client(session, client_id)
    now = utcnow()
    period_label = _default_period_label(now)
    context = ResolveContext(client=client, period_label=period_label, now=now, session=session)

    blocks: list[dict[str, object]] = []
    for key in block_keys:
        block = get_block(key)
        if block is None:
            blocks.append(
                {
                    "block_type_key": key,
                    "status": "unavailable",
                    "data": None,
                    "unavailable_reason": f"Unknown block type '{key}'.",
                }
            )
            continue
        resolver = _RESOLVERS.get(block.source)
        try:
            if resolver is None:
                result = BlockResult.unavailable(f"No resolver registered for source '{block.source}'.")
            else:
                result = resolver(block, context)
        except Exception as error:  # defensive: a resolver should not raise, but DB/network can
            result = BlockResult.unavailable(compact_error_message(error))
        blocks.append(
            {
                "block_type_key": key,
                "status": result.status,
                "data": result.data,
                "unavailable_reason": result.unavailable_reason,
            }
        )

    unavailable = sum(1 for block in blocks if block["status"] == "unavailable")
    logger.info(
        "report_generate client_id=%s blocks=%s unavailable=%s",
        client_id,
        len(blocks),
        unavailable,
    )
    return {"client_id": str(client_id), "period_label": period_label, "blocks": blocks}


# --- Save / update ------------------------------------------------------------

def _replace_blocks(session: Session, report_id: uuid.UUID, blocks: list[dict[str, object]]) -> None:
    existing = session.execute(
        select(ReportBlock).where(ReportBlock.report_id == report_id)
    ).scalars().all()
    for row in existing:
        session.delete(row)
    for position, block in enumerate(blocks):
        data = block.get("data")
        session.add(
            ReportBlock(
                report_id=report_id,
                block_type_key=str(block.get("block_type_key")),
                position=position,
                data_json=json.dumps(data) if data is not None else None,
                comment=(block.get("comment") or None),
                status=str(block.get("status") or "ok"),
                unavailable_reason=(block.get("unavailable_reason") or None),
            )
        )


def save_report(
    session: Session,
    *,
    client_id: uuid.UUID,
    period_label: str,
    blocks: list[dict[str, object]],
    generated_by: uuid.UUID,
) -> Report:
    _get_client(session, client_id)
    if not blocks:
        raise ValueError("Cannot save a report with no blocks. Generate a report first.")
    report = Report(
        client_id=client_id,
        period_label=(period_label or "").strip() or _default_period_label(utcnow()),
        generated_by=generated_by,
    )
    session.add(report)
    session.flush()  # assign report.id before adding block rows
    _replace_blocks(session, report.id, blocks)
    session.commit()
    session.refresh(report)
    logger.info("report_saved report_id=%s client_id=%s blocks=%s", report.id, client_id, len(blocks))
    return report


def update_report(
    session: Session,
    *,
    report_id: uuid.UUID,
    period_label: typing.Optional[str],
    blocks: list[dict[str, object]],
    generated_by: uuid.UUID,
) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise LookupError("Report not found.")
    if not blocks:
        raise ValueError("Cannot save a report with no blocks.")
    if period_label is not None and period_label.strip():
        report.period_label = period_label.strip()
    report.generated_by = generated_by
    report.updated_at = utcnow()
    _replace_blocks(session, report.id, blocks)
    session.commit()
    session.refresh(report)
    logger.info("report_updated report_id=%s blocks=%s", report.id, len(blocks))
    return report


# --- Read ---------------------------------------------------------------------

def list_reports_for_client(session: Session, client_id: uuid.UUID) -> list[Report]:
    return list(
        session.execute(
            select(Report).where(Report.client_id == client_id).order_by(Report.updated_at.desc())
        ).scalars()
    )


def get_report(session: Session, report_id: uuid.UUID) -> tuple[Report, list[ReportBlock]]:
    report = session.get(Report, report_id)
    if report is None:
        raise LookupError("Report not found.")
    blocks = list(
        session.execute(
            select(ReportBlock).where(ReportBlock.report_id == report_id).order_by(ReportBlock.position)
        ).scalars()
    )
    return report, blocks


# --- Serialization ------------------------------------------------------------

def serialize_client(client: Client) -> dict[str, object]:
    return {
        "id": str(client.id),
        "name": client.name,
        "domain": client.domain,
        "ga4_sheet_id": client.ga4_sheet_id,
        "clickup_list_id": client.clickup_list_id,
        "se_ranking_target": client.se_ranking_target,
        "created_at": client.created_at,
    }


def serialize_block(block: ReportBlock) -> dict[str, object]:
    return {
        "block_type_key": block.block_type_key,
        "status": block.status,
        "data": json.loads(block.data_json) if block.data_json else None,
        "comment": block.comment or "",
        "unavailable_reason": block.unavailable_reason,
    }


def serialize_report_summary(report: Report) -> dict[str, object]:
    return {
        "id": str(report.id),
        "client_id": str(report.client_id),
        "period_label": report.period_label,
        "generated_by": str(report.generated_by),
        "generated_at": report.generated_at,
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


def serialize_report_detail(report: Report, blocks: list[ReportBlock]) -> dict[str, object]:
    detail = serialize_report_summary(report)
    detail["blocks"] = [serialize_block(block) for block in blocks]
    return detail
