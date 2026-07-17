"""Shared types for report-builder data-source resolvers.

Each resolver takes a :class:`ResolveContext` and returns a :class:`BlockResult`
that is either ``ok`` (with a data payload) or ``unavailable`` (with a reason).
A resolver must never raise for an expected condition (missing per-client
config, unreachable source, no data) — it returns an ``unavailable`` result so
one block can fail without affecting the others (spec FR-006).
"""

from __future__ import annotations

import typing

import uuid

from dataclasses import dataclass, field
from datetime import datetime

from backend.app.models import Client


@dataclass
class BlockResult:
    status: str  # "ok" | "unavailable"
    data: typing.Optional[dict[str, object]] = None
    unavailable_reason: typing.Optional[str] = None

    @classmethod
    def ok(cls, data: dict[str, object]) -> "BlockResult":
        return cls(status="ok", data=data)

    @classmethod
    def unavailable(cls, reason: str) -> "BlockResult":
        return cls(status="unavailable", unavailable_reason=reason)


@dataclass
class ResolveContext:
    client: Client
    period_label: str
    now: datetime
    # SQLAlchemy Session; typed loosely to avoid a hard import cycle in resolvers
    # that don't touch the database.
    session: typing.Any = None
    # Shared per-generate-call cache so multiple blocks from the same external
    # source (e.g. several GA4 sheet blocks) fetch that source only once.
    cache: dict = field(default_factory=dict)
    # The user generating the report — used for per-user credentials (e.g. that
    # user's own ClickUp API token).
    user_id: typing.Optional[uuid.UUID] = None
