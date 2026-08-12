"""SE Ranking-backed block: tracked keyword positions.

Resolves via ``client.se_ranking_target`` (an SE Ranking project id, or a
substring matching a tracked project's URL/name — see
``se_ranking_client.resolve_site_id``). A missing target, no matching
project, or an API failure (e.g. an expired subscription) all yield
``unavailable`` (spec FR-006) rather than raising.
"""

from __future__ import annotations

from datetime import date

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources import se_ranking_client
from backend.app.report_builder.data_sources.ahrefs_client import ReportDates, resolve_report_dates
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext
from backend.app.report_builder.data_sources.se_ranking_client import SeRankingAccessError


# How many keywords the table carries. A tracked project commonly follows several
# hundred; the report shows the 100 best-ranking ones, paged in the template.
_KEYWORDS_LIMIT = 100

# Only keywords the site actually ranks for are reported. SE Ranking writes ``0``
# for "not found within the tracked depth", so an unfiltered table fills up with
# zero rows that tell a client nothing; 99 is the bottom of the useful range.
_MIN_POSITION = 1
_MAX_POSITION = 99


def _anchor_date(context: ResolveContext) -> date:
    """Same "most recent complete month" anchor Ahrefs uses (see
    ``ahrefs._anchor_date``), so both sources report on the same period by
    default."""
    selection = context.period_selection
    if selection is not None:
        end = selection.end
        year = end.year + (end.month // 12)
        month = (end.month % 12) + 1
        return date(year, month, 1)
    return context.now.date()


def _position_on_or_before(positions: list[dict], cutoff: str) -> int:
    """The most recent tracked position on or before ``cutoff`` (``positions``
    is chronological); ``0`` if nothing was tracked by then."""
    best = 0
    for point in positions:
        if str(point.get("date", "")) > cutoff:
            break
        best = int(point.get("pos") or 0)
    return best


def _load_keywords(site_id: int, dates: ReportDates) -> list[list]:
    date_from = dates.previous.replace(day=1).isoformat()
    date_to = dates.current.isoformat()
    engines = se_ranking_client.get_positions(site_id, date_from, date_to)
    keywords = engines[0].get("keywords", []) if engines else []

    rows = []
    for kw in keywords:
        positions = kw.get("positions") or []
        current = _position_on_or_before(positions, date_to)
        if not _MIN_POSITION <= current <= _MAX_POSITION:
            continue
        rows.append([
            kw.get("name", ""),
            int(kw.get("volume") or 0),
            current,
            _position_on_or_before(positions, dates.previous.isoformat()),
        ])
    # Best position first, so the table reads top-down as "where we stand"; volume
    # breaks ties so the more valuable keyword of two at the same rank leads.
    rows.sort(key=lambda row: (row[2], -row[1]))
    return rows[:_KEYWORDS_LIMIT]


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    target = (context.client.se_ranking_target or "").strip()
    if not target:
        return BlockResult.unavailable("Not configured for this client (no SE Ranking target set).")

    try:
        site_id = se_ranking_client.resolve_site_id(target)
        if site_id is None:
            return BlockResult.unavailable(f"No SE Ranking project found matching '{target}'.")

        dates = resolve_report_dates(_anchor_date(context))
        keywords = _load_keywords(site_id, dates)
    except SeRankingAccessError as error:
        return BlockResult.unavailable(str(error))

    return BlockResult.ok({
        "period": dates.current_label,
        "previous_period": dates.previous_label,
        "note": (
            f"Positions as of {dates.current_label}, compared to {dates.previous_label}. "
            f"Showing the top {_KEYWORDS_LIMIT} keywords ranking in positions "
            f"{_MIN_POSITION}–{_MAX_POSITION}; unranked keywords are left out."
        ),
        "keywords": keywords,
    })
