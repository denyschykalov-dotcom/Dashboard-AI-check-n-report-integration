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


class GenerateReportRequest(BaseModel):
    client_id: uuid.UUID
    block_keys: list[str] = Field(default_factory=list)


class ReportBlockPayload(BaseModel):
    block_type_key: str
    status: str = "ok"
    data: typing.Optional[dict] = None
    comment: str = ""
    unavailable_reason: typing.Optional[str] = None


class ReportSaveRequest(BaseModel):
    client_id: uuid.UUID
    period_label: str = ""
    blocks: list[ReportBlockPayload] = Field(default_factory=list)


class ReportUpdateRequest(BaseModel):
    period_label: typing.Optional[str] = None
    blocks: list[ReportBlockPayload] = Field(default_factory=list)


class ClickUpTokenRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
