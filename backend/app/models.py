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
    gpt_output: Mapped[typing.Optional[str]] = mapped_column(Text)
    gem_output: Mapped[typing.Optional[str]] = mapped_column(Text)
    grok_output: Mapped[typing.Optional[str]] = mapped_column(Text)
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
    clickup_list_id: Mapped[typing.Optional[str]] = mapped_column(Text)
    se_ranking_target: Mapped[typing.Optional[str]] = mapped_column(Text)
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


class Report(Base):
    __tablename__ = "Dashboard_ReportBuilder_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    period_label: Mapped[str] = mapped_column(Text, nullable=False)
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
