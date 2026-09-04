from __future__ import annotations

import typing

import json
import logging
import time
from dataclasses import dataclass

import httpx

from backend.app.config import Settings
from backend.app.domain import SentimentInput, normalize_citation_format
from backend.app.utils import coerce_float, extract_json_object, normalize_csv_text, read_text_file


logger = logging.getLogger("rankberry.llm")

JSON_OBJECT_RESEND_LIMIT = 5


class LLMError(Exception):
    pass


class RetryableLLMError(LLMError):
    pass


class NonRetryableLLMError(LLMError):
    pass


@dataclass(frozen=True)
class ModelPricing:
    input_per_million_usd: float
    output_per_million_usd: float


@dataclass(frozen=True)
class LLMUsage:
    provider: str
    model: str
    prompt_tokens: typing.Optional[int]
    completion_tokens: typing.Optional[int]
    total_tokens: typing.Optional[int]
    estimated_cost_usd: typing.Optional[float]


@dataclass(frozen=True)
class TextGenerationResult:
    text: str
    usage: typing.Optional[LLMUsage] = None


@dataclass(frozen=True)
class IterationAnalysis:
    response_count: typing.Optional[float]
    brand_list: typing.Optional[str]
    citation_format: typing.Optional[str]
    usage: typing.Optional[LLMUsage] = None


# Pricing snapshots checked against the providers' public pricing pages on 2026-04-01.
OPENAI_MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(input_per_million_usd=2.50, output_per_million_usd=10.00),
    "gpt-4o-mini": ModelPricing(input_per_million_usd=0.15, output_per_million_usd=0.60),
    "gpt-4.1": ModelPricing(input_per_million_usd=2.00, output_per_million_usd=8.00),
    "gpt-4.1-mini": ModelPricing(input_per_million_usd=0.40, output_per_million_usd=1.60),
}

GEMINI_MODEL_PRICING: dict[str, ModelPricing] = {
    "gemini-2.5-flash": ModelPricing(input_per_million_usd=0.45, output_per_million_usd=2.70),
    "gemini-2.5-flash-lite": ModelPricing(input_per_million_usd=0.125, output_per_million_usd=0.75),
    "gemini-2.0-flash": ModelPricing(input_per_million_usd=0.10, output_per_million_usd=0.40),
    "gemini-2.0-flash-lite": ModelPricing(input_per_million_usd=0.075, output_per_million_usd=0.30),
}

GROK_MODEL_PRICING: dict[str, ModelPricing] = {
    "grok-4.3": ModelPricing(input_per_million_usd=1.25, output_per_million_usd=2.50),
    "grok-4.20": ModelPricing(input_per_million_usd=1.25, output_per_million_usd=2.50),
    "grok-3-mini": ModelPricing(input_per_million_usd=0.30, output_per_million_usd=0.50),
}


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timeout = settings.request_timeout_seconds
        self.iteration_analysis_template = read_text_file(
            settings.iteration_analysis_prompt_file,
            (
                "Analyze the GPT, Gemini, and Grok outputs below.\n"
                "Return valid JSON with exactly these keys: response_count, brand_list, citation_format.\n"
                "Use null when a value is not available.\n"
                "citation_format must describe whether the mention format is plain text, URL/link, or absent.\n"
                'Allowed values: "text", "url", "N/A", or a comma-separated combination like "text, url".\n\n'
                "Keyword: {keyword}\n"
                "Domain: {domain}\n"
                "Brand variations: {brand}\n"
                "Project: {project}\n"
                "Iteration: {iteration_number}\n\n"
                "GPT output:\n{gpt_output}\n\n"
                "Gemini output:\n{gem_output}\n\n"
                "Grok output:\n{grok_output}\n"
            ),
        )
        self.final_sentiment_template = read_text_file(
            settings.final_sentiment_prompt_file,
            (
                "Provide one concise final sentiment analysis for this batch based on the selected outputs.\n"
                "Focus on tone, reputation risk, and whether the brand/domain is framed positively, negatively, or neutrally.\n\n"
                "Keyword: {keyword}\n"
                "Domain: {domain}\n"
                "Brand variations: {brand}\n"
                "Project: {project}\n\n"
                "Selected outputs:\n{selected_outputs}\n"
            ),
        )

    def generate_openai_output(self, prompt: str) -> TextGenerationResult:
        if not self.settings.openai_api_key:
            raise NonRetryableLLMError("OPENAI_API_KEY is not configured.")

        payload = {
            "model": self.settings.openai_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "You are a precise assistant."},
                {"role": "user", "content": prompt},
            ],
        }
        response = self._post_json(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
            json_body=payload,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise NonRetryableLLMError(
                f"Unexpected OpenAI response shape: {response}") from error
        return TextGenerationResult(
            text=str(content).strip(),
            usage=self._extract_openai_usage(
                response, self.settings.openai_model),
        )

    def generate_gemini_output(self, prompt: str) -> TextGenerationResult:
        return self._call_gemini(prompt, self.settings.gemini_model)

    def generate_grok_output(self, prompt: str) -> TextGenerationResult:
        return self._call_grok(prompt, self.settings.grok_model)

    def analyze_iteration(
        self,
        *,
        keyword: str,
        domain: str,
        brand: str,
        project: typing.Optional[str],
        iteration_number: int,
        gpt_output: str,
        gem_output: str,
        grok_output: str,
    ) -> IterationAnalysis:
        prompt = self.iteration_analysis_template.format(
            keyword=keyword,
            domain=domain,
            brand=brand,
            project=project or "",
            iteration_number=iteration_number,
            gpt_output=gpt_output,
            gem_output=gem_output,
            grok_output=grok_output,
        )
        parsed, usage = self._call_gemini_for_json_object(
            prompt=prompt,
            model=self.settings.gemini_analysis_model,
            expected_keys=("response_count", "brand_list", "citation_format"),
        )

        brand_list = parsed.get("brand_list")
        if isinstance(brand_list, list):
            brand_list = ", ".join(str(item).strip()
                                   for item in brand_list if str(item).strip())

        citation_format = parsed.get("citation_format")
        if isinstance(citation_format, list):
            citation_format = ", ".join(
                str(item).strip() for item in citation_format if str(item).strip()
            )

        return IterationAnalysis(
            response_count=coerce_float(parsed.get("response_count")),
            brand_list=normalize_csv_text(brand_list),
            citation_format=normalize_citation_format(
                normalize_csv_text(citation_format)),
            usage=usage,
        )

    def analyze_final_sentiment(
        self,
        *,
        keyword: str,
        domain: str,
        brand: str,
        project: typing.Optional[str],
        selected_inputs: list[SentimentInput],
    ) -> TextGenerationResult:
        prompt = self.final_sentiment_template.format(
            keyword=keyword,
            domain=domain,
            brand=brand,
            project=project or "",
            selected_outputs=self._format_selected_outputs(selected_inputs),
        )
        return self._call_gemini(prompt, self.settings.gemini_sentiment_model)

    def call_with_retry(self, operation_name: str, callback):
        delay_seconds = 1.0
        last_error: typing.Optional[Exception] = None
        for attempt in range(1, self.settings.max_llm_retries + 1):
            try:
                logger.info(
                    "llm_operation_attempt operation=%s attempt=%s max_attempts=%s",
                    operation_name,
                    attempt,
                    self.settings.max_llm_retries,
                )
                return callback()
            except NonRetryableLLMError as error:
                logger.error(
                    "llm_operation_non_retryable operation=%s attempt=%s error=%s",
                    operation_name,
                    attempt,
                    error,
                )
                raise
            except RetryableLLMError as error:
                last_error = error
                if attempt >= self.settings.max_llm_retries:
                    break
                logger.warning(
                    "llm_operation_retrying operation=%s attempt=%s next_delay_seconds=%s error=%s",
                    operation_name,
                    attempt,
                    delay_seconds,
                    error,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
            except httpx.HTTPError as error:
                last_error = error
                if attempt >= self.settings.max_llm_retries:
                    break
                logger.warning(
                    "llm_operation_http_retrying operation=%s attempt=%s next_delay_seconds=%s error=%s",
                    operation_name,
                    attempt,
                    delay_seconds,
                    error,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2
        logger.error(
            "llm_operation_exhausted operation=%s attempts=%s last_error=%s",
            operation_name,
            self.settings.max_llm_retries,
            last_error,
        )
        raise RetryableLLMError(
            f"{operation_name} failed after retries: {last_error}")

    def _format_selected_outputs(self, selected_inputs: list[SentimentInput]) -> str:
        chunks: list[str] = []
        for item in selected_inputs:
            chunks.append(
                f"[Iteration {item.iteration_number} | {item.provider.upper()} | mentioned={str(item.mentioned).lower()}]\n"
                f"{item.text}"
            )
        return "\n\n".join(chunks)

    def generate_gemini_json(
        self, prompt: str, *, model: str, schema: dict[str, object]
    ) -> tuple[dict[str, object], typing.Optional[LLMUsage]]:
        """One Gemini call answered as JSON matching ``schema``.

        Gemini enforces the schema itself, so unlike
        :meth:`_call_gemini_for_json_object` this needs no
        "that was not valid JSON, try again" round trips.
        """
        result = self._call_gemini(
            prompt,
            model,
            generation_config={"responseMimeType": "application/json", "responseSchema": schema},
        )
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError as error:
            raise RetryableLLMError(
                f"Gemini schema response was not JSON: {result.text[:300]}") from error
        if not isinstance(parsed, dict):
            raise RetryableLLMError(f"Gemini returned {type(parsed).__name__}, expected an object")
        return parsed, result.usage

    def _call_gemini(
        self,
        prompt: str,
        model: str,
        *,
        generation_config: typing.Optional[dict[str, object]] = None,
    ) -> TextGenerationResult:
        if not self.settings.gemini_api_key:
            raise NonRetryableLLMError("GEMINI_API_KEY is not configured.")

        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={self.settings.gemini_api_key}"
        )
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config or {"temperature": 0.2},
        }
        response = self._post_json(endpoint, headers={}, json_body=payload)
        try:
            parts = response["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as error:
            raise NonRetryableLLMError(
                f"Unexpected Gemini response shape: {response}") from error
        text = "\n".join(str(part.get("text", "")).strip()
                         for part in parts if part.get("text"))
        return TextGenerationResult(
            text=text.strip(),
            usage=self._extract_gemini_usage(response, model),
        )

    def _call_grok(self, prompt: str, model: str) -> TextGenerationResult:
        if not self.settings.grok_api_key:
            raise NonRetryableLLMError("GROK_API_KEY is not configured.")

        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "You are a precise assistant."},
                {"role": "user", "content": prompt},
            ],
        }
        response = self._post_json(
            f"{self.settings.grok_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.grok_api_key}"},
            json_body=payload,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise NonRetryableLLMError(
                f"Unexpected Grok response shape: {response}") from error
        return TextGenerationResult(
            text=str(content).strip(),
            usage=self._extract_grok_usage(response, model),
        )

    def _call_gemini_for_json_object(
        self,
        *,
        prompt: str,
        model: str,
        expected_keys: tuple[str, ...],
    ) -> tuple[dict[str, object], typing.Optional[LLMUsage]]:
        current_prompt = prompt
        usages: list[LLMUsage] = []
        last_error: typing.Optional[Exception] = None
        last_text = ""

        for resend_count in range(JSON_OBJECT_RESEND_LIMIT + 1):
            result = self._call_gemini(current_prompt, model)
            if result.usage is not None:
                usages.append(result.usage)
            last_text = result.text

            try:
                return extract_json_object(result.text), self._combine_usage(usages)
            except ValueError as error:
                last_error = error
                if resend_count >= JSON_OBJECT_RESEND_LIMIT:
                    break
                logger.warning(
                    "llm_json_object_retrying model=%s resend=%s max_resends=%s error=%s",
                    model,
                    resend_count + 1,
                    JSON_OBJECT_RESEND_LIMIT,
                    error,
                )
                current_prompt = self._build_json_object_retry_prompt(
                    original_prompt=prompt,
                    previous_text=result.text,
                    parse_error=error,
                    expected_keys=expected_keys,
                )

        logger.error(
            "llm_json_object_fallback model=%s attempts=%s last_error=%s last_text_prefix=%s",
            model,
            JSON_OBJECT_RESEND_LIMIT + 1,
            last_error,
            last_text[:200],
        )
        return {key: None for key in expected_keys}, self._combine_usage(usages)

    def _build_json_object_retry_prompt(
        self,
        *,
        original_prompt: str,
        previous_text: str,
        parse_error: Exception,
        expected_keys: tuple[str, ...],
    ) -> str:
        key_list = ", ".join(expected_keys)
        return (
            f"{original_prompt}\n\n"
            "The previous answer was not a valid JSON object.\n"
            f"Parser error: {parse_error}\n\n"
            "Previous answer:\n"
            f"{previous_text[:2000]}\n\n"
            "Try again. Return exactly one JSON object only. "
            f"The object must contain these keys: {key_list}. "
            "Do not include markdown, comments, arrays, or any text outside the JSON object."
        )

    def _post_json(self, url: str, *, headers: dict[str, str], json_body: dict[str, object]) -> dict:
        merged_headers = {"Content-Type": "application/json", **headers}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    url, headers=merged_headers, json=json_body)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            raise RetryableLLMError(str(error)) from error

        if response.status_code >= 400:
            logger.warning(
                "llm_provider_http_error status_code=%s url=%s",
                response.status_code,
                self._redact_url(url),
            )
            self._raise_for_status(response)

        try:
            return response.json()
        except json.JSONDecodeError as error:
            logger.error("llm_provider_invalid_json url=%s body_prefix=%s", self._redact_url(url), response.text[:200])
            raise RetryableLLMError(
                f"Provider returned invalid JSON: {response.text[:500]}") from error

    def _redact_url(self, url: str) -> str:
        return url.split("?key=", 1)[0] if "?key=" in url else url

    def _raise_for_status(self, response: httpx.Response) -> None:
        body_text = response.text[:1000]
        lowered = body_text.lower()

        if response.status_code in {401, 403}:
            raise NonRetryableLLMError(body_text or "Invalid API credentials.")
        if "insufficient_quota" in lowered or "quota" in lowered:
            raise NonRetryableLLMError(body_text or "Insufficient quota.")
        if response.status_code in {429, 500, 502, 503, 504}:
            raise RetryableLLMError(
                body_text or f"Transient upstream failure ({response.status_code}).")
        if "rate limit" in lowered:
            raise RetryableLLMError(body_text or "Rate limit.")

        raise NonRetryableLLMError(
            body_text or f"Provider request failed with {response.status_code}.")

    def _extract_openai_usage(self, response: dict[str, object], configured_model: str) -> typing.Optional[LLMUsage]:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return None

        prompt_tokens = self._coerce_int(usage.get("prompt_tokens"))
        completion_tokens = self._coerce_int(usage.get("completion_tokens"))
        total_tokens = self._coerce_int(usage.get("total_tokens"))
        model = str(response.get("model") or configured_model)

        return LLMUsage(
            provider="openai",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=self._estimate_text_cost(
                provider="openai",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )

    def _extract_gemini_usage(self, response: dict[str, object], configured_model: str) -> typing.Optional[LLMUsage]:
        usage = response.get("usageMetadata")
        if not isinstance(usage, dict):
            return None

        prompt_tokens = self._coerce_int(usage.get("promptTokenCount"))
        completion_tokens = self._coerce_int(usage.get("candidatesTokenCount"))
        total_tokens = self._coerce_int(usage.get("totalTokenCount"))
        if completion_tokens is None and total_tokens is not None and prompt_tokens is not None:
            completion_tokens = max(total_tokens - prompt_tokens, 0)

        model = str(response.get("modelVersion") or configured_model)
        return LLMUsage(
            provider="gemini",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=self._estimate_text_cost(
                provider="gemini",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )

    def _extract_grok_usage(self, response: dict[str, object], configured_model: str) -> typing.Optional[LLMUsage]:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return None

        prompt_tokens = self._coerce_int(usage.get("prompt_tokens"))
        completion_tokens = self._coerce_int(usage.get("completion_tokens"))
        total_tokens = self._coerce_int(usage.get("total_tokens"))
        model = str(response.get("model") or configured_model)

        return LLMUsage(
            provider="grok",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=self._estimate_text_cost(
                provider="grok",
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            ),
        )

    def _estimate_text_cost(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: typing.Optional[int],
        completion_tokens: typing.Optional[int],
    ) -> typing.Optional[float]:
        if prompt_tokens is None or completion_tokens is None:
            return None

        pricing = self._match_model_pricing(provider, model)
        if pricing is None:
            return None

        estimated_cost = (
            (prompt_tokens * pricing.input_per_million_usd)
            + (completion_tokens * pricing.output_per_million_usd)
        ) / 1_000_000
        return round(estimated_cost, 8)

    def _combine_usage(self, usages: list[LLMUsage]) -> typing.Optional[LLMUsage]:
        if not usages:
            return None

        first = usages[0]
        return LLMUsage(
            provider=first.provider,
            model=first.model,
            prompt_tokens=self._sum_optional_ints(item.prompt_tokens for item in usages),
            completion_tokens=self._sum_optional_ints(item.completion_tokens for item in usages),
            total_tokens=self._sum_optional_ints(item.total_tokens for item in usages),
            estimated_cost_usd=self._sum_optional_floats(item.estimated_cost_usd for item in usages),
        )

    def _sum_optional_ints(self, values: typing.Iterable[typing.Optional[int]]) -> typing.Optional[int]:
        total = 0
        has_value = False
        for value in values:
            if value is None:
                continue
            total += value
            has_value = True
        return total if has_value else None

    def _sum_optional_floats(self, values: typing.Iterable[typing.Optional[float]]) -> typing.Optional[float]:
        total = 0.0
        has_value = False
        for value in values:
            if value is None:
                continue
            total += value
            has_value = True
        return round(total, 8) if has_value else None

    def _match_model_pricing(self, provider: str, model: str) -> typing.Optional[ModelPricing]:
        normalized_model = model.strip().lower()
        pricing_tables = {
            "openai": OPENAI_MODEL_PRICING,
            "gemini": GEMINI_MODEL_PRICING,
            "grok": GROK_MODEL_PRICING,
        }
        pricing_table = pricing_tables.get(provider)
        if pricing_table is None:
            return None
        for base_name, pricing in pricing_table.items():
            if normalized_model == base_name or normalized_model.startswith(f"{base_name}-"):
                return pricing
        return None

    def _coerce_int(self, value: object) -> typing.Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
