"""Thin client for reading the GA4/GSC data out of a client's Google Sheet.

Each client's data lives in its own spreadsheet inside a shared Drive folder
(``GOOGLE_SHEETS_CLIENT_FOLDER_ID``), named after the client (e.g.
"onebyone.ua"). Sheets are populated externally by the Apps Script collector
described in README~1.MD; this module only reads already-collected tabs, using
a service account whose credentials file path comes from
``GOOGLE_SHEETS_CREDENTIALS_FILE`` (never hardcoded — the file itself is
gitignored and per-deployment).
"""

from __future__ import annotations

import typing

from datetime import date, datetime
from functools import lru_cache

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from backend.app.config import PROJECT_ROOT, get_settings


_SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3/files"
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class SheetsAccessError(Exception):
    """Raised for any expected, handled failure to read a client's sheet."""


def _resolve_credentials_path(raw_path: str) -> str:
    path = raw_path.strip()
    if not path:
        return path
    from pathlib import Path

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return str(candidate)


@lru_cache(maxsize=1)
def _load_credentials() -> typing.Optional[service_account.Credentials]:
    settings = get_settings()
    raw_path = settings.google_sheets_credentials_file
    if not raw_path:
        return None
    resolved = _resolve_credentials_path(raw_path)
    return service_account.Credentials.from_service_account_file(resolved, scopes=_SCOPES)


def _get_token() -> str:
    credentials = _load_credentials()
    if credentials is None:
        raise SheetsAccessError("Google Sheets credentials are not configured for this deployment.")
    if not credentials.valid:
        credentials.refresh(Request())
    return credentials.token


def fetch_tab_values(sheet_id: str, tab_names: list[str]) -> dict[str, list[list[str]]]:
    """Fetch each named tab's full values in one batched request.

    Returns a dict keyed by the *requested* tab name (not the API's echoed
    range string), values in request order, so callers can zip results back
    up reliably.
    """

    token = _get_token()
    ranges = [f"'{tab}'!A:Z" for tab in tab_names]
    url = f"{_SHEETS_API_BASE}/{sheet_id}/values:batchGet"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"ranges": ranges},
            timeout=20.0,
        )
    except httpx.HTTPError as error:
        raise SheetsAccessError(f"Could not reach Google Sheets: {error}") from error

    if response.status_code == 404:
        raise SheetsAccessError("Sheet not found — check the client's GA4 sheet ID.")
    if response.status_code == 403:
        raise SheetsAccessError("Access denied — share the sheet with the service account.")
    if response.status_code != 200:
        raise SheetsAccessError(f"Google Sheets API returned {response.status_code}.")

    payload = response.json()
    value_ranges = payload.get("valueRanges", [])
    return {
        tab_name: value_ranges[index].get("values", []) if index < len(value_ranges) else []
        for index, tab_name in enumerate(tab_names)
    }


def list_sheet_tabs(sheet_id: str) -> set[str]:
    """The set of tab (sheet) titles that actually exist in a spreadsheet.

    Needed before any ``fetch_tab_values`` call whose tab names might not all
    exist for a given client — ``batchGet`` fails its *entire* request (400)
    if even one requested range names a nonexistent tab.
    """

    token = _get_token()
    url = f"{_SHEETS_API_BASE}/{sheet_id}"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "sheets.properties.title"},
            timeout=20.0,
        )
    except httpx.HTTPError as error:
        raise SheetsAccessError(f"Could not reach Google Sheets: {error}") from error

    if response.status_code == 404:
        raise SheetsAccessError("Sheet not found — check the client's GA4 sheet ID.")
    if response.status_code == 403:
        raise SheetsAccessError("Access denied — share the sheet with the service account.")
    if response.status_code != 200:
        raise SheetsAccessError(f"Google Sheets API returned {response.status_code}.")

    payload = response.json()
    return {sheet["properties"]["title"] for sheet in payload.get("sheets", [])}


def resolve_tab_name(available: set[str], aliases: list[str]) -> typing.Optional[str]:
    """Pick whichever of a canonical tab's known alternate names actually
    exists in this sheet (different client sheets use slightly different tab
    names for the same data, e.g. "GA4 Summary" vs "GA4 Overview")."""

    for alias in aliases:
        if alias in available:
            return alias
    return None


def _normalize(value: typing.Optional[str]) -> str:
    return (value or "").strip().lower()


def find_client_sheet_id(folder_id: str, *, name: str, domain: str) -> typing.Optional[str]:
    """Find the spreadsheet in ``folder_id`` that belongs to this client.

    Client sheets are named after the client — usually its domain (e.g.
    "onebyone.ua"), sometimes just its short name. Matches, in priority order:
    exact domain, exact name, then a domain/title substring match — so
    "partsvu" and "partsvu.com" (both present in practice) resolve correctly
    when a client's domain is known.
    """

    token = _get_token()
    query = (
        f"'{folder_id}' in parents and "
        "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    )
    try:
        response = httpx.get(
            _DRIVE_API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "fields": "files(id,name)", "pageSize": 200},
            timeout=20.0,
        )
    except httpx.HTTPError as error:
        raise SheetsAccessError(f"Could not reach Google Drive: {error}") from error

    if response.status_code == 403:
        raise SheetsAccessError("Access denied — share the client folder with the service account.")
    if response.status_code != 200:
        raise SheetsAccessError(f"Google Drive API returned {response.status_code}.")

    files = response.json().get("files", [])
    domain_norm = _normalize(domain)
    name_norm = _normalize(name)

    for f in files:
        if domain_norm and _normalize(f.get("name")) == domain_norm:
            return f["id"]
    for f in files:
        if name_norm and _normalize(f.get("name")) == name_norm:
            return f["id"]
    for f in files:
        title_norm = _normalize(f.get("name"))
        if domain_norm and title_norm and (domain_norm in title_norm or title_norm in domain_norm):
            return f["id"]
    return None


def resolve_client_sheet_id(context: typing.Any) -> typing.Optional[str]:
    """The sheet id to read for this client's report-builder context.

    Fast path: use ``client.ga4_sheet_id`` if already set. Otherwise look it
    up by name in the configured Drive folder and cache the result — both in
    the per-generate-call ``context.cache`` and persisted back onto the
    ``Client`` row so future generations skip the Drive lookup entirely.
    """

    existing = (getattr(context.client, "ga4_sheet_id", None) or "").strip()
    if existing:
        return existing

    cache_key = ("resolved_client_sheet_id", context.client.id)
    if cache_key in context.cache:
        return context.cache[cache_key]

    folder_id = get_settings().google_sheets_client_folder_id
    if not folder_id:
        context.cache[cache_key] = None
        return None

    sheet_id = find_client_sheet_id(folder_id, name=context.client.name, domain=context.client.domain)
    if sheet_id:
        context.client.ga4_sheet_id = sheet_id
        if context.session is not None:
            context.session.add(context.client)
            context.session.commit()
    context.cache[cache_key] = sheet_id
    return sheet_id


def rows_to_dicts(rows: list[list[str]]) -> list[dict[str, str]]:
    """Convert a raw [[header...], [row...], ...] block into row dicts."""

    if not rows:
        return []
    header = rows[0]
    result: list[dict[str, str]] = []
    for row in rows[1:]:
        padded = row + [""] * (len(header) - len(row))
        result.append({header[i]: padded[i] for i in range(len(header))})
    return result


def parse_period_label(label: str) -> typing.Optional[date]:
    try:
        return datetime.strptime(label.strip(), "%b %Y").date().replace(day=1)
    except (ValueError, AttributeError):
        return None


def _shift_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def resolve_periods(period_labels: typing.Iterable[str]) -> dict[str, typing.Optional[str]]:
    """Given the raw 'Period' column values from a tab (e.g. "Jun 2026"),
    determine which label is current, previous (month-over-month), and the
    year-over-year comparison — by date, not row position, so extra/missing
    rows don't throw off the mapping.
    """

    by_date: dict[date, str] = {}
    for label in period_labels:
        parsed = parse_period_label(label)
        if parsed is not None:
            by_date[parsed] = label.strip()

    if not by_date:
        return {"current": None, "previous": None, "yoy": None}

    current_date = max(by_date)
    return {
        "current": by_date.get(current_date),
        "previous": by_date.get(_shift_months(current_date, -1)),
        "yoy": by_date.get(_shift_months(current_date, -12)),
    }
