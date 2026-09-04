"""Google Search Console sheet-backed blocks: summary, top queries & pages,
and the bar variant of branded-vs-non-branded clicks.

Resolves the client's sheet via ``client.ga4_sheet_id`` if already set,
otherwise by looking it up by name in the shared client Drive folder (GSC tabs
live in the same client sheet as GA4, per README~1.MD §2/§3): GSC Summary /
Positions / Daily / Queries / Top Pages — trying known alternate tab names too
("GSC Summary" vs "GSC Overview", "GSC Queries" vs "GSC Top Queries"), since
different client sheets in practice use slightly different titles.

Branded share is computed from the reporting period's query sample (README's
documented "top-50 sample" approximation), by matching each query against the
client's brand terms. Those terms come from two places:

* a guess from ``client.name`` and ``client.domain`` with the TLD stripped —
  cheap, offline, and the fallback when anything else fails, but blind to how a
  brand is actually typed (eatlebab.com is searched as "le bab");
* the month's own queries, read once by a model and stored against that period
  for good — see :func:`_brand_terms`.

Still an approximation over a 50-query sample, and deliberately not reviewed by
hand: a missed query worth a few dozen clicks moves the share by ~1 pp, which is
accepted.

Metrics are keyed by monthly ``Period`` label. A custom range or full-year
report aggregates several months (see :mod:`periods`): clicks/impressions are
summed, CTR is recomputed and average position impression-weighted, and the
position-bucket snapshot uses the window's most recent month.
"""

from __future__ import annotations

import logging
import re
import typing

from backend.app.report_builder import api_cache
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


logger = logging.getLogger("rankberry.report_builder.gsc")

_TAB_ALIASES: dict[str, list[str]] = {
    "GSC Summary": ["GSC Summary", "GSC Overview"],
    "GSC Positions": ["GSC Positions"],
    "GSC Daily": ["GSC Daily"],
    "GSC Queries": ["GSC Queries", "GSC Top Queries"],
    "GSC Top Pages": ["GSC Top Pages"],
}

_TOP_LIMIT = 20

# A trailing TLD (".ua", ".com", ".co.uk") on a client name/domain.
_TLD_SUFFIX = re.compile(r"\.[a-z]{2,4}(\.[a-z]{2,3})?$")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

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


def _squash(value: str) -> str:
    """Lowercase, drop everything that is not a letter/digit — so "One by One",
    "one-by-one" and "onebyone" all collapse to the same token."""
    return _NON_WORD.sub("", (value or "").lower())


def _guessed_brand_terms(client_name: str, domain: str) -> list[str]:
    """The brand strings guessed from the client's own name and domain.

    A client's *name* in this system frequently carries the domain suffix
    ("onebyone.ua"), which never appears inside a search query — matching on it
    verbatim scored every query non-branded. So each of the name and the domain
    contributes both its full form and its TLD-stripped registrable name.

    Still only a guess: it cannot know that eatlebab.com is searched as "le bab",
    or that onebyone.ua is searched as "ван бай ван". :func:`_brand_terms` asks a
    model for those and falls back here.
    """
    terms: set[str] = set()
    for raw in (client_name, domain):
        text = (raw or "").strip().lower().removeprefix("www.")
        for candidate in (text, _TLD_SUFFIX.sub("", text)):
            token = _squash(candidate)
            if len(token) >= 3:  # too short to match on without false positives
                terms.add(token)
    return sorted(terms)


def _is_branded(query: str, brand_terms: typing.Sequence[str]) -> bool:
    squashed = _squash(query)
    return any(term in squashed for term in brand_terms)


_BRAND_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "brand_name": {"type": "string"},
        "brand_terms": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["brand_name", "brand_terms"],
}

# Monobrand, typos only. Each client counts as one brand — its own — and a term
# earns its place only by being that same name spelled differently.
#
# The exclusions are the whole point, and each one is a bug this fixed:
#   * a manufacturer a site resells is not that site's brand. Without this,
#     "yamaha parts" counted as branded for yamahaonlineparts.com (95.7%) while
#     the same query was rejected for partsvu.com (41.7%) — one question, two
#     answers, and an 86 pp swing on a client's report.
#   * a sibling brand the same company owns is still a different brand.
#   * "anything that is not a name" — it once learned "yes" as a brand term.
# With them, five of six live clients answer identically call to call.
_BRAND_PROMPT = """You identify how one business's own name is typed into Google.

Site domain: {domain}
Business name on record: {name}

These are the site's real Google Search Console queries for one month, highest
clicks first:
{queries}

This site has exactly ONE brand: its own. Work out that brand's name, then list
the ways it appears in the queries above.

Return JSON:
  "brand_name": the single brand name, lowercase.
  "brand_terms": that name and only variants of that same name — misspellings,
                 mistypings, missing or extra spaces, joined words, reversed word
                 order, and the same name written in another alphabet.

Do NOT include anything that is not that one name:
- no other companies, including manufacturers whose products this site sells or
  resells, and no competitors;
- no other brands, sub-brands, product lines or venue names, even if this
  business owns them;
- no generic product, category, material, size or location words;
- no numbers, part codes or filler words.

A term belongs in the list only if a query above shows that spelling being used.
"""

_BRAND_TTL_DAYS = 365 * 50
_BRAND_QUERY_SAMPLE = 50


def _brand_terms(context: ResolveContext, tabs: dict, window: Window) -> list[str]:
    """Brand terms for this client: the guess from its name and domain, widened
    by asking a model which of the month's real queries name this business.

    One call per client per reporting period, about $0.001, then stored against
    that period and reused for good (see ``_BRAND_TTL_DAYS``). Any failure (no
    API key, rate limit, bad answer) falls back to the guess alone and stores
    nothing, so the block still renders and the next report retries.
    """
    guessed = _guessed_brand_terms(context.client.name or "", context.client.domain or "")
    rows = periods.window_rows(tabs.get("GSC Queries", []), window)
    period = window.display or window.latest
    if not rows or not period:
        return guessed

    sample = sorted(rows, key=lambda row: -_int(row.get("Clicks")))[:_BRAND_QUERY_SAMPLE]
    listing = "\n".join(
        f"- {(row.get('Query') or '').strip()} ({_int(row.get('Clicks'))} clicks)"
        for row in sample
        if (row.get("Query") or "").strip()
    )
    if not listing:
        return guessed

    memo_key = ("gsc_brand_terms", context.client.domain, period)
    if memo_key in context.cache:
        return context.cache[memo_key]

    def ask() -> dict[str, object]:
        from backend.app.config import get_settings
        from backend.app.llm import LLMClient

        settings = get_settings()
        prompt = _BRAND_PROMPT.format(
            domain=context.client.domain or "",
            name=context.client.name or "",
            queries=listing,
        )
        answer, _usage = LLMClient(settings).generate_gemini_json(
            prompt, model=settings.gemini_brand_model, schema=_BRAND_SCHEMA
        )
        return {
            "brand_name": str(answer.get("brand_name") or ""),
            "brand_terms": answer.get("brand_terms") or [],
        }

    try:
        payload = api_cache.get_or_fetch(
            context.session,
            f"gsc_brand_terms:v1:{context.client.domain}:{period}",
            ask,
            ttl_days=_BRAND_TTL_DAYS,
        )
    except Exception as error:  # noqa: BLE001 - any LLM/network failure, never fatal
        logger.warning("gsc_brand_terms_failed domain=%s period=%s error=%s",
                       context.client.domain, period, error)
        context.cache[memo_key] = guessed
        return guessed

    learned = {
        token
        for term in payload.get("brand_terms") or []
        if len(token := _squash(str(term))) >= 3
    }
    terms = sorted(set(guessed) | learned)
    logger.info("gsc_brand_terms domain=%s period=%s brand=%r guessed=%s learned=%s",
                context.client.domain, period, payload.get("brand_name"),
                guessed, sorted(learned))
    context.cache[memo_key] = terms
    return terms


def _branded_summary(tabs: dict, window: Window, brand_terms: typing.Sequence[str]) -> dict[str, object]:
    rows = periods.window_rows(tabs.get("GSC Queries", []), window)
    total_clicks = sum(_int(row.get("Clicks")) for row in rows)
    branded_clicks = sum(
        _int(row.get("Clicks")) for row in rows if _is_branded(row.get("Query", ""), brand_terms)
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


def _resolve_summary(tabs: dict, windows: Windows, brand_terms: typing.Sequence[str]) -> BlockResult:
    summary_rows = tabs.get("GSC Summary", [])
    positions_rows = tabs.get("GSC Positions", [])
    current_kpi = _summary_kpi(periods.window_rows(summary_rows, windows.current))
    # The collector writes Period rows with zeros when the sheet's Search Console
    # property is misconfigured, so "rows exist" is not "data arrived". Shipping a
    # section of zeros reads as a broken report; say the source is empty instead.
    if not current_kpi or not (current_kpi["clicks"] or current_kpi["impressions"]):
        return BlockResult.unavailable(
            f"No Search Console clicks or impressions found for {windows.current.display} "
            "— check the client sheet's GSC property."
        )
    return BlockResult.ok(
        {
            "period": windows.current.display,
            "previous_period": windows.previous.display,
            "yoy_period": windows.yoy.display,
            "kpis": {
                "current": current_kpi,
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
            "branded": _branded_summary(tabs, windows.current, brand_terms),
            "branded_previous": _branded_summary(tabs, windows.previous, brand_terms),
            "branded_yoy": _branded_summary(tabs, windows.yoy, brand_terms),
        }
    )


def _resolve_branded_bar(tabs: dict, windows: Windows, brand_terms: typing.Sequence[str]) -> BlockResult:
    branded = _branded_summary(tabs, windows.current, brand_terms)
    if not branded["total_clicks"]:
        return BlockResult.unavailable(f"No query click data found for {windows.current.display}.")
    return BlockResult.ok({"period": windows.current.display, "branded": branded})


def _resolve_top_queries(tabs: dict, windows: Windows) -> BlockResult:
    query_rows = periods.window_rows(tabs.get("GSC Queries", []), windows.current)
    page_rows = periods.window_rows(tabs.get("GSC Top Pages", []), windows.current)
    if not query_rows and not page_rows:
        return BlockResult.unavailable(f"No query/page data found for {windows.current.display}.")

    queries = _aggregate_items(query_rows, "Query", "query")[:_TOP_LIMIT]
    pages = _aggregate_items(page_rows, "Page", "page")[:_TOP_LIMIT]

    def _comparison(tab: str, key_field: str, label_key: str, shown: list, window) -> list:
        # Only the rows the report actually shows: a query sitting at #45 last
        # month still needs its previous numbers so the table can draw an
        # up/down arrow, but the rest of that month's tail is payload for nobody.
        wanted = {item[label_key] for item in shown}
        rows = periods.window_rows(tabs.get(tab, []), window)
        return [item for item in _aggregate_items(rows, key_field, label_key) if item[label_key] in wanted]

    data: dict[str, object] = {
        "period": windows.current.display,
        "previous_period": windows.previous.display,
        "yoy_period": windows.yoy.display,
        "queries": queries,
        "pages": pages,
    }
    for suffix, window in (("previous", windows.previous), ("yoy", windows.yoy)):
        data[f"queries_{suffix}"] = _comparison("GSC Queries", "Query", "query", queries, window)
        data[f"pages_{suffix}"] = _comparison("GSC Top Pages", "Page", "page", pages, window)
    return BlockResult.ok(data)


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

    brand_terms = _brand_terms(context, tabs, windows.current)
    if block.key == "gsc_summary":
        return _resolve_summary(tabs, windows, brand_terms)
    if block.key == "gsc_branded_bar":
        return _resolve_branded_bar(tabs, windows, brand_terms)
    if block.key == "gsc_top_queries":
        return _resolve_top_queries(tabs, windows)
    return BlockResult.unavailable(f"No GSC resolver for block '{block.key}'.")
