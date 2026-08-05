from __future__ import annotations

import typing

from dataclasses import dataclass
from statistics import mean
from urllib.parse import urlparse


@dataclass(frozen=True)
class IterationLike:
    iteration_number: int
    gpt_output: typing.Optional[str]
    gem_output: typing.Optional[str]
    grok_output: typing.Optional[str]
    gpt_domain_mention: bool
    gem_domain_mention: bool
    grok_domain_mention: bool
    gpt_brand_mention: bool
    gem_brand_mention: bool
    grok_brand_mention: bool
    response_count: typing.Optional[float]
    brand_list: typing.Optional[str]
    citation_format: typing.Optional[str]


@dataclass(frozen=True)
class SentimentInput:
    provider: str
    iteration_number: int
    text: str
    mentioned: bool


@dataclass(frozen=True)
class SentimentRef:
    """A candidate response identified by provider and iteration, without its text.

    Picking which responses go into the final sentiment prompt only needs to know
    that a response exists and whether it mentioned the brand or domain — never
    what it says. Selecting on refs lets the caller fetch the raw text for the
    handful that were actually chosen instead of loading every response.
    """

    provider: str
    iteration_number: int
    mentioned: bool


def split_brand_variations(raw_brand: typing.Optional[str]) -> list[str]:
    seen: set[str] = set()
    variations: list[str] = []
    for chunk in (raw_brand or "").split(","):
        cleaned = chunk.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        variations.append(cleaned)
    return variations


def normalize_domain_variations(raw_domain: typing.Optional[str]) -> list[str]:
    domain = (raw_domain or "").strip().lower()
    if not domain:
        return []

    prefixed = domain if "://" in domain else f"https://{domain}"
    parsed = urlparse(prefixed)
    host = (parsed.netloc or parsed.path).split("/")[0].strip().lower().rstrip("/")

    without_protocol = domain.split("://", 1)[-1].rstrip("/")
    without_www = without_protocol[4:] if without_protocol.startswith("www.") else without_protocol
    host_without_www = host[4:] if host.startswith("www.") else host

    candidates = [domain.rstrip("/"), without_protocol, without_www, host, host_without_www]

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = candidate.strip().lower().rstrip("/")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _contains_any(text: typing.Optional[str], variants: list[str]) -> bool:
    haystack = (text or "").lower()
    return any(variant in haystack for variant in variants if variant)


def detect_mentions(output_text: typing.Optional[str], raw_domain: typing.Optional[str], raw_brand: typing.Optional[str]) -> tuple[bool, bool]:
    domain_match = _contains_any(output_text, normalize_domain_variations(raw_domain))
    brand_match = _contains_any(output_text, [item.lower() for item in split_brand_variations(raw_brand)])
    return domain_match, brand_match


def merge_brand_lists(values: list[typing.Optional[str]]) -> typing.Optional[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for value in values:
        for item in (value or "").split(","):
            cleaned = item.strip()
            if not cleaned:
                continue
            lowered = cleaned.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            merged.append(cleaned)
    return ", ".join(merged) if merged else None


def normalize_citation_format(value: typing.Optional[str]) -> typing.Optional[str]:
    categories: set[str] = set()
    saw_na = False

    for item in (value or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in {"n/a", "na", "none", "null"} or "n/a" in lowered:
            saw_na = True
            continue
        if any(token in lowered for token in ("url", "link", "http://", "https://", "www.")):
            categories.add("url")
            continue
        categories.add("text")

    ordered = [label for label in ("text", "url") if label in categories]
    if ordered:
        return ", ".join(ordered)
    if saw_na:
        return "N/A"
    return None


def merge_citation_formats(values: list[typing.Optional[str]]) -> typing.Optional[str]:
    categories: set[str] = set()
    saw_na = False

    for value in values:
        normalized = normalize_citation_format(value)
        if normalized == "N/A":
            saw_na = True
            continue
        for item in (normalized or "").split(","):
            cleaned = item.strip()
            if cleaned in {"text", "url"}:
                categories.add(cleaned)

    ordered = [label for label in ("text", "url") if label in categories]
    if ordered:
        return ", ".join(ordered)
    if saw_na:
        return "N/A"
    return None


def average_response_count(values: list[typing.Optional[float]]) -> typing.Optional[float]:
    actual = [value for value in values if value is not None]
    if not actual:
        return None
    return float(mean(actual))


def aggregate_outputs(outputs: list[IterationLike]) -> dict[str, object]:
    ordered = sorted(outputs, key=lambda item: item.iteration_number)
    return {
        "gpt_domain_mention": any(item.gpt_domain_mention for item in ordered),
        "gem_domain_mention": any(item.gem_domain_mention for item in ordered),
        "grok_domain_mention": any(item.grok_domain_mention for item in ordered),
        "gpt_brand_mention": any(item.gpt_brand_mention for item in ordered),
        "gem_brand_mention": any(item.gem_brand_mention for item in ordered),
        "grok_brand_mention": any(item.grok_brand_mention for item in ordered),
        "response_count_avg": average_response_count([item.response_count for item in ordered]),
        "brand_list": merge_brand_lists([item.brand_list for item in ordered]),
        "citation_format": merge_citation_formats([item.citation_format for item in ordered]),
    }


def select_sentiment_refs(candidates: list[SentimentRef], limit: int = 4) -> list[SentimentRef]:
    """Pick which responses feed the final sentiment prompt.

    ``candidates`` must already be in iteration-then-provider order, holding only
    the responses that actually have text. Mentions come first, then the rest, and
    every provider present keeps at least one slot.
    """
    mentioned = [candidate for candidate in candidates if candidate.mentioned]
    others = [candidate for candidate in candidates if not candidate.mentioned]
    ordered_candidates = mentioned + others
    selected = ordered_candidates[:limit]
    if limit <= 0:
        return []

    provider_order: list[str] = []
    for candidate in candidates:
        if candidate.provider not in provider_order:
            provider_order.append(candidate.provider)

    for provider in provider_order:
        if any(item.provider == provider for item in selected):
            continue
        replacement = next((item for item in ordered_candidates if item.provider == provider), None)
        if replacement is None:
            continue
        if len(selected) < limit:
            selected.append(replacement)
            continue

        provider_counts = {
            selected_item.provider: sum(1 for item in selected if item.provider == selected_item.provider)
            for selected_item in selected
        }
        replace_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if provider_counts[selected[index].provider] > 1 and not selected[index].mentioned
            ),
            None,
        )
        if replace_index is None:
            replace_index = next(
                (
                    index
                    for index in range(len(selected) - 1, -1, -1)
                    if provider_counts[selected[index].provider] > 1
                ),
                len(selected) - 1,
            )
        selected[replace_index] = replacement

    return selected[:limit]


_PROVIDER_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("gpt", "gpt_output", "gpt_domain_mention", "gpt_brand_mention"),
    ("gemini", "gem_output", "gem_domain_mention", "gem_brand_mention"),
    ("grok", "grok_output", "grok_domain_mention", "grok_brand_mention"),
)


def sentiment_refs_from_presence(
    rows: list[tuple[int, dict[str, bool], dict[str, bool]]],
) -> list[SentimentRef]:
    """Build the candidate list from per-provider text presence and mention flags.

    ``rows`` is ``(iteration_number, {provider: has_text}, {provider: mentioned})``
    — everything the selection needs, and nothing that requires reading a response.
    """
    candidates: list[SentimentRef] = []
    for iteration_number, has_text, mentioned in sorted(rows, key=lambda row: row[0]):
        for provider, _, _, _ in _PROVIDER_FIELDS:
            if has_text.get(provider):
                candidates.append(
                    SentimentRef(
                        provider=provider,
                        iteration_number=iteration_number,
                        mentioned=bool(mentioned.get(provider)),
                    )
                )
    return candidates


def select_sentiment_inputs(outputs: list[IterationLike], limit: int = 4) -> list[SentimentInput]:
    """Select sentiment inputs from iterations that already carry their text.

    Kept for callers holding full iterations in memory; it shares its selection
    with :func:`select_sentiment_refs`, so both paths choose identically.
    """
    texts: dict[tuple[str, int], str] = {}
    rows: list[tuple[int, dict[str, bool], dict[str, bool]]] = []
    for item in outputs:
        has_text: dict[str, bool] = {}
        mentioned: dict[str, bool] = {}
        for provider, text_field, domain_field, brand_field in _PROVIDER_FIELDS:
            text = getattr(item, text_field)
            has_text[provider] = bool(text)
            mentioned[provider] = bool(getattr(item, domain_field) or getattr(item, brand_field))
            if text:
                texts[(provider, item.iteration_number)] = text
        rows.append((item.iteration_number, has_text, mentioned))

    return [
        SentimentInput(
            provider=ref.provider,
            iteration_number=ref.iteration_number,
            text=texts[(ref.provider, ref.iteration_number)],
            mentioned=ref.mentioned,
        )
        for ref in select_sentiment_refs(sentiment_refs_from_presence(rows), limit=limit)
    ]


def drop_one_gpt_for_sentiment_retry(inputs: list[SentimentInput]) -> list[SentimentInput]:
    if len(inputs) <= 3:
        return list(inputs)

    reduced = list(inputs)
    for index, item in enumerate(reduced):
        if item.provider == "gpt":
            del reduced[index]
            return reduced[:3]
    return reduced[:3]
