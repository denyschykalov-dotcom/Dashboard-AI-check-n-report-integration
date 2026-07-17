"""Google Search Console sheet-backed blocks: summary, top queries & pages,
and the bar variant of branded-vs-non-branded clicks.

Resolves the client's sheet via ``client.ga4_sheet_id`` if already set,
otherwise by looking it up by name in the shared client Drive folder (GSC tabs
live in the same client sheet as GA4, per README~1.MD §2/§3): GSC Summary /
Positions / Daily / Queries / Top Pages — trying known alternate tab names too
("GSC Summary" vs "GSC Overview", "GSC Queries" vs "GSC Top Queries"), since
different client sheets in practice use slightly different titles. Branded
share is computed from the current period's query sample (README's documented
"top-50 sample" approximation), classifying a query as branded when it
contains the client's name — an approximation, same as the original template;
it will not catch every transliteration/spelling variant.
"""

from __future__ import annotations

import typing

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext
from backend.app.report_builder.data_sources.sheets_client import (
    SheetsAccessError,
    fetch_tab_values,
    list_sheet_tabs,
    resolve_client_sheet_id,
    resolve_periods,
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


def _num(value: typing.Optional[str]) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _int(value: typing.Optional[str]) -> int:
    return int(_num(value))


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


def _period_rows(rows: list[dict[str, str]], period_label: typing.Optional[str]) -> list[dict[str, str]]:
    if not period_label:
        return []
    return [row for row in rows if (row.get("Period") or "").strip() == period_label]


def _period_row(rows: list[dict[str, str]], period_label: typing.Optional[str]) -> typing.Optional[dict[str, str]]:
    matches = _period_rows(rows, period_label)
    return matches[0] if matches else None


def _summary_kpi(row: typing.Optional[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if row is None:
        return None
    return {
        "clicks": _int(row.get("Clicks")),
        "impressions": _int(row.get("Impressions")),
        "ctr": _num(row.get("CTR %")),
        "avg_position": _num(row.get("Avg Position")),
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


def _daily_rows(tabs: dict, period_label: typing.Optional[str]) -> list[dict[str, object]]:
    rows = _period_rows(tabs.get("GSC Daily", []), period_label)
    return [
        {
            "date": row.get("Date", ""),
            "clicks": _int(row.get("Clicks")),
            "impressions": _int(row.get("Impressions")),
            "ctr": _num(row.get("CTR %")),
            "avg_position": _num(row.get("Avg Position")),
        }
        for row in rows
    ]


def _is_branded(query: str, client_name: str) -> bool:
    normalized_query = (query or "").lower().replace(" ", "")
    normalized_name = (client_name or "").lower().replace(" ", "")
    return bool(normalized_name) and normalized_name in normalized_query


def _branded_summary(tabs: dict, period_label: typing.Optional[str], client_name: str) -> dict[str, object]:
    rows = _period_rows(tabs.get("GSC Queries", []), period_label)
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
        "note": "Computed from the current period's query sample — an approximation, not the full query set.",
    }


def _resolve_summary(tabs: dict, periods: dict, client_name: str) -> BlockResult:
    summary_rows = tabs.get("GSC Summary", [])
    positions_rows = tabs.get("GSC Positions", [])
    return BlockResult.ok(
        {
            "period": periods["current"],
            "previous_period": periods["previous"],
            "yoy_period": periods["yoy"],
            "kpis": {
                "current": _summary_kpi(_period_row(summary_rows, periods["current"])),
                "previous": _summary_kpi(_period_row(summary_rows, periods["previous"])),
                "yoy": _summary_kpi(_period_row(summary_rows, periods["yoy"])),
            },
            "positions": {
                "current": _positions_kpi(_period_row(positions_rows, periods["current"])),
                "previous": _positions_kpi(_period_row(positions_rows, periods["previous"])),
                "yoy": _positions_kpi(_period_row(positions_rows, periods["yoy"])),
            },
            "daily": _daily_rows(tabs, periods["current"]),
            "branded": _branded_summary(tabs, periods["current"], client_name),
        }
    )


def _resolve_branded_bar(tabs: dict, periods: dict, client_name: str) -> BlockResult:
    branded = _branded_summary(tabs, periods["current"], client_name)
    if not branded["total_clicks"]:
        return BlockResult.unavailable(f"No query click data found for {periods['current']}.")
    return BlockResult.ok({"period": periods["current"], "branded": branded})


def _resolve_top_queries(tabs: dict, periods: dict) -> BlockResult:
    query_rows = _period_rows(tabs.get("GSC Queries", []), periods["current"])
    page_rows = _period_rows(tabs.get("GSC Top Pages", []), periods["current"])
    if not query_rows and not page_rows:
        return BlockResult.unavailable(f"No query/page data found for {periods['current']}.")

    def _to_item(row: dict[str, str], label_field: str, label_key: str) -> dict[str, object]:
        return {
            label_key: row.get(label_field, ""),
            "clicks": _int(row.get("Clicks")),
            "impressions": _int(row.get("Impressions")),
            "ctr": _num(row.get("CTR %")),
            "avg_position": _num(row.get("Avg Position")),
        }

    queries = sorted((_to_item(row, "Query", "query") for row in query_rows), key=lambda item: item["clicks"], reverse=True)
    pages = sorted((_to_item(row, "Page", "page") for row in page_rows), key=lambda item: item["clicks"], reverse=True)

    return BlockResult.ok(
        {
            "period": periods["current"],
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

    periods = resolve_periods(row.get("Period", "") for row in tabs.get("GSC Summary", []))
    if not periods.get("current"):
        return BlockResult.unavailable("Could not determine the current reporting period from the GSC sheet.")

    client_name = context.client.name or ""
    if block.key == "gsc_summary":
        return _resolve_summary(tabs, periods, client_name)
    if block.key == "gsc_branded_bar":
        return _resolve_branded_bar(tabs, periods, client_name)
    if block.key == "gsc_top_queries":
        return _resolve_top_queries(tabs, periods)
    return BlockResult.unavailable(f"No GSC resolver for block '{block.key}'.")
