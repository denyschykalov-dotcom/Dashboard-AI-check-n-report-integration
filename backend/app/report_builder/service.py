"""Orchestration for the client report builder: clients, generate, save,
reopen, update. Pure DB + catalog logic, deliberately framework-free so it is
unit-testable the same way ``domain.py`` is.
"""

from __future__ import annotations

import typing

import json
import logging
import re
import time
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app.models import Client, Report, ReportBlock, Run
from backend.app.observability import log_event
from backend.app.report_builder import localization
from backend.app.report_builder.block_catalog import catalog_as_dicts, get_block
from backend.app.report_builder.data_sources import (
    ahrefs,
    ai_visibility,
    clickup,
    ga4,
    gsc,
    periods,
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


def list_collector_sites(session: Session) -> list[dict[str, object]]:
    """The site list the Apps Script collector pulls from.

    Every client is listed, configured or not, each carrying its own reason: the
    collector logs a skip per unconfigured site so a missing property id is
    visible in the run log instead of showing up weeks later as an empty report
    section.
    """
    sites = []
    for client in list_clients(session):
        property_id = (client.ga4_property_id or "").strip()
        sites.append(
            {
                "domain": client.domain,
                "name": client.name,
                "ga4_property_id": property_id or None,
                # None tells the collector to probe for the working form.
                "gsc_property": (client.gsc_property or "").strip() or None,
                "collect": bool(property_id),
                "skip_reason": None if property_id else "No GA4 property id set for this client.",
            }
        )
    return sites


def create_client(
    session: Session,
    *,
    name: str,
    domain: str,
    created_by: uuid.UUID,
    report_language: typing.Optional[str] = None,
) -> Client:
    cleaned_name = (name or "").strip()
    cleaned_domain = (domain or "").strip()
    if not cleaned_name:
        raise ValueError("Client name is required.")
    if not cleaned_domain:
        raise ValueError("Client domain is required.")
    client = Client(
        name=cleaned_name,
        domain=cleaned_domain,
        created_by=created_by,
        report_language=localization.normalize_language(report_language),
    )
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def set_client_language(
    session: Session, *, client_id: uuid.UUID, report_language: str
) -> Client:
    """Change the language this client's reports are delivered in."""
    client = _get_client(session, client_id)
    client.report_language = localization.normalize_language(report_language)
    session.commit()
    session.refresh(client)
    return client


def update_client_settings(
    session: Session,
    *,
    client_id: uuid.UUID,
    se_ranking_target: typing.Optional[str] = None,
    ai_visibility_project: typing.Optional[str] = None,
    ga4_sheet_id: typing.Optional[str] = None,
    ga4_property_id: typing.Optional[str] = None,
    gsc_property: typing.Optional[str] = None,
) -> Client:
    """Set the per-client links this client's data sources need.

    Both are cleared by passing an empty string — ``se_ranking_target`` back to
    "not configured", ``ai_visibility_project`` back to matching on the client's
    name. ``None`` means "leave this one alone", so each field can be updated
    independently.
    """
    client = _get_client(session, client_id)
    if se_ranking_target is not None:
        client.se_ranking_target = se_ranking_target.strip() or None
    if ai_visibility_project is not None:
        client.ai_visibility_project = ai_visibility_project.strip() or None
    if ga4_sheet_id is not None:
        # Clearing it re-enables the Drive lookup, which caches its guess back
        # onto the client — fine when the folder holds one sheet per client,
        # wrong when it holds a live one and an abandoned one.
        client.ga4_sheet_id = _extract_sheet_id(ga4_sheet_id) or None
    if ga4_property_id is not None:
        client.ga4_property_id = _extract_ga4_property_id(ga4_property_id) or None
    if gsc_property is not None:
        # Empty restores the collector's probe, which is the better default than
        # a stale string: a wrong-but-readable property returns zero rows, not an
        # error, so it fails silently.
        client.gsc_property = gsc_property.strip() or None
    session.commit()
    session.refresh(client)
    return client


def _extract_ga4_property_id(value: str) -> str:
    """Accept a bare GA4 property id, "properties/123", or a pasted GA4 URL.

    Specialists copy this out of the GA4 admin screen or the URL bar, where it
    arrives as "properties/509009564" or "...?p=509009564" — the collector needs
    the bare digits.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    match = re.search(r"(?:properties/|[?&]p=)(\d+)", text)
    if match:
        return match.group(1)
    digits = re.sub(r"\D", "", text)
    return digits


def _extract_sheet_id(value: str) -> str:
    """Accept either a bare sheet id or a full Google Sheets URL.

    People copy the address bar, not the id, and a pasted URL that silently
    became the "id" would fail every fetch with a confusing 404.
    """
    text = (value or "").strip()
    if not text:
        return ""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", text)
    return match.group(1) if match else text


def list_ai_visibility_projects(session: Session) -> list[dict[str, object]]:
    """Every AI-check project that has runs, so a client can be pointed at one.

    Cross-user on purpose: a client's visibility shouldn't depend on which staff
    member ran the checks, matching how the blocks themselves aggregate.
    """
    rows = session.execute(
        select(
            func.trim(Run.project).label("project"),
            func.count().label("runs"),
            func.max(Run.created_at).label("last_run_at"),
        )
        .where(Run.project.is_not(None), func.trim(Run.project) != "")
        .group_by(func.trim(Run.project))
        .order_by(func.max(Run.created_at).desc())
    ).all()
    return [
        {"project": project, "runs": int(runs), "last_run_at": last_run_at}
        for project, runs, last_run_at in rows
    ]


def _get_client(session: Session, client_id: uuid.UUID) -> Client:
    client = session.get(Client, client_id)
    if client is None:
        raise LookupError("Client not found.")
    return client


# --- Generate -----------------------------------------------------------------

def _default_period_label(now) -> str:
    return now.strftime("%Y-%m")


def resolve_timeframe(
    now,
    *,
    period_preset: typing.Optional[str] = None,
    comparisons: typing.Optional[typing.Sequence[str]] = None,
    comparison: typing.Optional[str] = None,
    date_from: typing.Optional[str] = None,
    date_to: typing.Optional[str] = None,
    report_type: str = "monthly",
) -> tuple[typing.Optional[periods.PeriodSelection], list[str]]:
    """The reporting window plus the comparisons the report should offer.

    A period preset ("last_month" / "last_3_months") resolves to a concrete month
    window and the specialist's chosen comparisons ride along — each becomes a
    toggle in the exported report. ``comparison`` carries a legacy single-choice
    preset key and is honoured when no period preset is given. Otherwise an
    explicit range (Advanced custom timeframe / full-year report) overrides the
    default "latest month present" behaviour and both comparisons are offered.
    """
    chosen = list(comparisons or [])
    preset = period_preset
    if not preset and comparison:
        legacy = periods.legacy_comparison_preset(comparison)
        if legacy is not None:
            preset, legacy_comparisons = legacy
            chosen = chosen or legacy_comparisons

    selection = periods.parse_period_preset(preset, now)
    if selection is not None:
        return selection, periods.normalize_comparisons(chosen)
    return (
        periods.parse_selection(date_from, date_to, report_type),
        periods.normalize_comparisons(chosen or list(periods.COMPARISON_MODES)),
    )


def generate(
    session: Session,
    *,
    client_id: uuid.UUID,
    block_keys: list[str],
    user_id: typing.Optional[uuid.UUID] = None,
    period_preset: typing.Optional[str] = None,
    comparisons: typing.Optional[typing.Sequence[str]] = None,
    comparison: typing.Optional[str] = None,
    date_from: typing.Optional[str] = None,
    date_to: typing.Optional[str] = None,
    report_type: str = "monthly",
    planned_work_mode: str = "clickup",
    planned_work_text: str = "",
) -> dict[str, object]:
    if not block_keys:
        raise ValueError("Select at least one block before generating.")
    client = _get_client(session, client_id)
    now = utcnow()
    selection, chosen_comparisons = resolve_timeframe(
        now,
        period_preset=period_preset,
        comparisons=comparisons,
        comparison=comparison,
        date_from=date_from,
        date_to=date_to,
        report_type=report_type,
    )
    default_comparison = ",".join(chosen_comparisons)
    if selection is not None:
        period_label = periods.selection_display(selection) or _default_period_label(now)
    else:
        period_label = _default_period_label(now)
    context = ResolveContext(
        client=client,
        period_label=period_label,
        now=now,
        session=session,
        user_id=user_id,
        period_selection=selection,
    )

    manual_plan = (planned_work_mode or "clickup").strip().lower() == "manual"

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
        started = time.perf_counter()
        if key == "planned_works" and manual_plan:
            # Manual plan: the specialist typed the upcoming-period plan directly,
            # so skip ClickUp entirely and store the text as the block payload.
            result = BlockResult.ok({"mode": "manual", "text": planned_work_text or "", "tasks": []})
        else:
            resolver = _RESOLVERS.get(block.source)
            try:
                if resolver is None:
                    result = BlockResult.unavailable(f"No resolver registered for source '{block.source}'.")
                else:
                    result = resolver(block, context)
            except Exception as error:  # defensive: a resolver should not raise, but DB/network can
                result = BlockResult.unavailable(compact_error_message(error))

        # One line per section. This is the record that answers "why is this block
        # empty?" — which source it came from, how long it took, whether it
        # resolved, and crucially which period the data actually covers: a sheet
        # that has not caught up yet silently reports an older month than the one
        # that was asked for.
        data_period = (result.data or {}).get("period") if result.data else None
        log_event(
            logger,
            "block_resolved",
            level=logging.INFO if result.status == "ok" else logging.WARNING,
            client_id=client_id,
            block=key,
            source=block.source,
            status=result.status,
            duration_ms=round((time.perf_counter() - started) * 1000),
            requested_period=period_label,
            data_period=data_period,
            period_drift=bool(data_period and data_period != period_label),
            reason=result.unavailable_reason,
        )
        blocks.append(
            {
                "block_type_key": key,
                "status": result.status,
                "data": result.data,
                "unavailable_reason": result.unavailable_reason,
            }
        )
        if (
            selection is None
            and result.status == "ok"
            and result.data
            and period_label == _default_period_label(now)
            and result.data.get("period")
        ):
            # Prefer the sheet's own reporting period (e.g. "Jun 2026") over the
            # wall-clock default once a source that actually has one resolves.
            # Also propagate onto the shared context so blocks resolved later in
            # this same loop (e.g. ClickUp, which comes after GA4/GSC in catalog
            # order) filter against the real reporting period, not today's month.
            period_label = str(result.data["period"])
            context.period_label = period_label

    unavailable = sum(1 for block in blocks if block["status"] == "unavailable")
    logger.info(
        "report_generate client_id=%s blocks=%s unavailable=%s",
        client_id,
        len(blocks),
        unavailable,
    )
    return {
        "client_id": str(client_id),
        "period_label": period_label,
        "default_comparison": default_comparison,
        "blocks": blocks,
    }


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


def _normalize_comparison(value: typing.Optional[str]) -> str:
    """The stored comparison field: a comma-separated list of the comparisons the
    report offers, the first being the one it opens on (e.g. ``"yoy,mom"``).
    Single values from before multi-select ("mom"/"yoy") round-trip unchanged."""
    return ",".join(periods.normalize_comparisons((value or "").split(",")))


def _encode_customization(value: typing.Optional[dict]) -> typing.Optional[str]:
    if not value:
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def save_report(
    session: Session,
    *,
    client_id: uuid.UUID,
    period_label: str,
    blocks: list[dict[str, object]],
    generated_by: uuid.UUID,
    default_comparison: str = "mom",
    customization: typing.Optional[dict] = None,
) -> Report:
    _get_client(session, client_id)
    if not blocks:
        raise ValueError("Cannot save a report with no blocks. Generate a report first.")
    report = Report(
        client_id=client_id,
        period_label=(period_label or "").strip() or _default_period_label(utcnow()),
        default_comparison=_normalize_comparison(default_comparison),
        customization=_encode_customization(customization),
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
    default_comparison: typing.Optional[str] = None,
    customization: typing.Optional[dict] = None,
) -> Report:
    report = session.get(Report, report_id)
    if report is None:
        raise LookupError("Report not found.")
    if not blocks:
        raise ValueError("Cannot save a report with no blocks.")
    if period_label is not None and period_label.strip():
        report.period_label = period_label.strip()
    if default_comparison is not None:
        report.default_comparison = _normalize_comparison(default_comparison)
    if customization is not None:
        report.customization = _encode_customization(customization)
    report.generated_by = generated_by
    report.updated_at = utcnow()
    _replace_blocks(session, report.id, blocks)
    session.commit()
    session.refresh(report)
    invalidate_report_cache(report.id)
    logger.info("report_updated report_id=%s blocks=%s", report.id, len(blocks))
    return report


# --- Read ---------------------------------------------------------------------

def list_reports_for_client(session: Session, client_id: uuid.UUID) -> list[Report]:
    return list(
        session.execute(
            select(Report).where(Report.client_id == client_id).order_by(Report.updated_at.desc())
        ).scalars()
    )


def delete_report(session: Session, report_id: uuid.UUID) -> None:
    """Remove a saved report and its blocks.

    The blocks carry no foreign-key cascade, so they go first — the report row
    is the only handle on them and orphaning them would leak the (often large)
    ``data_json`` payloads with no way to find them again.
    """
    report = session.get(Report, report_id)
    if report is None:
        raise LookupError("Report not found.")

    session.execute(delete(ReportBlock).where(ReportBlock.report_id == report_id))
    session.delete(report)
    session.commit()
    invalidate_report_cache(report_id)


# --- Report read cache --------------------------------------------------------
# ``ReportBlock.data_json`` holds the whole GA4/GSC/Ahrefs/SE Ranking/ClickUp
# payload a block renders from — a few hundred KB to a few MB per report — and
# every read path funnels through get_report(): reopening a report, and each of
# the html/pdf/md exports, which are separate requests. Finishing one report
# means opening it, tweaking, exporting to check, tweaking again, exporting for
# the client: the same payload pulled out of Supabase five or six times over.
#
# The blocks only change when the report is saved, so the cache is invalidated on
# write rather than relying on the TTL for correctness. The TTL is the backstop
# for the one case invalidation cannot cover: uvicorn is deployed single-process
# (no --workers in the systemd unit), but if that ever changes, a write handled
# by one process would leave another process's copy stale until it expires.
REPORT_CACHE_TTL_SECONDS = 300.0
# Bound on cached reports. Entries are large, so this is a memory ceiling first
# and a hit-rate knob second — the working set is however many reports one
# specialist has open, which is realistically one.
_REPORT_CACHE_MAX_ENTRIES = 32
_REPORT_CACHE: dict[uuid.UUID, tuple[float, dict, list[dict]]] = {}

# Cached as plain values rather than ORM instances: a Report loaded in one
# request's session must not outlive it, and rebuilding transient copies per hit
# keeps every caller's signature — and its expectations — unchanged.
_REPORT_SNAPSHOT_FIELDS = (
    "id",
    "client_id",
    "period_label",
    "default_comparison",
    "customization",
    "generated_by",
    "generated_at",
    "created_at",
    "updated_at",
)
_BLOCK_SNAPSHOT_FIELDS = (
    "id",
    "report_id",
    "block_type_key",
    "position",
    "data_json",
    "comment",
    "status",
    "unavailable_reason",
    "created_at",
)


def _snapshot(instance: object, fields: tuple[str, ...]) -> dict:
    return {name: getattr(instance, name) for name in fields}


def invalidate_report_cache(report_id: typing.Optional[uuid.UUID] = None) -> None:
    """Drop one report's cached blocks, or the whole cache when given None."""
    if report_id is None:
        _REPORT_CACHE.clear()
    else:
        _REPORT_CACHE.pop(report_id, None)


def _prune_report_cache(now: float) -> None:
    for key, (expires_at, _, _) in list(_REPORT_CACHE.items()):
        if expires_at <= now:
            _REPORT_CACHE.pop(key, None)
    # Expiry alone can leave the cache full of live entries; drop the one closest
    # to expiring so an insert never grows past the ceiling.
    while len(_REPORT_CACHE) >= _REPORT_CACHE_MAX_ENTRIES:
        soonest = min(_REPORT_CACHE, key=lambda key: _REPORT_CACHE[key][0])
        _REPORT_CACHE.pop(soonest, None)


def get_report(session: Session, report_id: uuid.UUID) -> tuple[Report, list[ReportBlock]]:
    now = time.monotonic()
    cached = _REPORT_CACHE.get(report_id)
    if cached is not None and cached[0] > now:
        _, report_snapshot, block_snapshots = cached
        return (
            Report(**report_snapshot),
            [ReportBlock(**snapshot) for snapshot in block_snapshots],
        )

    report = session.get(Report, report_id)
    if report is None:
        raise LookupError("Report not found.")
    blocks = list(
        session.execute(
            select(ReportBlock).where(ReportBlock.report_id == report_id).order_by(ReportBlock.position)
        ).scalars()
    )

    if len(_REPORT_CACHE) >= _REPORT_CACHE_MAX_ENTRIES:
        _prune_report_cache(now)
    _REPORT_CACHE[report_id] = (
        now + REPORT_CACHE_TTL_SECONDS,
        _snapshot(report, _REPORT_SNAPSHOT_FIELDS),
        [_snapshot(block, _BLOCK_SNAPSHOT_FIELDS) for block in blocks],
    )
    return report, blocks


# --- Serialization ------------------------------------------------------------

def serialize_client(client: Client) -> dict[str, object]:
    return {
        "id": str(client.id),
        "name": client.name,
        "domain": client.domain,
        "ga4_sheet_id": client.ga4_sheet_id,
        "ga4_property_id": client.ga4_property_id,
        "gsc_property": client.gsc_property,
        "clickup_list_id": client.clickup_list_id,
        "se_ranking_target": client.se_ranking_target,
        "ai_visibility_project": client.ai_visibility_project,
        "report_language": localization.normalize_language(client.report_language),
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
        "default_comparison": report.default_comparison or "mom",
        "customization": (
            json.loads(report.customization)
            if report.customization
            else None
        ),
        "generated_by": str(report.generated_by),
        "generated_at": report.generated_at,
        "created_at": report.created_at,
        "updated_at": report.updated_at,
    }


def serialize_report_detail(report: Report, blocks: list[ReportBlock]) -> dict[str, object]:
    detail = serialize_report_summary(report)
    detail["blocks"] = [serialize_block(block) for block in blocks]
    return detail
