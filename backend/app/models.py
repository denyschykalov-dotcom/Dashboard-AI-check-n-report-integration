from __future__ import annotations

import typing

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.db import Base
from backend.app.utils import utcnow


class Profile(Base):
    __tablename__ = "Dashboard_AI_check_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )


class Draft(Base):
    __tablename__ = "Dashboard_AI_check_drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    keyword: Mapped[typing.Optional[str]] = mapped_column(Text)
    domain: Mapped[typing.Optional[str]] = mapped_column(Text)
    brand: Mapped[typing.Optional[str]] = mapped_column(Text)
    prompt: Mapped[typing.Optional[str]] = mapped_column(Text)
    project: Mapped[typing.Optional[str]] = mapped_column(Text)
    rows_json: Mapped[typing.Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
    )


class Run(Base):
    __tablename__ = "Dashboard_AI_check_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    project: Mapped[typing.Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", server_default="queued")
    total_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    completed_iterations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error_messages: Mapped[typing.Optional[str]] = mapped_column(Text)
    # Total spend for this run, summed once when it reaches a terminal state.
    # Stored rather than derived so the Overview never has to read the raw output
    # rows (which carry the multi-KB LLM responses), and so the figure survives
    # cleanup_old_outputs() deleting those rows after the retention window.
    total_cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
    started_at: Mapped[typing.Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[typing.Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Output(Base):
    __tablename__ = "Dashboard_AI_check_outputs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    iteration_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # The raw model responses are the whole reason this table is large: multi-KB
    # each, three per iteration. They are kept for the record but are write-only
    # in practice, so they are deferred at the mapper level — no `select(Output)`
    # anywhere pulls them, and `deferred_raiseload` turns an accidental
    # ``row.gpt_output`` into an error instead of a silent extra query. The one
    # legitimate reader (building the final sentiment prompt) asks for the
    # specific columns it needs with a column-level select. Writing them is
    # unaffected.
    gpt_output: Mapped[typing.Optional[str]] = mapped_column(
        Text, deferred=True, deferred_raiseload=True
    )
    gem_output: Mapped[typing.Optional[str]] = mapped_column(
        Text, deferred=True, deferred_raiseload=True
    )
    grok_output: Mapped[typing.Optional[str]] = mapped_column(
        Text, deferred=True, deferred_raiseload=True
    )
    gpt_domain_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    gem_domain_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    grok_domain_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    gpt_brand_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    gem_brand_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    grok_brand_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    response_count: Mapped[typing.Optional[float]] = mapped_column(Float)
    brand_list: Mapped[typing.Optional[str]] = mapped_column(Text)
    citation_format: Mapped[typing.Optional[str]] = mapped_column(Text)
    openai_generation_cost_usd: Mapped[typing.Optional[float]] = mapped_column(Float)
    gemini_generation_cost_usd: Mapped[typing.Optional[float]] = mapped_column(Float)
    grok_generation_cost_usd: Mapped[typing.Optional[float]] = mapped_column(Float)
    gemini_analysis_cost_usd: Mapped[typing.Optional[float]] = mapped_column(Float)
    project: Mapped[typing.Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )


class Client(Base):
    __tablename__ = "Dashboard_ReportBuilder_clients"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    ga4_sheet_id: Mapped[typing.Optional[str]] = mapped_column(Text)
    # What the Apps Script collector pulls from when filling this client's sheet.
    # NULL ga4_property_id means "not configured" and the collector skips the
    # site; NULL gsc_property means "probe for it" (sc-domain: vs https:// form).
    ga4_property_id: Mapped[typing.Optional[str]] = mapped_column(Text)
    gsc_property: Mapped[typing.Optional[str]] = mapped_column(Text)
    clickup_list_id: Mapped[typing.Optional[str]] = mapped_column(Text)
    se_ranking_target: Mapped[typing.Optional[str]] = mapped_column(Text)
    # Which AI-check project the AI-visibility blocks read from. NULL falls back
    # to matching a project whose name equals this client's name.
    ai_visibility_project: Mapped[typing.Optional[str]] = mapped_column(Text)
    # Language this client's reports are delivered in ("en" | "uk"). Reports are
    # always built in English, then translated by Claude when this is not "en".
    report_language: Mapped[str] = mapped_column(
        Text, nullable=False, default="en", server_default="en"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )


class UserSettings(Base):
    __tablename__ = "Dashboard_ReportBuilder_user_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    clickup_token_encrypted: Mapped[typing.Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
    )


class ReportSelection(Base):
    """The last-used block selection and timeframe for a (user, client) pair, so
    reopening a client's report starting point restores the previous checkboxes.
    """

    __tablename__ = "Dashboard_ReportBuilder_selections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    block_keys: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    report_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="monthly", server_default="monthly"
    )
    date_from: Mapped[typing.Optional[str]] = mapped_column(Text)
    date_to: Mapped[typing.Optional[str]] = mapped_column(Text)
    # The last-used comparison preset key (e.g. "last_month_vs_prev"); None when
    # the specialist used the Advanced custom-range / full-year controls instead.
    comparison: Mapped[typing.Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
    )


class Report(Base):
    __tablename__ = "Dashboard_ReportBuilder_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    period_label: Mapped[str] = mapped_column(Text, nullable=False)
    # Which comparison the exported report opens on: "mom" or "yoy". Set from the
    # chosen comparison preset at generate time (defaults to "mom").
    # Comma-separated list of the comparisons the report offers ("mom", "yoy"),
    # the first being the one it opens on. Single values predate multi-select.
    default_comparison: Mapped[str] = mapped_column(
        String(32), nullable=False, default="mom", server_default="mom"
    )
    # JSON blob of report customization (accent, text style, per-block chart
    # variants, section visibility). None means "template defaults".
    customization: Mapped[typing.Optional[str]] = mapped_column(Text)
    generated_by: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=utcnow,
        onupdate=utcnow,
    )


class ReportBlock(Base):
    __tablename__ = "Dashboard_ReportBuilder_report_blocks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    block_type_key: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    data_json: Mapped[typing.Optional[str]] = mapped_column(Text)
    comment: Mapped[typing.Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok", server_default="ok")
    unavailable_reason: Mapped[typing.Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )


class RunResult(Base):
    __tablename__ = "Dashboard_AI_check_run_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project: Mapped[typing.Optional[str]] = mapped_column(Text)
    gpt_domain_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    gem_domain_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    grok_domain_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    gpt_brand_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    gem_brand_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    grok_brand_mention: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    response_count_avg: Mapped[typing.Optional[float]] = mapped_column(Float)
    brand_list: Mapped[typing.Optional[str]] = mapped_column(Text)
    citation_format: Mapped[typing.Optional[str]] = mapped_column(Text)
    sentiment_analysis: Mapped[typing.Optional[str]] = mapped_column(Text)
    gemini_sentiment_cost_usd: Mapped[typing.Optional[float]] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
