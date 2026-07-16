"""SE Ranking-backed block: tracked keyword positions.

Resolves via ``client.se_ranking_target``. A missing target yields
``unavailable`` (not configured).

NOTE: The live SE Ranking API integration is not wired up yet (credentials are
out of scope for now; the account subscription was also expired in the source
template). When credentials are added, replace the body below with the real
tracked-keyword fetch keyed off ``client.se_ranking_target``.
"""

from __future__ import annotations

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    if not (context.client.se_ranking_target or "").strip():
        return BlockResult.unavailable("Not configured for this client (no SE Ranking target set).")
    return BlockResult.unavailable("Live SE Ranking integration is not enabled yet.")
