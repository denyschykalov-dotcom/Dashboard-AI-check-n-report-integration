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


def _domain_flag(result: RunResult, model: str) -> bool:
    if model == "gpt":
        return bool(result.gpt_domain_mention)
    if model == "gemini":
        return bool(result.gem_domain_mention)
    if model == "grok":
        return bool(result.grok_domain_mention)
    return bool(result.gpt_domain_mention or result.gem_domain_mention or result.grok_domain_mention)


def _brand_flag(result: RunResult, model: str) -> bool:
    if model == "gpt":
        return bool(result.gpt_brand_mention)
    if model == "gemini":
        return bool(result.gem_brand_mention)
    if model == "grok":
        return bool(result.grok_brand_mention)
    return bool(result.gpt_brand_mention or result.gem_brand_mention or result.grok_brand_mention)


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    window = block.ai_visibility_window
    model = block.ai_visibility_model
    if window is None or model is None:
        return BlockResult.unavailable(f"Block '{block.key}' is not a valid AI-visibility block.")
    if context.session is None:
        return BlockResult.unavailable("AI-visibility data is not available in this context.")

    client_name = (context.client.name or "").strip().lower()
    if not client_name:
        return BlockResult.unavailable("Client has no name to match against AI-visibility projects.")

    start_at = context.now - timedelta(days=_WINDOW_DAYS[window])
    rows: list[tuple[RunResult, Run]] = list(
        context.session.execute(
            select(RunResult, Run)
            .join(Run, Run.id == RunResult.run_id)
            .where(func.lower(func.trim(Run.project)) == client_name)
        ).all()
    )
    windowed = [
        (result, run)
        for result, run in rows
        if (run.created_at or context.now).astimezone(timezone.utc) >= start_at.astimezone(timezone.utc)
    ]

    if not windowed:
        return BlockResult.unavailable(
            f"No AI-visibility runs found for this client in the selected window ({_WINDOW_LABELS[window]})."
        )

    total = len(windowed)
    domain_matches = sum(1 for result, _ in windowed if _domain_flag(result, model))
    brand_matches = sum(1 for result, _ in windowed if _brand_flag(result, model))

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
            "users": len({str(run.user_id) for _, run in windowed}),
        }
    )
