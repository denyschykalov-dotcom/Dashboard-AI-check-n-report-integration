"""ClickUp-backed blocks: work completed and planned works.

Uses the *generating user's* own ClickUp API token (set per-user in Report
Builder settings). Finds the client's task list by name in that user's ClickUp
workspaces, then splits its tasks into two report sections by their exact
ClickUp status label (not the broader open/closed status *type* — client lists
commonly run a 4-stage workflow, e.g. todo -> doing -> done -> complete, and
only the named "Done"/"Todo" stages belong in the report):

* ``work_completed`` (DONE) — every task whose status is literally "Done" (not
  "Complete"/closed-archived), whenever it was finished. The section is the
  client's full record of delivered work rather than a per-month slice: scoping
  it to the report period meant each report showed only its own month's tasks
  and dropped everything delivered before it. Most recently completed first.
* ``planned_works`` (TODO) — tasks whose status is literally "Todo" (not
  "Doing" or any other in-progress/backlog status); these are the plans
  carried into the next period.

If the user hasn't connected ClickUp, or no list matches the client, or the
token can't reach it, the block resolves ``unavailable`` (spec FR-006).
"""

from __future__ import annotations

import logging
import re
import typing

from datetime import datetime, timezone

from backend.app.observability import log_event
from backend.app.report_builder import settings_service
from backend.app.report_builder.block_catalog import BlockType
from backend.app.report_builder.data_sources import clickup_client
from backend.app.report_builder.data_sources.clickup_client import ClickUpAccessError
from backend.app.report_builder.data_sources.base import BlockResult, ResolveContext


logger = logging.getLogger("rankberry.data_source.clickup")

_DONE_STATUS_NAME = "done"
_TODO_STATUS_NAME = "todo"


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
    # Prefer the plain-text description; ClickUp returns it as "text_content"
    # (markdown-stripped) with "description" as the raw fallback.
    description = (task.get("text_content") or task.get("description") or "").strip()
    return {
        "name": task.get("name", ""),
        "description": description,
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

    # Which list was picked, and every status label in it with a count. Status
    # names are per-list in ClickUp, so when a report shows the wrong tasks the
    # answer is almost always here: either a list whose name merely contained the
    # client's, or stages this code does not recognise as done/todo.
    histogram: dict[str, int] = {}
    for task in tasks:
        label = ((task.get("status") or {}).get("status") or "?").strip().lower()
        histogram[label] = histogram.get(label, 0) + 1
    log_event(
        logger,
        "clickup_list_loaded",
        client=context.client.name,
        list_name=matched["name"],
        list_id=matched["id"],
        tasks=len(tasks),
        statuses=",".join(f"{name}:{count}" for name, count in sorted(histogram.items())) or "-",
        recognised_done=_DONE_STATUS_NAME,
        recognised_todo=_TODO_STATUS_NAME,
    )

    # The token travels with the cached result so the tracked-time lookups below
    # don't have to re-read (and re-decrypt) it per task.
    result = {"list_name": matched["name"], "list_id": matched["id"], "tasks": tasks, "token": token}
    context.cache[cache_key] = result
    return result


def _status_name(task: dict) -> str:
    """A task's ClickUp status, normalized for comparison.

    Spaces, hyphens and underscores are stripped, so the stage a list calls
    "To Do", "to-do" or "TODO" all match the same way. ClickUp's own default
    status is literally "to do" with a space, so exact-string matching silently
    returned no planned works at all for any list using the stock workflow.
    """
    raw = ((task.get("status") or {}).get("status") or "").strip().lower()
    return re.sub(r"[\s_\-]+", "", raw)


def _done_tasks(tasks: list[dict]) -> list[dict[str, object]]:
    """Every task in the "Done" status, whenever it was completed.

    Ordered most recently completed first; the few tasks ClickUp gives no
    completion date for sort to the end rather than dropping out.

    Tracked time is read straight off the task (ClickUp's own aggregate, which
    rides along with the list fetch). The block used to ask for each task's time
    entries individually — one API call per done task — to work out which month
    it belonged to; with the section no longer scoped to a period, and tracked
    time no longer reported, those calls have no reason to happen.
    """
    items = []
    for task in tasks:
        if _status_name(task) != _DONE_STATUS_NAME:
            continue
        summary = _task_summary(task)
        try:
            summary["time_spent_ms"] = int(task.get("time_spent") or 0)
        except (TypeError, ValueError):
            summary["time_spent_ms"] = 0
        items.append(summary)
    items.sort(key=lambda item: str(item.get("date_done") or ""), reverse=True)
    return items


def _todo_tasks(tasks: list[dict]) -> list[dict[str, object]]:
    """Tasks in the "Todo" status — plans carried into the next period."""
    return [_task_summary(task) for task in tasks if _status_name(task) == _TODO_STATUS_NAME]


def resolve(block: BlockType, context: ResolveContext) -> BlockResult:
    try:
        data = _load_tasks(context)
    except ClickUpAccessError as error:
        return BlockResult.unavailable(str(error))

    tasks = data["tasks"]
    if block.key == "work_completed":
        items = _done_tasks(tasks)
        total_ms = sum(int(item.get("time_spent_ms") or 0) for item in items)
        log_event(
            logger,
            "clickup_block",
            block=block.key,
            list_name=data["list_name"],
            tasks_total=len(tasks),
            status_done=len(items),
            tracked_hours=round(total_ms / 3600000, 2),
        )
        return BlockResult.ok({
            "list_name": data["list_name"],
            "count": len(items),
            "total_time_spent_ms": total_ms,
            "tasks": items,
        })
    if block.key == "planned_works":
        items = _todo_tasks(tasks)
        log_event(
            logger,
            "clickup_block",
            block=block.key,
            list_name=data["list_name"],
            tasks_total=len(tasks),
            status_todo=len(items),
        )
        return BlockResult.ok(
            {"list_name": data["list_name"], "count": len(items), "tasks": items}
        )
    return BlockResult.unavailable(f"No ClickUp resolver for block '{block.key}'.")
