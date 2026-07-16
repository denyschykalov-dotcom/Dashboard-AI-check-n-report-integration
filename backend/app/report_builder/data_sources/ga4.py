"""GA4-sheet-backed blocks: summary, top landing pages, monetization,
AI traffic, and the donut/bar variants of session-mix-by-channel.

Resolves via ``client.ga4_sheet_id``. A missing sheet id yields ``unavailable``
(not configured); this is the same handled state used for a live fetch failure.

NOTE: The live Google Sheet read is not wired up yet (credentials are out of
scope for now). Until it is, configured clients still resolve ``unavailable``
with a clear reason. When credentials are added, replace the body below with the
real sheet read keyed off ``client.ga4_sheet_id`` and the requested block.
"""

from __future__ import annotations

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    if not (context.client.ga4_sheet_id or "").strip():
        return BlockResult.unavailable("Not configured for this client (no GA4 sheet linked).")
    return BlockResult.unavailable("Live GA4 sheet integration is not enabled yet.")
