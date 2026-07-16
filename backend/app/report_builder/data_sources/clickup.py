"""ClickUp-backed blocks: work completed and planned works.

Resolves via ``client.clickup_list_id``. A missing list id yields
``unavailable`` (not configured).

NOTE: The live ClickUp API integration is not wired up yet (credentials are out
of scope for now). When credentials are added, replace the body below with the
real task-list fetch keyed off ``client.clickup_list_id``.
"""

from __future__ import annotations

from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    if not (context.client.clickup_list_id or "").strip():
        return BlockResult.unavailable("Not configured for this client (no ClickUp list linked).")
    return BlockResult.unavailable("Live ClickUp integration is not enabled yet.")
