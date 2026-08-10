"""Claude-written report commentary.

Two jobs, deliberately split across two models:

* **Per-block comments** (Sonnet) — one short analyst comment per report section,
  written with the *whole* report as context so a section can reference what
  explains its movement. Generated right after a report is built and before the
  specialist ever sees the preview, so what they open is data + editable draft
  commentary rather than empty note boxes.
* **The report summary** (Opus) — one executive summary for the top of the
  report, written once the specialist has submitted, from the final data *and*
  their edited comments.

Both are drafts: everything lands in the same editable comment fields the
specialist already owns, and nothing here is authoritative over their edits.

Framework-free (plain dicts in, plain strings out) so it unit-tests like the
rest of ``report_builder``.
"""

from __future__ import annotations

import typing

import json
import logging
import time

import anthropic

from backend.app.config import Settings
from backend.app.observability import log_event
from backend.app.report_builder import localization
from backend.app.report_builder.block_catalog import get_block
from backend.app.report_builder.export import SECTION_BY_KEY
from backend.app.utils import read_text_file


logger = logging.getLogger("rankberry.report_builder.ai")


# Blocks that never get a generated *analyst comment*: the hero has no comment
# slot at all, the summary is the Opus job at submit time, and search_industry is
# editorial scene-setting written by its own web-searched call rather than a
# comment about the section's data (it has none).
_NO_COMMENT_BLOCKS = frozenset({"intro_header", "summary", "search_industry"})

SUMMARY_BLOCK_KEY = "summary"
SEARCH_INDUSTRY_BLOCK_KEY = "search_industry"

# Context budget. Report payloads carry per-day series and long top-N tables that
# add tokens without adding meaning to prose, so the digest prunes before the
# prompt is built.
_MAX_LIST_ITEMS = 12
_MAX_STRING_CHARS = 400
_MAX_CONTEXT_CHARS = 120_000
# Per-day series: the comment talks about the period, not about individual days.
_DROP_DATA_KEYS = frozenset({"daily", "daily_previous", "daily_yoy"})

_COMMENT_MAX_TOKENS = 16000
_SUMMARY_MAX_TOKENS = 16000
_TRANSLATE_MAX_TOKENS = 32000
# Several rounds of web search plus the write-up.
_SEARCH_INDUSTRY_MAX_TOKENS = 24000

# The reporting month is almost always more recent than the model's training
# cutoff, so this section is researched rather than recalled — writing it from
# memory would invent algorithm updates in a client-facing report.
_WEB_SEARCH_TOOL: dict[str, object] = {"type": "web_search_20260209", "name": "web_search"}
_SEARCH_INDUSTRY_MAX_WORDS = 150
_SEARCH_INDUSTRY_MAX_RESUMES = 3
# Opus with thinking on a full report can run well past the 60s the rest of the
# app uses for its providers.
_MIN_TIMEOUT_SECONDS = 240.0

_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Translation is a large, mechanical job (every comment in one call), so it gets a
# bigger output ceiling than the writing calls and is batched to keep any single
# response well inside it.
_TRANSLATE_BATCH_SIZE = 60

_DEFAULT_TRANSLATE_PROMPT = (
    "You are a professional translator localizing an SEO report for a business "
    "owner. Translate each input string from English into the target language. "
    "Preserve meaning, tone and register exactly; keep it natural rather than "
    "literal. Never translate: brand and product names (Rankberry, Google, "
    "Ahrefs, GA4, Google Analytics 4, Google Search Console, GSC, SE Ranking, "
    "ChatGPT, GPT, Gemini, Grok), metric acronyms (CTR, DR, AOV, MoM, YoY, SEO), "
    "URLs, and any number, percentage, currency symbol or date. Keep every "
    "number and figure byte-identical. Preserve leading/trailing punctuation and "
    "symbols (arrows like ▲ ▼, em dashes, colons) exactly as given. Keep short "
    "table headers and KPI labels short — they sit in narrow columns. Return one "
    "translation per input, in the same order, and translate every input."
)

# Order-preserving parallel array. A flat list (rather than a map keyed by the
# source string) keeps this strict-output friendly — arbitrary source text cannot
# be expressed as schema property names.
_TRANSLATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["index", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

_DEFAULT_COMMENT_PROMPT = (
    "You are a senior SEO analyst writing the specialist comment that sits under "
    "one section of a monthly client report. Use the whole report as context. "
    "Per comment: exactly two paragraphs, 550-650 characters, no markdown, no "
    "headings. Paragraph 1 explains what the section shows; paragraph 2 starts "
    "with the word CHANGES. and gives the MoM/YoY movement. Never invent a "
    "figure."
)

_DEFAULT_SEARCH_INDUSTRY_PROMPT = (
    "You are a senior SEO analyst writing the introductory 'Search industry' "
    "section of a monthly client report. Use web search — the reporting month is "
    "recent, so do not rely on memory. Summarise that month's confirmed Google "
    "algorithm updates, ranking volatility reported by SERP trackers, official "
    "announcements and notable expert observations. Write 2-4 items, one per "
    "line, each as 'LABEL — Headline sentence. One or two sentences of detail.' "
    "LABEL is SHORT and UPPERCASE, at most three words (CORE UPDATE, SPAM "
    "UPDATE, AI SEARCH, VOLATILITY, TECHNICAL, ANNOUNCEMENT, NO UPDATE). Under "
    "150 words total, plain text, no markdown, no bullets. Clearly separate what "
    "Google confirmed from what third parties merely observed, and say so "
    "plainly if no update was confirmed. Never invent events; if you find "
    "nothing reliable, return an empty string."
)

_DEFAULT_SUMMARY_PROMPT = (
    "You are the lead SEO strategist writing the executive summary at the top of "
    "a monthly client report. Use the report data and the specialist's edited "
    "comments; never contradict them. Plain business English for a non-SEO "
    "reader, 3 short paragraphs (context & headline; what worked; honest "
    "problems & next steps), 1500-1800 characters, no markdown, no headings. "
    "Use only figures present in the data. Return only the summary."
)

# One entry per requested section. A flat array (rather than a map keyed by block
# key) keeps the schema strict-output friendly — a dynamic key set cannot be
# expressed with additionalProperties: false.
_COMMENTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "block_key": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["block_key", "comment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comments"],
    "additionalProperties": False,
}


class AICommentaryUnavailable(RuntimeError):
    """Claude is not configured, or declined / failed the request.

    Always non-fatal for the caller: a report is still perfectly usable with
    hand-written comments, so callers surface this as a warning and carry on.
    """


# --- context digest -----------------------------------------------------------

def _prune(value: typing.Any, *, depth: int = 0) -> typing.Any:
    """One report value, shrunk to something worth spending prompt tokens on.

    Long lists are cut to their head with an explicit marker (so the model knows
    it is seeing a sample, not the whole table), long strings are truncated, and
    floats are rounded — the prose never needs six decimal places.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= _MAX_STRING_CHARS else text[:_MAX_STRING_CHARS] + "…"
    if isinstance(value, dict):
        if depth >= 6:
            return "…"
        out: dict[str, typing.Any] = {}
        for key, item in value.items():
            if key in _DROP_DATA_KEYS:
                continue
            out[str(key)] = _prune(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if depth >= 6:
            return "…"
        items = list(value)
        head = [_prune(item, depth=depth + 1) for item in items[:_MAX_LIST_ITEMS]]
        if len(items) > _MAX_LIST_ITEMS:
            head.append(f"…{len(items) - _MAX_LIST_ITEMS} more rows omitted")
        return head
    return str(value)


def _block_label(block_key: str) -> str:
    block = get_block(block_key)
    return block.display_name if block else block_key


def _drop_comparisons(value: typing.Any, drop_keys: set[str], *, depth: int = 0) -> typing.Any:
    """Strip comparison data the report isn't showing, at any nesting depth.

    Matches both the suffix convention the resolvers use for sibling series
    (``channels_yoy``, ``top_events_previous``) and the plain sub-keys inside a
    ``kpis`` block (``kpis.yoy``). Nothing else is touched.
    """
    if not drop_keys or depth >= 8:
        return value
    if isinstance(value, dict):
        out: dict[str, typing.Any] = {}
        for key, item in value.items():
            name = str(key).lower()
            if name in drop_keys or any(name.endswith(f"_{drop}") for drop in drop_keys):
                continue
            out[key] = _drop_comparisons(item, drop_keys, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_drop_comparisons(item, drop_keys, depth=depth + 1) for item in value]
    return value


def build_report_context(
    *,
    client_name: str,
    client_domain: str,
    period_label: str,
    default_comparison: str,
    blocks: list[dict[str, object]],
    include_comments: bool = False,
) -> dict[str, object]:
    """The report as the model sees it: client meta plus every section's data.

    ``include_comments`` carries the specialist's comments along — off while
    drafting those comments, on for the summary, which has to agree with them.
    """
    offered = {
        part.strip().lower() for part in (default_comparison or "").split(",") if part.strip()
    }
    # A block's payload always carries both comparisons, whichever the specialist
    # actually chose — the report just doesn't render the unselected one. Left in
    # the context the model wrote about it anyway, most visibly by noting there
    # was "no data for the previous year" on a report with year-on-year switched
    # off. Drop what the report does not show, so it cannot be commented on.
    drop_keys: set[str] = set()
    if "yoy" not in offered:
        drop_keys |= {"yoy", "yoy_period"}
    if "mom" not in offered:
        drop_keys |= {"previous", "previous_period"}

    sections: list[dict[str, object]] = []
    for block in blocks:
        key = str(block.get("block_type_key") or "")
        if not key:
            continue
        entry: dict[str, object] = {
            "block_key": key,
            "name": _block_label(key),
            "status": block.get("status") or "ok",
        }
        if entry["status"] == "ok":
            entry["data"] = _prune(_drop_comparisons(block.get("data") or {}, drop_keys))
        else:
            entry["unavailable_reason"] = block.get("unavailable_reason") or ""
        if include_comments:
            comment = str(block.get("comment") or "").strip()
            if comment:
                entry["specialist_comment"] = comment
        sections.append(entry)

    return {
        "client": {"name": client_name, "domain": client_domain},
        "reporting_period": period_label,
        "comparisons_offered": [
            part.strip().upper() for part in (default_comparison or "").split(",") if part.strip()
        ],
        "sections": sections,
    }


def _context_json(context: dict[str, object]) -> str:
    text = json.dumps(context, ensure_ascii=False, default=str)
    if len(text) <= _MAX_CONTEXT_CHARS:
        return text
    # Defensive: an unexpectedly huge payload is truncated rather than allowed to
    # blow the request. The prompt says so explicitly so the model doesn't treat
    # the cut as data.
    logger.warning("ai_context_truncated chars=%s limit=%s", len(text), _MAX_CONTEXT_CHARS)
    return text[:_MAX_CONTEXT_CHARS] + "\n… context truncated for length …"


def commentable_block_keys(blocks: list[dict[str, object]]) -> list[str]:
    """The sections that get a generated comment.

    A block only qualifies if it resolved with data *and* the report template has
    a place to show its comment — the AI-visibility and chart-variant blocks
    render inside another section, so a comment on them would never be seen.
    """
    keys: list[str] = []
    for block in blocks:
        key = str(block.get("block_type_key") or "")
        if not key or key in _NO_COMMENT_BLOCKS or key in keys:
            continue
        if (block.get("status") or "ok") != "ok":
            continue
        if key not in SECTION_BY_KEY:
            continue
        block_type = get_block(key)
        if block_type is not None and block_type.source == "ai_visibility":
            continue
        keys.append(key)
    return keys


# --- client -------------------------------------------------------------------

def _trim_to_length(text: str, max_chars: int) -> str:
    """Enforce the summary's character ceiling on a sentence/paragraph boundary."""
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    window = cleaned[: max_chars + 1]
    for boundary in ("\n\n", ". ", "! ", "? ", "\n"):
        cut = window.rfind(boundary)
        if cut > max_chars * 0.6:
            return window[: cut + (1 if boundary.startswith((".", "!", "?")) else 0)].strip()
    return window[:max_chars].rstrip() + "…"


def _trim_to_words(text: str, max_words: int) -> str:
    """Enforce a word ceiling on a sentence boundary where one is close enough.

    Line structure is load-bearing here: the search-industry section is written
    one item per line and the report renders each line as its own card, so the
    ceiling is spent line by line and a line that does not fit is dropped whole
    rather than reflowed into the one before it.
    """
    cleaned = (text or "").strip()
    if len(cleaned.split()) <= max_words:
        return cleaned

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) > 1:
        kept: list[str] = []
        spent = 0
        for line in lines:
            words = line.split()
            if spent + len(words) > max_words:
                break
            kept.append(" ".join(words))
            spent += len(words)
        if kept:
            return "\n".join(kept)

    window = " ".join(cleaned.split()[:max_words])
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut > len(window) * 0.6:
        return window[: cut + 1].strip()
    return window.rstrip(",;:—- ") + "…"


class AICommentaryClient:
    """Thin wrapper over the Anthropic Messages API for report commentary."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.comment_model = settings.anthropic_comment_model
        self.summary_model = settings.anthropic_summary_model
        self.summary_max_chars = settings.report_summary_max_chars
        self._client: typing.Optional[anthropic.Anthropic] = None
        self.comment_system_prompt = read_text_file(
            settings.report_block_comment_prompt_file, _DEFAULT_COMMENT_PROMPT
        )
        self.summary_system_prompt = read_text_file(
            settings.report_summary_prompt_file, _DEFAULT_SUMMARY_PROMPT
        )
        self.translate_system_prompt = read_text_file(
            settings.report_translate_prompt_file, _DEFAULT_TRANSLATE_PROMPT
        )
        self.search_industry_system_prompt = read_text_file(
            settings.report_search_industry_prompt_file, _DEFAULT_SEARCH_INDUSTRY_PROMPT
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    def _api(self) -> anthropic.Anthropic:
        if not self.is_configured:
            raise AICommentaryUnavailable(
                "ANTHROPIC_API_KEY is not configured — Claude commentary is unavailable."
            )
        if self._client is None:
            self._client = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key,
                timeout=max(self.settings.request_timeout_seconds, _MIN_TIMEOUT_SECONDS),
            )
        return self._client

    # -- comments --------------------------------------------------------------

    def generate_block_comments(
        self,
        *,
        context: dict[str, object],
        block_keys: list[str],
    ) -> dict[str, str]:
        """One draft comment per requested section, keyed by block key."""
        if not block_keys:
            return {}
        client = self._api()

        wanted = list(dict.fromkeys(block_keys))
        request_lines = "\n".join(
            f"{index}. {key} — {_block_label(key)}" for index, key in enumerate(wanted, start=1)
        )
        user_text = (
            f"Write one comment for each of these {len(wanted)} sections, using the "
            "block_key exactly as given:\n"
            f"{request_lines}\n\n"
            "Full report (JSON):\n"
            f"{_context_json(context)}"
        )

        message = self._create(
            client,
            model=self.comment_model,
            max_tokens=_COMMENT_MAX_TOKENS,
            system=self.comment_system_prompt,
            user_text=user_text,
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _COMMENTS_SCHEMA},
            },
            operation="block_comments",
        )

        payload = self._parse_json(self._text_of(message), operation="block_comments")
        allowed = set(wanted)
        comments: dict[str, str] = {}
        for entry in payload.get("comments") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("block_key") or "").strip()
            comment = str(entry.get("comment") or "").strip()
            if key in allowed and comment:
                comments[key] = comment

        missing = [key for key in wanted if key not in comments]
        logger.info(
            "ai_block_comments model=%s requested=%s written=%s missing=%s",
            self.comment_model,
            len(wanted),
            len(comments),
            ",".join(missing) or "-",
        )
        return comments

    # -- search industry -------------------------------------------------------

    def write_search_industry(
        self,
        *,
        client_domain: str,
        period_label: str,
    ) -> str:
        """The month's Google-search-landscape scene-setter, researched on the web.

        Unlike every other call here, this one is not about the client's data at
        all — it is about what happened in Google Search that month. The reporting
        period is routinely more recent than the model's training cutoff, so the
        web-search tool is not an enhancement but a correctness requirement:
        without it the model would confabulate algorithm updates into a report a
        client reads as fact.

        Returns "" when nothing reliable was found, which the caller surfaces as
        an empty editable section rather than as an error.
        """
        client = self._api()
        period = (period_label or "").strip() or "the reporting month"
        domain = (client_domain or "").strip() or "the client's site"

        user_text = (
            f"I'm preparing a monthly SEO performance report for a client at {domain}. "
            "For the introductory section of the report, I'm interested in general "
            f"information about what happened with Google's search engine in {period}. "
            "Put together a brief summary of the events, expert observations, updates, "
            f"and volatility that were noticed in {period}.\n\n"
            f"Search the web first, and confirm each item really falls in {period}. "
            f"Keep the result under {_SEARCH_INDUSTRY_MAX_WORDS} words."
        )

        messages: list[dict[str, typing.Any]] = [{"role": "user", "content": user_text}]
        searches = 0
        parts: list[str] = []
        # A server-tool turn that hits the search-iteration cap stops with
        # "pause_turn" and is resumed by sending it straight back. Bounded, so a
        # model that never settles can't loop forever.
        for _ in range(_SEARCH_INDUSTRY_MAX_RESUMES + 1):
            message = self._create(
                client,
                model=self.summary_model,
                max_tokens=_SEARCH_INDUSTRY_MAX_TOKENS,
                system=self.search_industry_system_prompt,
                user_text=user_text,
                output_config={"effort": "medium"},
                operation="search_industry",
                tools=[_WEB_SEARCH_TOOL],
                messages=messages,
            )
            content = getattr(message, "content", None) or []
            searches += sum(
                1 for block in content if getattr(block, "type", None) == "server_tool_use"
            )
            chunk = self._answer_text_of(message)
            if chunk:
                parts.append(chunk)
            if getattr(message, "stop_reason", None) != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": content}]

        text = _trim_to_words("\n".join(parts), _SEARCH_INDUSTRY_MAX_WORDS)
        logger.info(
            "ai_search_industry model=%s period=%s searches=%s words=%s",
            self.summary_model, period, searches, len(text.split()),
        )
        if searches == 0 and text:
            # The model answered from memory. For a month past its cutoff that is
            # exactly the failure mode this call exists to avoid.
            logger.warning(
                "ai_search_industry_unsourced period=%s — discarding unsourced text", period
            )
            return ""
        return text

    # -- summary ---------------------------------------------------------------

    def generate_summary(
        self,
        *,
        context: dict[str, object],
        existing_summary: str = "",
        guidance: str = "",
    ) -> str:
        """The report-wide executive summary, capped at the configured length.

        ``guidance`` is an optional one-off instruction from the specialist for
        this specific regeneration (e.g. "focus more on the YoY traffic gain") —
        distinct from ``existing_summary``, which is a draft to refine.
        """
        client = self._api()

        draft = (existing_summary or "").strip()
        draft_block = (
            "The specialist drafted this summary — keep its facts and intent, improve the "
            f"writing:\n{draft}\n\n"
            if draft
            else ""
        )
        guidance_text = (guidance or "").strip()
        guidance_block = (
            f"The specialist asked for this specific change to the summary — follow it, "
            f"while still keeping every other rule above:\n{guidance_text}\n\n"
            if guidance_text
            else ""
        )
        user_text = (
            f"Write the executive summary for this report in at most {self.summary_max_chars} "
            "characters.\n\n"
            f"{draft_block}"
            f"{guidance_block}"
            "Full report, including the specialist's edited comments (JSON):\n"
            f"{_context_json(context)}"
        )

        message = self._create(
            client,
            model=self.summary_model,
            max_tokens=_SUMMARY_MAX_TOKENS,
            system=self.summary_system_prompt,
            user_text=user_text,
            output_config=None,
            operation="summary",
            with_fallbacks=True,
        )
        summary = _trim_to_length(self._text_of(message), self.summary_max_chars)
        if not summary:
            raise AICommentaryUnavailable("Claude returned an empty summary.")
        logger.info(
            "ai_report_summary model=%s chars=%s limit=%s",
            self.summary_model,
            len(summary),
            self.summary_max_chars,
        )
        return summary

    # -- translation -----------------------------------------------------------

    def translate_strings(self, texts: list[str], language: str) -> dict[str, str]:
        """Translate a list of strings, returned as an English->target mapping.

        This is the "additional request" a non-English report costs: the report is
        written in English first, then handed here. Used for both jobs — the
        static UI vocabulary (once per language, cached by ``localization``) and a
        report's own prose (per report).

        Long inputs are batched so no single response approaches the token
        ceiling. A batch that comes back malformed or short is skipped rather
        than aborting the rest, so a partial translation still beats none.
        """
        wanted = [text for text in dict.fromkeys(texts) if text and text.strip()]
        if not wanted:
            return {}
        target = localization.language_name(language)
        client = self._api()

        out: dict[str, str] = {}
        for start in range(0, len(wanted), _TRANSLATE_BATCH_SIZE):
            batch = wanted[start : start + _TRANSLATE_BATCH_SIZE]
            numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(batch))
            user_text = (
                f"Translate these {len(batch)} strings into {target}.\n"
                "Return one entry per input, with `index` matching the number "
                "shown and `text` holding the translation.\n\n"
                f"{numbered}"
            )
            message = self._create(
                client,
                model=self.comment_model,
                max_tokens=_TRANSLATE_MAX_TOKENS,
                system=self.translate_system_prompt,
                user_text=user_text,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _TRANSLATION_SCHEMA},
                },
                operation="translation",
            )
            try:
                payload = self._parse_json(self._text_of(message), operation="translation")
            except AICommentaryUnavailable:
                logger.warning(
                    "ai_translate_batch_unreadable language=%s offset=%s size=%s",
                    language, start, len(batch),
                )
                continue
            for entry in payload.get("translations") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    index = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                translated = str(entry.get("text") or "").strip()
                if 0 <= index < len(batch) and translated:
                    out[batch[index]] = translated

        missing = [text for text in wanted if text not in out]
        logger.info(
            "ai_translate model=%s language=%s requested=%s translated=%s missing=%s",
            self.comment_model, language, len(wanted), len(out), len(missing),
        )
        return out

    def translate_ui_strings(self, texts: list[str], language: str) -> dict[str, str]:
        """``localization.ensure_ui_translations`` hook — same call, named for intent."""
        return self.translate_strings(texts, language)

    def translate_report_text(
        self, texts: dict[str, str], language: str
    ) -> dict[str, str]:
        """Translate a report's prose, keyed the same way it came in.

        ``texts`` maps an arbitrary key (a block key, or ``summary``) to English
        prose; the result maps those same keys to the translated prose. Anything
        Claude declined or dropped is simply absent, so callers keep the English.
        """
        pending = {key: value for key, value in texts.items() if value and value.strip()}
        if not pending or not localization.needs_translation(language):
            return {}
        translated = self.translate_strings(list(pending.values()), language)
        return {
            key: translated[value] for key, value in pending.items() if value in translated
        }

    # -- transport -------------------------------------------------------------

    def _create(
        self,
        client: anthropic.Anthropic,
        *,
        model: str,
        max_tokens: int,
        system: str,
        user_text: str,
        output_config: typing.Optional[dict[str, object]],
        operation: str,
        with_fallbacks: bool = False,
        tools: typing.Optional[list[dict[str, object]]] = None,
        messages: typing.Optional[list[dict[str, typing.Any]]] = None,
    ):
        kwargs: dict[str, typing.Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages
            if messages is not None
            else [{"role": "user", "content": user_text}],
        }
        if output_config is not None:
            kwargs["output_config"] = output_config
        if tools:
            kwargs["tools"] = tools

        started = time.perf_counter()
        try:
            if with_fallbacks:
                # A safety classifier can decline a request outright; opting into
                # the server-side fallback means the report still gets a summary
                # instead of an error. Orgs without the beta enabled fall through
                # to a plain request below.
                try:
                    message = client.beta.messages.create(
                        betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
                    )
                except anthropic.BadRequestError as error:
                    logger.warning(
                        "ai_fallbacks_rejected operation=%s error=%s", operation, error
                    )
                    message = client.messages.create(**kwargs)
            else:
                message = client.messages.create(**kwargs)
        except anthropic.APIStatusError as error:
            raise AICommentaryUnavailable(
                f"Claude request failed ({error.status_code}): {self._error_text(error)}"
            ) from error
        except anthropic.APIConnectionError as error:
            raise AICommentaryUnavailable(f"Could not reach Claude: {error}") from error

        # Every Claude call, on one line: what it cost in tokens and how long it
        # took. Without this a slow or expensive call is invisible until it shows
        # up as a timeout or a bill.
        usage = getattr(message, "usage", None)
        log_event(
            logger,
            "llm_call",
            operation=operation,
            model=model,
            stop_reason=getattr(message, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            cache_read=getattr(usage, "cache_read_input_tokens", None),
            max_tokens=max_tokens,
            tools=len(tools) if tools else 0,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise AICommentaryUnavailable(
                f"Claude declined to write the {operation.replace('_', ' ')} ({category})."
            )
        return message

    @staticmethod
    def _error_text(error: anthropic.APIStatusError) -> str:
        message = getattr(error, "message", "") or str(error)
        return message[:300]

    @staticmethod
    def _answer_text_of(message) -> str:
        """The model's *answer*, for a turn that used server-side tools.

        With web search the response interleaves narration and tool calls — "I'll
        search for…", then the searches, then the real write-up. Joining every
        text block the way :meth:`_text_of` does would paste that preamble into
        the report, so only the text after the last tool block counts.
        """
        blocks = list(getattr(message, "content", None) or [])
        last_tool = -1
        for index, block in enumerate(blocks):
            if str(getattr(block, "type", "") or "").endswith(("tool_result", "tool_use")):
                last_tool = index
        parts = [
            block.text
            for block in blocks[last_tool + 1 :]
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        return "\n".join(parts).strip()

    @staticmethod
    def _text_of(message) -> str:
        parts = [
            block.text
            for block in (getattr(message, "content", None) or [])
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        ]
        return "\n".join(parts).strip()

    @staticmethod
    def _parse_json(text: str, *, operation: str) -> dict[str, typing.Any]:
        # output_config.format guarantees valid JSON, so a parse failure here means
        # the response was truncated (max_tokens) rather than malformed prose.
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError) as error:
            logger.error("ai_json_parse_failed operation=%s prefix=%s", operation, text[:200])
            raise AICommentaryUnavailable(
                f"Claude returned an unreadable {operation.replace('_', ' ')} response."
            ) from error
        if not isinstance(parsed, dict):
            raise AICommentaryUnavailable(
                f"Claude returned an unexpected {operation.replace('_', ' ')} shape."
            )
        return parsed
