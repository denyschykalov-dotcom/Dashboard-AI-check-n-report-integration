"""Thin client for the SE Ranking Project Management API (rank tracker).

Wraps auth (``Authorization: Token`` header, key from ``SERANKING_API_KEY``)
and project resolution: a client is keyed by ``Client.se_ranking_target``,
which may be either an SE Ranking project id or a substring matching a
tracked project's URL/name (case-insensitive, scheme/``www.``/trailing-slash
agnostic) — a client's domain often maps to more than one SE Ranking project
(regional variants, priority keyword lists), so a plain domain match isn't
precise enough on its own.
"""

from __future__ import annotations

import typing

from urllib.parse import urlparse

import logging

import httpx

from backend.app.config import get_settings
from backend.app.observability import external_call


logger = logging.getLogger("rankberry.data_source.se_ranking")


_API_BASE = "https://api.seranking.com/v1/project-management"


class SeRankingAccessError(Exception):
    """Raised for any expected, handled failure to read SE Ranking data."""


def _token() -> str:
    key = get_settings().seranking_api_key
    if not key:
        raise SeRankingAccessError("SE Ranking API key is not configured for this deployment.")
    return key


def _get(path: str, params: typing.Optional[dict[str, typing.Any]] = None) -> typing.Any:
    url = f"{_API_BASE}{path}"
    headers = {"Authorization": f"Token {_token()}", "Accept": "application/json"}
    with external_call(logger, "se_ranking", path) as call:
        try:
            response = httpx.get(url, headers=headers, params=params or {}, timeout=40.0)
        except httpx.HTTPError as error:
            raise SeRankingAccessError(f"Could not reach SE Ranking: {error}") from error
        call["status"] = response.status_code
        call["bytes"] = len(response.content or b"")

        if response.status_code == 401:
            raise SeRankingAccessError("SE Ranking API rejected the key (401).")
        if response.status_code == 403:
            raise SeRankingAccessError("SE Ranking API access denied (403) — check the subscription/plan.")
        if response.status_code == 429:
            raise SeRankingAccessError("SE Ranking API rate limit reached (429) — try again later.")
        if response.status_code != 200:
            raise SeRankingAccessError(f"SE Ranking API returned {response.status_code}.")
        return response.json()


def _normalize(value: str) -> str:
    value = value.strip().lower()
    if "://" in value:
        value = urlparse(value).netloc or value
    return value.removeprefix("www.").rstrip("/")


def resolve_site_id(target: str) -> typing.Optional[int]:
    """Match ``target`` against the account's tracked projects.

    A purely numeric ``target`` is taken as the project id directly.
    Otherwise it's matched as a substring of each project's URL or title.
    Returns the first match's id, or ``None`` if nothing matches.
    """
    target = target.strip()
    if target.isdigit():
        return int(target)

    needle = _normalize(target)
    sites = _get("/sites") or []
    for site in sites:
        name = _normalize(str(site.get("name") or ""))
        title = _normalize(str(site.get("title") or ""))
        if needle and (needle in name or needle in title):
            return int(site["id"])
    return None


def get_positions(site_id: int, date_from: str, date_to: str) -> list[dict[str, typing.Any]]:
    """Per-search-engine keyword positions for ``site_id`` within the range.

    Each item is ``{"site_engine_id": ..., "keywords": [...]}``; each keyword
    entry carries ``name``, ``volume`` and a chronological ``positions``
    series (date + ``pos``, where ``0`` means not ranked in the tracked
    depth).
    """
    payload = _get(
        "/sites/positions",
        {"site_id": site_id, "date_from": date_from, "date_to": date_to},
    )
    return payload or []
