"""Resolvers for blocks that need no external system.

* ``intro_header``   — report header built from client meta.
* ``search_industry``— editorial starter text the specialist edits.
* ``summary``        — editorial summary placeholder the specialist edits.

These always resolve ``ok``; the specialist fills/edits the real prose via the
per-block comment field.
"""

from __future__ import annotations

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    client = context.client
    if block.key == "intro_header":
        return BlockResult.ok(
            {
                "client": client.name,
                "domain": client.domain,
                "period": context.period_label,
            }
        )
    if block.key == "search_industry":
        return BlockResult.ok(
            {
                "note": "Editorial block — review the month's algorithm updates and industry context, then edit below.",
                "items": [],
            }
        )
    if block.key == "summary":
        return BlockResult.ok(
            {
                "note": "Editorial summary — pre-filled from the numbers where available; edit below before sending.",
                "text": "",
            }
        )
    return BlockResult.unavailable(f"No static/editorial resolver for block '{block.key}'.")
