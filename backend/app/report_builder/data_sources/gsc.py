"""Google Search Console sheet-backed blocks: summary, top queries & pages,
and the bar variant of branded-vs-non-branded clicks.

Resolves the client's sheet via ``client.ga4_sheet_id`` if already set,
otherwise by looking it up by name in the shared client Drive folder (GSC tabs
live in the same client sheet as GA4, per README~1.MD §2/§3): GSC Summary /
Positions / Daily / Queries / Top Pages — trying known alternate tab names too
("GSC Summary" vs "GSC Overview", "GSC Queries" vs "GSC Top Queries"), since
different client sheets in practice use slightly different titles. Branded
share is computed from the reporting period's query sample (README's documented
"top-50 sample" approximation), classifying a query as branded when it
contains the client's name — an approximation, same as the original template;
it will not catch every transliteration/spelling variant.

Metrics are keyed by monthly ``Period`` label. A custom range or full-year
report aggregates several months (see :mod:`periods`): clicks/impressions are
summed, CTR is recomputed and average position impression-weighted, and the
position-bucket snapshot uses the window's most recent month.
"""

from __future__ import annotations

import typing

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources import periods
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext
from backend.app.report_builder.data_sources.periods import Window, Windows
from backend.app.report_builder.data_sources.sheets_client import (
    SheetsAccessError,
    fetch_tab_values,
    list_sheet_tabs,
    resolve_client_sheet_id,
    resolve_tab_name,
    rows_to_dicts,
)


_TAB_ALIASES: dict[str, list[str]] = {
    "GSC Summary": ["GSC Summary", "GSC Overview"],
    "GSC Positions": ["GSC Positions"],
    "GSC Daily": ["GSC Daily"],
    "GSC Queries": ["GSC Queries", "GSC Top Queries"],
    "GSC Top Pages": ["GSC Top Pages"],
}

_TOP_LIMIT = 20

_num = periods.num
_int = periods.to_int


def _load_tabs(context: ResolveContext, sheet_id: str) -> dict[str, list[dict[str, str]]]:
    cache_key = ("gsc_sheet_tabs", sheet_id)
    if cache_key in context.cache:
        return context.cache[cache_key]

    titles_cache_key = ("sheet_tab_titles", sheet_id)
    if titles_cache_key in context.cache:
        available = context.cache[titles_cache_key]
    else:
        available = list_sheet_tabs(sheet_id)
        context.cache[titles_cache_key] = available

    resolved_names: dict[str, str] = {}
    for canonical, aliases in _TAB_ALIASES.items():
        actual = resolve_tab_name(available, aliases)
        if actual:
            resolved_names[canonical] = actual

    raw = fetch_tab_values(sheet_id, list(resolved_names.values())) if resolved_names else {}
    parsed = {
        canonical: rows_to_dicts(raw.get(resolved_names[canonical], []))
        for canonical in resolved_names
    }
    for canonical in _TAB_ALIASES:
        parsed.setdefault(canonical, [])

    context.cache[cache_key] = parsed
    return parsed


def _snapshot_row(rows: list[dict[str, str]], window: Window) -> typing.Optional[dict[str, str]]:
    """The row for the window's most recent month — for point-in-time snapshots
    (position buckets) that can't be summed across months."""
    latest = window.latest
    if not latest:
        return None
    for row in rows:
        if (row.get("Period") or "").strip() == latest:
            return row
    return None


def _summary_kpi(rows: list[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if not rows:
        return None
    single = len(rows) == 1
    clicks = periods.sum_int(rows, "Clicks")
    impressions = periods.sum_int(rows, "Impressions")
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": _num(rows[0].get("CTR %")) if single else periods.ratio_pct(clicks, impressions),
        "avg_position": (
            _num(rows[0].get("Avg Position"))
            if single
            else periods.weighted_avg(rows, "Avg Position", "Impressions")
        ),
    }


def _positions_kpi(row: typing.Optional[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if row is None:
        return None
    return {
        "top3": _int(row.get("Top-3")),
        "top5": _int(row.get("Top-5")),
        "top10": _int(row.get("Top-10")),
        "top20": _int(row.get("Top-20")),
        "top50": _int(row.get("Top-50")),
        "total_sampled": _int(row.get("Total Sampled")),
    }


def _daily_rows(tabs: dict, window: Window) -> list[dict[str, object]]:
    rows = periods.window_rows(tabs.get("GSC Daily", []), window)
    items = [
        {
            "date": row.get("Date", ""),
            "clicks": _int(row.get("Clicks")),
            "impressions": _int(row.get("Impressions")),
            "ctr": _num(row.get("CTR %")),
            "avg_position": _num(row.get("Avg Position")),
        }
        for row in rows
    ]
    items.sort(key=lambda item: item["date"])
    return items


def _is_branded(query: str, client_name: str) -> bool:
    normalized_query = (query or "").lower().replace(" ", "")
    normalized_name = (client_name or "").lower().replace(" ", "")
    return bool(normalized_name) and normalized_name in normalized_query


def _branded_summary(tabs: dict, window: Window, client_name: str) -> dict[str, object]:
    rows = periods.window_rows(tabs.get("GSC Queries", []), window)
    total_clicks = sum(_int(row.get("Clicks")) for row in rows)
    branded_clicks = sum(
        _int(row.get("Clicks")) for row in rows if _is_branded(row.get("Query", ""), client_name)
    )
    share = round((branded_clicks / total_clicks) * 100, 1) if total_clicks else 0.0
    return {
        "branded_clicks": branded_clicks,
        "non_branded_clicks": max(total_clicks - branded_clicks, 0),
        "total_clicks": total_clicks,
        "branded_share_pct": share,
        "sample_size": len(rows),
        "note": "Computed from the reporting period's query sample — an approximation, not the full query set.",
    }


def _aggregate_items(
    rows: list[dict[str, str]], key_field: str, label_key: str
) -> list[dict[str, object]]:
    items = []
    for key, group in periods.group_by(rows, key_field).items():
        single = len(group) == 1
        clicks = periods.sum_int(group, "Clicks")
        impressions = periods.sum_int(group, "Impressions")
        items.append(
            {
                label_key: key,
                "clicks": clicks,
                "impressions": impressions,
                "ctr": _num(group[0].get("CTR %")) if single else periods.ratio_pct(clicks, impressions),
                "avg_position": (
                    _num(group[0].get("Avg Position"))
                    if single
                    else periods.weighted_avg(group, "Avg Position", "Impressions")
                ),
            }
        )
    items.sort(key=lambda item: item["clicks"], reverse=True)
    return items


def _resolve_summary(tabs: dict, windows: Windows, client_name: str) -> BlockResult:
    summary_rows = tabs.get("GSC Summary", [])
    positions_rows = tabs.get("GSC Positions", [])
    return BlockResult.ok(
        {
            "period": windows.current.display,
            "previous_period": windows.previous.display,
            "yoy_period": windows.yoy.display,
            "kpis": {
                "current": _summary_kpi(periods.window_rows(summary_rows, windows.current)),
                "previous": _summary_kpi(periods.window_rows(summary_rows, windows.previous)),
                "yoy": _summary_kpi(periods.window_rows(summary_rows, windows.yoy)),
            },
            "positions": {
                "current": _positions_kpi(_snapshot_row(positions_rows, windows.current)),
                "previous": _positions_kpi(_snapshot_row(positions_rows, windows.previous)),
                "yoy": _positions_kpi(_snapshot_row(positions_rows, windows.yoy)),
            },
            "daily": _daily_rows(tabs, windows.current),
            "daily_previous": _daily_rows(tabs, windows.previous),
            "daily_yoy": _daily_rows(tabs, windows.yoy),
            "branded": _branded_summary(tabs, windows.current, client_name),
        }
    )


def _resolve_branded_bar(tabs: dict, windows: Windows, client_name: str) -> BlockResult:
    branded = _branded_summary(tabs, windows.current, client_name)
    if not branded["total_clicks"]:
        return BlockResult.unavailable(f"No query click data found for {windows.current.display}.")
    return BlockResult.ok({"period": windows.current.display, "branded": branded})


def _resolve_top_queries(tabs: dict, windows: Windows) -> BlockResult:
    query_rows = periods.window_rows(tabs.get("GSC Queries", []), windows.current)
    page_rows = periods.window_rows(tabs.get("GSC Top Pages", []), windows.current)
    if not query_rows and not page_rows:
        return BlockResult.unavailable(f"No query/page data found for {windows.current.display}.")

    queries = _aggregate_items(query_rows, "Query", "query")
    pages = _aggregate_items(page_rows, "Page", "page")

    return BlockResult.ok(
        {
            "period": windows.current.display,
            "queries": queries[:_TOP_LIMIT],
            "pages": pages[:_TOP_LIMIT],
        }
    )


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    try:
        sheet_id = resolve_client_sheet_id(context)
    except SheetsAccessError as error:
        return BlockResult.unavailable(str(error))
    if not sheet_id:
        return BlockResult.unavailable(
            "No GA4/GSC sheet linked, and no matching sheet found in the client Drive folder."
        )

    try:
        tabs = _load_tabs(context, sheet_id)
    except SheetsAccessError as error:
        return BlockResult.unavailable(str(error))

    windows = periods.resolve_windows(
        (row.get("Period", "") for row in tabs.get("GSC Summary", [])),
        context.period_selection,
    )
    if not windows.current.labels:
        return BlockResult.unavailable("Could not determine the current reporting period from the GSC sheet.")

    client_name = context.client.name or ""
    if block.key == "gsc_summary":
        return _resolve_summary(tabs, windows, client_name)
    if block.key == "gsc_branded_bar":
        return _resolve_branded_bar(tabs, windows, client_name)
    if block.key == "gsc_top_queries":
        return _resolve_top_queries(tabs, windows)
    return BlockResult.unavailable(f"No GSC resolver for block '{block.key}'.")
