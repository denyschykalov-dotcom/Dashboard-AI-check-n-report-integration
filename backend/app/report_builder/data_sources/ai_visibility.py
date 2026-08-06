"""AI-visibility blocks, sourced from this dashboard's own AI-check data.

Aggregates ``Dashboard_AI_check_run_results`` (joined to ``..._runs`` for the
``project`` label and ``created_at``) where the run's ``project`` matches the
client's ``name`` (case-insensitive), across ALL users (a client's reported
visibility should not depend on which staff member ran the checks — matching the
existing admin overview's cross-user aggregation).

Each block variant is scoped by:
  * window  — last_month (~30d) or last_6_months (~183d)
  * model   — all (any of GPT/Gemini/Grok mentioned) or one specific model
"""

from __future__ import annotations

import typing

from datetime import timedelta, timezone

from sqlalchemy import func, select

from backend.app.models import Run, RunResult
from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


_WINDOW_DAYS: dict[str, int] = {"last_month": 30, "last_6_months": 183}
_WINDOW_LABELS: dict[str, str] = {"last_month": "Last month", "last_6_months": "Last 6 months"}
_MODEL_LABELS: dict[str, str] = {"all": "All models", "gpt": "GPT", "gemini": "Gemini", "grok": "Grok"}

# The longest window any block can ask for. One fetch of this range answers every
# block, because a shorter window is a subset of it.
_MAX_WINDOW_DAYS = max(_WINDOW_DAYS.values())


def _domain_flag(row: typing.Any, model: str) -> bool:
    if model == "gpt":
        return bool(row.gpt_domain_mention)
    if model == "gemini":
        return bool(row.gem_domain_mention)
    if model == "grok":
        return bool(row.grok_domain_mention)
    return bool(row.gpt_domain_mention or row.gem_domain_mention or row.grok_domain_mention)


def _brand_flag(row: typing.Any, model: str) -> bool:
    if model == "gpt":
        return bool(row.gpt_brand_mention)
    if model == "gemini":
        return bool(row.gem_brand_mention)
    if model == "grok":
        return bool(row.grok_brand_mention)
    return bool(row.gpt_brand_mention or row.gem_brand_mention or row.grok_brand_mention)


def _as_utc(moment, fallback):
    """Stored timestamps are UTC; SQLite hands them back without a tzinfo."""
    value = moment or fallback
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_rows(context: ResolveContext, match_on: str) -> list:
    """The project's AI-check results inside the widest window, fetched once.

    Two things keep this off the Supabase egress bill:

    * only the eight scalars the aggregation reads are selected. Loading whole
      ``RunResult``/``Run`` entities dragged each run's prompt and each result's
      brand list, citation format and sentiment JSON across the wire — kilobytes
      per row to compute six booleans and a timestamp.
    * the result is cached on the per-generate context, so the eight
      AI-visibility blocks (2 windows x 4 models) share a single query instead
      of re-reading the whole project history eight times.

    The window cut also happens in SQL rather than in Python, so rows outside
    the reporting range are never transferred at all.
    """
    cache_key = f"ai_visibility:rows:{match_on}"
    cached = context.cache.get(cache_key)
    if cached is not None:
        return cached

    start_at = context.now - timedelta(days=_MAX_WINDOW_DAYS)
    rows = list(
        context.session.execute(
            select(
                Run.created_at,
                Run.user_id,
                RunResult.gpt_domain_mention,
                RunResult.gem_domain_mention,
                RunResult.grok_domain_mention,
                RunResult.gpt_brand_mention,
                RunResult.gem_brand_mention,
                RunResult.grok_brand_mention,
            )
            .join(Run, Run.id == RunResult.run_id)
            .where(
                func.lower(func.trim(Run.project)) == match_on,
                Run.created_at >= start_at,
            )
        ).all()
    )
    context.cache[cache_key] = rows
    return rows


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    window = block.ai_visibility_window
    model = block.ai_visibility_model
    if window is None or model is None:
        return BlockResult.unavailable(f"Block '{block.key}' is not a valid AI-visibility block.")
    if context.session is None:
        return BlockResult.unavailable("AI-visibility data is not available in this context.")

    # The client can name the AI-check project explicitly; without one we fall
    # back to matching a project called the same thing as the client, which only
    # works when the two happen to be spelled alike.
    chosen_project = (getattr(context.client, "ai_visibility_project", None) or "").strip()
    match_on = (chosen_project or context.client.name or "").strip().lower()
    if not match_on:
        return BlockResult.unavailable(
            "No AI-visibility project is set for this client, and it has no name to match on."
        )

    start_at = (context.now - timedelta(days=_WINDOW_DAYS[window])).astimezone(timezone.utc)
    windowed = [
        row
        for row in _load_rows(context, match_on)
        if _as_utc(row.created_at, context.now) >= start_at
    ]

    if not windowed:
        # Say which project was actually searched — "no runs found" is baffling
        # when the cause is that the client's name matches no project at all.
        source = (
            f"project '{chosen_project}'"
            if chosen_project
            else f"a project named after this client ('{context.client.name}')"
        )
        hint = "" if chosen_project else " Pick the right AI-visibility project on the client."
        return BlockResult.unavailable(
            f"No AI-visibility runs found for {source} in the selected window "
            f"({_WINDOW_LABELS[window]}).{hint}"
        )

    total = len(windowed)
    domain_matches = sum(1 for row in windowed if _domain_flag(row, model))
    brand_matches = sum(1 for row in windowed if _brand_flag(row, model))

    def _rate(part: int) -> float:
        return round((part / total) * 100, 1) if total else 0.0

    return BlockResult.ok(
        {
            "window": window,
            "window_label": _WINDOW_LABELS[window],
            "model": model,
            "model_label": _MODEL_LABELS[model],
            "total_results": total,
            "brand_matches": brand_matches,
            "domain_matches": domain_matches,
            "brand_match_rate": _rate(brand_matches),
            "domain_match_rate": _rate(domain_matches),
            "users": len({str(row.user_id) for row in windowed}),
        }
    )
