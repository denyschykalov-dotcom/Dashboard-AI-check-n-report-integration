"""Ahrefs-backed blocks: domain analysis and top movers.

Resolves from ``client.domain`` alone (no extra per-client config needed).

NOTE: The live Ahrefs API integration is not wired up yet (API credentials are
out of scope for now). Until it is, these blocks resolve ``unavailable`` with a
clear reason. When credentials are added, replace the body below with the real
site-explorer / top-pages fetch and return ``BlockResult.ok(...)``.
"""

from __future__ import annotations

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    if not (context.client.domain or "").strip():
        return BlockResult.unavailable("No domain set for this client.")
    return BlockResult.unavailable("Live Ahrefs integration is not enabled yet.")
