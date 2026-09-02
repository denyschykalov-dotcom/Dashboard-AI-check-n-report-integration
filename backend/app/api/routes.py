from __future__ import annotations

import typing

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from backend.app.auth import AuthenticatedUser, get_current_user
from backend.app.db import SessionLocal, get_db_session
from backend.app.models import Client, Profile
from backend.app.report_builder import ai_commentary
from backend.app.report_builder import export as report_export
from backend.app.report_builder import localization
from backend.app.report_builder import selections_service as report_selections_service
from backend.app.report_builder import service as report_service
from backend.app.report_builder import settings_service as report_settings_service
from backend.app.report_builder.data_sources import clickup_client
from backend.app.report_builder.data_sources.clickup_client import ClickUpAccessError
from backend.app.schemas import (
    BulkRunActionResponse,
    ClickUpTokenRequest,
    ClientCreateRequest,
    ClientLanguageRequest,
    ClientSettingsRequest,
    DraftAppendPayload,
    DraftPayload,
    GenerateReportRequest,
    HistoryForwardRequest,
    HistoryForwardResponse,
    ProfileUpsertRequest,
    ReportAiRequest,
    ReportPreviewRequest,
    ReportSaveRequest,
    ReportShareRequest,
    ReportUpdateRequest,
    RunStartRequest,
    SelectionSaveRequest,
)
from backend.app.service_container import get_ai_commentary_client, get_run_service
from backend.app.utils import utcnow


router = APIRouter(prefix="/api")
logger = logging.getLogger("rankberry.api")


def _sum_costs(*values: typing.Optional[float]) -> typing.Optional[float]:
    known_values = [value for value in values if value is not None]
    if not known_values:
        return None
    return round(sum(known_values), 8)


def _reject_admin_service_access(current_user: AuthenticatedUser) -> None:
    if current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin accounts do not use the AI visibility service.")


@router.get("/health")
def healthcheck() -> dict[str, object]:
    with SessionLocal() as session:
        session.execute(text("select 1"))
    return {"status": "ok", "timestamp": utcnow()}


@router.post("/profile/upsert")
def upsert_profile(
    payload: ProfileUpsertRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    service = get_run_service()
    try:
        profile = service.upsert_profile(
            session,
            user_id=current_user.user_id,
            username=payload.username,
        )
    except ValueError as error:
        logger.warning("profile_upsert_invalid user_id=%s error=%s", current_user.user_id, error)
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {
        "id": profile.id,
        "user_id": profile.user_id,
        "username": profile.username,
        "email": current_user.email,
        "is_admin": current_user.is_admin,
        "created_at": profile.created_at,
    }


@router.get("/drafts/current")
def get_current_draft(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    draft = service.get_current_draft(session, user_id=current_user.user_id)
    rows = service.parse_draft_rows(draft)
    return {
        "id": draft.id,
        "user_id": draft.user_id,
        "keyword": draft.keyword,
        "domain": draft.domain,
        "brand": draft.brand,
        "prompt": draft.prompt,
        "project": draft.project,
        "rows": rows,
        "updated_at": draft.updated_at,
    }


@router.put("/drafts/current")
def upsert_current_draft(
    payload: DraftPayload,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    draft = service.upsert_current_draft(
        session,
        user_id=current_user.user_id,
        keyword=payload.keyword,
        domain=payload.domain,
        brand=payload.brand,
        prompt=payload.prompt,
        project=payload.project,
        rows=[row.model_dump() for row in payload.rows],
    )
    rows = service.parse_draft_rows(draft)
    return {
        "id": draft.id,
        "user_id": draft.user_id,
        "keyword": draft.keyword,
        "domain": draft.domain,
        "brand": draft.brand,
        "prompt": draft.prompt,
        "project": draft.project,
        "rows": rows,
        "updated_at": draft.updated_at,
    }


@router.post("/drafts/current/append")
def append_current_draft_rows(
    payload: DraftAppendPayload,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    draft = service.append_current_draft_rows(
        session,
        user_id=current_user.user_id,
        rows=[row.model_dump() for row in payload.rows],
    )
    rows = service.parse_draft_rows(draft)
    return {
        "id": draft.id,
        "user_id": draft.user_id,
        "keyword": draft.keyword,
        "domain": draft.domain,
        "brand": draft.brand,
        "prompt": draft.prompt,
        "project": draft.project,
        "rows": rows,
        "updated_at": draft.updated_at,
    }


@router.post("/runs/start")
def start_run(
    payload: RunStartRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    try:
        run = service.create_run(
            session,
            user_id=current_user.user_id,
            keyword=payload.keyword,
            domain=payload.domain,
            brand=payload.brand,
            prompt=payload.prompt,
            project=payload.project,
        )
    except ValueError as error:
        logger.warning("run_start_invalid user_id=%s error=%s", current_user.user_id, error)
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {"run_id": run.id, "status": run.status}


@router.get("/runs/active")
def get_active_runs(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    run_ids = service.list_active_run_ids(session, user_id=current_user.user_id)
    return {"run_ids": run_ids, "total_runs": len(run_ids)}


_MAX_STATUS_POLL_IDS = 200


@router.get("/runs/status")
def get_run_statuses(
    ids: str = Query(default="", description="Comma-separated run ids to report progress for."),
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """Progress ticks for the active-run poller — no raw LLM output, by design.

    The poller previously called ``GET /runs/{run_id}`` per active run every few
    seconds, which shipped every iteration's gpt/gem/grok text out of the
    database and down to the browser for a banner that renders neither.
    """
    run_ids: list[uuid.UUID] = []
    for raw_id in ids.split(","):
        candidate = raw_id.strip()
        if not candidate:
            continue
        try:
            run_ids.append(uuid.UUID(candidate))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid run id: {candidate}") from None

    if len(run_ids) > _MAX_STATUS_POLL_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many run ids requested (max {_MAX_STATUS_POLL_IDS}).",
        )

    service = get_run_service()
    runs = service.list_run_statuses(
        session,
        user_id=current_user.user_id,
        run_ids=run_ids,
        is_admin=current_user.is_admin,
    )
    return {"runs": runs, "total_runs": len(runs)}


@router.get("/projects")
def get_user_projects(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    service = get_run_service()
    projects = service.list_user_project_options(
        session,
        user_id=None if current_user.is_admin else current_user.user_id,
    )
    return {"projects": projects}


@router.get("/users/options")
def get_user_options(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    service = get_run_service()
    users = service.list_user_options(session)
    return {"users": users}


@router.get("/runs/failed")
def get_failed_runs(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    runs = service.list_failed_runs(session, user_id=current_user.user_id)
    return {
        "items": [
            {
                "id": run.id,
                "user_id": run.user_id,
                "keyword": run.keyword,
                "domain": run.domain,
                "brand": run.brand,
                "prompt": run.prompt,
                "project": run.project,
                "status": run.status,
                "total_iterations": run.total_iterations,
                "completed_iterations": run.completed_iterations,
                "error_messages": run.error_messages,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }
            for run in runs
        ],
        "total_runs": len(runs),
    }


@router.post("/runs/stop", response_model=BulkRunActionResponse)
def stop_runs(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> BulkRunActionResponse:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    run_ids = service.stop_user_runs(session, user_id=current_user.user_id)
    return BulkRunActionResponse(run_ids=run_ids, total_runs=len(run_ids), status="stopped")


@router.post("/runs/continue", response_model=BulkRunActionResponse)
def continue_runs(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> BulkRunActionResponse:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    run_ids = service.resume_user_runs(session, user_id=current_user.user_id)
    return BulkRunActionResponse(run_ids=run_ids, total_runs=len(run_ids), status="queued")


@router.post("/runs/retry-failed", response_model=BulkRunActionResponse)
def retry_failed_runs(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> BulkRunActionResponse:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    run_ids = service.retry_failed_user_runs(session, user_id=current_user.user_id)
    return BulkRunActionResponse(run_ids=run_ids, total_runs=len(run_ids), status="queued")


@router.get("/runs/{run_id}")
def get_run_detail(
    run_id: uuid.UUID,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    service = get_run_service()
    try:
        run, outputs, result = service.get_run_detail(
            session,
            user_id=current_user.user_id,
            run_id=run_id,
            is_admin=current_user.is_admin,
        )
    except LookupError as error:
        logger.warning(
            "run_detail_not_found requester_user_id=%s run_id=%s is_admin=%s",
            current_user.user_id,
            run_id,
            current_user.is_admin,
        )
        raise HTTPException(status_code=404, detail=str(error)) from error

    owner_username = session.execute(
        select(Profile.username).where(Profile.user_id == run.user_id).order_by(Profile.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    serialized_outputs = [
        {
            "id": item.id,
            "user_id": item.user_id,
            "run_id": item.run_id,
            "iteration_number": item.iteration_number,
            # The raw gpt/gem/grok responses are intentionally absent: they are
            # stored for the record, not served. Nothing in the UI renders them,
            # and shipping them here was the bulk of this endpoint's payload.
            "gpt_domain_mention": item.gpt_domain_mention,
            "gem_domain_mention": item.gem_domain_mention,
            "grok_domain_mention": item.grok_domain_mention,
            "gpt_brand_mention": item.gpt_brand_mention,
            "gem_brand_mention": item.gem_brand_mention,
            "grok_brand_mention": item.grok_brand_mention,
            "response_count": item.response_count,
            "brand_list": item.brand_list,
            "citation_format": item.citation_format,
            "openai_generation_cost_usd": item.openai_generation_cost_usd,
            "gemini_generation_cost_usd": item.gemini_generation_cost_usd,
            "grok_generation_cost_usd": item.grok_generation_cost_usd,
            "gemini_analysis_cost_usd": item.gemini_analysis_cost_usd,
            "estimated_total_cost_usd": _sum_costs(
                item.openai_generation_cost_usd,
                item.gemini_generation_cost_usd,
                item.grok_generation_cost_usd,
                item.gemini_analysis_cost_usd,
            ),
            "project": item.project,
            "created_at": item.created_at,
        }
        for item in outputs
    ]
    serialized_result = None
    if result is not None:
        serialized_result = {
            "id": result.id,
            "user_id": result.user_id,
            "run_id": result.run_id,
            "project": result.project,
            "gpt_domain_mention": result.gpt_domain_mention,
            "gem_domain_mention": result.gem_domain_mention,
            "grok_domain_mention": result.grok_domain_mention,
            "gpt_brand_mention": result.gpt_brand_mention,
            "gem_brand_mention": result.gem_brand_mention,
            "grok_brand_mention": result.grok_brand_mention,
            "response_count_avg": result.response_count_avg,
            "brand_list": result.brand_list,
            "citation_format": result.citation_format,
            "sentiment_analysis": result.sentiment_analysis,
            "gemini_sentiment_cost_usd": result.gemini_sentiment_cost_usd,
            "estimated_total_cost_usd": _sum_costs(result.gemini_sentiment_cost_usd),
            "created_at": result.created_at,
        }

    return {
        "run": {
            "id": run.id,
            "user_id": run.user_id,
            "keyword": run.keyword,
            "domain": run.domain,
            "brand": run.brand,
            "prompt": run.prompt,
            "username": owner_username or f"User {str(run.user_id)[:8]}",
            "project": run.project,
            "status": run.status,
            "total_iterations": run.total_iterations,
            "completed_iterations": run.completed_iterations,
            "error_messages": run.error_messages,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        },
        "outputs": serialized_outputs,
        "result": serialized_result,
        "estimated_total_cost_usd": _sum_costs(
            *[item["estimated_total_cost_usd"] for item in serialized_outputs],
            serialized_result["estimated_total_cost_usd"] if serialized_result else None,
        ),
    }


@router.get("/history")
def get_history(
    project: typing.Optional[str] = Query(default=None),
    prompt: typing.Optional[str] = Query(default=None),
    user: typing.Optional[str] = Query(default=None),
    date_from: typing.Optional[date] = Query(default=None),
    date_to: typing.Optional[date] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    service = get_run_service()
    items, total = service.list_history(
        session,
        user_id=current_user.user_id,
        is_admin=current_user.is_admin,
        project=project,
        prompt=prompt,
        user_query=user,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.post("/history/forward", response_model=HistoryForwardResponse)
def forward_history(
    payload: HistoryForwardRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> HistoryForwardResponse:
    service = get_run_service()
    try:
        result = service.forward_history_runs(
            session,
            requester_user_id=current_user.user_id,
            is_admin=current_user.is_admin,
            run_ids=payload.run_ids,
            target_user_id=payload.target_user_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return HistoryForwardResponse(**result)


@router.get("/outputs")
def get_outputs(
    project: typing.Optional[str] = Query(default=None),
    prompt: typing.Optional[str] = Query(default=None),
    local_date: typing.Optional[date] = Query(default=None),
    tz_offset_minutes: typing.Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    _reject_admin_service_access(current_user)
    service = get_run_service()
    items, total = service.list_outputs(
        session,
        user_id=current_user.user_id,
        project=project,
        prompt=prompt,
        local_date=local_date,
        tz_offset_minutes=tz_offset_minutes,
        page=page,
        page_size=page_size,
    )
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/overview/summary")
def get_overview_summary(
    project: typing.Optional[str] = Query(default=None),
    user_id: typing.Optional[uuid.UUID] = Query(default=None),
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    service = get_run_service()
    return service.get_overview_summary(
        session,
        user_id=current_user.user_id,
        project=project,
        selected_user_id=user_id if current_user.is_admin else None,
        is_admin=current_user.is_admin,
    )


# --- Report Builder ----------------------------------------------------------


@router.get("/report-builder/block-catalog")
def get_block_catalog(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    return {"blocks": report_service.get_block_catalog()}


@router.get("/report-builder/settings")
def get_report_settings(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    return report_settings_service.get_status(session, current_user.user_id)


@router.put("/report-builder/settings/clickup")
def set_clickup_token(
    payload: ClickUpTokenRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    # Validate the token against ClickUp before storing, so the user gets
    # immediate feedback instead of a silent failure at report time.
    try:
        user = clickup_client.verify_token(payload.token.strip())
    except ClickUpAccessError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        report_settings_service.set_clickup_token(session, current_user.user_id, payload.token)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    status = report_settings_service.get_status(session, current_user.user_id)
    status["clickup_username"] = user.get("username")
    return status


@router.delete("/report-builder/settings/clickup")
def clear_clickup_token(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    report_settings_service.clear_clickup_token(session, current_user.user_id)
    return report_settings_service.get_status(session, current_user.user_id)


def _client_language(client: Client) -> str:
    """The client's report language, with its UI vocabulary guaranteed translated.

    Reports are authored in English and localized on the way out. The static
    vocabulary (section titles, table headers, month names) is translated once per
    language and cached, so this is a no-op after the first non-English render —
    but it has to happen before the template is built, because rendering itself
    must never make an API call. Failures are swallowed inside
    ``ensure_ui_translations``: an untranslated label falls back to English.
    """
    language = localization.normalize_language(client.report_language)
    if localization.needs_translation(language) and localization.missing_ui_strings(language):
        ai_client = get_ai_commentary_client()
        if ai_client.is_configured:
            localization.ensure_ui_translations(language, ai_client.translate_ui_strings)
    return language


@router.get("/report-builder/clients")
def list_report_clients(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    clients = report_service.list_clients(session)
    return {"clients": [report_service.serialize_client(client) for client in clients]}


@router.post("/report-builder/clients", status_code=201)
def create_report_client(
    payload: ClientCreateRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        client = report_service.create_client(
            session,
            name=payload.name,
            domain=payload.domain,
            created_by=current_user.user_id,
            report_language=payload.report_language,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return report_service.serialize_client(client)


@router.get("/report-builder/ai-visibility-projects")
def list_ai_visibility_projects(
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """The AI-check projects that have runs, for pointing a client at one."""
    return {"projects": report_service.list_ai_visibility_projects(session)}


@router.put("/report-builder/clients/{client_id}/settings")
def update_report_client_settings(
    client_id: uuid.UUID,
    payload: ClientSettingsRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """Set the SE Ranking target and/or AI-visibility project for a client."""
    try:
        client = report_service.update_client_settings(
            session,
            client_id=client_id,
            se_ranking_target=payload.se_ranking_target,
            ai_visibility_project=payload.ai_visibility_project,
            ga4_sheet_id=payload.ga4_sheet_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return report_service.serialize_client(client)


@router.put("/report-builder/clients/{client_id}/language")
def set_report_client_language(
    client_id: uuid.UUID,
    payload: ClientLanguageRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """Set the language this client's reports are delivered in.

    Reports are always built in English; a non-English language adds a Claude
    translation pass over the report's prose at generate/submit time.
    """
    try:
        client = report_service.set_client_language(
            session, client_id=client_id, report_language=payload.report_language
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return report_service.serialize_client(client)


@router.post("/report-builder/generate")
def generate_report(
    payload: GenerateReportRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        result = report_service.generate(
            session,
            client_id=payload.client_id,
            block_keys=payload.block_keys,
            user_id=current_user.user_id,
            period_preset=payload.period_preset,
            comparisons=payload.comparisons,
            comparison=payload.comparison,
            date_from=payload.date_from,
            date_to=payload.date_to,
            report_type=payload.report_type,
            planned_work_mode=payload.planned_work_mode,
            planned_work_text=payload.planned_work_text,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    # Persist the starting point so reopening this client restores the checkboxes
    # and timeframe from the report just generated.
    try:
        report_selections_service.save_selection(
            session,
            current_user.user_id,
            payload.client_id,
            block_keys=payload.block_keys,
            comparison=payload.comparison,
            period_preset=payload.period_preset,
            comparisons=payload.comparisons,
            report_type=payload.report_type,
            date_from=payload.date_from,
            date_to=payload.date_to,
        )
    except Exception:  # persistence of the convenience selection must never fail a generate
        logger.warning("Failed to persist report-builder selection", exc_info=True)
    return result


@router.post("/report-builder/reports", status_code=201)
def save_report(
    payload: ReportSaveRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        report = report_service.save_report(
            session,
            client_id=payload.client_id,
            period_label=payload.period_label,
            blocks=[block.model_dump() for block in payload.blocks],
            generated_by=current_user.user_id,
            default_comparison=payload.default_comparison,
            customization=payload.customization,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return report_service.serialize_report_summary(report)


@router.put("/report-builder/reports/{report_id}")
def update_report(
    report_id: uuid.UUID,
    payload: ReportUpdateRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        report = report_service.update_report(
            session,
            report_id=report_id,
            period_label=payload.period_label,
            blocks=[block.model_dump() for block in payload.blocks],
            generated_by=current_user.user_id,
            default_comparison=payload.default_comparison,
            customization=payload.customization,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return report_service.serialize_report_summary(report)


@router.post("/report-builder/preview")
def preview_report(
    payload: ReportPreviewRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Render a live, client-styled report preview from unsaved blocks +
    customization, returned as a self-contained HTML document for an iframe."""
    client = session.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found.")
    document = report_export.build_preview_html(
        period_label=payload.period_label,
        default_comparison=payload.default_comparison,
        blocks=[block.model_dump() for block in payload.blocks],
        client_name=client.name,
        client_domain=client.domain,
        customization=payload.customization,
        editable=True,
        language=_client_language(client),
    )
    return Response(content=document, media_type="text/html")


@router.post("/report-builder/ai/comments")
def write_report_comments(
    payload: ReportAiRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """Claude's draft comment for each section, keyed by block key.

    Runs between generate and the first preview render, so the specialist opens a
    report that already has commentary to edit rather than empty note boxes. The
    whole report goes in as context — a section's comment may explain itself by
    reference to another section.
    """
    client = session.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found.")

    blocks = [block.model_dump() for block in payload.blocks]
    ai_client = get_ai_commentary_client()
    block_keys = ai_commentary.commentable_block_keys(blocks)
    if not block_keys:
        return {"comments": {}, "model": ai_client.comment_model}

    context = ai_commentary.build_report_context(
        client_name=client.name,
        client_domain=client.domain,
        period_label=payload.period_label,
        default_comparison=payload.default_comparison,
        blocks=blocks,
    )
    # Comments are written in the client's report language directly — a second
    # translation request used to leave English text wherever a batch was dropped.
    language = _client_language(client)
    # One retry: this call runs alongside the search-industry request, so a single
    # 429/529/timeout used to hand the specialist an empty preview whose only cure
    # was clicking "Rewrite comments" — which is the same request again.
    for attempt in (1, 2):
        try:
            comments = ai_client.generate_block_comments(
                context=context, block_keys=block_keys, language=language
            )
            break
        except ai_commentary.AICommentaryUnavailable as error:
            logger.warning("ai_block_comments_failed attempt=%s error=%s", attempt, error)
            if attempt == 2:
                raise HTTPException(status_code=503, detail=str(error)) from error

    return {"comments": comments, "model": ai_client.comment_model, "language": language}


@router.post("/report-builder/ai/search-industry")
def write_search_industry(
    payload: ReportAiRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """The month's Google-search landscape for the report's intro section.

    Deliberately its own request. It is researched with web search because the
    reporting month is normally past the model's training cutoff, which makes it
    by far the slowest call here — around 90 seconds. Bundled into the comments
    request it delayed the preview by two minutes, so the specialist saw nothing
    at all while it ran; on its own the preview renders first and this fills in.

    Fails soft: an empty section the analyst writes themselves beats invented
    algorithm updates in a report a client reads as fact.
    """
    client = session.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found.")

    language = _client_language(client)
    try:
        text_out = ai_client_search_industry(client, payload.period_label, language)
    except ai_commentary.AICommentaryUnavailable as error:
        logger.warning("ai_search_industry_failed error=%s", error)
        # Still a 200 with empty text — the specialist writes the section
        # themselves. The reason rides along so the dashboard can say why the
        # section came back blank instead of leaving it a mystery.
        return {
            "text": "",
            "reason": str(error),
            "block_type_key": ai_commentary.SEARCH_INDUSTRY_BLOCK_KEY,
        }

    return {
        "text": text_out,
        "block_type_key": ai_commentary.SEARCH_INDUSTRY_BLOCK_KEY,
        "language": language,
    }


def ai_client_search_industry(
    client: Client, period_label: str, language: str = localization.DEFAULT_LANGUAGE
) -> str:
    return get_ai_commentary_client().write_search_industry(
        client_domain=client.domain, period_label=period_label, language=language
    )


@router.post("/report-builder/ai/summary")
def write_report_summary(
    payload: ReportAiRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """The report-wide executive summary that opens the report.

    Written at submit time from the final data plus the comments the specialist
    reviewed, so it can only ever agree with what the report already says.
    """
    client = session.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found.")

    context = ai_commentary.build_report_context(
        client_name=client.name,
        client_domain=client.domain,
        period_label=payload.period_label,
        default_comparison=payload.default_comparison,
        blocks=[block.model_dump() for block in payload.blocks],
        include_comments=True,
    )
    ai_client = get_ai_commentary_client()
    # Same as the block comments: written in the report's language, not translated.
    language = _client_language(client)
    try:
        summary = ai_client.generate_summary(
            context=context,
            existing_summary=payload.existing_summary,
            guidance=payload.summary_guidance,
            language=language,
        )
    except ai_commentary.AICommentaryUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return {
        "summary": summary,
        "model": ai_client.summary_model,
        "block_type_key": ai_commentary.SUMMARY_BLOCK_KEY,
        "language": language,
    }


@router.get("/report-builder/clients/{client_id}/selection")
def get_report_selection(
    client_id: uuid.UUID,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    return report_selections_service.get_selection(session, current_user.user_id, client_id)


@router.put("/report-builder/clients/{client_id}/selection")
def save_report_selection(
    client_id: uuid.UUID,
    payload: SelectionSaveRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    return report_selections_service.save_selection(
        session,
        current_user.user_id,
        client_id,
        block_keys=payload.block_keys,
        comparison=payload.comparison,
        period_preset=payload.period_preset,
        comparisons=payload.comparisons,
        report_type=payload.report_type,
        date_from=payload.date_from,
        date_to=payload.date_to,
    )


@router.get("/report-builder/clients/{client_id}/reports")
def list_client_reports(
    client_id: uuid.UUID,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    reports = report_service.list_reports_for_client(session, client_id)
    return {"reports": [report_service.serialize_report_summary(report) for report in reports]}


@router.get("/report-builder/reports/{report_id}")
def get_report_detail(
    report_id: uuid.UUID,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        report, blocks = report_service.get_report(session, report_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return report_service.serialize_report_detail(report, blocks)


@router.delete("/report-builder/reports/{report_id}", status_code=204)
def delete_report(
    report_id: uuid.UUID,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    try:
        report_service.delete_report(session, report_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    logger.info(
        "report_deleted report_id=%s requester_user_id=%s",
        report_id,
        current_user.user_id,
    )
    return Response(status_code=204)


@router.get("/report-builder/reports/{report_id}/export")
def export_report(
    report_id: uuid.UUID,
    format: str = Query(default="html", pattern="^(html|pdf|md)$"),
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    try:
        report, blocks = report_service.get_report(session, report_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    client = session.get(Client, report.client_id)
    client_name = client.name if client else "Client"
    client_domain = client.domain if client else ""
    # An export is the client-facing artifact, so it is rendered in their language.
    language = _client_language(client) if client else localization.DEFAULT_LANGUAGE
    safe_name = "".join(ch if ch.isalnum() else "-" for ch in client_name).strip("-") or "client"
    filename_base = f"{safe_name}-{report.period_label}-report"

    if format == "md":
        document = report_export.build_report_markdown(
            report,
            blocks,
            client_name=client_name,
            client_domain=client_domain,
            language=language,
        )
        return Response(
            content=document,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.md"'},
        )

    if format == "pdf":
        try:
            pdf_bytes = report_export.build_report_pdf(
                report,
                blocks,
                client_name=client_name,
                client_domain=client_domain,
                language=language,
            )
        except report_export.PdfRenderError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.pdf"'},
        )

    document = report_export.build_report_html(
        report,
        blocks,
        client_name=client_name,
        client_domain=client_domain,
        language=language,
    )
    return Response(
        content=document,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.html"'},
    )


@router.put("/report-builder/reports/{report_id}/share")
def set_report_share_link(
    report_id: uuid.UUID,
    payload: ReportShareRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    """Turn this report's public link on or off.

    On gives back a token to put in a `/r/<token>` URL, which serves the same
    rendered report the export produces, with no login. Off drops the token,
    which kills any link already sent.
    """
    try:
        token = report_service.set_report_share(
            session, report_id=report_id, shared=payload.shared
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    logger.info(
        "report_share_%s report_id=%s requester_user_id=%s",
        "enabled" if token else "revoked", report_id, current_user.user_id,
    )
    return {"share_token": token, "share_path": f"/r/{token}" if token else None}


# --- public: the shared report page -------------------------------------------
#
# Its own router because it must not sit under /api and must not require a
# login — the whole point is a URL a client can open. Everything it serves is
# the finished, non-editable report the export already produces.
public_router = APIRouter()


@public_router.get("/r/{share_token}")
def view_shared_report(
    share_token: str,
    session: Session = Depends(get_db_session),
) -> Response:
    try:
        report, blocks = report_service.get_shared_report(session, share_token)
    except LookupError:
        # Same 404 for an unknown token and a revoked one, and no detail: a
        # public endpoint should not confirm which reports exist.
        raise HTTPException(status_code=404, detail="This report link is not available.")

    client = session.get(Client, report.client_id)
    document = report_export.build_report_html(
        report,
        blocks,
        client_name=client.name if client else "Client",
        client_domain=client.domain if client else "",
        language=_client_language(client) if client else localization.DEFAULT_LANGUAGE,
    )
    # Rendered in the browser, not downloaded — no Content-Disposition. Told not
    # to cache, so revoking a link takes effect immediately, and kept out of
    # search results in case a client posts the URL somewhere public.
    return Response(
        content=document,
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )
