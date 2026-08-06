"""Thin client for the ClickUp API v2.

Uses a *per-user* personal API token (each dashboard user connects their own
ClickUp account in Report Builder settings). Finds a client's task list by name
across the workspaces/spaces/folders the token can see, then reads its tasks.
"""

from __future__ import annotations

import typing

import logging
import time

import httpx

from backend.app.observability import external_call, log_event


logger = logging.getLogger("rankberry.data_source.clickup")

_API_BASE = "https://api.clickup.com/api/v2"

# ClickUp allows ~100 requests/minute per token. Reading tracked time costs one
# call per DONE task, so a report can burst into that ceiling; ride out one 429
# rather than failing the block.
_RATE_LIMIT_RETRIES = 1
_RATE_LIMIT_DEFAULT_WAIT_S = 5.0
_RATE_LIMIT_MAX_WAIT_S = 30.0


class ClickUpAccessError(Exception):
    """Raised for any expected, handled failure to read ClickUp data."""


def _get(token: str, path: str, params: typing.Optional[dict] = None) -> dict:
    """One ClickUp GET, retrying a 429 once after the rate-limit window.

    A single report can make one call per DONE task on top of the list read, so
    a burst brushing ClickUp's ~100/min ceiling is a normal occurrence rather
    than an exceptional one — worth riding out instead of failing the block.
    """
    url = f"{_API_BASE}/{path.lstrip('/')}"
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        with external_call(logger, "clickup", path) as call:
            try:
                response = httpx.get(
                    url, headers={"Authorization": token}, params=params or {}, timeout=30.0
                )
            except httpx.HTTPError as error:
                raise ClickUpAccessError(f"Could not reach ClickUp: {error}") from error
            call["status"] = response.status_code
            call["bytes"] = len(response.content or b"")

            if response.status_code == 401:
                raise ClickUpAccessError("ClickUp token is invalid or expired (401).")
            if response.status_code == 403:
                raise ClickUpAccessError("ClickUp token has no access to that resource (403).")
            if response.status_code == 429:
                if attempt >= _RATE_LIMIT_RETRIES:
                    raise ClickUpAccessError(
                        "ClickUp API rate limit reached (429) — try again later."
                    )
                delay = _retry_after_seconds(response)
                log_event(
                    logger, "clickup_rate_limited", level=logging.WARNING,
                    path=path, retry_in_s=delay,
                )
                time.sleep(delay)
                continue
            if response.status_code != 200:
                raise ClickUpAccessError(f"ClickUp API returned {response.status_code}.")
            return response.json()
    # Unreachable: the loop either returns or raises on its last attempt.
    raise ClickUpAccessError("ClickUp API rate limit reached (429) — try again later.")


def _retry_after_seconds(response: httpx.Response) -> float:
    """How long to wait out a 429, from ClickUp's own headers where available.

    Clamped so a bad header value can't stall a report generation.
    """
    for header in ("Retry-After", "X-RateLimit-Reset"):
        raw = response.headers.get(header)
        if not raw:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if header == "X-RateLimit-Reset" and value > 10_000:
            # Some responses send an absolute epoch second rather than a delta;
            # a window that already elapsed clamps to "retry now", not a wait.
            value = value - time.time()
        return min(max(value, 0.0), _RATE_LIMIT_MAX_WAIT_S)
    return _RATE_LIMIT_DEFAULT_WAIT_S


def verify_token(token: str) -> dict:
    """Validate a token; returns the authorized user (raises on failure)."""
    return _get(token, "user").get("user", {})


def _normalize(value: typing.Optional[str]) -> str:
    return (value or "").strip().lower()


def _name_matches(list_name: str, needles: list[str]) -> bool:
    haystack = _normalize(list_name)
    return any(n and n in haystack for n in needles)


def _iter_all_lists(token: str) -> typing.Iterator[dict]:
    """Yield every list the token can see: folder lists + folderless lists,
    across all spaces of all workspaces, plus the "Shared with me" hierarchy.
    Each yielded dict has id/name. Duplicate list ids are suppressed."""

    seen: set[str] = set()

    def emit(list_id: str, name: str, folder: typing.Optional[str]) -> typing.Iterator[dict]:
        if list_id in seen:
            return
        seen.add(list_id)
        yield {"id": list_id, "name": name, "folder": folder}

    teams = _get(token, "team").get("teams", [])
    for team in teams:
        spaces = _get(token, f"team/{team['id']}/space", {"archived": "false"}).get("spaces", [])
        for space in spaces:
            space_id = space["id"]
            folders = _get(token, f"space/{space_id}/folder", {"archived": "false"}).get("folders", [])
            for folder in folders:
                for lst in folder.get("lists", []):
                    yield from emit(lst["id"], lst["name"], folder.get("name"))
            folderless = _get(token, f"space/{space_id}/list", {"archived": "false"}).get("lists", [])
            for lst in folderless:
                yield from emit(lst["id"], lst["name"], None)

        # Lists shared directly with this user live outside the team's own
        # space tree (ClickUp's "Shared with me"), so the traversal above never
        # reaches them. Pull them from the shared-hierarchy endpoint.
        try:
            shared = _get(token, f"team/{team['id']}/shared").get("shared", {})
        except ClickUpAccessError:
            shared = {}
        for folder in shared.get("folders", []):
            for lst in folder.get("lists", []):
                yield from emit(lst["id"], lst["name"], folder.get("name"))
        for lst in shared.get("lists", []):
            yield from emit(lst["id"], lst["name"], None)


def find_client_list(token: str, *, name: str, domain: str) -> typing.Optional[dict]:
    """Find the ClickUp list whose name matches the client.

    Matches against the client name and the domain's root label (e.g.
    "onebyone.ua" -> "onebyone"), so a list called "onebyone (30)" resolves.
    Returns {id, name} of the first match, or None.
    """

    needles = []
    if name:
        needles.append(_normalize(name))
    root_label = _normalize(domain).split(".")[0] if domain else ""
    if root_label:
        needles.append(root_label)
    needles = [n for n in dict.fromkeys(needles) if n]  # dedupe, keep order
    if not needles:
        return None

    for lst in _iter_all_lists(token):
        if _name_matches(lst["name"], needles):
            return {"id": lst["id"], "name": lst["name"]}
    return None


def fetch_task_time(token: str, task_id: str) -> list[dict]:
    """Tracked-time intervals for one task, grouped by user.

    Uses the legacy per-task endpoint rather than the workspace-wide
    ``team/{id}/time_entries`` one on purpose: ``time_entries`` returns only the
    *token owner's* own entries, and narrowing it to anyone else with
    ``assignee`` needs workspace-admin rights (403 ``TIMEENTRY_059`` otherwise).
    A per-user token would therefore see zero tracked time on every task a
    colleague logged. This endpoint returns every user's intervals.

    Returns the raw blocks: ``[{user, time, intervals: [{start, time, ...}]}]``.
    """
    return _get(token, f"task/{task_id}/time").get("data", []) or []


def fetch_tasks(token: str, list_id: str) -> list[dict]:
    """All tasks in a list, including closed ones, paging through results."""

    tasks: list[dict] = []
    page = 0
    while True:
        payload = _get(
            token,
            f"list/{list_id}/task",
            {"archived": "false", "include_closed": "true", "subtasks": "true", "page": str(page)},
        )
        batch = payload.get("tasks", [])
        tasks.extend(batch)
        if payload.get("last_page", True) or not batch:
            break
        page += 1
        if page > 20:  # safety backstop
            break
    return tasks
