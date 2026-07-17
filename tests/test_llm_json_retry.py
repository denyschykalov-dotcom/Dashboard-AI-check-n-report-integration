import unittest
from pathlib import Path

from backend.app.config import Settings
from backend.app.llm import (
    JSON_OBJECT_RESEND_LIMIT,
    LLMClient,
    LLMUsage,
    TextGenerationResult,
)


def build_settings() -> Settings:
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


class JsonRetryLLMClient(LLMClient):
    def __init__(self, responses: list[TextGenerationResult]) -> None:
        super().__init__(build_settings())
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def _call_gemini(self, prompt: str, model: str) -> TextGenerationResult:
        self.prompts.append(prompt)
        return next(self.responses)


class LLMJsonRetryTests(unittest.TestCase):
    def test_iteration_analysis_retries_invalid_json_and_records_all_spend(self) -> None:
        client = JsonRetryLLMClient(
            [
                TextGenerationResult(
                    text="I found the brand but this is not JSON.",
                    usage=LLMUsage("gemini", "test-gemini-analysis", 100, 20, 120, 0.00001),
                ),
                TextGenerationResult(
                    text='{"response_count": 4, "brand_list": ["Rankberry", "RB"], "citation_format": ["text", "url"]}',
                    usage=LLMUsage("gemini", "test-gemini-analysis", 110, 30, 140, 0.00002),
                ),
            ]
        )

        analysis = client.analyze_iteration(
            keyword="keyword",
            domain="example.com",
            brand="Rankberry",
            project="Demo",
            iteration_number=1,
            gpt_output="gpt",
            gem_output="gem",
            grok_output="grok",
        )

        self.assertEqual(len(client.prompts), 2)
        self.assertIn("The previous answer was not a valid JSON object.", client.prompts[1])
        self.assertEqual(analysis.response_count, 4.0)
        self.assertEqual(analysis.brand_list, "Rankberry, RB")
        self.assertEqual(analysis.citation_format, "text, url")
        self.assertIsNotNone(analysis.usage)
        self.assertEqual(analysis.usage.prompt_tokens, 210)
        self.assertEqual(analysis.usage.completion_tokens, 50)
        self.assertEqual(analysis.usage.total_tokens, 260)
        self.assertAlmostEqual(analysis.usage.estimated_cost_usd or 0.0, 0.00003)

    def test_iteration_analysis_falls_back_after_json_resend_limit_with_spend(self) -> None:
        client = JsonRetryLLMClient(
            [
                TextGenerationResult(
                    text="[]",
                    usage=LLMUsage("gemini", "test-gemini-analysis", 10, 5, 15, 0.00001),
                )
                for _ in range(JSON_OBJECT_RESEND_LIMIT + 1)
            ]
        )

        analysis = client.analyze_iteration(
            keyword="keyword",
            domain="example.com",
            brand="Rankberry",
            project="Demo",
            iteration_number=1,
            gpt_output="gpt",
            gem_output="gem",
            grok_output="grok",
        )

        self.assertEqual(len(client.prompts), JSON_OBJECT_RESEND_LIMIT + 1)
        self.assertIsNone(analysis.response_count)
        self.assertIsNone(analysis.brand_list)
        self.assertIsNone(analysis.citation_format)
        self.assertIsNotNone(analysis.usage)
        self.assertAlmostEqual(analysis.usage.estimated_cost_usd or 0.0, 0.00006)


if __name__ == "__main__":
    unittest.main()
