from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse

from backend.app.api.routes import public_router, router
from backend.app.config import get_settings
from backend.app.logging_config import configure_logging
from backend.app.migrations.runner import run_pending_migrations


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = PROJECT_ROOT / "dist"

configure_logging()
logger = logging.getLogger("rankberry.backend")

app = FastAPI(title="Rankberry Dashboard API")
# The dashboard is served from this same app (see the static mount at the
# bottom), so in production nothing needs CORS at all — the list exists for the
# Vite dev server, and for a frontend hosted somewhere else. It used to be "*"
# alongside allow_credentials, which is not a legal pair and let any site on the
# internet put questions to an API that answers with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
# Before the SPA's catch-all static mount, or /r/<token> would serve index.html.
app.include_router(public_router)


@app.middleware("http")
async def normalize_supabase_auth_confirm_path(request: Request, call_next):
    stripped_path = request.scope.get("path", "").lstrip("/")
    if stripped_path == "auth/confirm":
        request.scope["path"] = "/auth/confirm"
    return await call_next(request)


@app.middleware("http")
async def log_api_requests(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)

    started_at = time.perf_counter()
    client_host = request.client.host if request.client else "-"
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        logger.exception(
            "api_request_failed method=%s path=%s duration_ms=%s client=%s",
            request.method,
            path,
            duration_ms,
            client_host,
        )
        raise

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    current_user = getattr(request.state, "current_user", None)
    user_id = getattr(current_user, "user_id", None)
    log_method = logger.info
    if response.status_code >= 500:
        log_method = logger.error
    elif response.status_code >= 400:
        log_method = logger.warning
    log_method(
        "api_request method=%s path=%s status_code=%s duration_ms=%s user_id=%s client=%s",
        request.method,
        path,
        response.status_code,
        duration_ms,
        user_id or "-",
        client_host,
    )
    return response


@app.on_event("startup")
def startup() -> None:
    logger.info("backend_startup migrations=starting")
    run_pending_migrations()
    logger.info("backend_startup migrations=complete")


@app.get("//auth/confirm", include_in_schema=False)
@app.get("/auth/confirm", include_in_schema=False)
def confirm_supabase_auth_link(request: Request) -> RedirectResponse:
    query_params = request.query_params
    params = {
        key: value
        for key, value in {
            "token_hash": query_params.get("token_hash"),
            "type": query_params.get("type"),
            "error": query_params.get("error"),
            "error_code": query_params.get("error_code"),
            "error_description": query_params.get("error_description"),
        }.items()
        if value
    }
    target = f"/?{urlencode(params)}" if params else "/"
    return RedirectResponse(url=target, status_code=303)


if DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True), name="frontend")
