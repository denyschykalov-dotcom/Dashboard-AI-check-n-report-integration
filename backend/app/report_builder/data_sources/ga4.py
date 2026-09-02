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

Metrics are keyed by monthly ``Period`` label. A report normally covers a single
month, but a custom range or full-year report aggregates several months at once
(see :mod:`periods`): additive metrics are summed, rates recomputed from their
components or session-weighted.
"""

from __future__ import annotations

import typing

from backend.app.report_builder import localization
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
    "GA4 AI Ecommerce": ["GA4 AI Ecommerce", "GA4 Ecommerce AI"],
    "GA4 AI Summary": ["GA4 AI Summary"],
    "GA4 AI Traffic": ["GA4 AI Traffic"],
    "GA4 AI Top Pages": ["GA4 AI Top Pages"],
}

_TOP_PAGES_LIMIT = 20
_TOP_EVENTS_LIMIT = 10

_num = periods.num
_int = periods.to_int


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


def _summary_kpi(rows: list[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if not rows:
        return None
    single = len(rows) == 1
    sessions = periods.sum_int(rows, "Sessions")
    engaged = periods.sum_int(rows, "Engaged Sessions")
    page_views = periods.sum_int(rows, "Page Views")
    return {
        "sessions": sessions,
        "organic_sessions": periods.sum_int(rows, "Organic Sessions"),
        "total_users": periods.sum_int(rows, "Total Users"),
        "new_users": periods.sum_int(rows, "New Users"),
        "returning_users": periods.sum_int(rows, "Returning Users"),
        "engaged_sessions": engaged,
        "engagement_rate": (
            _num(rows[0].get("Engagement Rate %")) if single else periods.ratio_pct(engaged, sessions)
        ),
        "bounce_rate": (
            _num(rows[0].get("Bounce Rate %"))
            if single
            else periods.weighted_avg(rows, "Bounce Rate %", "Sessions")
        ),
        "avg_session_duration_seconds": (
            _num(rows[0].get("Avg Session Duration (s)"))
            if single
            else periods.weighted_avg(rows, "Avg Session Duration (s)", "Sessions")
        ),
        "page_views": page_views,
        "pages_per_session": (
            _num(rows[0].get("Pages/Session"))
            if single
            else (round(page_views / sessions, 2) if sessions else 0.0)
        ),
        "key_events": periods.sum_int(rows, "Key Events"),
    }


def _ecommerce_kpi(rows: list[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if not rows:
        return None
    return {
        "purchases": periods.sum_int(rows, "Purchases"),
        "revenue": periods.sum_float(rows, "Revenue"),
        "add_to_carts": periods.sum_int(rows, "Add to Carts"),
        "checkouts": periods.sum_int(rows, "Checkouts"),
    }


def _currency_of(*row_groups: list[dict[str, str]]) -> str:
    """The currency the revenue figures are in, read from the sheet.

    The ecommerce tabs carry a "Currency" cell alongside Revenue. It is the only
    place the currency exists — GA4 reports purchaseRevenue in the analytics
    property's own currency and nothing else in the sheet records which that is.
    The tabs are checked in the order given and the first filled cell wins;
    sheets written before the column existed fall back to US dollars.
    """
    for rows in row_groups:
        for row in rows:
            if str(row.get("Currency") or "").strip():
                return localization.currency_symbol(row.get("Currency"))
    return localization.DEFAULT_CURRENCY


def _ai_summary_kpi(rows: list[dict[str, str]]) -> typing.Optional[dict[str, object]]:
    if not rows:
        return None
    single = len(rows) == 1
    sessions = periods.sum_int(rows, "Total AI Sessions")
    engaged = periods.sum_int(rows, "Engaged Sessions")
    return {
        "total_ai_sessions": sessions,
        "engaged_sessions": engaged,
        "engagement_rate": (
            _num(rows[0].get("Engagement Rate %")) if single else periods.ratio_pct(engaged, sessions)
        ),
    }


def _channel_mix(tabs: dict, window: Window) -> list[dict[str, object]]:
    rows = periods.window_rows(tabs.get("GA4 Channels", []), window)
    items = [
        {
            "channel": channel,
            "sessions": periods.sum_int(group, "Sessions"),
            "engaged_sessions": periods.sum_int(group, "Engaged Sessions"),
            "users": periods.sum_int(group, "Users"),
        }
        for channel, group in periods.group_by(rows, "Channel").items()
    ]
    items.sort(key=lambda item: item["sessions"], reverse=True)
    return items


def _daily_rows(tabs: dict, window: Window) -> list[dict[str, object]]:
    rows = periods.window_rows(tabs.get("GA4 Daily", []), window)
    items = [
        {
            "date": row.get("Date", ""),
            "sessions": _int(row.get("Sessions")),
            "engaged_sessions": _int(row.get("Engaged Sessions")),
            "users": _int(row.get("Users")),
        }
        for row in rows
    ]
    items.sort(key=lambda item: item["date"])
    return items


def _top_events(tabs: dict, window: Window) -> list[dict[str, object]]:
    rows = periods.window_rows(tabs.get("GA4 Events", []), window)
    items = [
        {
            "event_name": event_name,
            "count": periods.sum_int(group, "Count"),
            "users": periods.sum_int(group, "Users"),
        }
        for event_name, group in periods.group_by(rows, "Event Name").items()
    ]
    items.sort(key=lambda item: item["count"], reverse=True)
    return items[:_TOP_EVENTS_LIMIT]


def _resolve_summary(tabs: dict, windows: Windows) -> BlockResult:
    summary_rows = tabs.get("GA4 Summary", [])
    kpis = {
        "current": _summary_kpi(periods.window_rows(summary_rows, windows.current)),
        "previous": _summary_kpi(periods.window_rows(summary_rows, windows.previous)),
        "yoy": _summary_kpi(periods.window_rows(summary_rows, windows.yoy)),
    }
    return BlockResult.ok(
        {
            "period": windows.current.display,
            "previous_period": windows.previous.display,
            "yoy_period": windows.yoy.display,
            "kpis": kpis,
            "channels": _channel_mix(tabs, windows.current),
            "channels_previous": _channel_mix(tabs, windows.previous),
            "channels_yoy": _channel_mix(tabs, windows.yoy),
            "daily": _daily_rows(tabs, windows.current),
            "daily_previous": _daily_rows(tabs, windows.previous),
            "daily_yoy": _daily_rows(tabs, windows.yoy),
            "top_events": _top_events(tabs, windows.current),
            "top_events_previous": _top_events(tabs, windows.previous),
            "top_events_yoy": _top_events(tabs, windows.yoy),
        }
    )


def _resolve_channel_mix_bar(tabs: dict, windows: Windows) -> BlockResult:
    channels = _channel_mix(tabs, windows.current)
    if not channels:
        return BlockResult.unavailable(f"No channel data found for {windows.current.display}.")
    return BlockResult.ok({"period": windows.current.display, "channels": channels})


def _top_pages(tabs: dict, window: Window) -> list[dict[str, object]]:
    rows = periods.window_rows(tabs.get("GA4 Top Pages", []), window)
    items = []
    for page, group in periods.group_by(rows, "Landing Page").items():
        items.append(
            {
                "page": page,
                "sessions": periods.sum_int(group, "Sessions"),
                "engaged_sessions": periods.sum_int(group, "Engaged Sessions"),
                "key_events": periods.sum_int(group, "Key Events"),
                "bounce_rate": (
                    _num(group[0].get("Bounce Rate %"))
                    if len(group) == 1
                    else periods.weighted_avg(group, "Bounce Rate %", "Sessions")
                ),
            }
        )
    items.sort(key=lambda item: item["sessions"], reverse=True)
    return items


def _resolve_top_pages(tabs: dict, windows: Windows) -> BlockResult:
    pages = _top_pages(tabs, windows.current)[:_TOP_PAGES_LIMIT]
    if not pages:
        return BlockResult.unavailable(f"No top-pages data found for {windows.current.display}.")

    # Comparison rows are cut down to the pages the report shows, so every listed
    # page can draw its delta without shipping the whole tail of last month.
    wanted = {item["page"] for item in pages}
    return BlockResult.ok(
        {
            "period": windows.current.display,
            "previous_period": windows.previous.display,
            "yoy_period": windows.yoy.display,
            "pages": pages,
            "pages_previous": [p for p in _top_pages(tabs, windows.previous) if p["page"] in wanted],
            "pages_yoy": [p for p in _top_pages(tabs, windows.yoy) if p["page"] in wanted],
        }
    )


def _resolve_monetization(tabs: dict, windows: Windows) -> BlockResult:
    site_rows = tabs.get("GA4 Ecommerce", [])
    organic_rows = tabs.get("GA4 Ecommerce Organic", [])
    # AI-driven sales: purchases/revenue attributed to AI-assistant referrers,
    # read from the client sheet's "GA4 AI Ecommerce" tab. Absent for clients
    # whose collector doesn't yet populate it — the section then renders empty.
    ai_rows = tabs.get("GA4 AI Ecommerce", [])
    return BlockResult.ok(
        {
            "period": windows.current.display,
            "previous_period": windows.previous.display,
            "yoy_period": windows.yoy.display,
            "currency": _currency_of(site_rows, organic_rows, ai_rows),
            "site_wide": {
                "current": _ecommerce_kpi(periods.window_rows(site_rows, windows.current)),
                "previous": _ecommerce_kpi(periods.window_rows(site_rows, windows.previous)),
                "yoy": _ecommerce_kpi(periods.window_rows(site_rows, windows.yoy)),
            },
            "organic": {
                "current": _ecommerce_kpi(periods.window_rows(organic_rows, windows.current)),
                "previous": _ecommerce_kpi(periods.window_rows(organic_rows, windows.previous)),
                "yoy": _ecommerce_kpi(periods.window_rows(organic_rows, windows.yoy)),
            },
            "ai": {
                "current": _ecommerce_kpi(periods.window_rows(ai_rows, windows.current)),
                "previous": _ecommerce_kpi(periods.window_rows(ai_rows, windows.previous)),
                "yoy": _ecommerce_kpi(periods.window_rows(ai_rows, windows.yoy)),
            },
            "note": (
                "Organic-only figures are read directly from the client sheet's "
                "'GA4 Ecommerce Organic' tab (previously a manual-process limitation)."
            ),
        }
    )


def _resolve_ai_traffic(tabs: dict, windows: Windows) -> BlockResult:
    summary_rows = tabs.get("GA4 AI Summary", [])
    tools_rows = periods.window_rows(tabs.get("GA4 AI Traffic", []), windows.current)
    top_pages_rows = periods.window_rows(tabs.get("GA4 AI Top Pages", []), windows.current)
    ai_ecommerce_rows = tabs.get("GA4 AI Ecommerce", [])

    # No rows in any of the three tabs means they were never collected, which is
    # different from a site that genuinely had no AI traffic: that one still gets
    # a summary row of zeros. Seen where a sheet carried one "GA4 AI Assistants"
    # tab instead — real data under a name and shape nothing here reads. Saying
    # so beats a section of zeros that reads as "no AI traffic".
    current_summary = periods.window_rows(summary_rows, windows.current)
    if not current_summary and not tools_rows and not top_pages_rows:
        return BlockResult.unavailable(
            "No AI-traffic data in the sheet for "
            f"{windows.current.display} — it needs the tabs "
            "'GA4 AI Summary', 'GA4 AI Traffic' and 'GA4 AI Top Pages'."
        )

    tools = sorted(
        (
            {
                "source": source,
                "sessions": periods.sum_int(group, "Sessions"),
                "engaged_sessions": periods.sum_int(group, "Engaged Sessions"),
            }
            for source, group in periods.group_by(tools_rows, "Source").items()
        ),
        key=lambda item: item["sessions"],
        reverse=True,
    )
    top_pages = sorted(
        (
            {
                "page": page,
                "sessions": periods.sum_int(group, "Sessions"),
                "engaged_sessions": periods.sum_int(group, "Engaged Sessions"),
            }
            for page, group in periods.group_by(top_pages_rows, "Landing Page").items()
        ),
        key=lambda item: item["sessions"],
        reverse=True,
    )

    return BlockResult.ok(
        {
            "period": windows.current.display,
            "previous_period": windows.previous.display,
            "yoy_period": windows.yoy.display,
            "summary": {
                "current": _ai_summary_kpi(periods.window_rows(summary_rows, windows.current)),
                "previous": _ai_summary_kpi(periods.window_rows(summary_rows, windows.previous)),
                "yoy": _ai_summary_kpi(periods.window_rows(summary_rows, windows.yoy)),
            },
            "tools": tools,
            "top_pages": top_pages,
            "currency": _currency_of(ai_ecommerce_rows),
            # Sales made by AI-referred visitors. Carried here as well as in the
            # monetization block, because the AI-Traffic section shows them and a
            # report can select this block without that one.
            "ecommerce": {
                "current": _ecommerce_kpi(periods.window_rows(ai_ecommerce_rows, windows.current)),
                "previous": _ecommerce_kpi(periods.window_rows(ai_ecommerce_rows, windows.previous)),
                "yoy": _ecommerce_kpi(periods.window_rows(ai_ecommerce_rows, windows.yoy)),
            },
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

    windows = periods.resolve_windows(
        (row.get("Period", "") for row in tabs.get("GA4 Summary", [])),
        context.period_selection,
    )
    if not windows.current.labels:
        return BlockResult.unavailable("Could not determine the current reporting period from the GA4 sheet.")

    if block.key == "ga4_summary":
        return _resolve_summary(tabs, windows)
    if block.key == "ga4_session_mix_bar":
        return _resolve_channel_mix_bar(tabs, windows)
    if block.key == "ga4_top_pages":
        return _resolve_top_pages(tabs, windows)
    if block.key == "ga4_monetization":
        return _resolve_monetization(tabs, windows)
    if block.key == "ga4_ai_traffic":
        return _resolve_ai_traffic(tabs, windows)
    return BlockResult.unavailable(f"No GA4 resolver for block '{block.key}'.")
