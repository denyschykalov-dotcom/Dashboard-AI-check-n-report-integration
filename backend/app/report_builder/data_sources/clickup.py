"""ClickUp-backed blocks: work completed and planned works.

Uses the *generating user's* own ClickUp API token (set per-user in Report
Builder settings). Finds the client's task list by name in that user's ClickUp
workspaces, then splits its tasks into two report sections by their exact
ClickUp status label (not the broader open/closed status *type* — client lists
commonly run a 4-stage workflow, e.g. todo -> doing -> done -> complete, and
only the named "Done"/"Todo" stages belong in the report):

* ``work_completed`` (DONE) — tasks whose status is literally "Done" (not
  "Complete"/closed-archived) and whose completion date falls in the report
  month. A task closed in an earlier month is not re-listed every period.
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

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# "2026-07" / "2026/07"
_ISO_MONTH_RE = re.compile(r"^(\d{4})[-/](\d{1,2})$")
# "07/2026"
_NUMERIC_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})/(\d{4})$")
# "Jun 2026" / "June 2026" / "Jun, 2026"
_NAME_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]+)\.?,?\s+(\d{4})$")


def _parse_period_label(label: typing.Optional[str]) -> typing.Optional[tuple[int, int]]:
    """Best-effort parse of a free-form period label into (year, month).

    Report periods are author-chosen strings (e.g. "Jun 2026", matched
    verbatim against a "Period" column in GA4/GSC sheets) as well as the
    wall-clock "YYYY-MM" default, so this accepts the common shapes rather
    than assuming one fixed format. Returns None if unparseable.
    """
    text = (label or "").strip()
    if not text:
        return None

    match = _ISO_MONTH_RE.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        return (year, month) if 1 <= month <= 12 else None

    match = _NUMERIC_MONTH_YEAR_RE.match(text)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        return (year, month) if 1 <= month <= 12 else None

    match = _NAME_MONTH_YEAR_RE.match(text)
    if match:
        month = _MONTH_NAMES.get(match.group(1).lower())
        if month:
            return (int(match.group(2)), month)

    return None


def _period_months(context: ResolveContext) -> set[tuple[int, int]]:
    """The set of (year, month) the report covers, for scoping "completed this
    period".

    For a custom range or full-year report every month in the selection counts.
    Otherwise prefer the report's own period label (the same value GA4/GSC filter
    on), falling back to the generation timestamp's month.
    """
    selection = context.period_selection
    if selection is not None:
        months: set[tuple[int, int]] = set()
        year, month = selection.start.year, selection.start.month
        end = (selection.end.year, selection.end.month)
        while (year, month) <= end:
            months.add((year, month))
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return months
    parsed = _parse_period_label(context.period_label)
    if parsed:
        return {parsed}
    return {(context.now.year, context.now.month)}


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

    result = {"list_name": matched["name"], "list_id": matched["id"], "tasks": tasks}
    context.cache[cache_key] = result
    return result


def _completed_in_period(summary: dict[str, object], period_months: set[tuple[int, int]]) -> bool:
    """A DONE task counts for this report only if it was completed within the
    report's month(s)."""
    date_done = summary.get("date_done")
    if not date_done:
        return False
    try:
        year, month = (int(part) for part in str(date_done).split("-")[:2])
    except ValueError:
        return False
    return (year, month) in period_months


def _status_name(task: dict) -> str:
    """A task's ClickUp status, normalized for comparison.

    Spaces, hyphens and underscores are stripped, so the stage a list calls
    "To Do", "to-do" or "TODO" all match the same way. ClickUp's own default
    status is literally "to do" with a space, so exact-string matching silently
    returned no planned works at all for any list using the stock workflow.
    """
    raw = ((task.get("status") or {}).get("status") or "").strip().lower()
    return re.sub(r"[\s_\-]+", "", raw)


def _done_tasks(tasks: list[dict], period_months: set[tuple[int, int]]) -> list[dict[str, object]]:
    """Tasks in the "Done" status, completed during the reporting period."""
    out = []
    for task in tasks:
        if _status_name(task) != _DONE_STATUS_NAME:
            continue
        summary = _task_summary(task)
        if _completed_in_period(summary, period_months):
            out.append(summary)
    return out


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
        months = _period_months(context)
        items = _done_tasks(tasks, months)
        # A "Done" task still drops out if it was completed outside the report
        # month, so count both to tell "wrong status" apart from "wrong month".
        done_any_month = sum(1 for task in tasks if _status_name(task) == _DONE_STATUS_NAME)
        log_event(
            logger,
            "clickup_block",
            block=block.key,
            list_name=data["list_name"],
            tasks_total=len(tasks),
            status_done=done_any_month,
            in_period=len(items),
            period_months=",".join(f"{y}-{m:02d}" for y, m in sorted(months)),
        )
        return BlockResult.ok(
            {"list_name": data["list_name"], "count": len(items), "tasks": items}
        )
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
