from __future__ import annotations

import typing

import logging
import time
import uuid
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException, Request

from backend.app.config import get_settings


TOKEN_CACHE_TTL_SECONDS = 60
_TOKEN_CACHE: dict[str, tuple[float, "AuthenticatedUser"]] = {}
logger = logging.getLogger("rankberry.auth")


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    access_token: str
    email: str
    is_admin: bool


def get_current_user(
    request: Request,
    authorization: typing.Optional[str] = Header(default=None),
) -> AuthenticatedUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    access_token = authorization.split(" ", 1)[1].strip()
    if not access_token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    now = time.time()
    cached = _TOKEN_CACHE.get(access_token)
    if cached and cached[0] > now:
        request.state.current_user = cached[1]
        return cached[1]

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        logger.error(
            "auth_config_incomplete supabase_url_configured=%s anon_key_configured=%s",
            bool(settings.supabase_url),
            bool(settings.supabase_anon_key),
        )
        raise HTTPException(
            status_code=500,
            detail="Supabase auth configuration is incomplete. Set SUPABASE_URL and SUPABASE_ANON_KEY.",
        )

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "apikey": settings.supabase_anon_key,
                },
            )
    except httpx.HTTPError as error:
        logger.warning("supabase_auth_request_failed error=%s", error)
        raise HTTPException(status_code=502, detail=f"Supabase auth request failed: {error}") from error

    if response.status_code != 200:
        logger.warning("supabase_session_rejected status_code=%s", response.status_code)
        raise HTTPException(status_code=401, detail="Invalid or expired Supabase session.")

    payload = response.json()
    try:
        email = str(payload["email"]).strip().lower()
        user = AuthenticatedUser(
            user_id=uuid.UUID(payload["id"]),
            access_token=access_token,
            email=email,
            is_admin=email == settings.admin_email,
        )
    except (KeyError, ValueError) as error:
        logger.warning("supabase_user_payload_invalid error=%s", error)
        raise HTTPException(status_code=401, detail="Supabase user payload was invalid.") from error

    request.state.current_user = user

    _TOKEN_CACHE[access_token] = (now + TOKEN_CACHE_TTL_SECONDS, user)
    return user
