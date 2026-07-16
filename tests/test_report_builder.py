import os
import unittest
import uuid
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy.orm import sessionmaker

from backend.app.db import Base, build_engine
from backend.app.models import Client, Report, ReportBlock, Run, RunResult
from backend.app.report_builder import export as report_export
from backend.app.report_builder import service as report_service
from backend.app.report_builder.block_catalog import BLOCK_CATALOG, get_block
from backend.app.report_builder.data_sources import ai_visibility, ga4, static_editorial
from backend.app.report_builder.data_sources.base import ResolveContext
from backend.app.utils import utcnow


def _make_session():
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _client(session, *, name="Acme Co", domain="acme.com", **kwargs) -> Client:
    client = Client(name=name, domain=domain, created_by=uuid.uuid4(), **kwargs)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def _seed_ai_run(session, *, project, created_at, gpt_domain=False, gem_brand=False):
    run = Run(
        user_id=uuid.uuid4(),
        keyword="kw",
        domain="acme.com",
        brand="Acme",
        prompt="p",
        project=project,
        created_at=created_at,
    )
    session.add(run)
    session.flush()
    result = RunResult(
        user_id=run.user_id,
        run_id=run.id,
        project=project,
        gpt_domain_mention=gpt_domain,
        gem_brand_mention=gem_brand,
    )
    session.add(result)
    session.commit()


class BlockCatalogTests(unittest.TestCase):
    def test_catalog_has_expected_size_and_unique_keys(self) -> None:
        self.assertEqual(len(BLOCK_CATALOG), 24)
        keys = [block.key for block in BLOCK_CATALOG]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ai_visibility_blocks_carry_window_and_model(self) -> None:
        ai_blocks = [block for block in BLOCK_CATALOG if block.source == "ai_visibility"]
        self.assertEqual(len(ai_blocks), 8)
        for block in ai_blocks:
            self.assertIn(block.ai_visibility_window, {"last_month", "last_6_months"})
            self.assertIn(block.ai_visibility_model, {"all", "gpt", "gemini", "grok"})

    def test_bar_variants_exist_for_donut_blocks(self) -> None:
        self.assertIsNotNone(get_block("ga4_session_mix_bar"))
        self.assertIsNotNone(get_block("gsc_branded_bar"))


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def _context(self, client) -> ResolveContext:
        return ResolveContext(client=client, period_label="2026-06", now=utcnow(), session=self.session)

    def test_static_editorial_block_is_ok(self) -> None:
        client = _client(self.session)
        result = static_editorial.resolve(get_block("intro_header"), self._context(client))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["client"], "Acme Co")

    def test_ga4_block_unavailable_when_not_configured(self) -> None:
        client = _client(self.session)  # no ga4_sheet_id
        result = ga4.resolve(get_block("ga4_summary"), self._context(client))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("Not configured", result.unavailable_reason)

    def test_ai_visibility_aggregates_matching_project_all_models(self) -> None:
        client = _client(self.session, name="Acme Co")
        _seed_ai_run(self.session, project="acme co", created_at=utcnow(), gpt_domain=True)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gem_brand=True)
        result = ai_visibility.resolve(get_block("ai_visibility_all_1mo"), self._context(client))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["total_results"], 2)
        self.assertEqual(result.data["domain_matches"], 1)
        self.assertEqual(result.data["brand_matches"], 1)

    def test_ai_visibility_specific_model_scope(self) -> None:
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        gpt_result = ai_visibility.resolve(get_block("ai_visibility_gpt_1mo"), self._context(client))
        grok_result = ai_visibility.resolve(get_block("ai_visibility_grok_1mo"), self._context(client))
        self.assertEqual(gpt_result.data["domain_matches"], 1)
        self.assertEqual(grok_result.data["domain_matches"], 0)

    def test_ai_visibility_window_filtering(self) -> None:
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow() - timedelta(days=60), gpt_domain=True)
        one_month = ai_visibility.resolve(get_block("ai_visibility_all_1mo"), self._context(client))
        six_month = ai_visibility.resolve(get_block("ai_visibility_all_6mo"), self._context(client))
        self.assertEqual(one_month.status, "unavailable")  # older than 30 days
        self.assertEqual(six_month.status, "ok")

    def test_ai_visibility_unavailable_when_no_matching_runs(self) -> None:
        client = _client(self.session, name="Other Client")
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        result = ai_visibility.resolve(get_block("ai_visibility_all_1mo"), self._context(client))
        self.assertEqual(result.status, "unavailable")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def test_generate_marks_unknown_key_unavailable_without_failing_others(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["intro_header", "does_not_exist"],
        )
        by_key = {block["block_type_key"]: block for block in result["blocks"]}
        self.assertEqual(by_key["intro_header"]["status"], "ok")
        self.assertEqual(by_key["does_not_exist"]["status"], "unavailable")

    def test_generate_rejects_empty_selection(self) -> None:
        client = _client(self.session)
        with self.assertRaises(ValueError):
            report_service.generate(self.session, client_id=client.id, block_keys=[])

    def test_save_persists_report_and_blocks(self) -> None:
        client = _client(self.session)
        user_id = uuid.uuid4()
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {"client": "Acme Co"}, "comment": "hi"},
                {"block_type_key": "ga4_summary", "status": "unavailable", "data": None, "comment": "", "unavailable_reason": "Not configured"},
            ],
            generated_by=user_id,
        )
        _, blocks = report_service.get_report(self.session, report.id)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].block_type_key, "intro_header")
        self.assertEqual(blocks[0].comment, "hi")

    def test_update_edits_in_place_without_duplicating(self) -> None:
        client = _client(self.session)
        user_id = uuid.uuid4()
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {"x": 1}, "comment": "first"}],
            generated_by=user_id,
        )
        report_service.update_report(
            self.session,
            report_id=report.id,
            period_label="2026-06",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {"x": 1}, "comment": "second"}],
            generated_by=user_id,
        )
        reports = report_service.list_reports_for_client(self.session, client.id)
        self.assertEqual(len(reports), 1)  # no duplicate created
        _, blocks = report_service.get_report(self.session, report.id)
        self.assertEqual(blocks[0].comment, "second")

    def test_save_allows_empty_comments(self) -> None:
        client = _client(self.session)
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
        )
        self.assertIsInstance(report.id, uuid.UUID)


class ExportTests(unittest.TestCase):
    def test_export_html_is_self_contained_and_includes_data_and_comment(self) -> None:
        session = _make_session()
        client = _client(session)
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {"headline": "Great month"}, "comment": "Client is happy"},
            ],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(session, report.id)
        html_doc = report_export.build_report_html(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com"
        )
        self.assertIn("<!doctype html>", html_doc)
        self.assertIn("Great month", html_doc)
        self.assertIn("Client is happy", html_doc)
        self.assertIn("Acme Co", html_doc)
        # self-contained: no external resource references
        self.assertNotIn("http://", html_doc)
        self.assertNotIn("https://", html_doc)


if __name__ == "__main__":
    unittest.main()
