"""Symmetric encryption for API tokens stored at rest (e.g. per-user ClickUp
tokens).

Key resolution, in order:
  1. ``REPORT_BUILDER_SECRET_KEY`` env — an explicit urlsafe-base64 Fernet key.
     Best for production / multi-instance deployments (pin the same key
     everywhere).
  2. Otherwise a key auto-generated once and persisted to
     ``backend/data/report_builder_secret.key`` (gitignored). This gives
     encryption-at-rest with zero setup on a single host.

Stored values are prefixed with a scheme tag (``enc:``) so the format is
explicit and future-proof. Decryption of an unknown/blank value returns None
rather than raising, so a rotated/missing key degrades to "token not
available" (the ClickUp block simply resolves ``unavailable``) instead of
crashing report generation.
"""

from __future__ import annotations

import typing

import logging
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from backend.app.config import PROJECT_ROOT, get_settings


logger = logging.getLogger("rankberry.report_builder")

_ENC_PREFIX = "enc:"
_KEY_FILE = PROJECT_ROOT / "backend" / "data" / "report_builder_secret.key"


def _load_or_create_key() -> bytes:
    configured = (get_settings().report_builder_secret_key or "").strip()
    if configured:
        return configured.encode("utf-8")

    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()

    key = Fernet.generate_key()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    logger.info("report_builder_secret_key generated at %s", _KEY_FILE)
    return key


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{_ENC_PREFIX}{token}"


def decrypt(stored: typing.Optional[str]) -> typing.Optional[str]:
    if not stored:
        return None
    if not stored.startswith(_ENC_PREFIX):
        # Legacy/plaintext value — return as-is so old rows keep working.
        return stored
    ciphertext = stored[len(_ENC_PREFIX):].encode("utf-8")
    try:
        return _fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        logger.warning("report_builder token could not be decrypted (key rotated or corrupt).")
        return None


def hint(plaintext: typing.Optional[str]) -> typing.Optional[str]:
    """A non-secret display hint for a stored token, e.g. 'pk_…c3d4'."""
    if not plaintext:
        return None
    if len(plaintext) <= 8:
        return "…" + plaintext[-2:]
    return f"{plaintext[:3]}…{plaintext[-4:]}"
