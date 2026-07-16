# Phase 1 Data Model: Client Report Builder

Entities correspond to the Key Entities in `spec.md`, refined with the integration decisions from `research.md`. Table names follow the existing `Dashboard_<Feature>_<table>` convention seen in `backend/app/models.py`.

## Client

New table: `Dashboard_ReportBuilder_clients`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `name` | text, required | Display name; also the matching key against the existing AI-visibility `project` field (research.md §3) |
| `domain` | text, required | Used to resolve Ahrefs/GSC blocks |
| `ga4_sheet_id` | text, nullable | Set outside this feature; `null` ⇒ GA4/GSC blocks render unavailable |
| `clickup_list_id` | text, nullable | Set outside this feature; `null` ⇒ work-completed/planned-works blocks render unavailable |
| `se_ranking_target` | text, nullable | Set outside this feature; `null` ⇒ SE Ranking block renders unavailable |
| `created_by` | UUID, required | `user_id` of whoever created the client (FR-010-style provenance) |
| `created_at` | timestamptz, required | |

**Validation rules**:
- `name` and `domain` are both required and non-empty (FR-002).
- `name` should be unique in practice (case-insensitive) so it lines up 1:1 with an AI-visibility `project` label, but this is a soft/UX-level rule, not enforced as a hard DB constraint, since the existing `project` field itself has no uniqueness constraint.

**Lifecycle**: Created once, read many times. No update/delete flows are in scope for this feature (editing a client's source config, if ever needed, is a separate concern per the spec's Assumptions).

## Report Block Type (catalog, not a table)

Defined in code (`backend/app/report_builder/block_catalog.py`), not persisted. Each entry has:

| Field | Notes |
|---|---|
| `key` | Stable identifier, e.g. `ahrefs_domain_analysis`, `ga4_session_mix_bar`, `ai_visibility_gpt_6mo` |
| `display_name` | e.g. "GA4 — Session mix by channel (bar chart)" |
| `source` | One of: `ahrefs`, `ga4_sheet`, `gsc_sheet`, `clickup`, `se_ranking`, `ai_visibility`, `static` (intro/header), `editorial` (search industry, summary) |
| `render_style` | `donut` \| `bar` \| `table` \| `text` \| `list` (drives frontend presentation only) |
| `ai_visibility_window` | `last_month` \| `last_6_months` \| `null` (only set for the AI-visibility family) |
| `ai_visibility_model` | `all` \| `gpt` \| `gemini` \| `grok` \| `null` (only set for the AI-visibility family) |

The catalog contains, per `spec.md` FR-003/003a/003b:
- 14 baseline entries (intro/header, search industry, Ahrefs domain analysis, Ahrefs top movers, GA4 summary, GA4 top landing pages, GA4 monetization, GA4 AI traffic, GSC summary, GSC top queries/pages, SE Ranking keywords, work completed, planned works, summary)
- 2 bar-chart variants (GA4 session mix by channel; GSC branded-vs-non-branded clicks)
- 8 AI-visibility variants (2 windows × 4 model scopes)

Total catalog size today: 24 entries. This is explicitly a floor, not a fixed count (spec Assumptions) — new entries are added by extending the registry.

## Report

New table: `Dashboard_ReportBuilder_reports`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `client_id` | UUID, FK → Client | |
| `period_label` | text, required | The reporting period the pulled data covers (e.g. "2026-06"), captured at generation time (FR-010) |
| `generated_by` | UUID, required | `user_id` who last generated/saved this report |
| `generated_at` | timestamptz, required | When **Generate Report** last populated this report's blocks |
| `created_at` | timestamptz, required | First save |
| `updated_at` | timestamptz, required | Last save (User Story 2 — editable anytime) |

**Validation rules**:
- Must have at least one associated `ReportBlockInstance` before it can be saved (FR-014 / edge case: Save disabled until generation happened).
- `client_id` must reference an existing Client (either pre-existing or just created in the same session per FR-002).

**Lifecycle**: `Generate Report` produces an in-memory/draft set of block instances; `Save` persists a `Report` row plus its block rows (create), or updates an existing `Report`'s block rows in place when reopened and re-saved (update) — never creates a duplicate for an edit-and-resave (FR-012, User Story 2). Saving a **new** report for a client that already has one for the same period is allowed and creates a distinct row (spec Assumptions — no dedup-by-period).

## Report Block Instance

New table: `Dashboard_ReportBuilder_report_blocks`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `report_id` | UUID, FK → Report | |
| `block_type_key` | text, required | Matches a `Report Block Type` catalog `key` |
| `data_json` | text (JSON-encoded), nullable | The fetched snapshot for this block at generation time; `null` when `status = unavailable` |
| `comment` | text, nullable | Free-form specialist note (FR-007/FR-008 — may be empty) |
| `status` | text, required | `ok` \| `unavailable` |
| `unavailable_reason` | text, nullable | e.g. "SE Ranking subscription expired", "Not configured for this client" — required when `status = unavailable` (FR-006) |

**Validation rules**:
- Exactly one row per selected `block_type_key` per `Report` (re-generating replaces the set for that report, per the edge case "changes selection and regenerates before saving").
- `data_json` and `unavailable_reason` are mutually exclusive in practice (one populated depending on `status`).

**Relationships**: `Report 1 —* Report Block Instance`; `Client 1 —* Report`; `Report Block Instance` references a `Report Block Type` catalog entry by key (not a DB foreign key, since the catalog isn't a table).

## Entity-relationship summary

```text
Client (1) ──── (many) Report ──── (many) Report Block Instance
                                          │
                                          └── block_type_key ──> Report Block Type (code catalog, not a table)
```
