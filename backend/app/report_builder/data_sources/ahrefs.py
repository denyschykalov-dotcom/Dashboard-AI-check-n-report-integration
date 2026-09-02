"""Ahrefs-backed blocks: domain analysis and top movers.

Resolves from ``client.domain`` via the Ahrefs API v3 (Site Explorer):

* ``ahrefs_domain_analysis`` — Domain Rating + Ahrefs Rank (``domain-rating``),
  backlink/refdomain profile (``backlinks-stats``), organic/paid metrics for
  current / previous-month / year-over-year (``metrics`` x3), and a 14-month
  organic-traffic trend (``metrics-history``).
* ``ahrefs_top_movers`` — top 20 traffic gainers and top 20 losers month over
  month (``top-pages`` with ``date_compared``).

Any missing domain or API failure resolves ``unavailable`` (spec FR-006).
"""

from __future__ import annotations

import typing

from datetime import date

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources import ahrefs_client
from backend.app.report_builder.data_sources.ahrefs_client import AhrefsAccessError, ReportDates
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


# Every Site Explorer call that takes a mode uses this one. "domain" excludes
# the www subdomain, so a site served from www.<domain> reported zero organic
# traffic, zero keywords, an empty trend chart and a fraction of its backlinks —
# while Top movers, which already used "subdomains", listed its www pages. Seen
# on eatlebab.com: 0 vs 5,910 organic visits. For a site that is not on a
# subdomain the two modes return identical numbers.
_MODE = "subdomains"  # cover root + www + all paths on the domain
_MOVERS_LIMIT = 20
_MOVERS_SELECT = ",".join(
    [
        "url",
        "sum_traffic",
        "sum_traffic_prev",
        "traffic_diff",
        "keywords",
        "top_keyword",
        "top_keyword_volume",
        "top_keyword_best_position",
        "top_keyword_best_position_prev",
    ]
)


def _num(value: typing.Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: typing.Any) -> int:
    return int(_num(value))


def _metrics_for(target: str, when: date) -> dict[str, object]:
    payload = ahrefs_client.get(
        "metrics",
        {"target": target, "date": when.isoformat(), "mode": _MODE, "volume_mode": "monthly"},
    )
    m = payload.get("metrics", {}) or {}
    return {
        "org_keywords": _int(m.get("org_keywords")),
        "org_keywords_top3": _int(m.get("org_keywords_1_3")),
        "paid_keywords": _int(m.get("paid_keywords")),
        "org_traffic": _int(m.get("org_traffic")),
        "org_cost_cents": _int(m.get("org_cost")),
        "paid_traffic": _int(m.get("paid_traffic")),
        "paid_cost_cents": _int(m.get("paid_cost")),
        "paid_pages": _int(m.get("paid_pages")),
    }


def _load_domain_analysis(context: ResolveContext, dates: ReportDates) -> dict[str, object]:
    cache_key = ("ahrefs_domain_analysis", context.client.domain)
    if cache_key in context.cache:
        return context.cache[cache_key]

    target = context.client.domain

    dr_payload = ahrefs_client.get(
        "domain-rating", {"target": target, "date": dates.current.isoformat()}
    ).get("domain_rating", {}) or {}

    bl = ahrefs_client.get(
        "backlinks-stats",
        {"target": target, "date": dates.current.isoformat(), "mode": _MODE},
    ).get("metrics", {}) or {}

    history = ahrefs_client.get(
        "metrics-history",
        {
            "target": target,
            "date_from": dates.trend_from.isoformat(),
            "date_to": dates.current.isoformat(),
            "history_grouping": "monthly",
            "mode": _MODE,
            "volume_mode": "monthly",
        },
    ).get("metrics", []) or []

    trend = [
        [str(point.get("date", ""))[:7], _int(point.get("org_traffic"))]
        for point in history
    ]

    result = {
        "period": dates.current_label,
        "previous_period": dates.previous_label,
        "yoy_period": dates.yoy_label,
        "domain_rating": _num(dr_payload.get("domain_rating")),
        "ahrefs_rank": _int(dr_payload.get("ahrefs_rank")),
        "backlinks": {
            "live": _int(bl.get("live")),
            "all_time": _int(bl.get("all_time")),
            "live_refdomains": _int(bl.get("live_refdomains")),
            "all_time_refdomains": _int(bl.get("all_time_refdomains")),
        },
        "metrics": {
            "current": _metrics_for(target, dates.current),
            "previous": _metrics_for(target, dates.previous),
            "yoy": _metrics_for(target, dates.yoy),
        },
        "trend": trend,
    }
    context.cache[cache_key] = result
    return result


def _movers(target: str, dates: ReportDates, order_by: str) -> list[dict[str, object]]:
    payload = ahrefs_client.get(
        "top-pages",
        {
            "target": target,
            "date": dates.current.isoformat(),
            "date_compared": dates.previous.isoformat(),
            "mode": _MODE,
            "select": _MOVERS_SELECT,
            "order_by": order_by,
            "limit": str(_MOVERS_LIMIT),
        },
    )
    rows = payload.get("pages", []) or []
    return [
        {
            "url": row.get("url", ""),
            "traffic": _int(row.get("sum_traffic")),
            "traffic_prev": _int(row.get("sum_traffic_prev")),
            "traffic_diff": _int(row.get("traffic_diff")),
            "keywords": _int(row.get("keywords")),
            "top_keyword": row.get("top_keyword") or "",
            "top_keyword_volume": _int(row.get("top_keyword_volume")),
            "position": _int(row.get("top_keyword_best_position")),
            "position_prev": _int(row.get("top_keyword_best_position_prev")),
        }
        for row in rows
    ]


def _load_top_movers(context: ResolveContext, dates: ReportDates) -> dict[str, object]:
    cache_key = ("ahrefs_top_movers", context.client.domain)
    if cache_key in context.cache:
        return context.cache[cache_key]
    target = context.client.domain
    result = {
        "period": dates.current_label,
        "previous_period": dates.previous_label,
        "gainers": _movers(target, dates, "traffic_diff:desc"),
        "losers": _movers(target, dates, "traffic_diff:asc"),
    }
    context.cache[cache_key] = result
    return result


def _anchor_date(context: ResolveContext) -> date:
    """The date Ahrefs snapshots are taken relative to.

    Ahrefs metrics are point-in-time, so a range/full-year report anchors on the
    last month of the selection (``resolve_report_dates`` reports the most recent
    *complete* month before the date it's given, so we pass the first day of the
    month after the range end). With no selection this is just today.

    Never anchors in the future: Ahrefs has no snapshot for a month that hasn't
    ended and answers `400 "bad date"`, which took the whole block down. A range
    running into the current month (or a full-year report for the year in
    progress) therefore anchors on today, and the block reports the last complete
    month — its own labels say which month that is.
    """
    selection = context.period_selection
    today = context.now.date()
    if selection is not None:
        end = selection.end
        year = end.year + (end.month // 12)
        month = (end.month % 12) + 1
        return min(date(year, month, 1), today)
    return today


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    target = (context.client.domain or "").strip()
    if not target:
        return BlockResult.unavailable("No domain set for this client.")

    dates = ahrefs_client.resolve_report_dates(_anchor_date(context))
    try:
        if block.key == "ahrefs_domain_analysis":
            return BlockResult.ok(_load_domain_analysis(context, dates))
        if block.key == "ahrefs_top_movers":
            return BlockResult.ok(_load_top_movers(context, dates))
    except AhrefsAccessError as error:
        return BlockResult.unavailable(str(error))
    return BlockResult.unavailable(f"No Ahrefs resolver for block '{block.key}'.")
