"""Claude-written report commentary: context digest, response handling, and the
planned-works payload the numbered plan renders from.

No network: the Anthropic client is stubbed, so these assert *our* contract
(what goes into the prompt, what we accept back) rather than the model's prose.
"""

import json
import os
import types
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from backend.app.config import Settings
from backend.app.report_builder import ai_commentary
from backend.app.report_builder import export as report_export
from backend.app.report_builder.ai_commentary import (
    AICommentaryClient,
    AICommentaryUnavailable,
    build_report_context,
    commentable_block_keys,
)


def build_settings(*, anthropic_api_key="test-key", summary_max_chars=1500) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        migration_database_url="sqlite+pysqlite:///:memory:",
        db_pool_mode="null",
        db_pool_size=1,
        db_max_overflow=0,
        admin_email="analytics@rankberry.marketing",
        supabase_url=None,
        supabase_anon_key=None,
        google_sheets_credentials_file=None,
        google_sheets_client_folder_id=None,
        ahrefs_api_token=None,
        seranking_api_key=None,
        report_builder_secret_key=None,
        openai_api_key=None,
        gemini_api_key=None,
        grok_api_key=None,
        anthropic_api_key=anthropic_api_key,
        anthropic_comment_model="claude-sonnet-5",
        anthropic_summary_model="claude-opus-5",
        report_summary_max_chars=summary_max_chars,
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
        queue_poll_max_seconds=0.1,
        worker_concurrency=1,
        enforce_one_active_run_per_user=True,
        total_iterations=3,
        iteration_analysis_prompt_file=Path("iteration-analysis.txt"),
        final_sentiment_prompt_file=Path("final-sentiment.txt"),
        report_block_comment_prompt_file=Path("report-block-comment.txt"),
        report_summary_prompt_file=Path("report-summary.txt"),
        report_translate_prompt_file=Path("report-translate.txt"),
        report_search_industry_prompt_file=Path("report-industry.txt"),
    )


def _text_message(text, *, stop_reason="end_turn", stop_details=None):
    block = types.SimpleNamespace(type="text", text=text)
    return types.SimpleNamespace(
        content=[block], stop_reason=stop_reason, stop_details=stop_details
    )


class _StubMessages:
    """Records the request and replays a queued response (or raises)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _stub_api(responses, beta_responses=None):
    messages = _StubMessages(responses)
    beta_messages = _StubMessages(beta_responses if beta_responses is not None else [])
    return types.SimpleNamespace(
        messages=messages,
        beta=types.SimpleNamespace(messages=beta_messages),
    )


def _client(api, **settings_kwargs) -> AICommentaryClient:
    client = AICommentaryClient(build_settings(**settings_kwargs))
    client._client = api
    return client


class CommentableBlocksTests(unittest.TestCase):
    def test_only_sections_that_can_show_a_comment_qualify(self):
        blocks = [
            {"block_type_key": "intro_header", "status": "ok"},      # no comment slot
            {"block_type_key": "summary", "status": "ok"},           # written by Opus at submit
            {"block_type_key": "ga4_summary", "status": "ok"},
            {"block_type_key": "gsc_summary", "status": "unavailable"},  # no data
            {"block_type_key": "ai_visibility_gpt_1mo", "status": "ok"},  # renders inside another section
            {"block_type_key": "ga4_summary", "status": "ok"},       # duplicate
            {"block_type_key": "", "status": "ok"},
        ]
        self.assertEqual(commentable_block_keys(blocks), ["ga4_summary"])

    def test_every_commentable_key_has_a_template_section(self):
        blocks = [{"block_type_key": key, "status": "ok"} for key in report_export.SECTION_BY_KEY]
        for key in commentable_block_keys(blocks):
            self.assertIn(key, report_export.SECTION_BY_KEY)

    def test_search_industry_is_not_an_analyst_comment(self):
        """It is editorial scene-setting written by its own web-searched call.

        Left in the comment run it would get a comment *about* a section that has
        no data — which is what used to fill it with meta-prose.
        """
        blocks = [
            {"block_type_key": "search_industry", "status": "ok"},
            {"block_type_key": "ga4_summary", "status": "ok"},
        ]
        self.assertEqual(commentable_block_keys(blocks), ["ga4_summary"])


class SearchIndustryTextTests(unittest.TestCase):
    """The web-search write-up: word ceiling, and ignoring the search preamble."""

    def test_trim_to_words_keeps_short_text_untouched(self):
        text = "Google confirmed no update in July."
        self.assertEqual(ai_commentary._trim_to_words(text, 150), text)

    def test_trim_to_words_cuts_on_a_sentence_boundary(self):
        text = " ".join(["word"] * 40) + ". " + " ".join(["tail"] * 40) + "."
        out = ai_commentary._trim_to_words(text, 45)
        self.assertTrue(out.endswith("."))
        self.assertLessEqual(len(out.split()), 45)
        self.assertNotIn("tail", out)

    def test_trim_to_words_drops_whole_lines(self):
        """One item per line is the format the report parses into cards.

        Trimming must not reflow the lines into one paragraph, or four items
        collapse into a single card.
        """
        lines = [f"LABEL{i} — " + " ".join(["word"] * 20) + "." for i in range(4)]
        out = ai_commentary._trim_to_words("\n".join(lines), 50)
        self.assertEqual(out.splitlines(), lines[:2])

    def test_trim_to_words_falls_back_to_an_ellipsis(self):
        out = ai_commentary._trim_to_words(" ".join(["word"] * 200), 150)
        self.assertEqual(len(out.split()), 150)
        self.assertTrue(out.endswith("…"))

    def test_answer_text_ignores_the_pre_search_preamble(self):
        """Only text after the last tool block is the answer.

        A web-search turn narrates before it searches ("I'll look that up…");
        joining every text block pasted that into the report.
        """
        message = types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="text", text="I'll search for that period."),
                types.SimpleNamespace(type="server_tool_use", name="web_search"),
                types.SimpleNamespace(type="web_search_tool_result", content=[]),
                types.SimpleNamespace(type="text", text="In July 2026, Google confirmed no update."),
            ]
        )
        self.assertEqual(
            AICommentaryClient._answer_text_of(message),
            "In July 2026, Google confirmed no update.",
        )

    def test_answer_text_joins_citation_split_blocks_without_a_break(self):
        """Citations split one sentence into several text blocks.

        A newline between them broke a search-industry item into half-sentence
        cards, because that section renders one card per line.
        """
        message = types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="web_search_tool_result", content=[]),
                types.SimpleNamespace(type="text", text="NO UPDATE — No update in July"),
                types.SimpleNamespace(type="text", text=", applying globally"),
                types.SimpleNamespace(type="text", text="."),
            ]
        )
        self.assertEqual(
            AICommentaryClient._answer_text_of(message),
            "NO UPDATE — No update in July, applying globally.",
        )

    def test_answer_text_handles_a_turn_with_no_tool_use(self):
        message = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="Just the answer.")]
        )
        self.assertEqual(AICommentaryClient._answer_text_of(message), "Just the answer.")


class ReportContextTests(unittest.TestCase):
    def _context(self, **kwargs):
        blocks = kwargs.pop("blocks")
        return build_report_context(
            client_name="Acme Co",
            client_domain="acme.com",
            period_label="Jun 2026",
            default_comparison="mom,yoy",
            blocks=blocks,
            **kwargs,
        )

    def test_meta_and_comparisons(self):
        context = self._context(blocks=[])
        self.assertEqual(context["client"], {"name": "Acme Co", "domain": "acme.com"})
        self.assertEqual(context["reporting_period"], "Jun 2026")
        self.assertEqual(context["comparisons_offered"], ["MOM", "YOY"])

    def test_long_lists_are_sampled_with_a_marker_and_floats_rounded(self):
        context = self._context(
            blocks=[{
                "block_type_key": "ga4_top_pages",
                "status": "ok",
                "data": {"pages": [{"sessions": 1.23456} for _ in range(40)]},
            }]
        )
        pages = context["sections"][0]["data"]["pages"]
        self.assertEqual(len(pages), ai_commentary._MAX_LIST_ITEMS + 1)
        self.assertIn("more rows omitted", pages[-1])
        self.assertEqual(pages[0]["sessions"], 1.23)

    def test_per_day_series_are_dropped(self):
        context = self._context(
            blocks=[{
                "block_type_key": "ga4_summary",
                "status": "ok",
                "data": {"daily": [1, 2], "daily_previous": [1], "kpis": {"current": {"sessions": 10}}},
            }]
        )
        data = context["sections"][0]["data"]
        self.assertNotIn("daily", data)
        self.assertNotIn("daily_previous", data)
        self.assertEqual(data["kpis"]["current"]["sessions"], 10)

    def test_unavailable_sections_carry_their_reason_not_data(self):
        context = self._context(
            blocks=[{
                "block_type_key": "gsc_summary",
                "status": "unavailable",
                "data": None,
                "unavailable_reason": "No sheet found.",
            }]
        )
        section = context["sections"][0]
        self.assertEqual(section["unavailable_reason"], "No sheet found.")
        self.assertNotIn("data", section)

    def test_specialist_comments_ride_along_only_for_the_summary_pass(self):
        blocks = [{"block_type_key": "ga4_summary", "status": "ok", "data": {}, "comment": "Traffic is up."}]
        self.assertNotIn("specialist_comment", self._context(blocks=blocks)["sections"][0])
        with_comments = self._context(blocks=blocks, include_comments=True)
        self.assertEqual(with_comments["sections"][0]["specialist_comment"], "Traffic is up.")


class BlockCommentTests(unittest.TestCase):
    def test_comments_are_keyed_by_block_and_unknown_keys_dropped(self):
        payload = {"comments": [
            {"block_key": "ga4_summary", "comment": "Sessions rose 18%."},
            {"block_key": "gsc_summary", "comment": "   "},          # empty after strip
            {"block_key": "not_requested", "comment": "Ignore me."},
            "junk",
        ]}
        api = _stub_api([_text_message(json.dumps(payload))])
        client = _client(api)

        comments = client.generate_block_comments(
            context={"sections": []}, block_keys=["ga4_summary", "gsc_summary", "ga4_summary"]
        )

        self.assertEqual(comments, {"ga4_summary": "Sessions rose 18%."})
        request = api.messages.calls[0]
        self.assertEqual(request["model"], "claude-sonnet-5")
        # Structured output, so a parse failure can only mean truncation.
        self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
        # Each requested section is named once, deduplicated.
        self.assertEqual(request["messages"][0]["content"].count("ga4_summary"), 1)

    def test_no_requested_sections_makes_no_request(self):
        api = _stub_api([])
        self.assertEqual(_client(api).generate_block_comments(context={}, block_keys=[]), {})
        self.assertEqual(api.messages.calls, [])

    def test_unreadable_response_is_reported_as_unavailable(self):
        api = _stub_api([_text_message("not json at all")])
        with self.assertRaises(AICommentaryUnavailable):
            _client(api).generate_block_comments(context={}, block_keys=["ga4_summary"])

    def test_a_refusal_is_reported_as_unavailable(self):
        refusal = _text_message(
            "", stop_reason="refusal", stop_details=types.SimpleNamespace(category="cyber")
        )
        api = _stub_api([refusal])
        with self.assertRaises(AICommentaryUnavailable) as caught:
            _client(api).generate_block_comments(context={}, block_keys=["ga4_summary"])
        self.assertIn("cyber", str(caught.exception))

    def test_missing_api_key_is_reported_as_unavailable(self):
        client = AICommentaryClient(build_settings(anthropic_api_key=None))
        self.assertFalse(client.is_configured)
        with self.assertRaises(AICommentaryUnavailable):
            client.generate_block_comments(context={}, block_keys=["ga4_summary"])


class SummaryTests(unittest.TestCase):
    def test_summary_runs_on_opus_with_a_server_side_fallback(self):
        api = _stub_api([], beta_responses=[_text_message("A tidy summary.")])
        client = _client(api)

        self.assertEqual(client.generate_summary(context={"sections": []}), "A tidy summary.")

        request = api.beta.messages.calls[0]
        self.assertEqual(request["model"], "claude-opus-5")
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(api.messages.calls, [])

    def test_an_over_long_summary_is_trimmed_on_a_sentence_boundary(self):
        sentences = "First sentence here. Second sentence here. Third sentence here."
        api = _stub_api([], beta_responses=[_text_message(sentences)])
        client = _client(api, summary_max_chars=45)

        summary = client.generate_summary(context={})

        self.assertLessEqual(len(summary), 45)
        self.assertTrue(summary.endswith("."))
        self.assertEqual(summary, "First sentence here. Second sentence here.")

    def test_a_drafted_summary_is_handed_over_to_be_polished(self):
        api = _stub_api([], beta_responses=[_text_message("Polished.")])
        client = _client(api)

        client.generate_summary(context={}, existing_summary="My rough draft.")

        self.assertIn("My rough draft.", api.beta.messages.calls[0]["messages"][0]["content"])

    def test_an_empty_summary_is_reported_as_unavailable(self):
        api = _stub_api([], beta_responses=[_text_message("   ")])
        with self.assertRaises(AICommentaryUnavailable):
            _client(api).generate_summary(context={})


class PlannedWorksPayloadTests(unittest.TestCase):
    """The numbered plan renders from richer per-task fields than the old table."""

    def _data(self, planned_block):
        return report_export._build_data(
            period_label="Jun 2026",
            default_comparison="mom",
            prepared="2026-07-01",
            blocks=[planned_block],
            client_name="Acme Co",
            client_domain="acme.com",
        )

    def test_clickup_todo_tasks_carry_due_date_and_owners(self):
        data = self._data({
            "block_type_key": "planned_works",
            "status": "ok",
            "data": {"tasks": [{
                "name": "Rewrite service pages",
                "description": "Target head terms",
                "url": "https://app.clickup.com/t/abc123",
                "due_date": "2026-08-12",
                "assignees": ["dana", ""],
            }]},
        })
        self.assertEqual(data["workPlanned"], [{
            "name": "Rewrite service pages",
            "description": "Target head terms",
            "taskId": "abc123",
            "due": "2026-08-12",
            "assignees": ["dana"],
        }])

    def test_a_manual_plan_still_renders_as_prose(self):
        data = self._data({
            "block_type_key": "planned_works",
            "status": "ok",
            "data": {"mode": "manual", "text": "Ship the redesign.", "tasks": []},
        })
        self.assertEqual(data["workPlanned"], [])
        self.assertIn("Ship the redesign.", data["workPlannedManual"])


class AiRouteTests(unittest.TestCase):
    """The HTTP contract: what the report builder page sends and gets back."""

    def setUp(self):
        import uuid

        from fastapi.testclient import TestClient
        from sqlalchemy.orm import sessionmaker

        from backend.app.api import routes
        from backend.app.auth import AuthenticatedUser, get_current_user
        from backend.app.db import Base, build_engine, get_db_session
        from backend.app.main import app
        from backend.app.models import Client

        engine = build_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine, expire_on_commit=False)()
        self.client_row = Client(name="Acme Co", domain="acme.com", created_by=uuid.uuid4())
        self.session.add(self.client_row)
        self.session.commit()

        app.dependency_overrides[get_db_session] = lambda: self.session
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            user_id=uuid.uuid4(),
            email="specialist@rankberry.marketing",
            is_admin=False,
            access_token="test-token",
        )
        self.addCleanup(app.dependency_overrides.clear)
        self.routes = routes
        self.http = TestClient(app)

    def _install(self, ai_client):
        original = self.routes.get_ai_commentary_client
        self.routes.get_ai_commentary_client = lambda: ai_client
        self.addCleanup(lambda: setattr(self.routes, "get_ai_commentary_client", original))

    def _payload(self, **overrides):
        body = {
            "client_id": str(self.client_row.id),
            "period_label": "Jun 2026",
            "default_comparison": "mom",
            "blocks": [
                {"block_type_key": "ga4_summary", "status": "ok", "data": {"kpis": {}}, "comment": ""},
                {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""},
            ],
        }
        body.update(overrides)
        return body

    def test_comments_route_returns_a_comment_per_section(self):
        payload = {"comments": [{"block_key": "ga4_summary", "comment": "Sessions rose 18%."}]}
        self._install(_client(_stub_api([_text_message(json.dumps(payload))])))

        response = self.http.post("/api/report-builder/ai/comments", json=self._payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["comments"], {"ga4_summary": "Sessions rose 18%."})

    def test_comments_route_skips_the_call_when_nothing_is_commentable(self):
        api = _stub_api([])
        self._install(_client(api))

        response = self.http.post(
            "/api/report-builder/ai/comments",
            json=self._payload(blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}}]),
        )

        self.assertEqual(response.json()["comments"], {})
        self.assertEqual(api.messages.calls, [])

    def test_summary_route_returns_the_summary_and_its_block_key(self):
        self._install(_client(_stub_api([], beta_responses=[_text_message("A tidy summary.")])))

        response = self.http.post("/api/report-builder/ai/summary", json=self._payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], "A tidy summary.")
        self.assertEqual(response.json()["block_type_key"], "summary")

    def test_claude_being_unavailable_is_a_503_not_a_500(self):
        self._install(AICommentaryClient(build_settings(anthropic_api_key=None)))

        response = self.http.post("/api/report-builder/ai/summary", json=self._payload())

        self.assertEqual(response.status_code, 503)
        self.assertIn("ANTHROPIC_API_KEY", response.json()["detail"])

    def test_an_unknown_client_is_a_404(self):
        import uuid

        self._install(_client(_stub_api([])))
        response = self.http.post(
            "/api/report-builder/ai/comments", json=self._payload(client_id=str(uuid.uuid4()))
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
