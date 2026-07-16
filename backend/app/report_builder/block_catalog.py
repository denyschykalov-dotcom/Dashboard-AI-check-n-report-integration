"""Static registry of selectable report blocks.

This is the single source of truth for what blocks a user can put in a report.
It is intentionally code (not a DB table): the catalog only changes when a
developer adds a block type, which is a code change either way.

The catalog covers, per the feature spec (FR-003 / FR-003a / FR-003b):
  * 14 baseline blocks from the OnebyOne report template
  * 2 bar-chart variants of the two baseline blocks that render a donut chart
    (GA4 session mix by channel; GSC branded vs non-branded clicks)
  * 8 AI-visibility blocks: {last month, last 6 months} x {all, gpt, gemini, grok}

The catalog size is a floor, not a ceiling — new entries are added by extending
``BLOCK_CATALOG`` below.
"""

from __future__ import annotations

import typing

from dataclasses import dataclass


# Data source a block reads from. "static"/"editorial" need no external system.
Source = typing.Literal[
    "static",
    "editorial",
    "ahrefs",
    "ga4_sheet",
    "gsc_sheet",
    "se_ranking",
    "clickup",
    "ai_visibility",
]

RenderStyle = typing.Literal["text", "table", "list", "donut", "bar"]

AiVisibilityWindow = typing.Literal["last_month", "last_6_months"]
AiVisibilityModel = typing.Literal["all", "gpt", "gemini", "grok"]


@dataclass(frozen=True)
class BlockType:
    key: str
    display_name: str
    source: Source
    render_style: RenderStyle
    ai_visibility_window: typing.Optional[AiVisibilityWindow] = None
    ai_visibility_model: typing.Optional[AiVisibilityModel] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "source": self.source,
            "render_style": self.render_style,
            "ai_visibility_window": self.ai_visibility_window,
            "ai_visibility_model": self.ai_visibility_model,
        }


def _ai_visibility_blocks() -> list[BlockType]:
    windows: list[tuple[AiVisibilityWindow, str]] = [
        ("last_month", "Last month"),
        ("last_6_months", "Last 6 months"),
    ]
    models: list[tuple[AiVisibilityModel, str, str]] = [
        ("all", "all", "All models"),
        ("gpt", "gpt", "GPT"),
        ("gemini", "gemini", "Gemini"),
        ("grok", "grok", "Grok"),
    ]
    window_keys = {"last_month": "1mo", "last_6_months": "6mo"}
    blocks: list[BlockType] = []
    for window, window_label in windows:
        for model, model_key, model_label in models:
            blocks.append(
                BlockType(
                    key=f"ai_visibility_{model_key}_{window_keys[window]}",
                    display_name=f"AI Visibility — {model_label} — {window_label}",
                    source="ai_visibility",
                    render_style="table",
                    ai_visibility_window=window,
                    ai_visibility_model=model,
                )
            )
    return blocks


# --- 14 baseline blocks (order mirrors the OnebyOne report template) ----------
_BASELINE_BLOCKS: list[BlockType] = [
    BlockType("intro_header", "Intro / header", "static", "text"),
    BlockType("search_industry", "Search industry", "editorial", "text"),
    BlockType("ahrefs_domain_analysis", "Ahrefs — Domain analysis", "ahrefs", "table"),
    BlockType("ahrefs_top_movers", "Ahrefs — Top movers (pages & keywords)", "ahrefs", "table"),
    BlockType("ga4_summary", "Google Analytics 4", "ga4_sheet", "table"),
    BlockType("ga4_top_pages", "GA4 — Top landing pages", "ga4_sheet", "table"),
    BlockType("ga4_monetization", "GA4 — Monetization", "ga4_sheet", "table"),
    BlockType("ga4_ai_traffic", "GA4 — AI Traffic", "ga4_sheet", "table"),
    BlockType("gsc_summary", "Google Search Console", "gsc_sheet", "table"),
    BlockType("gsc_top_queries", "GSC — Top queries & pages", "gsc_sheet", "table"),
    BlockType("se_ranking_keywords", "SE Ranking — Tracked keywords", "se_ranking", "table"),
    BlockType("work_completed", "Work completed", "clickup", "list"),
    BlockType("planned_works", "Planned works", "clickup", "list"),
    BlockType("summary", "Summary", "editorial", "text"),
]

# --- 2 bar-chart variants of the donut baseline blocks ------------------------
_BAR_VARIANT_BLOCKS: list[BlockType] = [
    BlockType("ga4_session_mix_bar", "GA4 — Session mix by channel (bar chart)", "ga4_sheet", "bar"),
    BlockType("gsc_branded_bar", "GSC — Branded vs non-branded clicks (bar chart)", "gsc_sheet", "bar"),
]

BLOCK_CATALOG: list[BlockType] = [
    *_BASELINE_BLOCKS,
    *_BAR_VARIANT_BLOCKS,
    *_ai_visibility_blocks(),
]

_BLOCK_BY_KEY: dict[str, BlockType] = {block.key: block for block in BLOCK_CATALOG}


def get_block(key: str) -> typing.Optional[BlockType]:
    return _BLOCK_BY_KEY.get(key)


def catalog_as_dicts() -> list[dict[str, object]]:
    return [block.to_dict() for block in BLOCK_CATALOG]
