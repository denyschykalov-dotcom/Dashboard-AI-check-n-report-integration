import os
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from backend.app.config import Settings, _runtime_database_url
from backend.app.database_url import normalize_postgresql_url
from backend.app.db import Base, build_engine
from backend.app.llm import IterationAnalysis, LLMUsage, TextGenerationResult
from backend.app.models import Draft, Output, Profile, Run, RunResult
from backend.app.prompt_builders import build_generation_request_prompt
from backend.app.run_service import RunService, RunSnapshot
from backend.app.utils import utcnow


class FakeLLMClient:
    def __init__(self) -> None:
        self.gpt_outputs = iter(
            [
                TextGenerationResult(
                    text="Rankberry is visible at example.com in the first response.",
                    usage=LLMUsage("openai", "gpt-4o-mini", 100, 40, 140, 0.000039),
                ),
                TextGenerationResult(
                    text="Second GPT response without mention.",
                    usage=LLMUsage("openai", "gpt-4o-mini", 110, 30, 140, 0.0000345),
                ),
                TextGenerationResult(
                    text="Third GPT response also without mention.",
                    usage=LLMUsage("openai", "gpt-4o-mini", 120, 20, 140, 0.00003),
                ),
            ]
        )
        self.gem_outputs = iter(
            [
                TextGenerationResult(
                    text="First Gemini response without mention.",
                    usage=LLMUsage("gemini", "gemini-2.0-flash", 90, 35, 125, 0.000023),
                ),
                TextGenerationResult(
                    text="Gemini references Rankberry in the second response.",
                    usage=LLMUsage("gemini", "gemini-2.0-flash", 95, 45, 140, 0.0000275),
                ),
                TextGenerationResult(
                    text="Third Gemini response without mention.",
                    usage=LLMUsage("gemini", "gemini-2.0-flash", 100, 25, 125, 0.00002),
                ),
            ]
        )
        self.grok_outputs = iter(
            [
                TextGenerationResult(
                    text="Grok notes example.com as a relevant domain.",
                    usage=LLMUsage("grok", "grok-4.3", 80, 30, 110, 0.000175),
                ),
                TextGenerationResult(
                    text="Second Grok response without mention.",
                    usage=LLMUsage("grok", "grok-4.3", 85, 25, 110, 0.00016875),
                ),
                TextGenerationResult(
                    text="Third Grok response mentions RB.",
                    usage=LLMUsage("grok", "grok-4.3", 90, 35, 125, 0.0002),
                ),
            ]
        )
        self.analyses = iter(
            [
                IterationAnalysis(
                    2.0,
                    "Rankberry",
                    "text",
                    LLMUsage("gemini", "gemini-2.0-flash", 150, 25, 175, 0.000025),
                ),
                IterationAnalysis(
                    4.0,
                    "RB, Rankberry",
                    "N/A",
                    LLMUsage("gemini", "gemini-2.0-flash", 155, 30, 185, 0.0000275),
                ),
                IterationAnalysis(
                    None,
                    None,
                    "url",
                    LLMUsage("gemini", "gemini-2.0-flash", 160, 20, 180, 0.000024),
                ),
            ]
        )

    def call_with_retry(self, _operation_name: str, callback):
        return callback()

    def generate_openai_output(self, _prompt: str) -> TextGenerationResult:
        return next(self.gpt_outputs)

    def generate_gemini_output(self, _prompt: str) -> TextGenerationResult:
        return next(self.gem_outputs)

    def generate_grok_output(self, _prompt: str) -> TextGenerationResult:
        return next(self.grok_outputs)

    def analyze_iteration(self, **_kwargs) -> IterationAnalysis:
        return next(self.analyses)

    def analyze_final_sentiment(self, **_kwargs) -> TextGenerationResult:
        return TextGenerationResult(
            text="Mostly positive with direct brand visibility in selected outputs.",
            usage=LLMUsage("gemini", "gemini-2.0-flash", 180, 60, 240, 0.000042),
        )


def build_settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        migration_database_url=database_url,
        db_pool_mode="null",
        db_pool_size=1,
        db_max_overflow=0,
        admin_email="analytics@rankberry.marketing",
        supabase_url=None,
        supabase_anon_key=None,
        google_sheets_credentials_file=None,
        google_sheets_client_folder_id=None,
        ahrefs_api_token=None,
        report_builder_secret_key=None,
        openai_api_key="test",
        gemini_api_key="test",
        grok_api_key="test",
        openai_model="test-openai",
        gemini_model="test-gemini",
        gemini_analysis_model="test-gemini-analysis",
        gemini_sentiment_model="test-gemini-sentiment",
        grok_model="test-grok",
        grok_base_url="https://api.x.ai/v1",
        max_llm_retries=1,
        request_timeout_seconds=5.0,
        raw_output_retention_days=30,
        queue_poll_seconds=0.1,
        worker_concurrency=1,
        enforce_one_active_run_per_user=True,
        total_iterations=3,
        iteration_analysis_prompt_file=Path("iteration-analysis.txt"),
        final_sentiment_prompt_file=Path("final-sentiment.txt"),
    )


class IntegrationFlowTests(unittest.TestCase):
    def test_supabase_runtime_url_switches_pooler_port_to_6543(self) -> None:
        runtime_url = _runtime_database_url(
            "".join(
                [
                    "postgresql",
                    "://postgres.project:secret@aws-1-eu-west-1.pooler.supabase.com:5432/postgres",
                ]
            )
        )
        self.assertEqual(
            runtime_url,
            "postgresql+psycopg://postgres.project:secret@aws-1-eu-west-1.pooler.supabase.com:6543/postgres",
        )

    def test_postgres_engine_uses_null_pool_by_default(self) -> None:
        legacy_url = "".join(["postgresql", "://user:pass@localhost/testdb"])
        engine = build_engine(legacy_url)
        self.assertIsInstance(engine.pool, NullPool)
        self.assertEqual(
            engine.url.render_as_string(hide_password=False),
            "postgresql+psycopg://user:pass@localhost/testdb",
        )
        engine.dispose()

    def test_postgres_engine_can_opt_in_to_queue_pool(self) -> None:
        engine = build_engine(
            "postgresql+psycopg://user:pass@localhost/testdb",
            pool_mode="queue",
            pool_size=1,
            max_overflow=0,
        )
        self.assertIsInstance(engine.pool, QueuePool)
        self.assertEqual(
            normalize_postgresql_url("postgresql+psycopg://user:pass@localhost/testdb"),
            "postgresql+psycopg://user:pass@localhost/testdb",
        )
        engine.dispose()

    def test_generation_prompt_builder_uses_only_ui_prompt_by_default(self) -> None:
        self.assertEqual(
            build_generation_request_prompt(
                user_prompt="  user prompt only  ",
                keyword="keyword",
                domain="example.com",
                brand="brand",
                project="project",
                iteration_number=2,
            ),
            "user prompt only",
        )

    def test_draft_round_trip_preserves_multiple_rows(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        rows = [
            {
                "keyword": "first keyword",
                "domain": "example.com",
                "brand": "Rankberry",
                "prompt": "First prompt",
                "project": "Alpha",
            },
            {
                "keyword": "second keyword",
                "domain": "example.org",
                "brand": "RB",
                "prompt": "Second prompt",
                "project": "Beta",
            },
        ]

        with session_factory() as session:
            service.upsert_current_draft(
                session,
                user_id=user_id,
                keyword="first keyword",
                domain="example.com",
                brand="Rankberry",
                prompt="First prompt",
                project="Alpha",
                rows=rows,
            )

        with session_factory() as session:
            draft = session.execute(select(Draft).where(Draft.user_id == user_id)).scalar_one()
            parsed_rows = service.parse_draft_rows(draft)

            self.assertEqual(len(parsed_rows), 2)
            self.assertEqual(parsed_rows[1]["keyword"], "second keyword")
            self.assertEqual(draft.keyword, "first keyword")
            self.assertEqual(draft.project, "Alpha")

    def test_append_current_draft_rows_keeps_existing_duplicate_rows(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        existing_row = {
            "keyword": "lalalalala",
            "domain": "good.com",
            "brand": "goodgoods",
            "prompt": "give me goods",
            "project": "good",
        }
        imported_row = {
            "keyword": "csv keyword",
            "domain": "csv.example",
            "brand": "CSV Brand",
            "prompt": "csv prompt",
            "project": "csv",
        }

        with session_factory() as session:
            service.upsert_current_draft(
                session,
                user_id=user_id,
                keyword=existing_row["keyword"],
                domain=existing_row["domain"],
                brand=existing_row["brand"],
                prompt=existing_row["prompt"],
                project=existing_row["project"],
                rows=[existing_row, existing_row, existing_row],
            )

        with session_factory() as session:
            service.append_current_draft_rows(session, user_id=user_id, rows=[imported_row])

        with session_factory() as session:
            draft = session.execute(select(Draft).where(Draft.user_id == user_id)).scalar_one()
            parsed_rows = service.parse_draft_rows(draft)

            self.assertEqual(parsed_rows, [existing_row, existing_row, existing_row, imported_row])
            self.assertEqual(draft.keyword, existing_row["keyword"])
            self.assertEqual(draft.project, existing_row["project"])

    def test_list_user_project_options_uses_only_current_user_runs(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()

        with session_factory() as session:
            service.upsert_current_draft(
                session,
                user_id=user_id,
                keyword="draft keyword",
                domain="example.com",
                brand="Rankberry",
                prompt="Draft prompt",
                project="Draft Alpha",
                rows=[
                    {"keyword": "", "domain": "", "brand": "", "prompt": "", "project": "Draft Alpha"},
                    {"keyword": "", "domain": "", "brand": "", "prompt": "", "project": "Draft Beta"},
                ],
            )
            service.upsert_current_draft(
                session,
                user_id=other_user_id,
                keyword="other draft keyword",
                domain="example.net",
                brand="Rankberry",
                prompt="Other draft prompt",
                project="Other Draft",
                rows=[{"keyword": "", "domain": "", "brand": "", "prompt": "", "project": "Other Draft"}],
            )

            session.add_all(
                [
                    Run(
                        user_id=user_id,
                        keyword="run one",
                        domain="example.com",
                        brand="Rankberry",
                        prompt="Prompt",
                        project="Run Alpha",
                        status="completed",
                        total_iterations=3,
                        completed_iterations=3,
                    ),
                    Run(
                        user_id=other_user_id,
                        keyword="other run",
                        domain="example.org",
                        brand="Rankberry",
                        prompt="Prompt",
                        project="Other Run",
                        status="completed",
                        total_iterations=3,
                        completed_iterations=3,
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            projects = service.list_user_project_options(session, user_id=user_id)

        self.assertEqual(projects, ["Run Alpha"])

    def test_run_processor_creates_outputs_and_final_result(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        with session_factory() as session:
            run = service.create_run(
                session,
                user_id=user_id,
                keyword="best running shoes",
                domain="https://www.example.com",
                brand="Rankberry, RB",
                prompt="Compare search intent and note the brand visibility.",
                project="Demo",
            )

        snapshot = RunSnapshot(
            id=run.id,
            user_id=user_id,
            keyword=run.keyword,
            domain=run.domain,
            brand=run.brand,
            prompt=run.prompt,
            project=run.project,
        )
        service.process_claimed_run(snapshot)

        with session_factory() as session:
            stored_run = session.execute(select(Run).where(Run.id == run.id)).scalar_one()
            outputs = list(session.execute(select(Output).order_by(Output.iteration_number.asc())).scalars())
            result = session.execute(select(RunResult).where(RunResult.run_id == run.id)).scalar_one()

            self.assertEqual(stored_run.status, "completed")
            self.assertEqual(stored_run.completed_iterations, 3)
            self.assertEqual(len(outputs), 3)
            self.assertAlmostEqual(outputs[0].openai_generation_cost_usd or 0.0, 0.000039)
            self.assertAlmostEqual(outputs[1].gemini_generation_cost_usd or 0.0, 0.0000275)
            self.assertAlmostEqual(outputs[2].grok_generation_cost_usd or 0.0, 0.0002)
            self.assertAlmostEqual(outputs[2].gemini_analysis_cost_usd or 0.0, 0.000024)
            self.assertTrue(result.gpt_domain_mention)
            self.assertTrue(result.gem_brand_mention)
            self.assertTrue(result.grok_domain_mention)
            self.assertTrue(result.grok_brand_mention)
            self.assertEqual(result.response_count_avg, 3.0)
            self.assertEqual(result.brand_list, "Rankberry, RB")
            self.assertEqual(result.citation_format, "text, url")
            self.assertIn("Mostly positive", result.sentiment_analysis or "")
            self.assertAlmostEqual(result.gemini_sentiment_cost_usd or 0.0, 0.000042)

    def test_overview_summary_scopes_project_windows_to_current_user(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        now = utcnow().replace(hour=10, minute=0, second=0, microsecond=0)

        with session_factory() as session:
            service.upsert_current_draft(
                session,
                user_id=other_user_id,
                keyword="draft keyword",
                domain="example.net",
                brand="Rankberry",
                prompt="Draft prompt",
                project="Draft Only",
                rows=[{"keyword": "", "domain": "", "brand": "", "prompt": "", "project": "Draft Only"}],
            )

            recent_user_run = Run(
                user_id=user_id,
                keyword="recent alpha",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
                created_at=now - timedelta(days=20),
            )
            old_user_run = Run(
                user_id=user_id,
                keyword="old alpha",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
                created_at=now - timedelta(days=220),
            )
            recent_other_run = Run(
                user_id=other_user_id,
                keyword="other alpha",
                domain="example.org",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
                created_at=now - timedelta(days=5),
            )
            queued_alpha_run = Run(
                user_id=user_id,
                keyword="queued alpha",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="queued",
                total_iterations=3,
                completed_iterations=0,
                created_at=now,
            )
            queued_beta_run = Run(
                user_id=user_id,
                keyword="queued beta",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Beta",
                status="queued",
                total_iterations=3,
                completed_iterations=0,
                created_at=now,
            )
            session.add_all([recent_user_run, old_user_run, recent_other_run, queued_alpha_run, queued_beta_run])
            session.flush()

            session.add_all(
                [
                    RunResult(
                        user_id=user_id,
                        run_id=recent_user_run.id,
                        project="Alpha",
                        gpt_domain_mention=True,
                        gem_domain_mention=False,
                        gpt_brand_mention=True,
                        gem_brand_mention=False,
                        created_at=recent_user_run.created_at,
                    ),
                    RunResult(
                        user_id=user_id,
                        run_id=old_user_run.id,
                        project="Alpha",
                        gpt_domain_mention=True,
                        gem_domain_mention=False,
                        gpt_brand_mention=False,
                        gem_brand_mention=False,
                        created_at=old_user_run.created_at,
                    ),
                    RunResult(
                        user_id=other_user_id,
                        run_id=recent_other_run.id,
                        project="Alpha",
                        gpt_domain_mention=False,
                        gem_domain_mention=False,
                        gpt_brand_mention=False,
                        gem_brand_mention=True,
                        created_at=recent_other_run.created_at,
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            summary = service.get_overview_summary(session, user_id=user_id, project="Alpha")

        self.assertEqual(summary["selected_project"], "Alpha")
        self.assertIn("Alpha", summary["project_options"])
        self.assertNotIn("Draft Only", summary["project_options"])
        self.assertEqual(summary["stats"]["user_half_year"]["total_results"], 1)
        self.assertEqual(summary["stats"]["user_half_year"]["brand_matches"], 1)
        self.assertEqual(summary["stats"]["user_half_year"]["domain_matches"], 1)
        self.assertEqual(summary["stats"]["user_active_runs"], 1)
        self.assertEqual(summary["stats"]["global_last_month"]["total_results"], 2)
        self.assertEqual(summary["stats"]["global_last_month"]["brand_matches"], 2)
        self.assertEqual(summary["stats"]["global_last_month"]["users"], 2)
        self.assertEqual(len(summary["monthly"]), 12)
        self.assertEqual(sum(item["total_runs"] for item in summary["monthly"]), 2)

    def test_admin_overview_summary_aggregates_all_users_and_spend(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        admin_user_id = uuid.uuid4()
        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        now = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)

        with session_factory() as session:
            session.add_all(
                [
                    Profile(user_id=user_id, username="Alpha Analyst"),
                    Profile(user_id=other_user_id, username="Beta Strategist"),
                ]
            )
            recent_user_run = Run(
                user_id=user_id,
                keyword="recent alpha",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
                created_at=now - timedelta(days=10),
            )
            recent_other_run = Run(
                user_id=other_user_id,
                keyword="other alpha",
                domain="example.org",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
                created_at=now - timedelta(days=6),
            )
            active_run = Run(
                user_id=other_user_id,
                keyword="queued alpha",
                domain="example.org",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="queued",
                total_iterations=3,
                completed_iterations=0,
                created_at=now,
            )
            session.add_all([recent_user_run, recent_other_run, active_run])
            session.flush()

            session.add_all(
                [
                    RunResult(
                        user_id=user_id,
                        run_id=recent_user_run.id,
                        project="Alpha",
                        gpt_domain_mention=True,
                        gpt_brand_mention=False,
                        gem_domain_mention=False,
                        gem_brand_mention=True,
                    ),
                    RunResult(
                        user_id=other_user_id,
                        run_id=recent_other_run.id,
                        project="Alpha",
                        gpt_domain_mention=False,
                        gpt_brand_mention=True,
                        gem_domain_mention=True,
                        gem_brand_mention=False,
                    ),
                    Output(
                        user_id=user_id,
                        run_id=recent_user_run.id,
                        iteration_number=1,
                        openai_generation_cost_usd=0.01,
                        gemini_generation_cost_usd=0.02,
                        gemini_analysis_cost_usd=0.03,
                    ),
                    Output(
                        user_id=other_user_id,
                        run_id=recent_other_run.id,
                        iteration_number=1,
                        openai_generation_cost_usd=0.04,
                        gemini_generation_cost_usd=0.05,
                        gemini_analysis_cost_usd=0.06,
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            summary = service.get_overview_summary(
                session,
                user_id=admin_user_id,
                project="Alpha",
                is_admin=True,
            )

        self.assertTrue(summary["is_admin"])
        self.assertEqual(summary["stats"]["user_half_year"]["total_results"], 2)
        self.assertEqual(summary["stats"]["global_last_month"]["total_results"], 2)
        self.assertEqual(summary["stats"]["user_active_runs"], 1)
        self.assertAlmostEqual(summary["stats"]["user_half_year"]["spend_usd"], 0.21)
        self.assertEqual(sum(item["total_runs"] for item in summary["monthly"]), 2)
        self.assertAlmostEqual(sum(item["spend_usd"] for item in summary["monthly"]), 0.21)
        self.assertEqual(
            summary["user_options"],
            [
                {"user_id": str(user_id), "username": "Alpha Analyst"},
                {"user_id": str(other_user_id), "username": "Beta Strategist"},
            ],
        )
        self.assertIsNone(summary["selected_user_id"])

        with session_factory() as session:
            filtered_summary = service.get_overview_summary(
                session,
                user_id=admin_user_id,
                project="Alpha",
                selected_user_id=user_id,
                is_admin=True,
            )

        self.assertEqual(filtered_summary["selected_user_id"], str(user_id))
        self.assertEqual(filtered_summary["stats"]["user_half_year"]["total_results"], 1)
        self.assertEqual(filtered_summary["stats"]["global_last_month"]["total_results"], 1)
        self.assertEqual(filtered_summary["stats"]["global_last_month"]["users"], 1)
        self.assertEqual(filtered_summary["stats"]["user_active_runs"], 0)
        self.assertAlmostEqual(filtered_summary["stats"]["user_half_year"]["spend_usd"], 0.06)
        self.assertEqual(sum(item["total_runs"] for item in filtered_summary["monthly"]), 1)
        self.assertAlmostEqual(sum(item["spend_usd"] for item in filtered_summary["monthly"]), 0.06)

    def test_admin_history_can_filter_by_username(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        admin_user_id = uuid.uuid4()
        alpha_user_id = uuid.uuid4()
        beta_user_id = uuid.uuid4()

        with session_factory() as session:
            session.add_all(
                [
                    Profile(user_id=alpha_user_id, username="Alpha Analyst"),
                    Profile(user_id=beta_user_id, username="Beta Strategist"),
                ]
            )
            alpha_run = Run(
                user_id=alpha_user_id,
                keyword="alpha keyword",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
            )
            beta_run = Run(
                user_id=beta_user_id,
                keyword="beta keyword",
                domain="example.org",
                brand="Rankberry",
                prompt="Prompt",
                project="Beta",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
            )
            session.add_all([alpha_run, beta_run])
            session.flush()
            session.add_all(
                [
                    RunResult(user_id=alpha_user_id, run_id=alpha_run.id, project="Alpha"),
                    RunResult(user_id=beta_user_id, run_id=beta_run.id, project="Beta"),
                ]
            )
            session.commit()

        with session_factory() as session:
            items, total = service.list_history(
                session,
                user_id=admin_user_id,
                is_admin=True,
                project=None,
                prompt=None,
                user_query="beta",
                date_from=None,
                date_to=None,
                page=1,
                page_size=20,
            )

        self.assertEqual(total, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["username"], "Beta Strategist")
        self.assertEqual(items[0]["keyword"], "beta keyword")

    def test_stop_and_resume_runs_only_affect_current_user_and_clear_partial_outputs(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        now = utcnow().replace(microsecond=0)

        with session_factory() as session:
            user_stopped_run = Run(
                user_id=user_id,
                keyword="user stopped",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="stopped",
                total_iterations=3,
                completed_iterations=1,
                created_at=now - timedelta(minutes=5),
                started_at=now - timedelta(minutes=4),
                finished_at=now - timedelta(minutes=1),
                error_messages="Stopped by user.",
            )
            user_running_run = Run(
                user_id=user_id,
                keyword="user running",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="running",
                total_iterations=3,
                completed_iterations=1,
                created_at=now - timedelta(minutes=3),
                started_at=now - timedelta(minutes=2),
            )
            other_user_run = Run(
                user_id=other_user_id,
                keyword="other user stopped",
                domain="example.org",
                brand="Rankberry",
                prompt="Prompt",
                project="Beta",
                status="stopped",
                total_iterations=3,
                completed_iterations=2,
                created_at=now - timedelta(minutes=2),
                finished_at=now - timedelta(minutes=1),
                error_messages="Stopped by user.",
            )
            session.add_all([user_stopped_run, user_running_run, other_user_run])
            session.flush()

            session.add_all(
                [
                    Output(
                        user_id=user_id,
                        run_id=user_stopped_run.id,
                        iteration_number=1,
                        gpt_output="partial",
                        project="Alpha",
                    ),
                    Output(
                        user_id=user_id,
                        run_id=user_running_run.id,
                        iteration_number=1,
                        gpt_output="partial",
                        project="Alpha",
                    ),
                    Output(
                        user_id=other_user_id,
                        run_id=other_user_run.id,
                        iteration_number=1,
                        gpt_output="keep me",
                        project="Beta",
                    ),
                    RunResult(
                        user_id=user_id,
                        run_id=user_stopped_run.id,
                        project="Alpha",
                        sentiment_analysis="partial result",
                    ),
                    RunResult(
                        user_id=other_user_id,
                        run_id=other_user_run.id,
                        project="Beta",
                        sentiment_analysis="other user result",
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            active_before = service.list_active_run_ids(session, user_id=user_id)
            stopped_ids = service.stop_user_runs(session, user_id=user_id)

        self.assertEqual(len(active_before), 2)
        self.assertEqual(len(stopped_ids), 2)

        with session_factory() as session:
            resumed_ids = service.resume_user_runs(session, user_id=user_id)

            refreshed_runs = {
                str(run.id): run
                for run in session.execute(select(Run).where(Run.user_id == user_id)).scalars()
            }
            remaining_user_outputs = list(
                session.execute(select(Output).where(Output.user_id == user_id)).scalars()
            )
            remaining_other_outputs = list(
                session.execute(select(Output).where(Output.user_id == other_user_id)).scalars()
            )
            remaining_results = list(session.execute(select(RunResult)).scalars())

        self.assertEqual(sorted(resumed_ids), sorted(active_before))
        self.assertTrue(all(run.status == "queued" for run in refreshed_runs.values()))
        self.assertTrue(all(run.completed_iterations == 0 for run in refreshed_runs.values()))
        self.assertTrue(all(run.started_at is None for run in refreshed_runs.values()))
        self.assertTrue(all(run.finished_at is None for run in refreshed_runs.values()))
        self.assertTrue(all(run.error_messages is None for run in refreshed_runs.values()))
        self.assertEqual(remaining_user_outputs, [])
        self.assertEqual(len(remaining_other_outputs), 1)
        self.assertEqual(remaining_other_outputs[0].gpt_output, "keep me")
        self.assertEqual(len(remaining_results), 1)
        self.assertEqual(remaining_results[0].user_id, other_user_id)

    def test_retry_failed_runs_only_affects_current_user_and_clears_failed_partials(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        now = utcnow().replace(microsecond=0)

        with session_factory() as session:
            user_failed_run = Run(
                user_id=user_id,
                keyword="user failed",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="failed",
                total_iterations=3,
                completed_iterations=1,
                created_at=now - timedelta(minutes=3),
                started_at=now - timedelta(minutes=2),
                finished_at=now - timedelta(minutes=1),
                error_messages="OpenAI timeout",
            )
            user_completed_run = Run(
                user_id=user_id,
                keyword="user completed",
                domain="example.com",
                brand="Rankberry",
                prompt="Prompt",
                project="Alpha",
                status="completed",
                total_iterations=3,
                completed_iterations=3,
                created_at=now - timedelta(minutes=2),
            )
            other_failed_run = Run(
                user_id=other_user_id,
                keyword="other failed",
                domain="example.org",
                brand="Rankberry",
                prompt="Prompt",
                project="Beta",
                status="failed",
                total_iterations=3,
                completed_iterations=2,
                created_at=now - timedelta(minutes=1),
                started_at=now - timedelta(minutes=1),
                finished_at=now,
                error_messages="Gemini unavailable",
            )
            session.add_all([user_failed_run, user_completed_run, other_failed_run])
            session.flush()

            session.add_all(
                [
                    Output(
                        user_id=user_id,
                        run_id=user_failed_run.id,
                        iteration_number=1,
                        gpt_output="failed partial",
                        project="Alpha",
                    ),
                    Output(
                        user_id=other_user_id,
                        run_id=other_failed_run.id,
                        iteration_number=1,
                        gpt_output="other failed partial",
                        project="Beta",
                    ),
                    RunResult(
                        user_id=user_id,
                        run_id=user_failed_run.id,
                        project="Alpha",
                        sentiment_analysis="should be cleared",
                    ),
                    RunResult(
                        user_id=other_user_id,
                        run_id=other_failed_run.id,
                        project="Beta",
                        sentiment_analysis="keep me",
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            failed_runs = service.list_failed_runs(session, user_id=user_id)
            retried_ids = service.retry_failed_user_runs(session, user_id=user_id)

            refreshed_user_runs = {
                str(run.id): run
                for run in session.execute(select(Run).where(Run.user_id == user_id)).scalars()
            }
            remaining_outputs = list(session.execute(select(Output)).scalars())
            remaining_results = list(session.execute(select(RunResult)).scalars())

        self.assertEqual([run.keyword for run in failed_runs], ["user failed"])
        self.assertEqual(len(retried_ids), 1)
        self.assertEqual(refreshed_user_runs[retried_ids[0]].status, "queued")
        self.assertEqual(refreshed_user_runs[retried_ids[0]].completed_iterations, 0)
        self.assertIsNone(refreshed_user_runs[retried_ids[0]].error_messages)
        self.assertIsNone(refreshed_user_runs[retried_ids[0]].started_at)
        self.assertIsNone(refreshed_user_runs[retried_ids[0]].finished_at)
        self.assertEqual(refreshed_user_runs[str(user_completed_run.id)].status, "completed")
        self.assertEqual(len(remaining_outputs), 1)
        self.assertEqual(remaining_outputs[0].user_id, other_user_id)
        self.assertEqual(len(remaining_results), 1)
        self.assertEqual(remaining_results[0].user_id, other_user_id)

    def test_list_outputs_filters_to_selected_local_date(self) -> None:
        database_url = "sqlite+pysqlite:///:memory:"
        engine = build_engine(database_url)
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
        service = RunService(build_settings(database_url), session_factory, FakeLLMClient())

        user_id = uuid.uuid4()
        with session_factory() as session:
            runs = [
                Run(
                    user_id=user_id,
                    keyword="before window",
                    domain="example.com",
                    brand="Rankberry",
                    prompt="Prompt",
                    project="Alpha",
                    status="completed",
                    total_iterations=3,
                    completed_iterations=3,
                    created_at=datetime(2026, 3, 30, 21, 59, tzinfo=timezone.utc),
                ),
                Run(
                    user_id=user_id,
                    keyword="start window",
                    domain="example.com",
                    brand="Rankberry",
                    prompt="Prompt",
                    project="Alpha",
                    status="completed",
                    total_iterations=3,
                    completed_iterations=3,
                    created_at=datetime(2026, 3, 30, 22, 1, tzinfo=timezone.utc),
                ),
                Run(
                    user_id=user_id,
                    keyword="end window",
                    domain="example.com",
                    brand="Rankberry",
                    prompt="Prompt",
                    project="Alpha",
                    status="completed",
                    total_iterations=3,
                    completed_iterations=3,
                    created_at=datetime(2026, 3, 31, 21, 59, tzinfo=timezone.utc),
                ),
                Run(
                    user_id=user_id,
                    keyword="after window",
                    domain="example.com",
                    brand="Rankberry",
                    prompt="Prompt",
                    project="Alpha",
                    status="completed",
                    total_iterations=3,
                    completed_iterations=3,
                    created_at=datetime(2026, 3, 31, 22, 1, tzinfo=timezone.utc),
                ),
            ]
            session.add_all(runs)
            session.flush()
            session.add_all(
                [
                    RunResult(user_id=user_id, run_id=run.id, project="Alpha", created_at=run.created_at)
                    for run in runs
                ]
            )
            session.commit()

        with session_factory() as session:
            items, total = service.list_outputs(
                session,
                user_id=user_id,
                project=None,
                prompt=None,
                local_date=date(2026, 3, 31),
                tz_offset_minutes=-120,
                page=1,
                page_size=20,
            )

        self.assertEqual(total, 2)
        self.assertEqual([item["keyword"] for item in items], ["end window", "start window"])


if __name__ == "__main__":
    unittest.main()
