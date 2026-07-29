from __future__ import annotations

from functools import lru_cache

from backend.app.config import get_settings
from backend.app.db import SessionLocal
from backend.app.llm import LLMClient
from backend.app.report_builder.ai_commentary import AICommentaryClient
from backend.app.run_service import RunService


@lru_cache(maxsize=1)
def get_run_service() -> RunService:
    settings = get_settings()
    return RunService(settings=settings, session_factory=SessionLocal, llm_client=LLMClient(settings))


@lru_cache(maxsize=1)
def get_ai_commentary_client() -> AICommentaryClient:
    """Claude client for report-builder commentary (block comments + summary)."""
    return AICommentaryClient(get_settings())
