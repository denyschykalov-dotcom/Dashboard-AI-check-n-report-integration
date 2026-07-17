"""GA4-sheet-backed blocks: summary, top landing pages, monetization,
AI traffic, and the bar variant of session-mix-by-channel.

Resolves the client's sheet via ``client.ga4_sheet_id`` if already set,
otherwise by looking it up by name in the shared client Drive folder (see
``sheets_client.resolve_client_sheet_id``). Reads the tabs the Apps Script
collector populates (README~1.MD §2/§3): GA4 Summary / Channels / Daily /
Events / Top Pages / Ecommerce / Ecommerce Organic / AI Summary / AI Traffic /
AI Top Pages — trying known alternate tab names too, since different client
sheets in practice use slightly different titles for the same data (e.g.
"GA4 Summary" vs "GA4 Overview", "GA4 Events" vs "GA4 Key Events"). A missing
sheet, or any read failure, resolves ``unavailable`` (spec FR-006).
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


# canonical tab name -> known alternate titles seen across real client sheets,
# in priority order (first match wins).
_TAB_ALIASES: dict[str, list[str]] = {
    "GA4 Summary": ["GA4 Summary", "GA4 Overview"],
    "GA4 Channels": ["GA4 Channels"],
    "GA4 Daily": ["GA4 Daily"],
    "GA4 Events": ["GA4 Events", "GA4 Key Events"],
    "GA4 Top Pages": ["GA4 Top Pages"],
    "GA4 Ecommerce": ["GA4 Ecommerce"],
    "GA4 Ecommerce Organic": ["GA4 Ecommerce Organic"],
    "GA4 AI Summary": ["GA4 AI Summary"],
    "GA4 AI Traffic": ["GA4 AI Traffic"],
    "GA4 AI Top Pages": ["GA4 AI Top Pages"],
}

_TOP_PAGES_LIMIT = 20
_TOP_EVENTS_LIMIT = 10


def _num(value: typing.Optional[str]) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _int(value: typing.Optional[str]) -> int:
    return int(_num(value))


def _load_tabs(context: ResolveContext, sheet_id: str) -> dict[str, list[dict[str, str]]]:
    cache_key = ("ga4_sheet_tabs", sheet_id)
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
        "sessions": _int(row.get("Sessions")),
        "organic_sessions": _int(row.get("Organic Sessions")),
        "total_users": _int(row.get("Total Users")),
        "new_users": _int(row.get("New Users")),
        "returning_users": _int(row.get("Returning Users")),
        "engaged_sessions": _int(row.get("Engaged Sessions")),
        "engagement_rate": _num(row.get("Engagement Rate %")),
        "bounce_rate": _num(row.get("Bounce Rate %")),
        "avg_session_duration_seconds": _num(row.get("Avg Session Duration (s)")),
        "page_views": _int(row.get("Page Views")),
        "pages_per_session": _num(row.get("Pages/Session")),
        "key_events": _int(row.get("Key Events")),
    }


def _ecommerce_kpi(row: typing.Optional[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if row is None:
        return None
    return {
        "purchases": _int(row.get("Purchases")),
        "revenue": _num(row.get("Revenue")),
        "add_to_carts": _int(row.get("Add to Carts")),
        "checkouts": _int(row.get("Checkouts")),
    }


def _ai_summary_kpi(row: typing.Optional[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if row is None:
        return None
    return {
        "total_ai_sessions": _int(row.get("Total AI Sessions")),
        "engaged_sessions": _int(row.get("Engaged Sessions")),
        "engagement_rate": _num(row.get("Engagement Rate %")),
    }


def _channel_mix(tabs: dict, period_label: typing.Optional[str]) -> list[dict[str, object]]:
    rows = _period_rows(tabs.get("GA4 Channels", []), period_label)
    items = [
        {
            "channel": row.get("Channel", ""),
            "sessions": _int(row.get("Sessions")),
            "engaged_sessions": _int(row.get("Engaged Sessions")),
            "users": _int(row.get("Users")),
        }
        for row in rows
    ]
    items.sort(key=lambda item: item["sessions"], reverse=True)
    return items


def _daily_rows(tabs: dict, period_label: typing.Optional[str]) -> list[dict[str, object]]:
    rows = _period_rows(tabs.get("GA4 Daily", []), period_label)
    return [
        {
            "date": row.get("Date", ""),
            "sessions": _int(row.get("Sessions")),
            "engaged_sessions": _int(row.get("Engaged Sessions")),
            "users": _int(row.get("Users")),
        }
        for row in rows
    ]


def _top_events(tabs: dict, period_label: typing.Optional[str]) -> list[dict[str, object]]:
    rows = _period_rows(tabs.get("GA4 Events", []), period_label)
    items = [
        {
            "event_name": row.get("Event Name", ""),
            "count": _int(row.get("Count")),
            "users": _int(row.get("Users")),
        }
        for row in rows
    ]
    items.sort(key=lambda item: item["count"], reverse=True)
    return items[:_TOP_EVENTS_LIMIT]


def _resolve_summary(tabs: dict, periods: dict) -> BlockResult:
    summary_rows = tabs.get("GA4 Summary", [])
    kpis = {
        "current": _summary_kpi(_period_row(summary_rows, periods["current"])),
        "previous": _summary_kpi(_period_row(summary_rows, periods["previous"])),
        "yoy": _summary_kpi(_period_row(summary_rows, periods["yoy"])),
    }
    return BlockResult.ok(
        {
            "period": periods["current"],
            "previous_period": periods["previous"],
            "yoy_period": periods["yoy"],
            "kpis": kpis,
            "channels": _channel_mix(tabs, periods["current"]),
            "daily": _daily_rows(tabs, periods["current"]),
            "top_events": _top_events(tabs, periods["current"]),
        }
    )


def _resolve_channel_mix_bar(tabs: dict, periods: dict) -> BlockResult:
    channels = _channel_mix(tabs, periods["current"])
    if not channels:
        return BlockResult.unavailable(f"No channel data found for {periods['current']}.")
    return BlockResult.ok({"period": periods["current"], "channels": channels})


def _resolve_top_pages(tabs: dict, periods: dict) -> BlockResult:
    rows = _period_rows(tabs.get("GA4 Top Pages", []), periods["current"])
    if not rows:
        return BlockResult.unavailable(f"No top-pages data found for {periods['current']}.")
    items = [
        {
            "page": row.get("Landing Page", ""),
            "sessions": _int(row.get("Sessions")),
            "engaged_sessions": _int(row.get("Engaged Sessions")),
            "key_events": _int(row.get("Key Events")),
            "bounce_rate": _num(row.get("Bounce Rate %")),
        }
        for row in rows
    ]
    items.sort(key=lambda item: item["sessions"], reverse=True)
    return BlockResult.ok({"period": periods["current"], "pages": items[:_TOP_PAGES_LIMIT]})


def _resolve_monetization(tabs: dict, periods: dict) -> BlockResult:
    site_rows = tabs.get("GA4 Ecommerce", [])
    organic_rows = tabs.get("GA4 Ecommerce Organic", [])
    return BlockResult.ok(
        {
            "period": periods["current"],
            "previous_period": periods["previous"],
            "yoy_period": periods["yoy"],
            "site_wide": {
                "current": _ecommerce_kpi(_period_row(site_rows, periods["current"])),
                "previous": _ecommerce_kpi(_period_row(site_rows, periods["previous"])),
                "yoy": _ecommerce_kpi(_period_row(site_rows, periods["yoy"])),
            },
            "organic": {
                "current": _ecommerce_kpi(_period_row(organic_rows, periods["current"])),
                "previous": _ecommerce_kpi(_period_row(organic_rows, periods["previous"])),
                "yoy": _ecommerce_kpi(_period_row(organic_rows, periods["yoy"])),
            },
            "note": (
                "Organic-only figures are read directly from the client sheet's "
                "'GA4 Ecommerce Organic' tab (previously a manual-process limitation)."
            ),
        }
    )


def _resolve_ai_traffic(tabs: dict, periods: dict) -> BlockResult:
    summary_rows = tabs.get("GA4 AI Summary", [])
    tools_rows = _period_rows(tabs.get("GA4 AI Traffic", []), periods["current"])
    top_pages_rows = _period_rows(tabs.get("GA4 AI Top Pages", []), periods["current"])

    tools = sorted(
        (
            {
                "source": row.get("Source", ""),
                "sessions": _int(row.get("Sessions")),
                "engaged_sessions": _int(row.get("Engaged Sessions")),
            }
            for row in tools_rows
        ),
        key=lambda item: item["sessions"],
        reverse=True,
    )
    top_pages = sorted(
        (
            {
                "page": row.get("Landing Page", ""),
                "sessions": _int(row.get("Sessions")),
                "engaged_sessions": _int(row.get("Engaged Sessions")),
            }
            for row in top_pages_rows
        ),
        key=lambda item: item["sessions"],
        reverse=True,
    )

    return BlockResult.ok(
        {
            "period": periods["current"],
            "previous_period": periods["previous"],
            "yoy_period": periods["yoy"],
            "summary": {
                "current": _ai_summary_kpi(_period_row(summary_rows, periods["current"])),
                "previous": _ai_summary_kpi(_period_row(summary_rows, periods["previous"])),
                "yoy": _ai_summary_kpi(_period_row(summary_rows, periods["yoy"])),
            },
            "tools": tools,
            "top_pages": top_pages,
        }
    )


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    try:
        sheet_id = resolve_client_sheet_id(context)
    except SheetsAccessError as error:
        return BlockResult.unavailable(str(error))
    if not sheet_id:
        return BlockResult.unavailable(
            "No GA4 sheet linked, and no matching sheet found in the client Drive folder."
        )

    try:
        tabs = _load_tabs(context, sheet_id)
    except SheetsAccessError as error:
        return BlockResult.unavailable(str(error))

    periods = resolve_periods(row.get("Period", "") for row in tabs.get("GA4 Summary", []))
    if not periods.get("current"):
        return BlockResult.unavailable("Could not determine the current reporting period from the GA4 sheet.")

    if block.key == "ga4_summary":
        return _resolve_summary(tabs, periods)
    if block.key == "ga4_session_mix_bar":
        return _resolve_channel_mix_bar(tabs, periods)
    if block.key == "ga4_top_pages":
        return _resolve_top_pages(tabs, periods)
    if block.key == "ga4_monetization":
        return _resolve_monetization(tabs, periods)
    if block.key == "ga4_ai_traffic":
        return _resolve_ai_traffic(tabs, periods)
    return BlockResult.unavailable(f"No GA4 resolver for block '{block.key}'.")
