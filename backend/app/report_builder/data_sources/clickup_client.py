"""Thin client for the ClickUp API v2.

Uses a *per-user* personal API token (each dashboard user connects their own
ClickUp account in Report Builder settings). Finds a client's task list by name
across the workspaces/spaces/folders the token can see, then reads its tasks.
"""

from __future__ import annotations

import typing

import httpx


_API_BASE = "https://api.clickup.com/api/v2"


class ClickUpAccessError(Exception):
    """Raised for any expected, handled failure to read ClickUp data."""


def _get(token: str, path: str, params: typing.Optional[dict] = None) -> dict:
    url = f"{_API_BASE}/{path.lstrip('/')}"
    try:
        response = httpx.get(url, headers={"Authorization": token}, params=params or {}, timeout=30.0)
    except httpx.HTTPError as error:
        raise ClickUpAccessError(f"Could not reach ClickUp: {error}") from error

    if response.status_code == 401:
        raise ClickUpAccessError("ClickUp token is invalid or expired (401).")
    if response.status_code == 403:
        raise ClickUpAccessError("ClickUp token has no access to that resource (403).")
    if response.status_code == 429:
        raise ClickUpAccessError("ClickUp API rate limit reached (429) — try again later.")
    if response.status_code != 200:
        raise ClickUpAccessError(f"ClickUp API returned {response.status_code}.")
    return response.json()


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
    across all spaces of all workspaces. Each yielded dict has id/name."""

    teams = _get(token, "team").get("teams", [])
    for team in teams:
        spaces = _get(token, f"team/{team['id']}/space", {"archived": "false"}).get("spaces", [])
        for space in spaces:
            space_id = space["id"]
            folders = _get(token, f"space/{space_id}/folder", {"archived": "false"}).get("folders", [])
            for folder in folders:
                for lst in folder.get("lists", []):
                    yield {"id": lst["id"], "name": lst["name"], "folder": folder.get("name")}
            folderless = _get(token, f"space/{space_id}/list", {"archived": "false"}).get("lists", [])
            for lst in folderless:
                yield {"id": lst["id"], "name": lst["name"], "folder": None}


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
