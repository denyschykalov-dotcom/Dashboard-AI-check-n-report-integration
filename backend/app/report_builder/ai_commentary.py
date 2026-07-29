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

import anthropic

from backend.app.config import Settings
from backend.app.report_builder.block_catalog import get_block
from backend.app.report_builder.export import SECTION_BY_KEY
from backend.app.utils import read_text_file


logger = logging.getLogger("rankberry.report_builder.ai")


# The two blocks that never get a generated per-block comment: the hero has no
# comment slot at all, and the summary is the Opus job at submit time.
_NO_COMMENT_BLOCKS = frozenset({"intro_header", "summary"})

SUMMARY_BLOCK_KEY = "summary"

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
# Opus with thinking on a full report can run well past the 60s the rest of the
# app uses for its providers.
_MIN_TIMEOUT_SECONDS = 240.0

_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_DEFAULT_COMMENT_PROMPT = (
    "You are a senior SEO analyst writing the specialist comment that sits under "
    "one section of a monthly client report. Use the whole report as context. "
    "Per comment: 2-4 sentences, 220-500 characters, no markdown, no headings. "
    "Lead with what happened using the actual figures from the data, then what it "
    "means for the client and the next step. Never invent a figure."
)

_DEFAULT_SUMMARY_PROMPT = (
    "You are the lead SEO strategist writing the executive summary at the top of "
    "a monthly client report. Use the report data and the specialist's edited "
    "comments; never contradict them. Plain business English for a non-SEO "
    "reader, 2-4 short paragraphs, maximum 1500 characters, no markdown, no "
    "headings. Use only figures present in the data. Return only the summary."
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
            entry["data"] = _prune(block.get("data") or {})
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

    # -- summary ---------------------------------------------------------------

    def generate_summary(
        self,
        *,
        context: dict[str, object],
        existing_summary: str = "",
    ) -> str:
        """The report-wide executive summary, capped at the configured length."""
        client = self._api()

        draft = (existing_summary or "").strip()
        draft_block = (
            "The specialist drafted this summary — keep its facts and intent, improve the "
            f"writing:\n{draft}\n\n"
            if draft
            else ""
        )
        user_text = (
            f"Write the executive summary for this report in at most {self.summary_max_chars} "
            "characters.\n\n"
            f"{draft_block}"
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
    ):
        kwargs: dict[str, typing.Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }
        if output_config is not None:
            kwargs["output_config"] = output_config

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
