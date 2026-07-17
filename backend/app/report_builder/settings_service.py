"""Per-user Report Builder settings — currently the user's own ClickUp API
token, stored encrypted at rest and never returned to the client verbatim.
"""

from __future__ import annotations

import typing

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import UserSettings
from backend.app.report_builder import secrets_crypto
from backend.app.utils import utcnow


def _get_row(session: Session, user_id: uuid.UUID) -> typing.Optional[UserSettings]:
    return session.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    ).scalar_one_or_none()


def get_clickup_token(session: Session, user_id: uuid.UUID) -> typing.Optional[str]:
    row = _get_row(session, user_id)
    if row is None:
        return None
    return secrets_crypto.decrypt(row.clickup_token_encrypted)


def set_clickup_token(session: Session, user_id: uuid.UUID, token: str) -> None:
    cleaned = (token or "").strip()
    if not cleaned:
        raise ValueError("ClickUp API token cannot be empty.")
    encrypted = secrets_crypto.encrypt(cleaned)
    row = _get_row(session, user_id)
    if row is None:
        row = UserSettings(user_id=user_id, clickup_token_encrypted=encrypted)
        session.add(row)
    else:
        row.clickup_token_encrypted = encrypted
        row.updated_at = utcnow()
    session.commit()


def clear_clickup_token(session: Session, user_id: uuid.UUID) -> None:
    row = _get_row(session, user_id)
    if row is not None:
        row.clickup_token_encrypted = None
        row.updated_at = utcnow()
        session.commit()


def get_status(session: Session, user_id: uuid.UUID) -> dict[str, object]:
    token = get_clickup_token(session, user_id)
    return {
        "clickup_configured": bool(token),
        "clickup_token_hint": secrets_crypto.hint(token),
    }
