"""Persistent cache for external-API pulls that cannot change once fetched.

Ahrefs bills per request — one report's eight Site Explorer calls cost about
2,300 API units — and its figures are point-in-time snapshots of a *finished*
month: asking twice returns the same numbers. So the payload is stored once and
replayed for every later report of that same month, until the TTL runs out.

Only data that is immutable by nature belongs here. Live figures go stale inside
a two-week window and are deliberately left out: SE Ranking keyword positions
move daily, ClickUp task lists change as work lands, and the GA4/GSC sheets are
appended to by a collector after the month ends.

The cache is a plain table rather than an in-process dict because the point is
reuse *across* reports, days apart, in whichever web worker serves the request.
"""

from __future__ import annotations

import typing

import json
import logging
from datetime import timedelta, timezone

from backend.app.models import ApiCache
from backend.app.utils import utcnow


logger = logging.getLogger("rankberry.report_builder.api_cache")

# "Pull it once a fortnight" — long enough that a month's reporting round costs
# one pull, short enough that a revised Ahrefs snapshot still lands eventually.
DEFAULT_TTL_DAYS = 14


def _as_utc(moment):
    """Stored timestamps are UTC; SQLite hands them back without a tzinfo."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def get_or_fetch(
    session: typing.Any,
    key: str,
    fetch: typing.Callable[[], dict],
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict:
    """The stored payload for ``key``, else ``fetch()``'s result, stored for ``ttl_days``.

    A failing ``fetch`` propagates and stores nothing, so a rate-limited or
    broken pull is retried on the next report rather than cached as the answer.
    """
    if session is None:
        return fetch()  # no database in this context: always read through

    now = utcnow()
    row = session.get(ApiCache, key)
    if row is not None and _as_utc(row.expires_at) > now:
        try:
            return json.loads(row.payload_json)
        except ValueError:
            logger.warning("api_cache_unreadable key=%s", key)  # refetch below

    payload = fetch()
    row = ApiCache(
        cache_key=key,
        payload_json=json.dumps(payload),
        expires_at=now + timedelta(days=ttl_days),
        created_at=now,
    )
    session.merge(row)
    session.commit()
    logger.info("api_cache_stored key=%s ttl_days=%s", key, ttl_days)
    return payload
