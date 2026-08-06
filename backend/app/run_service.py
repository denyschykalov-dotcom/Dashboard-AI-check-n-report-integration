from __future__ import annotations

import typing

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
# ``datetime.time`` above already owns the name, so the clock comes in directly.
from time import monotonic

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.orm import Load, Session, sessionmaker

from backend.app.config import Settings
from backend.app.domain import (
    IterationLike,
    SentimentInput,
    SentimentRef,
    aggregate_outputs,
    detect_mentions,
    drop_one_gpt_for_sentiment_retry,
    select_sentiment_refs,
    sentiment_refs_from_presence,
)
from backend.app.llm import LLMClient, TextGenerationResult
from backend.app.models import Draft, Output, Profile, Run, RunResult
from backend.app.prompt_builders import build_generation_request_prompt
from backend.app.utils import compact_error_message, utcnow


logger = logging.getLogger("rankberry.run_service")


# --- Active-run poll cache ----------------------------------------------------
# The progress banner polls /api/runs/status every few seconds, and every open
# tab polls independently — the same handful of rows, re-read from Supabase per
# tab per tick. A short TTL collapses those concurrent reads into one without
# making a single tab's ticks any staler than they already are: the window is
# shorter than the poll interval, so one tab never serves itself a cached tick.
STATUS_POLL_CACHE_TTL_SECONDS = 3.0
# Bound on distinct (requester, run-id-set) keys held. Keys are transient — a
# set changes as runs finish — so stale ones are pruned rather than left to grow.
_STATUS_POLL_CACHE_MAX_ENTRIES = 512
_STATUS_POLL_CACHE: dict[tuple, tuple[float, list[dict[str, object]]]] = {}


def _prune_status_poll_cache(now: float) -> None:
    for key, (expires_at, _) in list(_STATUS_POLL_CACHE.items()):
        if expires_at <= now:
            _STATUS_POLL_CACHE.pop(key, None)


def clear_status_poll_cache() -> None:
    """Drop every cached poll response (tests, and any state reset)."""
    _STATUS_POLL_CACHE.clear()


@dataclass(frozen=True)
class RunSnapshot:
    id: uuid.UUID
    user_id: uuid.UUID
    keyword: str
    domain: str
    brand: str
    prompt: str
    project: typing.Optional[str]


class StopRequestedError(RuntimeError):
    pass


class RunService:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session], llm_client: LLMClient) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.llm_client = llm_client

    def upsert_profile(self, session: Session, *, user_id: uuid.UUID, username: str) -> Profile:
        cleaned_username = self._sanitize_username(username)
        profile = session.execute(select(Profile).where(Profile.user_id == user_id)).scalar_one_or_none()
        if profile is None:
            profile = Profile(user_id=user_id, username=cleaned_username)
            session.add(profile)
        else:
            profile.username = cleaned_username
        session.commit()
        session.refresh(profile)
        return profile

    def get_current_draft(self, session: Session, *, user_id: uuid.UUID) -> Draft:
        draft = session.execute(select(Draft).where(Draft.user_id == user_id)).scalar_one_or_none()
        if draft is not None:
            return draft

        empty_rows = self._serialize_draft_rows(
            [{"keyword": "", "domain": "", "brand": "", "prompt": "", "project": ""}]
        )
        draft = Draft(
            user_id=user_id,
            keyword="",
            domain="",
            brand="",
            prompt="",
            project="",
            rows_json=empty_rows,
        )
        session.add(draft)
        session.commit()
        session.refresh(draft)
        return draft

    def upsert_current_draft(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        keyword: str,
        domain: str,
        brand: str,
        prompt: str,
        project: typing.Optional[str],
        rows: typing.Optional[list[dict[str, str]]] = None,
    ) -> Draft:
        draft = session.execute(select(Draft).where(Draft.user_id == user_id)).scalar_one_or_none()
        if draft is None:
            draft = Draft(user_id=user_id)
            session.add(draft)

        normalized_rows = self._normalize_draft_rows(
            rows
            if rows is not None
            else [
                {
                    "keyword": keyword,
                    "domain": domain,
                    "brand": brand,
                    "prompt": prompt,
                    "project": project or "",
                }
            ]
        )
        first_row = normalized_rows[0]

        draft.keyword = first_row["keyword"]
        draft.domain = first_row["domain"]
        draft.brand = first_row["brand"]
        draft.prompt = first_row["prompt"]
        draft.project = first_row["project"]
        draft.rows_json = self._serialize_draft_rows(normalized_rows)
        draft.updated_at = utcnow()
        session.commit()
        session.refresh(draft)
        return draft

    def append_current_draft_rows(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        rows: list[dict[str, str]],
    ) -> Draft:
        draft = self.get_current_draft(session, user_id=user_id)
        existing_rows = self.parse_draft_rows(draft)
        appended_rows = [row for row in self._normalize_draft_rows(rows) if self._draft_row_has_value(row)]
        if not appended_rows:
            logger.info("draft_append_skipped user_id=%s reason=no_filled_rows", user_id)
            return draft
        base_rows = existing_rows if any(self._draft_row_has_value(row) for row in existing_rows) else []
        combined_rows = base_rows + appended_rows
        first_row = combined_rows[0]
        logger.info(
            "draft_rows_appended user_id=%s existing_rows=%s appended_rows=%s total_rows=%s",
            user_id,
            len(base_rows),
            len(appended_rows),
            len(combined_rows),
        )

        return self.upsert_current_draft(
            session,
            user_id=user_id,
            keyword=first_row["keyword"],
            domain=first_row["domain"],
            brand=first_row["brand"],
            prompt=first_row["prompt"],
            project=first_row["project"],
            rows=combined_rows,
        )

    def create_run(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        keyword: str,
        domain: str,
        brand: str,
        prompt: str,
        project: typing.Optional[str],
    ) -> Run:
        keyword = keyword.strip()
        domain = domain.strip()
        brand = brand.strip()
        prompt = prompt.strip()
        project = (project or "").strip() or None

        missing_fields = [
            field_name
            for field_name, value in {
                "keyword": keyword,
                "domain": domain,
                "brand": brand,
                "prompt": prompt,
            }.items()
            if not value
        ]
        if missing_fields:
            raise ValueError(f"Missing required fields: {', '.join(missing_fields)}")

        run = Run(
            user_id=user_id,
            keyword=keyword,
            domain=domain,
            brand=brand,
            prompt=prompt,
            project=project,
            status="queued",
            total_iterations=self.settings.total_iterations,
            completed_iterations=0,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        logger.info(
            "run_queued run_id=%s user_id=%s project=%s keyword=%s total_iterations=%s",
            run.id,
            run.user_id,
            run.project or "-",
            run.keyword,
            run.total_iterations,
        )
        return run

    def claim_next_run(self) -> typing.Optional[RunSnapshot]:
        with self.session_factory() as session:
            running_users_subquery = select(Run.user_id).where(Run.status == "running")

            statement = (
                select(Run)
                .where(Run.status == "queued")
                .order_by(Run.created_at.asc(), Run.id.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if self.settings.enforce_one_active_run_per_user:
                statement = statement.where(Run.user_id.not_in(running_users_subquery))

            run = session.execute(statement).scalar_one_or_none()
            if run is None:
                session.rollback()
                return None

            run.status = "running"
            run.started_at = utcnow()
            run.finished_at = None
            run.error_messages = None
            session.commit()
            logger.info("run_marked_running run_id=%s user_id=%s project=%s", run.id, run.user_id, run.project or "-")
            return RunSnapshot(
                id=run.id,
                user_id=run.user_id,
                keyword=run.keyword,
                domain=run.domain,
                brand=run.brand,
                prompt=run.prompt,
                project=run.project,
            )

    def process_claimed_run(self, run: RunSnapshot) -> str:
        logger.info("run_processing_started run_id=%s user_id=%s project=%s", run.id, run.user_id, run.project or "-")
        try:
            self._raise_if_run_stopped(run.id)
            for iteration_number in range(1, self.settings.total_iterations + 1):
                self._raise_if_run_stopped(run.id)
                self._process_iteration(run, iteration_number)
            self._raise_if_run_stopped(run.id)
            self._finalize_run(run)
            self._raise_if_run_stopped(run.id)
            self._mark_run_completed(run.id)
            logger.info("run_processing_completed run_id=%s user_id=%s project=%s", run.id, run.user_id, run.project or "-")
            return "completed"
        except StopRequestedError:
            self._mark_run_stopped(run.id)
            logger.warning("run_processing_stopped run_id=%s user_id=%s project=%s", run.id, run.user_id, run.project or "-")
            return "stopped"
        except Exception as error:
            self._mark_run_failed(run.id, error)
            logger.exception(
                "run_processing_failed run_id=%s user_id=%s project=%s error=%s",
                run.id,
                run.user_id,
                run.project or "-",
                compact_error_message(error),
            )
            raise

    def get_run_detail(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
        is_admin: bool = False,
    ) -> tuple[Run, list[Output], typing.Optional[RunResult]]:
        statement = select(Run).where(Run.id == run_id)
        if not is_admin:
            statement = statement.where(Run.user_id == user_id)
        run = session.execute(statement).scalar_one_or_none()
        if run is None:
            raise LookupError("Run not found.")

        # Carries per-iteration mentions, metrics and costs. The raw responses are
        # deferred on the model, so this select leaves them in the database.
        outputs = list(
            session.execute(
                select(Output).where(Output.run_id == run_id)
                .order_by(Output.iteration_number.asc(), Output.created_at.asc())
            ).scalars()
        )
        result = session.execute(
            select(RunResult).where(RunResult.run_id == run_id)
        ).scalar_one_or_none()
        return run, outputs, result

    def list_active_run_ids(self, session: Session, *, user_id: uuid.UUID) -> list[str]:
        active_run_ids = list(
            session.execute(
                select(Run.id)
                .where(Run.user_id == user_id)
                .where(Run.status.in_(["queued", "running", "stopped"]))
                .order_by(Run.created_at.desc(), Run.id.desc())
            ).scalars()
        )
        return [str(run_id) for run_id in active_run_ids]

    def list_run_statuses(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        run_ids: list[uuid.UUID],
        is_admin: bool = False,
    ) -> list[dict[str, object]]:
        """Progress-only view of the given runs, for the active-run poller.

        Deliberately selects individual Run columns and never touches Output:
        the poller ticks every few seconds per open tab, and get_run_detail()
        would drag the multi-KB raw LLM responses out of the database on every
        tick for a banner that only shows status and iteration progress.
        Unknown or foreign run ids are simply absent from the result.

        Results are held for ``STATUS_POLL_CACHE_TTL_SECONDS`` so that several
        tabs polling the same runs cost one database read instead of one each.
        The key includes the requester, so no tab can be served another user's
        rows out of the cache.
        """
        if not run_ids:
            return []

        now = monotonic()
        cache_key = (user_id, is_admin, tuple(sorted(str(run_id) for run_id in run_ids)))
        cached = _STATUS_POLL_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        statement = (
            select(
                Run.id,
                Run.keyword,
                Run.status,
                Run.total_iterations,
                Run.completed_iterations,
                Run.error_messages,
            )
            .where(Run.id.in_(run_ids))
            .order_by(Run.created_at.desc(), Run.id.desc())
        )
        if not is_admin:
            statement = statement.where(Run.user_id == user_id)

        statuses = [
            {
                "id": str(row.id),
                "keyword": row.keyword,
                "status": row.status,
                "total_iterations": row.total_iterations,
                "completed_iterations": row.completed_iterations,
                "error_messages": row.error_messages,
            }
            for row in session.execute(statement)
        ]

        if len(_STATUS_POLL_CACHE) >= _STATUS_POLL_CACHE_MAX_ENTRIES:
            _prune_status_poll_cache(now)
        _STATUS_POLL_CACHE[cache_key] = (now + STATUS_POLL_CACHE_TTL_SECONDS, statuses)
        return statuses

    def list_failed_runs(self, session: Session, *, user_id: uuid.UUID) -> list[Run]:
        return list(
            session.execute(
                select(Run)
                .where(Run.user_id == user_id)
                .where(Run.status == "failed")
                .order_by(Run.created_at.desc(), Run.id.desc())
            ).scalars()
        )

    def list_user_project_options(self, session: Session, *, user_id: typing.Optional[uuid.UUID]) -> list[str]:
        project_options: set[str] = set()

        statement = select(Run.project).where(Run.project.is_not(None)).where(Run.project != "")
        if user_id is not None:
            statement = statement.where(Run.user_id == user_id)

        for value in session.execute(statement).scalars():
            cleaned = (value or "").strip()
            if cleaned:
                project_options.add(cleaned)

        return sorted(project_options, key=lambda value: value.lower())

    def stop_user_runs(self, session: Session, *, user_id: uuid.UUID) -> list[str]:
        runs = list(
            session.execute(
                select(Run)
                .where(Run.user_id == user_id)
                .where(Run.status.in_(["queued", "running", "stopped"]))
                .order_by(Run.created_at.desc(), Run.id.desc())
            ).scalars()
        )
        if not runs:
            return []

        stopped_at = utcnow()
        stopped_run_ids: list[str] = []
        for run in runs:
            run.status = "stopped"
            run.finished_at = stopped_at
            run.error_messages = "Stopped by user."
            stopped_run_ids.append(str(run.id))

        session.commit()
        logger.warning("user_runs_stopped user_id=%s run_count=%s run_ids=%s", user_id, len(stopped_run_ids), ",".join(stopped_run_ids))
        return stopped_run_ids

    def resume_user_runs(self, session: Session, *, user_id: uuid.UUID) -> list[str]:
        runs = list(
            session.execute(
                select(Run)
                .where(Run.user_id == user_id)
                .where(Run.status.in_(["queued", "running", "stopped"]))
                .order_by(Run.created_at.asc(), Run.id.asc())
            ).scalars()
        )
        if not runs:
            return []

        run_ids = [run.id for run in runs]
        session.execute(delete(Output).where(Output.user_id == user_id, Output.run_id.in_(run_ids)))
        session.execute(delete(RunResult).where(RunResult.user_id == user_id, RunResult.run_id.in_(run_ids)))

        for run in runs:
            run.status = "queued"
            run.completed_iterations = 0
            run.error_messages = None
            run.started_at = None
            run.finished_at = None

        session.commit()
        serialized_run_ids = [str(run_id) for run_id in run_ids]
        logger.info("user_runs_resumed user_id=%s run_count=%s run_ids=%s", user_id, len(serialized_run_ids), ",".join(serialized_run_ids))
        return serialized_run_ids

    def retry_failed_user_runs(self, session: Session, *, user_id: uuid.UUID) -> list[str]:
        runs = list(
            session.execute(
                select(Run)
                .where(Run.user_id == user_id)
                .where(Run.status == "failed")
                .order_by(Run.created_at.asc(), Run.id.asc())
            ).scalars()
        )
        if not runs:
            return []

        run_ids = [run.id for run in runs]
        session.execute(delete(Output).where(Output.user_id == user_id, Output.run_id.in_(run_ids)))
        session.execute(delete(RunResult).where(RunResult.user_id == user_id, RunResult.run_id.in_(run_ids)))

        for run in runs:
            run.status = "queued"
            run.completed_iterations = 0
            run.error_messages = None
            run.started_at = None
            run.finished_at = None

        session.commit()
        serialized_run_ids = [str(run_id) for run_id in run_ids]
        logger.info(
            "user_failed_runs_requeued user_id=%s run_count=%s run_ids=%s",
            user_id,
            len(serialized_run_ids),
            ",".join(serialized_run_ids),
        )
        return serialized_run_ids

    def list_history(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        is_admin: bool,
        project: typing.Optional[str],
        prompt: typing.Optional[str],
        user_query: typing.Optional[str],
        date_from: typing.Optional[date],
        date_to: typing.Optional[date],
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, object]], int]:
        statement = (
            select(RunResult, Run, Profile.username)
            .join(Run, Run.id == RunResult.run_id)
            .outerjoin(Profile, Profile.user_id == Run.user_id)
        )
        if not is_admin:
            statement = statement.where(Run.user_id == user_id)
        statement = self._apply_history_filters(
            statement,
            project=project,
            prompt=prompt,
            user_query=user_query if is_admin else None,
            date_from=date_from,
            date_to=date_to,
        )
        total = session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()

        rows = session.execute(
            statement.order_by(Run.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return [
            self._serialize_history_row(run_result, run, username=username)
            for run_result, run, username in rows
        ], total

    def get_overview_summary(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        project: typing.Optional[str],
        selected_user_id: typing.Optional[uuid.UUID] = None,
        is_admin: bool = False,
    ) -> dict[str, object]:
        # Only the columns the counters below actually touch. Left as full ORM
        # rows this hauled RunResult.sentiment_analysis and Run.prompt — free
        # text nobody reads here — across the wire for every run ever recorded,
        # to produce a few KB of counts. Primary keys load regardless, so
        # run.id stays available for the cost lookup.
        all_rows = list(
            session.execute(
                select(RunResult, Run)
                .join(Run, Run.id == RunResult.run_id)
                .options(
                    Load(RunResult).load_only(
                        RunResult.gpt_brand_mention,
                        RunResult.gem_brand_mention,
                        RunResult.grok_brand_mention,
                        RunResult.gpt_domain_mention,
                        RunResult.gem_domain_mention,
                        RunResult.grok_domain_mention,
                    ),
                    Load(Run).load_only(
                        Run.user_id,
                        Run.project,
                        Run.created_at,
                        Run.total_cost_usd,
                    ),
                )
            ).all()
        )
        selected_project = (project or "").strip() or None
        selected_user_id = selected_user_id if is_admin else None
        project_options = self._collect_project_options(session, user_id=None if is_admin else user_id)
        user_options = self._collect_user_options(session) if is_admin else []
        # Read the stored per-run total rather than re-summing the raw outputs.
        # The runs are already loaded above, so this costs no extra query at all —
        # where the old path pulled every Output row (~5.8 KB each, carrying the
        # full LLM responses) on every page view.
        run_costs = {run.id: float(run.total_cost_usd or 0.0) for _, run in all_rows}

        scoped_global_rows = [
            (run_result, run)
            for run_result, run in all_rows
            if self._project_matches(run.project, selected_project)
        ]
        scoped_user_rows = [
            (run_result, run)
            for run_result, run in scoped_global_rows
            if run.user_id == user_id
        ]
        scoped_admin_rows = [
            (run_result, run)
            for run_result, run in scoped_global_rows
            if selected_user_id is None or run.user_id == selected_user_id
        ]
        scoped_summary_rows = scoped_admin_rows if is_admin else scoped_user_rows

        now = utcnow()
        user_half_year_rows = self._filter_rows_since(scoped_summary_rows, now - timedelta(days=183))
        global_last_month_scope = scoped_summary_rows if is_admin else scoped_global_rows
        global_last_month_rows = self._filter_rows_since(global_last_month_scope, now - timedelta(days=30))
        monthly = self._build_monthly_overview(scoped_summary_rows, run_costs=run_costs, months=12)

        user_active_runs_statement = (
            select(func.count())
            .select_from(Run)
            .where(Run.status.in_(["queued", "running"]))
        )
        if is_admin:
            if selected_user_id is not None:
                user_active_runs_statement = user_active_runs_statement.where(Run.user_id == selected_user_id)
        else:
            user_active_runs_statement = user_active_runs_statement.where(Run.user_id == user_id)
        if selected_project is not None:
            user_active_runs_statement = user_active_runs_statement.where(Run.project == selected_project)
        user_active_runs = session.execute(user_active_runs_statement).scalar_one()

        return {
            "is_admin": is_admin,
            "stats": {
                "user_half_year": self._build_window_stats(user_half_year_rows, run_costs=run_costs),
                "user_active_runs": int(user_active_runs),
                "global_last_month": self._build_window_stats(global_last_month_rows, run_costs=run_costs),
                "global_projects": len(project_options),
            },
            "project_options": project_options,
            "user_options": user_options,
            "selected_project": selected_project,
            "selected_user_id": str(selected_user_id) if selected_user_id else None,
            "monthly": monthly,
        }

    def list_outputs(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        project: typing.Optional[str],
        prompt: typing.Optional[str],
        local_date: typing.Optional[date],
        tz_offset_minutes: typing.Optional[int],
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, object]], int]:
        statement = select(RunResult, Run).join(Run, Run.id == RunResult.run_id).where(Run.user_id == user_id)
        if project:
            statement = statement.where(Run.project == project)
        if prompt:
            statement = statement.where(Run.prompt.ilike(f"%{prompt.strip()}%"))
        if local_date is not None and tz_offset_minutes is not None:
            start_dt, end_dt = self._resolve_local_date_bounds(local_date, tz_offset_minutes)
            statement = statement.where(Run.created_at >= start_dt).where(Run.created_at < end_dt)

        total = session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
        rows = session.execute(
            statement.order_by(Run.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return [self._serialize_history_row(run_result, run) for run_result, run in rows], total

    def _process_iteration(self, run: RunSnapshot, iteration_number: int) -> None:
        prompt = self._build_generation_prompt(run, iteration_number)
        logger.info("run_iteration_started run_id=%s iteration=%s", run.id, iteration_number)

        gpt_output: typing.Optional[str] = None
        gem_output: typing.Optional[str] = None
        grok_output: typing.Optional[str] = None
        gpt_result: typing.Optional[TextGenerationResult] = None
        gem_result: typing.Optional[TextGenerationResult] = None
        grok_result: typing.Optional[TextGenerationResult] = None
        generation_errors: list[str] = []

        try:
            gpt_result = self.llm_client.call_with_retry(
                "OpenAI generation",
                lambda: self.llm_client.generate_openai_output(prompt),
            )
            gpt_output = gpt_result.text
            self._raise_if_run_stopped(run.id)
        except Exception as error:
            error_message = compact_error_message(error)
            logger.warning("openai_generation_failed run_id=%s iteration=%s error=%s", run.id, iteration_number, error_message)
            generation_errors.append(f"OpenAI: {error_message}")

        try:
            gem_result = self.llm_client.call_with_retry(
                "Gemini generation",
                lambda: self.llm_client.generate_gemini_output(prompt),
            )
            gem_output = gem_result.text
            self._raise_if_run_stopped(run.id)
        except Exception as error:
            error_message = compact_error_message(error)
            logger.warning("gemini_generation_failed run_id=%s iteration=%s error=%s", run.id, iteration_number, error_message)
            generation_errors.append(f"Gemini: {error_message}")

        try:
            grok_result = self.llm_client.call_with_retry(
                "Grok generation",
                lambda: self.llm_client.generate_grok_output(prompt),
            )
            grok_output = grok_result.text
            self._raise_if_run_stopped(run.id)
        except Exception as error:
            error_message = compact_error_message(error)
            logger.warning("grok_generation_failed run_id=%s iteration=%s error=%s", run.id, iteration_number, error_message)
            generation_errors.append(f"Grok: {error_message}")

        gpt_domain_mention, gpt_brand_mention = detect_mentions(gpt_output, run.domain, run.brand)
        gem_domain_mention, gem_brand_mention = detect_mentions(gem_output, run.domain, run.brand)
        grok_domain_mention, grok_brand_mention = detect_mentions(grok_output, run.domain, run.brand)

        with self.session_factory() as session:
            output_row = session.execute(
                select(Output).where(and_(Output.run_id == run.id, Output.iteration_number == iteration_number))
            ).scalar_one_or_none()
            if output_row is None:
                output_row = Output(run_id=run.id, user_id=run.user_id, iteration_number=iteration_number)
                session.add(output_row)

            output_row.project = run.project
            output_row.gpt_output = gpt_output
            output_row.gem_output = gem_output
            output_row.grok_output = grok_output
            output_row.openai_generation_cost_usd = (
                gpt_result.usage.estimated_cost_usd if gpt_result and gpt_result.usage else None
            )
            output_row.gemini_generation_cost_usd = (
                gem_result.usage.estimated_cost_usd if gem_result and gem_result.usage else None
            )
            output_row.grok_generation_cost_usd = (
                grok_result.usage.estimated_cost_usd if grok_result and grok_result.usage else None
            )
            output_row.gpt_domain_mention = gpt_domain_mention
            output_row.gpt_brand_mention = gpt_brand_mention
            output_row.gem_domain_mention = gem_domain_mention
            output_row.gem_brand_mention = gem_brand_mention
            output_row.grok_domain_mention = grok_domain_mention
            output_row.grok_brand_mention = grok_brand_mention
            session.commit()

        self._raise_if_run_stopped(run.id)
        if generation_errors:
            logger.error(
                "run_iteration_generation_failed run_id=%s iteration=%s errors=%s",
                run.id,
                iteration_number,
                " | ".join(generation_errors),
            )
            raise RuntimeError(f"Iteration {iteration_number} generation failed. {' | '.join(generation_errors)}")

        try:
            analysis = self.llm_client.call_with_retry(
                "Gemini iteration analysis",
                lambda: self.llm_client.analyze_iteration(
                    keyword=run.keyword,
                    domain=run.domain,
                    brand=run.brand,
                    project=run.project,
                    iteration_number=iteration_number,
                    gpt_output=gpt_output or "",
                    gem_output=gem_output or "",
                    grok_output=grok_output or "",
                ),
            )
        except Exception as error:
            logger.exception(
                "gemini_iteration_analysis_failed run_id=%s iteration=%s error=%s",
                run.id,
                iteration_number,
                compact_error_message(error),
            )
            raise

        with self.session_factory() as session:
            output_row = session.execute(
                select(Output).where(and_(Output.run_id == run.id, Output.iteration_number == iteration_number))
            ).scalar_one()
            output_row.response_count = analysis.response_count
            output_row.brand_list = analysis.brand_list
            output_row.citation_format = analysis.citation_format
            output_row.gemini_analysis_cost_usd = analysis.usage.estimated_cost_usd if analysis.usage else None

            parent_run = session.execute(select(Run).where(Run.id == run.id)).scalar_one()
            parent_run.completed_iterations = max(parent_run.completed_iterations or 0, iteration_number)
            session.commit()
        logger.info("run_iteration_completed run_id=%s iteration=%s", run.id, iteration_number)
        self._raise_if_run_stopped(run.id)

    _SENTIMENT_TEXT_COLUMNS = {
        "gpt": Output.gpt_output,
        "gemini": Output.gem_output,
        "grok": Output.grok_output,
    }

    def _load_sentiment_texts(
        self,
        session: Session,
        *,
        run_id: uuid.UUID,
        refs: list[SentimentRef],
    ) -> list[SentimentInput]:
        """Fetch the raw text for the selected responses only — nothing else.

        This is the single place in the application that reads a stored model
        response back out of the database, and it pulls at most one column per
        selected ref (four by default) rather than every response in the run.
        A ref whose text has since been cleaned up is skipped.
        """
        inputs: list[SentimentInput] = []
        for ref in refs:
            column = self._SENTIMENT_TEXT_COLUMNS.get(ref.provider)
            if column is None:
                continue
            text = session.execute(
                select(column).where(
                    and_(Output.run_id == run_id, Output.iteration_number == ref.iteration_number)
                )
            ).scalar_one_or_none()
            if not text:
                logger.warning(
                    "sentiment_text_missing run_id=%s iteration=%s provider=%s",
                    run_id,
                    ref.iteration_number,
                    ref.provider,
                )
                continue
            inputs.append(
                SentimentInput(
                    provider=ref.provider,
                    iteration_number=ref.iteration_number,
                    text=text,
                    mentioned=ref.mentioned,
                )
            )
        return inputs

    def _finalize_run(self, run: RunSnapshot) -> None:
        logger.info("run_finalize_started run_id=%s", run.id)
        # Aggregation needs the mention flags and metrics, never the responses
        # themselves, so this reads named columns plus a server-side presence
        # check per provider. `length(...) > 0` matches the old truthiness test
        # on the text without the text ever leaving the database.
        with self.session_factory() as session:
            rows = list(
                session.execute(
                    select(
                        Output.iteration_number,
                        Output.gpt_domain_mention,
                        Output.gem_domain_mention,
                        Output.grok_domain_mention,
                        Output.gpt_brand_mention,
                        Output.gem_brand_mention,
                        Output.grok_brand_mention,
                        Output.response_count,
                        Output.brand_list,
                        Output.citation_format,
                        (func.coalesce(func.length(Output.gpt_output), 0) > 0).label("has_gpt"),
                        (func.coalesce(func.length(Output.gem_output), 0) > 0).label("has_gem"),
                        (func.coalesce(func.length(Output.grok_output), 0) > 0).label("has_grok"),
                    )
                    .where(Output.run_id == run.id)
                    .order_by(Output.iteration_number.asc(), Output.created_at.asc())
                )
            )

        if len(rows) < self.settings.total_iterations:
            logger.error(
                "run_finalize_missing_outputs run_id=%s output_count=%s expected=%s",
                run.id,
                len(rows),
                self.settings.total_iterations,
            )
            raise RuntimeError("Not all iteration rows are available for aggregation.")

        # aggregate_outputs() reads only the metric fields; the text stays None
        # here because it is deliberately not loaded.
        output_views = [
            IterationLike(
                iteration_number=row.iteration_number,
                gpt_output=None,
                gem_output=None,
                grok_output=None,
                gpt_domain_mention=row.gpt_domain_mention,
                gem_domain_mention=row.gem_domain_mention,
                grok_domain_mention=row.grok_domain_mention,
                gpt_brand_mention=row.gpt_brand_mention,
                gem_brand_mention=row.gem_brand_mention,
                grok_brand_mention=row.grok_brand_mention,
                response_count=row.response_count,
                brand_list=row.brand_list,
                citation_format=row.citation_format,
            )
            for row in rows
        ]
        aggregate_payload = aggregate_outputs(output_views)

        candidate_refs = sentiment_refs_from_presence(
            [
                (
                    row.iteration_number,
                    {"gpt": bool(row.has_gpt), "gemini": bool(row.has_gem), "grok": bool(row.has_grok)},
                    {
                        "gpt": bool(row.gpt_domain_mention or row.gpt_brand_mention),
                        "gemini": bool(row.gem_domain_mention or row.gem_brand_mention),
                        "grok": bool(row.grok_domain_mention or row.grok_brand_mention),
                    },
                )
                for row in rows
            ]
        )
        selected_refs = select_sentiment_refs(candidate_refs, limit=4)
        # Only now, and only for the handful the prompt will actually carry.
        with self.session_factory() as session:
            sentiment_inputs = self._load_sentiment_texts(session, run_id=run.id, refs=selected_refs)
        logger.info(
            "run_finalize_sentiment_inputs run_id=%s candidates=%s loaded=%s",
            run.id,
            len(candidate_refs),
            len(sentiment_inputs),
        )

        try:
            sentiment_result = self.llm_client.call_with_retry(
                "Gemini final sentiment",
                lambda: self.llm_client.analyze_final_sentiment(
                    keyword=run.keyword,
                    domain=run.domain,
                    brand=run.brand,
                    project=run.project,
                    selected_inputs=sentiment_inputs,
                ),
            )
        except Exception as error:
            logger.warning(
                "final_sentiment_primary_failed run_id=%s selected_inputs=%s error=%s",
                run.id,
                len(sentiment_inputs),
                compact_error_message(error),
            )
            reduced_inputs = drop_one_gpt_for_sentiment_retry(sentiment_inputs)
            sentiment_result = self.llm_client.call_with_retry(
                "Gemini final sentiment fallback",
                lambda: self.llm_client.analyze_final_sentiment(
                    keyword=run.keyword,
                    domain=run.domain,
                    brand=run.brand,
                    project=run.project,
                    selected_inputs=reduced_inputs,
                ),
            )

        self._raise_if_run_stopped(run.id)
        with self.session_factory() as session:
            result = session.execute(select(RunResult).where(RunResult.run_id == run.id)).scalar_one_or_none()
            if result is None:
                result = RunResult(run_id=run.id, user_id=run.user_id)
                session.add(result)

            result.project = run.project
            result.gpt_domain_mention = bool(aggregate_payload["gpt_domain_mention"])
            result.gem_domain_mention = bool(aggregate_payload["gem_domain_mention"])
            result.grok_domain_mention = bool(aggregate_payload["grok_domain_mention"])
            result.gpt_brand_mention = bool(aggregate_payload["gpt_brand_mention"])
            result.gem_brand_mention = bool(aggregate_payload["gem_brand_mention"])
            result.grok_brand_mention = bool(aggregate_payload["grok_brand_mention"])
            result.response_count_avg = aggregate_payload["response_count_avg"]  # type: ignore[assignment]
            result.brand_list = aggregate_payload["brand_list"]  # type: ignore[assignment]
            result.citation_format = aggregate_payload["citation_format"]  # type: ignore[assignment]
            result.sentiment_analysis = sentiment_result.text
            result.gemini_sentiment_cost_usd = (
                sentiment_result.usage.estimated_cost_usd if sentiment_result.usage else None
            )
            session.commit()
        logger.info("run_finalize_completed run_id=%s sentiment_inputs=%s", run.id, len(sentiment_inputs))

    @staticmethod
    def _recalculate_run_cost(session: Session, run_id: uuid.UUID) -> float:
        """Sum this run's spend in the database and store it on the run row.

        Aggregated in SQL over one run, so it reads the four cost columns and
        never the TOASTed LLM-response text beside them. Called at every terminal
        transition, which is the point the cost stops changing.
        """
        outputs_total = session.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.coalesce(Output.openai_generation_cost_usd, 0.0)
                        + func.coalesce(Output.gemini_generation_cost_usd, 0.0)
                        + func.coalesce(Output.grok_generation_cost_usd, 0.0)
                        + func.coalesce(Output.gemini_analysis_cost_usd, 0.0)
                    ),
                    0.0,
                )
            ).where(Output.run_id == run_id)
        ).scalar_one()
        results_total = session.execute(
            select(
                func.coalesce(
                    func.sum(func.coalesce(RunResult.gemini_sentiment_cost_usd, 0.0)), 0.0
                )
            ).where(RunResult.run_id == run_id)
        ).scalar_one()

        total = round(float(outputs_total or 0.0) + float(results_total or 0.0), 8)
        session.execute(update(Run).where(Run.id == run_id).values(total_cost_usd=total))
        return total

    def _mark_run_completed(self, run_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            run = session.execute(select(Run).where(Run.id == run_id)).scalar_one()
            run.status = "completed"
            run.finished_at = utcnow()
            run.error_messages = None
            self._recalculate_run_cost(session, run_id)
            session.commit()

    def _mark_run_failed(self, run_id: uuid.UUID, error: Exception) -> None:
        with self.session_factory() as session:
            run = session.execute(select(Run).where(Run.id == run_id)).scalar_one_or_none()
            if run is None:
                return
            run.status = "failed"
            run.finished_at = utcnow()
            run.error_messages = compact_error_message(error)
            self._recalculate_run_cost(session, run_id)
            session.commit()

    def _mark_run_stopped(self, run_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            run = session.execute(select(Run).where(Run.id == run_id)).scalar_one_or_none()
            if run is None:
                return
            run.status = "stopped"
            run.finished_at = utcnow()
            if not run.error_messages:
                run.error_messages = "Stopped by user."
            self._recalculate_run_cost(session, run_id)
            session.commit()

    def _serialize_history_row(
        self,
        run_result: RunResult,
        run: Run,
        *,
        username: typing.Optional[str] = None,
    ) -> dict[str, object]:
        return {
            "run_id": str(run.id),
            "user_id": str(run.user_id),
            "username": self._format_username(username, run.user_id),
            "project": run.project,
            "keyword": run.keyword,
            "domain": run.domain,
            "brand": run.brand,
            "prompt": run.prompt,
            "status": run.status,
            "created_at": run.created_at,
            "completed_iterations": run.completed_iterations,
            "total_iterations": run.total_iterations,
            "gpt_domain_mention": run_result.gpt_domain_mention,
            "gem_domain_mention": run_result.gem_domain_mention,
            "grok_domain_mention": run_result.grok_domain_mention,
            "gpt_brand_mention": run_result.gpt_brand_mention,
            "gem_brand_mention": run_result.gem_brand_mention,
            "grok_brand_mention": run_result.grok_brand_mention,
            "response_count_avg": run_result.response_count_avg,
            "brand_list": run_result.brand_list,
            "citation_format": run_result.citation_format,
            "sentiment_analysis": run_result.sentiment_analysis,
        }

    def _apply_history_filters(
        self,
        statement,
        *,
        project: typing.Optional[str],
        prompt: typing.Optional[str],
        user_query: typing.Optional[str],
        date_from: typing.Optional[date],
        date_to: typing.Optional[date],
    ):
        if project:
            statement = statement.where(Run.project == project)
        if prompt:
            statement = statement.where(Run.prompt.ilike(f"%{prompt.strip()}%"))
        if user_query:
            statement = statement.where(Profile.username.ilike(f"%{user_query.strip()}%"))
        if date_from:
            start_dt = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
            statement = statement.where(Run.created_at >= start_dt)
        if date_to:
            end_dt = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
            statement = statement.where(Run.created_at < end_dt)
        return statement

    def _project_matches(self, project_value: typing.Optional[str], selected_project: typing.Optional[str]) -> bool:
        if selected_project is None:
            return True
        return (project_value or "").strip() == selected_project

    def _filter_rows_since(
        self,
        rows: list[tuple[RunResult, Run]],
        start_at: datetime,
    ) -> list[tuple[RunResult, Run]]:
        return [
            (run_result, run)
            for run_result, run in rows
            if (run.created_at or utcnow()).astimezone(timezone.utc) >= start_at.astimezone(timezone.utc)
        ]

    def _build_window_stats(
        self,
        rows: list[tuple[RunResult, Run]],
        *,
        run_costs: typing.Optional[dict[uuid.UUID, float]] = None,
    ) -> dict[str, typing.Union[int, float]]:
        return {
            "total_results": len(rows),
            "brand_matches": sum(
                1
                for run_result, _ in rows
                if run_result.gpt_brand_mention
                or run_result.gem_brand_mention
                or run_result.grok_brand_mention
            ),
            "domain_matches": sum(
                1
                for run_result, _ in rows
                if run_result.gpt_domain_mention
                or run_result.gem_domain_mention
                or run_result.grok_domain_mention
            ),
            "users": len({str(run.user_id) for _, run in rows}),
            "spend_usd": round(sum((run_costs or {}).get(run.id, 0.0) for _, run in rows), 8),
        }

    def _collect_project_options(self, session: Session, *, user_id: typing.Optional[uuid.UUID]) -> list[str]:
        project_options: set[str] = set()

        run_statement = select(Run.project).where(Run.project.is_not(None)).where(Run.project != "")
        if user_id is not None:
            run_statement = run_statement.where(Run.user_id == user_id)

        for value in session.execute(run_statement).scalars():
            cleaned = (value or "").strip()
            if cleaned:
                project_options.add(cleaned)

        draft_statement = select(Draft.project, Draft.rows_json)
        if user_id is not None:
            draft_statement = draft_statement.where(Draft.user_id == user_id)

        for draft_project, rows_json in session.execute(draft_statement).all():
            cleaned_draft_project = (draft_project or "").strip()
            if cleaned_draft_project:
                project_options.add(cleaned_draft_project)
            for row in self._deserialize_draft_rows(rows_json):
                cleaned_row_project = str(row.get("project", "") or "").strip()
                if cleaned_row_project:
                    project_options.add(cleaned_row_project)

        return sorted(project_options, key=lambda value: value.lower())

    def list_user_options(self, session: Session) -> list[dict[str, str]]:
        return self._collect_user_options(session)

    def forward_history_runs(
        self,
        session: Session,
        *,
        requester_user_id: uuid.UUID,
        is_admin: bool,
        run_ids: list[uuid.UUID],
        target_user_id: uuid.UUID,
    ) -> dict[str, object]:
        unique_run_ids = list(dict.fromkeys(run_ids))
        if not unique_run_ids:
            raise ValueError("Select at least one history row to forward.")

        if target_user_id == requester_user_id and not is_admin:
            raise ValueError("Choose another user to forward rows to.")

        target_exists = session.execute(
            select(func.count())
            .select_from(Profile)
            .where(Profile.user_id == target_user_id)
        ).scalar_one()
        if not target_exists:
            target_exists = session.execute(
                select(func.count())
                .select_from(Run)
                .where(Run.user_id == target_user_id)
        ).scalar_one()
        if not target_exists:
            raise LookupError("Target user was not found.")

        statement = select(Run).where(Run.id.in_(unique_run_ids))
        if not is_admin:
            statement = statement.where(Run.user_id == requester_user_id)
        runs = list(session.execute(statement).scalars())
        if len(runs) != len(unique_run_ids):
            raise LookupError("One or more selected history rows were not found.")

        found_run_ids = [run.id for run in runs]
        for run in runs:
            run.user_id = target_user_id

        outputs_updated = session.execute(
            update(Output)
            .where(Output.run_id.in_(found_run_ids))
            .values(user_id=target_user_id)
        ).rowcount or 0
        results_updated = session.execute(
            update(RunResult)
            .where(RunResult.run_id.in_(found_run_ids))
            .values(user_id=target_user_id)
        ).rowcount or 0
        session.commit()

        logger.info(
            "history_runs_forwarded requester_user_id=%s target_user_id=%s is_admin=%s run_count=%s outputs_updated=%s results_updated=%s",
            requester_user_id,
            target_user_id,
            is_admin,
            len(found_run_ids),
            outputs_updated,
            results_updated,
        )
        return {
            "run_ids": [str(run_id) for run_id in found_run_ids],
            "total_runs": len(found_run_ids),
            "outputs_updated": int(outputs_updated),
            "results_updated": int(results_updated),
            "target_user_id": str(target_user_id),
        }

    def _collect_user_options(self, session: Session) -> list[dict[str, str]]:
        usernames_by_user_id: dict[uuid.UUID, typing.Optional[str]] = {}
        for profile_user_id, username in session.execute(select(Profile.user_id, Profile.username)).all():
            usernames_by_user_id[profile_user_id] = username

        for run_user_id in session.execute(select(Run.user_id).group_by(Run.user_id)).scalars():
            usernames_by_user_id.setdefault(run_user_id, None)

        options = [
            {
                "user_id": str(option_user_id),
                "username": self._format_username(username, option_user_id),
            }
            for option_user_id, username in usernames_by_user_id.items()
        ]
        return sorted(options, key=lambda item: item["username"].lower())

    def _resolve_local_date_bounds(self, local_date: date, tz_offset_minutes: int) -> tuple[datetime, datetime]:
        local_timezone = timezone(timedelta(minutes=-tz_offset_minutes))
        start_local = datetime.combine(local_date, time.min, tzinfo=local_timezone)
        end_local = start_local + timedelta(days=1)
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)

    def _sanitize_username(self, username: str) -> str:
        cleaned = " ".join(username.strip().split())
        if not cleaned:
            raise ValueError("Username is required.")
        return cleaned[:80]

    def parse_draft_rows(self, draft: Draft) -> list[dict[str, str]]:
        rows = self._deserialize_draft_rows(draft.rows_json)
        if rows:
            return rows
        return self._normalize_draft_rows(
            [
                {
                    "keyword": draft.keyword or "",
                    "domain": draft.domain or "",
                    "brand": draft.brand or "",
                    "prompt": draft.prompt or "",
                    "project": draft.project or "",
                }
            ]
        )

    def _normalize_draft_rows(self, rows: typing.Optional[list[dict[str, str]]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for row in rows or []:
            normalized.append(
                {
                    "keyword": str(row.get("keyword", "") or "").strip(),
                    "domain": str(row.get("domain", "") or "").strip(),
                    "brand": str(row.get("brand", "") or "").strip(),
                    "prompt": str(row.get("prompt", "") or "").strip(),
                    "project": str(row.get("project", "") or "").strip(),
                }
            )
        return normalized or [{"keyword": "", "domain": "", "brand": "", "prompt": "", "project": ""}]

    def _draft_row_has_value(self, row: dict[str, str]) -> bool:
        return any(str(value or "").strip() for value in row.values())

    def _serialize_draft_rows(self, rows: list[dict[str, str]]) -> str:
        return json.dumps(rows)

    def _deserialize_draft_rows(self, rows_json: typing.Optional[str]) -> list[dict[str, str]]:
        if not rows_json:
            return []
        try:
            parsed = json.loads(rows_json)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        draft_rows: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            draft_rows.append(
                {
                    "keyword": str(item.get("keyword", "") or ""),
                    "domain": str(item.get("domain", "") or ""),
                    "brand": str(item.get("brand", "") or ""),
                    "prompt": str(item.get("prompt", "") or ""),
                    "project": str(item.get("project", "") or ""),
                }
            )
        return self._normalize_draft_rows(draft_rows)

    def _build_generation_prompt(self, run: RunSnapshot, iteration_number: int) -> str:
        return build_generation_request_prompt(
            user_prompt=run.prompt,
            keyword=run.keyword,
            domain=run.domain,
            brand=run.brand,
            project=run.project,
            iteration_number=iteration_number,
        )

    def _raise_if_run_stopped(self, run_id: uuid.UUID) -> None:
        with self.session_factory() as session:
            status = session.execute(select(Run.status).where(Run.id == run_id)).scalar_one_or_none()
        if status == "stopped":
            raise StopRequestedError(f"Run {run_id} was stopped by user.")

    def _build_monthly_overview(
        self,
        rows: list[tuple[RunResult, Run]],
        *,
        run_costs: typing.Optional[dict[uuid.UUID, float]] = None,
        months: int = 12,
    ) -> list[dict[str, object]]:
        today = utcnow().date().replace(day=1)
        month_values: list[date] = []
        current = today
        for _ in range(months):
            month_values.append(current)
            if current.month == 1:
                current = current.replace(year=current.year - 1, month=12)
            else:
                current = current.replace(month=current.month - 1)
        month_sequence = list(reversed(month_values))

        buckets: dict[str, dict[str, typing.Union[int, float]]] = defaultdict(
            lambda: {"brand_matches": 0, "domain_matches": 0, "total_runs": 0, "spend_usd": 0.0}
        )
        for run_result, run in rows:
            created = (run.created_at or utcnow()).astimezone(timezone.utc)
            key = created.strftime("%Y-%m")
            bucket = buckets[key]
            bucket["total_runs"] += 1
            bucket["spend_usd"] += (run_costs or {}).get(run.id, 0.0)
            if (
                run_result.gpt_brand_mention
                or run_result.gem_brand_mention
                or run_result.grok_brand_mention
            ):
                bucket["brand_matches"] += 1
            if (
                run_result.gpt_domain_mention
                or run_result.gem_domain_mention
                or run_result.grok_domain_mention
            ):
                bucket["domain_matches"] += 1

        return [
            {
                "month": month.strftime("%Y-%m"),
                "label": month.strftime("%b %Y"),
                "brand_matches": buckets[month.strftime("%Y-%m")]["brand_matches"],
                "domain_matches": buckets[month.strftime("%Y-%m")]["domain_matches"],
                "total_runs": buckets[month.strftime("%Y-%m")]["total_runs"],
                "spend_usd": round(float(buckets[month.strftime("%Y-%m")]["spend_usd"]), 8),
            }
            for month in month_sequence
        ]

    def _format_username(self, username: typing.Optional[str], user_id: uuid.UUID) -> str:
        cleaned = (username or "").strip()
        if cleaned:
            return cleaned
        return f"User {str(user_id)[:8]}"
