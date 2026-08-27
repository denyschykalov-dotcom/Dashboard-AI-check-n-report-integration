from __future__ import annotations

import typing

import uuid

from pydantic import BaseModel, Field


class ProfileUpsertRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)


class DraftRowPayload(BaseModel):
    keyword: str = ""
    domain: str = ""
    brand: str = ""
    prompt: str = ""
    project: str = ""


class DraftPayload(BaseModel):
    keyword: str = ""
    domain: str = ""
    brand: str = ""
    prompt: str = ""
    project: str = ""
    rows: list[DraftRowPayload] = Field(default_factory=list)


class DraftAppendPayload(BaseModel):
    rows: list[DraftRowPayload] = Field(default_factory=list)


class RunStartRequest(BaseModel):
    keyword: str
    domain: str
    brand: str
    prompt: str
    project: str = ""


class BulkRunActionResponse(BaseModel):
    run_ids: list[str] = Field(default_factory=list)
    total_runs: int = 0
    status: str


class HistoryForwardRequest(BaseModel):
    run_ids: list[uuid.UUID] = Field(min_length=1)
    target_user_id: uuid.UUID


class HistoryForwardResponse(BaseModel):
    run_ids: list[str] = Field(default_factory=list)
    total_runs: int = 0
    outputs_updated: int = 0
    results_updated: int = 0
    target_user_id: str


# --- Report Builder ----------------------------------------------------------


class ClientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=200)
    # Language this client's reports are delivered in ("en" | "uk"). Unknown
    # values fall back to English rather than failing the request.
    report_language: str = "en"


class ClientLanguageRequest(BaseModel):
    """Change the language a client's reports are delivered in."""

    report_language: str = Field(min_length=2, max_length=10)


class ClientSettingsRequest(BaseModel):
    """Per-client links the data sources need.

    Each field is optional so they can be set independently; an empty string
    clears one (SE Ranking back to "not configured", AI visibility back to
    matching on the client's name).
    """

    se_ranking_target: typing.Optional[str] = Field(default=None, max_length=200)
    ai_visibility_project: typing.Optional[str] = Field(default=None, max_length=200)
    # A bare sheet id or a pasted Google Sheets URL; empty restores auto-lookup.
    ga4_sheet_id: typing.Optional[str] = Field(default=None, max_length=300)
    # What the Apps Script collector reads. A bare GA4 property id, or a pasted
    # "properties/123" / GA4 URL. Empty clears it and the collector skips the site.
    ga4_property_id: typing.Optional[str] = Field(default=None, max_length=100)
    # e.g. "sc-domain:example.com" or "https://example.com/". Empty makes the
    # collector probe both forms and keep whichever returns data.
    gsc_property: typing.Optional[str] = Field(default=None, max_length=300)


class GenerateReportRequest(BaseModel):
    client_id: uuid.UUID
    block_keys: list[str] = Field(default_factory=list)
    # The reporting period preset ("last_month" | "last_3_months"). When set it
    # drives the reporting window and overrides report_type/date_from/date_to
    # (the Advanced custom-range path).
    period_preset: typing.Optional[str] = None
    # The comparisons the report should offer ("mom" and/or "yoy"); each becomes a
    # toggle in the exported report and the first is the one it opens on.
    comparisons: list[str] = Field(default_factory=list)
    # Legacy single-choice comparison preset ("last_month_vs_prev" |
    # "last_month_vs_year" | "last_3_months_vs_year"), honoured when no
    # period_preset is given.
    comparison: typing.Optional[str] = None
    # Optional reporting range. Omit for the default latest-month report.
    report_type: str = "monthly"  # "monthly" | "yearly"
    date_from: typing.Optional[str] = None  # YYYY-MM-DD or YYYY-MM
    date_to: typing.Optional[str] = None
    # Planned-work source: pull the ClickUp "Todo" tasks, or a manually typed plan.
    planned_work_mode: str = "clickup"  # "clickup" | "manual"
    planned_work_text: str = ""


class SelectionSaveRequest(BaseModel):
    block_keys: list[str] = Field(default_factory=list)
    period_preset: typing.Optional[str] = None
    comparisons: list[str] = Field(default_factory=list)
    comparison: typing.Optional[str] = None  # legacy single-choice preset key
    report_type: str = "monthly"
    date_from: typing.Optional[str] = None
    date_to: typing.Optional[str] = None


class ReportBlockPayload(BaseModel):
    block_type_key: str
    status: str = "ok"
    data: typing.Optional[dict] = None
    comment: str = ""
    unavailable_reason: typing.Optional[str] = None


class ReportSaveRequest(BaseModel):
    client_id: uuid.UUID
    period_label: str = ""
    default_comparison: str = "mom"  # "mom" | "yoy"
    customization: typing.Optional[dict] = None
    blocks: list[ReportBlockPayload] = Field(default_factory=list)


class ReportUpdateRequest(BaseModel):
    period_label: typing.Optional[str] = None
    default_comparison: typing.Optional[str] = None
    customization: typing.Optional[dict] = None
    blocks: list[ReportBlockPayload] = Field(default_factory=list)


class ReportPreviewRequest(BaseModel):
    """Render a live report preview from unsaved blocks + customization."""

    client_id: uuid.UUID
    period_label: str = ""
    default_comparison: str = "mom"
    customization: typing.Optional[dict] = None
    blocks: list[ReportBlockPayload] = Field(default_factory=list)


class ReportAiRequest(BaseModel):
    """A report in progress, sent to Claude for commentary.

    Same payload as the preview request: the unsaved blocks plus enough meta for
    the model to know what period it is writing about.
    """

    client_id: uuid.UUID
    period_label: str = ""
    default_comparison: str = "mom"
    blocks: list[ReportBlockPayload] = Field(default_factory=list)
    # Only used by the summary call: a summary the specialist already drafted, to
    # be polished rather than replaced.
    existing_summary: str = ""
    # Only used by the summary call: an optional specialist instruction for this
    # regeneration (e.g. "focus more on the YoY traffic gain").
    summary_guidance: str = ""


class ClickUpTokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
