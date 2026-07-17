"""ClickUp-backed blocks: work completed and planned works.

Uses the *generating user's* own ClickUp API token (set per-user in Report
Builder settings). Finds the client's task list by name in that user's ClickUp
workspaces, then splits its tasks:

* ``work_completed`` — tasks in a done/closed status.
* ``planned_works``  — tasks still open (todo / backlog / in-progress).

If the user hasn't connected ClickUp, or no list matches the client, or the
token can't reach it, the block resolves ``unavailable`` (spec FR-006).
"""

from __future__ import annotations

import typing

from datetime import datetime, timezone

from backend.app.report_builder import settings_service
from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources import clickup_client
from backend.app.report_builder.data_sources.clickup_client import ClickUpAccessError
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


_COMPLETED_STATUS_TYPES = {"done", "closed"}


def _epoch_ms_to_iso(value: typing.Optional[str]) -> typing.Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _task_summary(task: dict) -> dict[str, object]:
    status = task.get("status") or {}
    assignees = [a.get("username") or a.get("email") or "" for a in task.get("assignees", [])]
    return {
        "name": task.get("name", ""),
        "status": status.get("status", ""),
        "status_type": status.get("type", ""),
        "url": task.get("url", ""),
        "date_done": _epoch_ms_to_iso(task.get("date_done")),
        "due_date": _epoch_ms_to_iso(task.get("due_date")),
        "assignees": [a for a in assignees if a],
    }


def _load_tasks(context: ResolveContext) -> dict[str, object]:
    """Resolve the client's list and fetch its tasks once per generate call."""
    cache_key = ("clickup_tasks", context.client.id)
    if cache_key in context.cache:
        return context.cache[cache_key]

    token = settings_service.get_clickup_token(context.session, context.user_id) if context.user_id else None
    if not token:
        raise ClickUpAccessError(
            "No ClickUp API key connected. Add yours in Report Builder settings."
        )

    matched = clickup_client.find_client_list(
        token, name=context.client.name, domain=context.client.domain
    )
    if not matched:
        raise ClickUpAccessError(
            f"No ClickUp list found matching '{context.client.name}' in your workspaces."
        )

    tasks = clickup_client.fetch_tasks(token, matched["id"])
    result = {"list_name": matched["name"], "list_id": matched["id"], "tasks": tasks}
    context.cache[cache_key] = result
    return result


def _split(tasks: list[dict], *, completed: bool) -> list[dict[str, object]]:
    out = []
    for task in tasks:
        status_type = ((task.get("status") or {}).get("type") or "").lower()
        is_completed = status_type in _COMPLETED_STATUS_TYPES
        if is_completed == completed:
            out.append(_task_summary(task))
    return out


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    try:
        data = _load_tasks(context)
    except ClickUpAccessError as error:
        return BlockResult.unavailable(str(error))

    tasks = data["tasks"]
    if block.key == "work_completed":
        items = _split(tasks, completed=True)
        return BlockResult.ok(
            {"list_name": data["list_name"], "count": len(items), "tasks": items}
        )
    if block.key == "planned_works":
        items = _split(tasks, completed=False)
        return BlockResult.ok(
            {"list_name": data["list_name"], "count": len(items), "tasks": items}
        )
    return BlockResult.unavailable(f"No ClickUp resolver for block '{block.key}'.")
