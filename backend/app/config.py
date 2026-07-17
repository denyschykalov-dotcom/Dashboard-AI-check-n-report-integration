from __future__ import annotations

import typing

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse
from dotenv import load_dotenv

from backend.app.database_url import normalize_postgresql_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
PROMPTS_ROOT = BACKEND_ROOT / "prompts"

# Keep the root .env as the single source of truth for both backend and frontend.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _read_env(name: str, default: typing.Optional[str] = None) -> typing.Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _derive_supabase_url(database_url: typing.Optional[str]) -> typing.Optional[str]:
    if not database_url:
        return None
    parsed = urlparse(database_url)
    username = unquote(parsed.username or "")
    if username.startswith("postgres."):
        project_ref = username.split(".", 1)[1]
        if project_ref:
            return f"https://{project_ref}.supabase.co"
    return None


def _is_supabase_pooler_host(database_url: typing.Optional[str]) -> bool:
    if not database_url:
        return False
    hostname = urlparse(database_url).hostname or ""
    return hostname.endswith(".pooler.supabase.com")


def _runtime_database_url(database_url: typing.Optional[str]) -> typing.Optional[str]:
    if not database_url:
        return None
    if _is_supabase_pooler_host(database_url):
        return normalize_postgresql_url(database_url, port=6543)
    return normalize_postgresql_url(database_url)


@dataclass(frozen=True)
class Settings:
    database_url: str
    migration_database_url: str
    db_pool_mode: str
    db_pool_size: int
    db_max_overflow: int
    admin_email: str
    supabase_url: typing.Optional[str]
    supabase_anon_key: typing.Optional[str]
    google_sheets_credentials_file: typing.Optional[str]
    google_sheets_client_folder_id: typing.Optional[str]
    ahrefs_api_token: typing.Optional[str]
    report_builder_secret_key: typing.Optional[str]
    openai_api_key: typing.Optional[str]
    gemini_api_key: typing.Optional[str]
    grok_api_key: typing.Optional[str]
    openai_model: str
    gemini_model: str
    gemini_analysis_model: str
    gemini_sentiment_model: str
    grok_model: str
    grok_base_url: str
    max_llm_retries: int
    request_timeout_seconds: float
    raw_output_retention_days: int
    queue_poll_seconds: float
    worker_concurrency: int
    enforce_one_active_run_per_user: bool
    total_iterations: int
    iteration_analysis_prompt_file: Path
    final_sentiment_prompt_file: Path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    database_url = _read_env("RUNTIME_DATABASE_URL") or _read_env("DATABASE_URL")
    migration_database_url = _read_env("MIGRATION_DATABASE_URL") or _read_env("DATABASE_URL")

    runtime_database_url = _runtime_database_url(database_url)
    if not runtime_database_url:
        raise RuntimeError("DATABASE_URL or RUNTIME_DATABASE_URL is required.")
    if not migration_database_url:
        migration_database_url = runtime_database_url
    else:
        migration_database_url = normalize_postgresql_url(migration_database_url)

    supabase_url = (
        _read_env("SUPABASE_URL")
        or _read_env("VITE_SUPABASE_URL")
        or _derive_supabase_url(runtime_database_url)
    )
    supabase_anon_key = _read_env(
        "SUPABASE_ANON_KEY") or _read_env("VITE_SUPABASE_ANON_KEY")
    admin_email = (
        _read_env("ADMIN_EMAIL", "analytics@rankberry.marketing")
        or "analytics@rankberry.marketing"
    ).strip().lower()

    return Settings(
        database_url=runtime_database_url,
        migration_database_url=migration_database_url,
        db_pool_mode=_read_env("DB_POOL_MODE", "null") or "null",
        db_pool_size=max(int(_read_env("DB_POOL_SIZE", "1") or "1"), 1),
        db_max_overflow=max(int(_read_env("DB_MAX_OVERFLOW", "0") or "0"), 0),
        admin_email=admin_email,
        supabase_url=supabase_url,
        supabase_anon_key=supabase_anon_key,
        google_sheets_credentials_file=_read_env("GOOGLE_SHEETS_CREDENTIALS_FILE"),
        google_sheets_client_folder_id=_read_env("GOOGLE_SHEETS_CLIENT_FOLDER_ID"),
        # Env var name preserves the existing project spelling ("ACHREVS_API").
        ahrefs_api_token=_read_env("AHREFS_API_TOKEN") or _read_env("ACHREVS_API"),
        report_builder_secret_key=_read_env("REPORT_BUILDER_SECRET_KEY"),
        openai_api_key=_read_env("OPENAI_API_KEY"),
        gemini_api_key=_read_env("GEMINI_API_KEY"),
        grok_api_key=_read_env("GROK_API_KEY") or _read_env("XAI_API_KEY"),
        openai_model=_read_env("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        gemini_model=_read_env(
            "GEMINI_MODEL", "gemini-2.0-flash") or "gemini-2.0-flash",
        gemini_analysis_model=(
            _read_env("GEMINI_ANALYSIS_MODEL", _read_env(
        "GEMINI_MODEL", "gemini-2.0-flash"))
            or "gemini-2.0-flash"
        ),
        gemini_sentiment_model=(
            _read_env("GEMINI_SENTIMENT_MODEL", _read_env(
                "GEMINI_MODEL", "gemini-2.0-flash"))
            or "gemini-2.0-flash"
        ),
        grok_model=_read_env("GROK_MODEL", "grok-4.3") or "grok-4.3",
        grok_base_url=(
            _read_env("GROK_BASE_URL", "https://api.x.ai/v1")
            or "https://api.x.ai/v1"
        ).rstrip("/"),
        max_llm_retries=max(int(_read_env("MAX_LLM_RETRIES", "3") or "3"), 1),
        request_timeout_seconds=max(
            float(_read_env("REQUEST_TIMEOUT_SECONDS", "60") or "60"), 5.0),
        raw_output_retention_days=max(
            int(_read_env("RAW_OUTPUT_RETENTION_DAYS", "30") or "30"), 1),
        queue_poll_seconds=max(
            float(_read_env("QUEUE_POLL_SECONDS", "2") or "2"), 0.5),
        worker_concurrency=max(
            int(_read_env("WORKER_CONCURRENCY", "1") or "1"), 1),
        enforce_one_active_run_per_user=(
            (_read_env("ENFORCE_ONE_ACTIVE_RUN_PER_USER", "true") or "true").lower()
            not in {"0", "false", "no"}
        ),
        total_iterations=3,
        iteration_analysis_prompt_file=PROMPTS_ROOT / "iteration_analysis.txt",
        final_sentiment_prompt_file=PROMPTS_ROOT / "final_sentiment.txt",
    )
