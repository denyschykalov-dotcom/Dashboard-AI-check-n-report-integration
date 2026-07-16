"""Google Search Console sheet-backed blocks: summary, top queries & pages,
and the donut/bar variants of branded-vs-non-branded clicks.

Resolves via ``client.ga4_sheet_id`` (GSC tabs live in the same client sheet as
GA4, per the report template). A missing sheet id yields ``unavailable``.

NOTE: The live Google Sheet read is not wired up yet (credentials are out of
scope for now). When credentials are added, replace the body below with the real
sheet read.
"""

from __future__ import annotations

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    if not (context.client.ga4_sheet_id or "").strip():
        return BlockResult.unavailable("Not configured for this client (no GA4/GSC sheet linked).")
    return BlockResult.unavailable("Live Search Console sheet integration is not enabled yet.")
