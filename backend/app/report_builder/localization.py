"""Report language: the static UI vocabulary and its Claude-written translations.

A report is always *built* in English — every data label, section title and
Claude-written comment. When the client's ``report_language`` is not English the
report is then translated, which splits into two very different jobs:

* **The static UI vocabulary** (this module) — section titles, table headers, KPI
  labels, month names. A fixed set that only changes when a developer edits the
  template, so it is translated once per language and cached on disk. Reports do
  not pay for it.
* **The report's prose** (``ai_commentary``) — the per-block comments and the
  executive summary. Different every report, so it is translated per report as
  one extra Claude request.

Lookup is keyed by the **English string itself** rather than an invented message
id. That keeps the template free of key plumbing (its render code goes on
emitting English, and one post-render pass swaps the text), and it makes a
missing translation degrade to English instead of to a broken placeholder.

Framework-free, and Claude-free: the translating callable is injected, so this
module stays a pure catalog + cache and unit-tests without a network.
"""

from __future__ import annotations

import typing

import json
import logging
import re
import threading
from pathlib import Path

from backend.app.config import BACKEND_ROOT


logger = logging.getLogger("rankberry.report_builder.i18n")

DEFAULT_LANGUAGE = "en"

# Languages a report can be delivered in. English is the language reports are
# authored in, so it needs no translation pass.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "uk": "Ukrainian",
}

_CACHE_DIR = BACKEND_ROOT / "data" / "ui_translations"

# Only translate what the *client* reads. The editable-preview chrome (accent
# picker, per-panel size/weight toggles, chart-variant selects, comment
# placeholders) is specialist-facing and stripped from the client report, so it
# stays English — less to translate and nothing for the client to see.
UI_STRINGS: tuple[str, ...] = (
    # -- report chrome ---------------------------------------------------------
    "SEO Report",
    "SEO REPORT",
    "SEO & Visibility Report",
    (
        "Organic performance, search visibility, authority metrics, AI presence "
        "and work completed across"
    ),
    "Client",
    "Domain",
    "Period",
    "Prepared",
    "Comment",
    "Summary",
    "Overview",
    "Make your business blossom.",
    "Rankberry · Make your business blossom",
    "Metric",
    "Change",
    "Share",
    "Share of total",
    "Total",
    "Other",
    "No data.",
    "vs prev month",
    # -- section titles --------------------------------------------------------
    "Ahrefs — Domain analysis",
    "Ahrefs — Top movers (pages & keywords)",
    "Google Analytics 4",
    "GA4 — Top landing pages",
    "GA4 — Monetization",
    "GA4 — AI Traffic",
    "Google Search Console",
    "GSC — Top queries & pages",
    "SE Ranking — Tracked keywords",
    "AI Visibility",
    "Traffic Sources",
    # -- Ahrefs ----------------------------------------------------------------
    "Domain Rating",
    "Ahrefs Rank",
    "Backlinks",
    "Ref. domains",
    "Organic keywords",
    "Organic traffic",
    "Paid keywords",
    "Paid traffic",
    "Traffic value",
    "Ahrefs est. / mo",
    "Backlink & authority profile",
    "Organic traffic — 14-month trend (Ahrefs)",
    "▲ Top 20 gainers",
    "▼ Top 20 losers",
    "Top keyword",
    "Volume",
    "Vol",
    # -- GA4 -------------------------------------------------------------------
    "Sessions",
    "Total sessions",
    "Organic sessions",
    "Engaged sessions",
    "Engagement rate",
    "Engagement",
    "Eng. rate",
    "Engaged",
    "Bounce rate",
    "Bounce",
    "Avg. session duration",
    "Avg. session",
    "Pages / session",
    "Page views",
    "Key events",
    "New users",
    "Returning users",
    "New",
    "Channel",
    "Top channels",
    "Session mix",
    "Session mix by channel —",
    "Top events",
    "Top events —",
    "Events",
    "Daily trend",
    "Daily sessions —",
    "Landing page",
    "Top landing pages",
    "Page",
    "Traffic",
    "Organic",
    "Scope: all channels (site-wide)",
    # KPI sub-captions: each is its own element, so they match as whole strings.
    "lower is better",
    "of all sessions",
    "of organic sessions",
    "vs organic search",
    "new channel",
    # -- GA4 monetization ------------------------------------------------------
    "Revenue",
    "Number of sales",
    "Avg order value",
    "Add to cart",
    "Checkout",
    "Purchase",
    "Checkout→Purchase",
    "Purchase funnel —",
    "Funnel detail",
    "Stage",
    "Contributors",
    "AI-Revenue",
    "AI revenue",
    "AI number of sales",
    "AI avg order value",
    "AI add to cart",
    "AI sessions",
    "Sales driven by AI assistants",
    "Top landing pages from AI",
    "Traffic by AI tool —",
    # -- GSC -------------------------------------------------------------------
    "Clicks",
    "Impressions",
    "Impr.",
    "CTR",
    "Position",
    "Positions",
    "Avg position",
    "Avg pos",
    "Pos",
    "Query",
    "Top queries",
    "Top pages",
    "Branded",
    "Non-branded",
    "Branded traffic",
    "Branded share trend",
    "Branded vs non-branded clicks —",
    "Daily clicks",
    "Daily clicks —",
    # -- SE Ranking ------------------------------------------------------------
    "Keyword",
    "Keyword position distribution —",
    "Top-3",
    "Top-5",
    "Top-10",
    "Top-20",
    "Top-50",
    "Top-3 keywords",
    "Top-10 keywords",
    "Total (top)",
    # -- AI visibility ---------------------------------------------------------
    "Brand mentions",
    "Domain mentions",
    "Results checked",
    "All models",
    "GPT",
    "Gemini",
    "Grok",
    "Last month",
    "Last 6 months",
    # -- work / plan -----------------------------------------------------------
    "Work completed",
    "Planned works",
    "Task",
    "Due",
    "No planned tasks for the next period.",
    # The count line that opens each of the two work sections; the number itself
    # sits in its own element, so only the trailing phrase is a translatable key.
    "task planned for the next period",
    "tasks planned for the next period",
    "task completed this period",
    "tasks completed this period",
    "Total time tracked:",
    "No tasks were completed in this period.",
    # -- search industry -------------------------------------------------------
    "Search industry",
    # -- months (short, then long) --------------------------------------------
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


class UnsupportedLanguage(ValueError):
    """A language outside ``SUPPORTED_LANGUAGES`` was requested."""


def normalize_language(value: typing.Optional[str]) -> str:
    """Coerce stored/user input to a supported language code.

    Unknown values fall back to English rather than raising: a report that
    renders in the wrong language is a nuisance, one that fails to render is an
    outage.
    """
    code = (value or "").strip().lower()
    if not code:
        return DEFAULT_LANGUAGE
    code = code.replace("_", "-").split("-", 1)[0]
    if code in SUPPORTED_LANGUAGES:
        return code
    logger.warning("i18n_unknown_language value=%s falling_back=%s", value, DEFAULT_LANGUAGE)
    return DEFAULT_LANGUAGE


def language_name(code: str) -> str:
    return SUPPORTED_LANGUAGES.get(normalize_language(code), "English")


def needs_translation(code: str) -> bool:
    """English is the authoring language, so it never needs a translation pass."""
    return normalize_language(code) != DEFAULT_LANGUAGE


# --- cache --------------------------------------------------------------------

# Cache writes happen from request threads; the lock keeps two concurrent
# first-renders from clobbering each other's file.
_write_lock = threading.Lock()
_memo: dict[str, dict[str, str]] = {}


def _cache_path(language: str) -> Path:
    return _CACHE_DIR / f"{language}.json"


def load_ui_translations(language: str) -> dict[str, str]:
    """The cached English->target map, or empty when nothing is cached yet."""
    language = normalize_language(language)
    if not needs_translation(language):
        return {}
    if language in _memo:
        return _memo[language]
    path = _cache_path(language)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("i18n_cache_unreadable language=%s error=%s", language, error)
        return {}
    if not isinstance(raw, dict):
        return {}
    mapping = {str(k): str(v) for k, v in raw.items() if str(v).strip()}
    _memo[language] = mapping
    return mapping


def _store_ui_translations(language: str, mapping: dict[str, str]) -> None:
    with _write_lock:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(language)
        payload = json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True)
        # Write-then-replace so a crash mid-write can't leave a truncated cache.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(path)
        _memo[language] = mapping


def missing_ui_strings(language: str) -> list[str]:
    """Catalog entries with no cached translation yet."""
    if not needs_translation(language):
        return []
    cached = load_ui_translations(language)
    seen: set[str] = set()
    missing: list[str] = []
    for text in UI_STRINGS:
        if text in seen or text in cached:
            continue
        seen.add(text)
        missing.append(text)
    return missing


TranslateFn = typing.Callable[[list[str], str], dict[str, str]]


def ensure_ui_translations(language: str, translate: TranslateFn) -> dict[str, str]:
    """Translate any catalog entries not cached yet, then return the full map.

    ``translate`` takes (texts, language) and returns an English->target mapping.
    Only the gap is sent, so adding one label to ``UI_STRINGS`` costs one small
    request rather than re-translating the whole vocabulary.

    Never raises: a translation failure leaves the report in English, which is
    degraded but perfectly usable.
    """
    language = normalize_language(language)
    if not needs_translation(language):
        return {}
    missing = missing_ui_strings(language)
    if not missing:
        return load_ui_translations(language)

    logger.info("i18n_translating_ui language=%s missing=%s", language, len(missing))
    try:
        fresh = translate(missing, language)
    except Exception as error:  # noqa: BLE001 - never fail a report over labels
        logger.warning("i18n_ui_translation_failed language=%s error=%s", language, error)
        return load_ui_translations(language)

    merged = dict(load_ui_translations(language))
    merged.update({k: v for k, v in fresh.items() if k and str(v).strip()})
    try:
        _store_ui_translations(language, merged)
    except OSError as error:
        logger.warning("i18n_cache_unwritable language=%s error=%s", language, error)
        _memo[language] = merged
    return merged


# --- lookup -------------------------------------------------------------------

def translator(language: str) -> typing.Callable[[str], str]:
    """A ``t(english) -> localized`` callable, identity for English.

    Falls back to the English input for anything untranslated, so a stale cache
    shows a few English labels rather than blanks.
    """
    mapping = load_ui_translations(language)
    if not mapping:
        return lambda text: text

    def translate(text: str) -> str:
        if not text:
            return text
        hit = mapping.get(text)
        if hit:
            return hit
        # Labels are frequently rendered with a trailing separator ("Top events —")
        # or surrounding whitespace; match the core and keep the decoration.
        stripped = text.strip()
        hit = mapping.get(stripped)
        if hit:
            return text.replace(stripped, hit, 1)
        return text

    return translate


_MONTH_TOKEN = re.compile(r"\b([A-Z][a-z]{2,8})\b")


def localize_period_label(label: str, language: str) -> str:
    """Translate the month names inside a period label ("Jun 2026", "Jun–Aug 2026").

    Period labels are assembled from month names and digits, so translating the
    month tokens in place is enough — and leaves the numbers alone.
    """
    if not label or not needs_translation(language):
        return label
    t = translator(language)
    return _MONTH_TOKEN.sub(lambda m: t(m.group(1)), label)
