"""Thin client for the Ahrefs API v3 (Site Explorer).

Wraps auth (Bearer token from ``ACHREVS_API`` / ``AHREFS_API_TOKEN``) and the
per-report date math (current / previous-month / year-over-year comparison
points), so the resolver stays focused on shaping data for the report blocks.
"""

from __future__ import annotations

import typing

from dataclasses import dataclass
from datetime import date, timedelta

import logging
import time

import httpx

from backend.app.observability import external_call

from backend.app.config import get_settings


logger = logging.getLogger("rankberry.data_source.ahrefs")

_API_BASE = "https://api.ahrefs.com/v3/site-explorer"


class AhrefsAccessError(Exception):
    """Raised for any expected, handled failure to read Ahrefs data.

    ``retryable`` marks the failures worth a second attempt — a rate limit, a
    server-side error, a dropped connection. A rejected token or a bad date will
    fail again identically, so those are not retried.
    """

    def __init__(self, *args: object, retryable: bool = False) -> None:
        super().__init__(*args)
        self.retryable = retryable


# One retry, then give up: a report block is worth a second attempt but not a
# stalled request. The delay is deliberately short because this runs inside the
# generate call the specialist is waiting on.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class ReportDates:
    current: date
    previous: date
    yoy: date
    trend_from: date  # start of the 14-month organic-traffic trend window

    @property
    def current_label(self) -> str:
        return self.current.strftime("%b %Y")

    @property
    def previous_label(self) -> str:
        return self.previous.strftime("%b %Y")

    @property
    def yoy_label(self) -> str:
        return self.yoy.strftime("%b %Y")


def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _month_end(anchor: date, months_back: int) -> date:
    """Last calendar day of the month `months_back` months before `anchor`'s month."""
    total = anchor.year * 12 + (anchor.month - 1) - months_back
    year, month = divmod(total, 12)
    month += 1
    return _last_day_of_month(year, month)


def resolve_report_dates(today: date) -> ReportDates:
    """The report covers the most recent *complete* month relative to ``today``.

    E.g. today in July 2026 → current = Jun 2026, previous = May 2026,
    year-over-year = Jun 2025. Trend window starts 13 months before current so
    the series holds 14 monthly points ending at the current month.
    """
    current = _month_end(today, 1)
    previous = _month_end(today, 2)
    yoy = _month_end(today, 13)
    # Start 13 months before the current month so the monthly series holds 14
    # points ending at the current month (matches the report template).
    trend_start_total = current.year * 12 + (current.month - 1) - 13
    trend_from = date(trend_start_total // 12, trend_start_total % 12 + 1, 1)
    return ReportDates(current=current, previous=previous, yoy=yoy, trend_from=trend_from)


def _token() -> str:
    token = get_settings().ahrefs_api_token
    if not token:
        raise AhrefsAccessError("Ahrefs API token is not configured for this deployment.")
    return token


def get(endpoint: str, params: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """One request, retried once when the failure looks transient.

    A rate limit or a dropped connection used to take a whole report block down
    for the rest of the day — the specialist's only recourse was regenerating the
    entire report. Permanent failures (bad token, bad date) are raised on the
    first attempt, since the second would return the same thing.
    """
    for attempt in (1, 2):
        try:
            return _request(endpoint, params)
        except AhrefsAccessError as error:
            if attempt == 2 or not error.retryable:
                raise
            logger.warning(
                "ahrefs_retrying endpoint=%s after=%s delay_s=%s", endpoint, error,
                _RETRY_DELAY_SECONDS,
            )
            time.sleep(_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


def _request(endpoint: str, params: dict[str, typing.Any]) -> dict[str, typing.Any]:
    url = f"{_API_BASE}/{endpoint}"
    headers = {"Authorization": f"Bearer {_token()}", "Accept": "application/json"}
    with external_call(logger, "ahrefs", endpoint) as call:
        try:
            response = httpx.get(url, headers=headers, params=params, timeout=40.0)
        except httpx.HTTPError as error:
            raise AhrefsAccessError(
                f"Could not reach Ahrefs: {error}", retryable=True
            ) from error
        call["status"] = response.status_code
        call["bytes"] = len(response.content or b"")

        if response.status_code == 401:
            raise AhrefsAccessError("Ahrefs API rejected the token (401).")
        if response.status_code == 403:
            raise AhrefsAccessError("Ahrefs API access denied (403) — check the subscription/plan.")
        if response.status_code == 429:
            raise AhrefsAccessError(
                "Ahrefs API rate limit reached (429) — try again later.", retryable=True
            )
        if response.status_code != 200:
            # Ahrefs puts the reason in the body ({"error": "bad date"} and the
            # like). Without it a 400 in the report says nothing anyone can act on.
            detail = " ".join((response.text or "").split())[:200]
            raise AhrefsAccessError(
                f"Ahrefs API returned {response.status_code}" + (f": {detail}" if detail else "."),
                retryable=response.status_code in _RETRY_STATUSES,
            )
        return response.json()
