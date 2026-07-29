"""Per-user, per-client Report Builder selections.

Stores the last-used block checkbox selection and timeframe for each
(user, client) pair so reopening a client restores where the specialist left
off (spec: "checkboxes have to remain from the previous report"). Pure DB
logic, framework-free so it unit-tests like the rest of ``report_builder``.
"""

from __future__ import annotations

import typing

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import ReportSelection
from backend.app.report_builder.data_sources import periods
from backend.app.utils import utcnow


_VALID_REPORT_TYPES = {"monthly", "yearly"}


def _encode_comparison(
    period_preset: typing.Optional[str], comparisons: typing.Sequence[str]
) -> typing.Optional[str]:
    """Pack the period choice and its comparisons into the single ``comparison``
    column as ``"last_month:mom,yoy"``. None means the specialist used the
    Advanced custom-range / full-year controls instead."""
    preset = (period_preset or "").strip()
    if not preset:
        return None
    modes = periods.normalize_comparisons(comparisons)
    return f"{preset}:{','.join(modes)}"


def _decode_comparison(
    raw: typing.Optional[str],
) -> tuple[typing.Optional[str], list[str]]:
    """Unpack the stored ``comparison`` column, tolerating the pre-multi-select
    format where it held a single preset key ("last_month_vs_year")."""
    text = (raw or "").strip()
    if not text:
        return None, []
    if ":" in text:
        preset, _, modes = text.partition(":")
        preset = preset.strip()
        if not preset:
            return None, []
        return preset, periods.normalize_comparisons(modes.split(","))
    legacy = periods.legacy_comparison_preset(text)
    if legacy is None:
        return None, []
    return legacy


def _get_row(
    session: Session, user_id: uuid.UUID, client_id: uuid.UUID
) -> typing.Optional[ReportSelection]:
    return session.execute(
        select(ReportSelection).where(
            ReportSelection.user_id == user_id,
            ReportSelection.client_id == client_id,
        )
    ).scalar_one_or_none()


def _decode_keys(raw: typing.Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(key) for key in value]


def get_selection(
    session: Session, user_id: uuid.UUID, client_id: uuid.UUID
) -> dict[str, object]:
    """The saved selection for this pair, or sensible empty defaults."""
    row = _get_row(session, user_id, client_id)
    if row is None:
        return {
            "block_keys": [],
            "comparison": None,
            "period_preset": None,
            "comparisons": [],
            "report_type": "monthly",
            "date_from": None,
            "date_to": None,
        }
    period_preset, comparisons = _decode_comparison(row.comparison)
    return {
        "block_keys": _decode_keys(row.block_keys),
        "comparison": row.comparison,
        "period_preset": period_preset,
        "comparisons": comparisons,
        "report_type": row.report_type or "monthly",
        "date_from": row.date_from,
        "date_to": row.date_to,
    }


def save_selection(
    session: Session,
    user_id: uuid.UUID,
    client_id: uuid.UUID,
    *,
    block_keys: typing.Sequence[str],
    comparison: typing.Optional[str] = None,
    period_preset: typing.Optional[str] = None,
    comparisons: typing.Optional[typing.Sequence[str]] = None,
    report_type: str = "monthly",
    date_from: typing.Optional[str] = None,
    date_to: typing.Optional[str] = None,
) -> dict[str, object]:
    """Insert or update the selection for a (user, client) pair."""
    cleaned_keys = [str(key) for key in block_keys]
    encoded = json.dumps(cleaned_keys)
    normalized_type = report_type if report_type in _VALID_REPORT_TYPES else "monthly"
    if period_preset:
        normalized_comparison = _encode_comparison(period_preset, comparisons or [])
    else:
        # No period preset: keep a legacy preset key as-is so older clients still
        # round-trip; anything else means the Advanced controls were used.
        normalized_comparison = (comparison or "").strip() or None

    row = _get_row(session, user_id, client_id)
    if row is None:
        row = ReportSelection(
            user_id=user_id,
            client_id=client_id,
            block_keys=encoded,
            comparison=normalized_comparison,
            report_type=normalized_type,
            date_from=date_from or None,
            date_to=date_to or None,
        )
        session.add(row)
    else:
        row.block_keys = encoded
        row.comparison = normalized_comparison
        row.report_type = normalized_type
        row.date_from = date_from or None
        row.date_to = date_to or None
        row.updated_at = utcnow()
    session.commit()
    return get_selection(session, user_id, client_id)
