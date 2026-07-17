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
from backend.app.report_builder import export as report_export
from backend.app.report_builder import service as report_service
from backend.app.report_builder import settings_service as report_settings_service
from backend.app.report_builder.data_sources import clickup_client
from backend.app.report_builder.data_sources.clickup_client import ClickUpAccessError
from backend.app.schemas import (
    BulkRunActionResponse,
    ClickUpTokenRequest,
    ClientCreateRequest,
    DraftAppendPayload,
    DraftPayload,
    GenerateReportRequest,
    HistoryForwardRequest,
    HistoryForwardResponse,
    ProfileUpsertRequest,
    ReportSaveRequest,
    ReportUpdateRequest,
    RunStartRequest,
)
from backend.app.service_container import get_run_service
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
            "gpt_output": item.gpt_output,
            "gem_output": item.gem_output,
            "grok_output": item.grok_output,
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
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return report_service.serialize_client(client)


@router.post("/report-builder/generate")
def generate_report(
    payload: GenerateReportRequest,
    session: Session = Depends(get_db_session),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, object]:
    try:
        return report_service.generate(
            session,
            client_id=payload.client_id,
            block_keys=payload.block_keys,
            user_id=current_user.user_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


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
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return report_service.serialize_report_summary(report)


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


@router.get("/report-builder/reports/{report_id}/export")
def export_report(
    report_id: uuid.UUID,
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
    document = report_export.build_report_html(
        report,
        blocks,
        client_name=client_name,
        client_domain=client_domain,
    )
    safe_name = "".join(ch if ch.isalnum() else "-" for ch in client_name).strip("-") or "client"
    filename = f"{safe_name}-{report.period_label}-report.html"
    return Response(
        content=document,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
