# Tasks: Client Report Builder

**Input**: Design documents from `/specs/001-client-report-builder/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/report-builder-api.md, quickstart.md (all present)

**Tests**: Not explicitly requested in `spec.md`. Included anyway at a light-touch level (one shared `tests/test_report_builder.py`, extended per story) because `research.md` §10 and `plan.md`'s Project Structure already commit to it as the established, repo-wide convention (every existing backend module — `run_service.py`, `domain.py`, etc. — has a corresponding `unittest` file). No dedicated contract-test-per-endpoint or write-first TDD scaffolding is included, since that workflow was not requested.

**Organization**: Tasks are grouped by user story (P1/P2/P3 from `spec.md`) so each can be implemented and validated independently, per `quickstart.md`'s scenarios.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no unmet dependencies)
- **[Story]**: US1/US2/US3, only on user-story-phase tasks
- File paths are exact, relative to the repository root

## Path Conventions

This repo is a single frontend (`src/`) + single backend (`backend/app/`) at the repository root — see `plan.md`'s Project Structure. All paths below follow that existing layout; there is no `frontend/`/`backend/` monorepo split to adjust for.

---

## Phase 1: Setup

**Purpose**: Create the empty scaffolding this feature's code will live in. No new dependencies are needed — `plan.md`'s Technical Context confirms the existing React/Vite/FastAPI/SQLAlchemy stack covers everything.

- [X] T001 [P] Create backend package skeleton: `backend/app/report_builder/__init__.py` and `backend/app/report_builder/data_sources/__init__.py` (empty packages, no logic yet)
- [X] T002 [P] Create frontend feature folder skeleton: `src/reportBuilder/types.ts`, `src/reportBuilder/blockCatalog.ts`, `src/reportBuilder/ReportBuilderPage.tsx` (placeholder exports only)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared entities, catalog data, schemas, and page wiring that every user story builds on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Add `Client`, `Report`, `ReportBlockInstance` ORM models to `backend/app/models.py` per `data-model.md` (tables `Dashboard_ReportBuilder_clients`, `Dashboard_ReportBuilder_reports`, `Dashboard_ReportBuilder_report_blocks`)
- [X] T004 Add a SQL migration for the three new tables in `backend/app/migrations/sql/` following the existing migration file naming/ordering convention (depends on T003)
- [X] T005 [P] Implement the static block-type catalog registry in `backend/app/report_builder/block_catalog.py` per `data-model.md` § Report Block Type — all 24 entries (14 baseline + 2 bar-chart variants for the GA4 session-mix and GSC branded-clicks donuts + 8 AI-visibility variants across `{last_month, last_6_months} × {all, gpt, gemini, grok}`), each with `key`, `display_name`, `source`, `render_style`, and (for AI-visibility entries) `ai_visibility_window`/`ai_visibility_model`
- [X] T006 [P] Mirror the catalog for display purposes in `src/reportBuilder/blockCatalog.ts` (static array matching the backend keys/display names/render styles from T005, used only for local UI grouping — the authoritative list is still fetched from the API)
- [X] T007 [P] Define shared frontend types in `src/reportBuilder/types.ts` (`Client`, `ReportBlockType`, `GeneratedBlock`, `Report`, `ReportBlockInstance`) matching the response shapes in `contracts/report-builder-api.md`
- [X] T008 Add a `"reportBuilder"` member to the `Page` union type and a new entry to `navItems` in `src/App.tsx`, following the existing pattern used for `"overview" | "service" | "outputs" | "history"`
- [X] T009 Create the `ReportBuilderPage` shell component in `src/reportBuilder/ReportBuilderPage.tsx` (renders a placeholder for now) and route to it from `src/App.tsx`'s page switch when `page === "reportBuilder"` (depends on T008)
- [X] T010 [P] Add new Pydantic request/response schemas to `backend/app/schemas.py` per `contracts/report-builder-api.md`: `ClientCreateRequest`, `ClientResponse`, `GenerateReportRequest`, `GenerateReportResponse`, `ReportSaveRequest`, `ReportResponse`, `ReportSummaryResponse`, `ReportDetailResponse`, `BlockCatalogResponse`

**Checkpoint**: Foundation ready — database schema, catalog data, shared types, and page routing all exist. User story implementation can now begin.

---

## Phase 3: User Story 1 - Generate and save a client report (Priority: P1) 🎯 MVP

**Goal**: A user can select or create a client, pick any subset of the block catalog, click **Generate Report** to populate those blocks from their sources (or see a clear unavailable reason), add per-block comments, and click **Save** to persist the report.

**Independent Test**: Per `spec.md` User Story 1 — pick a client, select a subset of blocks, generate, comment, save; confirm the saved report contains exactly the selected blocks' data and comments. Validated end-to-end via `quickstart.md` Scenario 1.

### Implementation for User Story 1

- [X] T011 [US1] Implement `GET /api/report-builder/block-catalog` in `backend/app/api/routes.py`, returning the registry from T005 (depends on T005, T010)
- [X] T012 [US1] Implement `GET /api/report-builder/clients` and `POST /api/report-builder/clients` in `backend/app/api/routes.py`, backed by list/create logic in `backend/app/report_builder/service.py` (depends on T003, T010)
- [X] T013 [P] [US1] Implement the static/editorial data-source resolver in `backend/app/report_builder/data_sources/static_editorial.py` (intro/header from client meta; hard-coded search-industry starter items; auto-generated, editable summary text)
- [X] T014 [P] [US1] Implement the Ahrefs data-source resolver in `backend/app/report_builder/data_sources/ahrefs.py` (domain analysis + top movers blocks; resolves via `Client.domain`; returns `unavailable` with a reason on request failure)
- [X] T015 [P] [US1] Implement the GA4 sheet data-source resolver in `backend/app/report_builder/data_sources/ga4.py` (summary, top landing pages, monetization, AI traffic, and both the donut and bar variants of session-mix-by-channel; resolves via `Client.ga4_sheet_id`; `unavailable` when null/unreachable)
- [X] T016 [P] [US1] Implement the GSC sheet data-source resolver in `backend/app/report_builder/data_sources/gsc.py` (summary, top queries/pages, and both the donut and bar variants of branded-vs-non-branded clicks; resolves via `Client.ga4_sheet_id`; `unavailable` when null/unreachable)
- [X] T017 [P] [US1] Implement the ClickUp data-source resolver in `backend/app/report_builder/data_sources/clickup.py` (work completed, planned works; resolves via `Client.clickup_list_id`; `unavailable` when null/unreachable)
- [X] T018 [P] [US1] Implement the SE Ranking data-source resolver in `backend/app/report_builder/data_sources/se_ranking.py` (tracked keywords; resolves via `Client.se_ranking_target`; `unavailable` when null or subscription expired)
- [X] T019 [P] [US1] Implement the AI-visibility data-source resolver in `backend/app/report_builder/data_sources/ai_visibility.py`, matching `Client.name` to `Run.project` and aggregating `RunResult` across all users for the `last_month`/`last_6_months` windows and `all`/`gpt`/`gemini`/`grok` model scopes, per `research.md` §4 (depends on T003)
- [X] T020 [US1] Implement generate orchestration in `backend/app/report_builder/service.py`: given `client_id` + `block_keys`, look up each key in the T005 catalog and dispatch to the matching T013–T019 resolver, catching per-block failures so one failing block never blocks the others (depends on T013, T014, T015, T016, T017, T018, T019)
- [X] T021 [US1] Implement `POST /api/report-builder/generate` in `backend/app/api/routes.py`, calling the T020 orchestration and returning the per-block `ok`/`unavailable` results (depends on T020)
- [X] T022 [US1] Implement `POST /api/report-builder/reports` (save) in `backend/app/api/routes.py`, backed by `backend/app/report_builder/service.py` logic that persists a `Report` row plus one `ReportBlockInstance` row per submitted block (data/comment/status exactly as submitted, no re-fetching) (depends on T021)
- [X] T023 [US1] Build the client-selection UI (pick an existing client or create one via name+domain) in `src/reportBuilder/ReportBuilderPage.tsx`, calling the T012 endpoints (depends on T009, T012)
- [X] T024 [US1] Build the block-selection UI (catalog checkboxes grouped by source, using T006/T011, with an at-least-one-selected validation before enabling Generate) in `src/reportBuilder/ReportBuilderPage.tsx` (depends on T023, T011)
- [X] T025 [US1] Build the Generate Report action and per-block rendering (populated view, or a clear unavailable-with-reason state) in `src/reportBuilder/ReportBuilderPage.tsx`, calling the T021 endpoint (depends on T024)
- [X] T026 [US1] Add an optional, per-block comment field under each rendered block (blank is allowed) in `src/reportBuilder/ReportBuilderPage.tsx` (depends on T025)
- [X] T027 [US1] Add the Save action (disabled until a report has been generated in this session) calling the T022 endpoint in `src/reportBuilder/ReportBuilderPage.tsx` (depends on T026, T022)
- [X] T028 [US1] Add unit tests in `tests/test_report_builder.py` for the T005 catalog's shape/contents, each T013–T019 resolver (mocked external clients, including the "missing per-client config ⇒ unavailable" path), and the T020 generate/save orchestration (depends on T005, T013, T014, T015, T016, T017, T018, T019, T020, T022)

**Checkpoint**: User Story 1 is fully functional and independently testable — `quickstart.md` Scenario 1 and Scenario 4 (source-unavailable handling) should both pass.

---

## Phase 4: User Story 2 - Reopen and edit a saved report (Priority: P2)

**Goal**: A user can browse previously saved reports for a client, reopen one, edit a comment, and save the change back to the same report without creating a duplicate.

**Independent Test**: Per `spec.md` User Story 2 — save a report, reopen it, change a comment, save again; confirm the same report record reflects the change. Validated end-to-end via `quickstart.md` Scenario 2.

### Implementation for User Story 2

- [X] T029 [US2] Implement `GET /api/report-builder/clients/{client_id}/reports` (list) in `backend/app/api/routes.py` + `backend/app/report_builder/service.py` (depends on T022)
- [X] T030 [US2] Implement `GET /api/report-builder/reports/{report_id}` (detail) in `backend/app/api/routes.py` + `backend/app/report_builder/service.py` (depends on T022)
- [X] T031 [US2] Implement `PUT /api/report-builder/reports/{report_id}` in `backend/app/api/routes.py` + `backend/app/report_builder/service.py`, replacing the report's block rows and comments in place — never creating a new `Report` row for an edit (depends on T030)
- [X] T032 [US2] Build the "saved reports for this client" list UI (period/author/date, per `FR-011`) in `src/reportBuilder/ReportBuilderPage.tsx`, calling T029 (depends on T023, T029)
- [X] T033 [US2] Build the "reopen report" flow, loading a saved report's blocks/comments via T030 into the same generate/comment view built in T025/T026 (depends on T025, T030)
- [X] T034 [US2] Wire the Save action to call `PUT` (T031) instead of `POST` when editing an already-saved (vs. freshly generated) report, updating the same record (depends on T027, T031, T033)
- [X] T035 [US2] Add unit tests in `tests/test_report_builder.py` for the list/detail/update-in-place service logic, including a case asserting no duplicate `Report` row is created on re-save (depends on T029, T030, T031)

**Checkpoint**: User Stories 1 and 2 both work independently — a report can be built, saved, reopened, corrected, and re-saved.

---

## Phase 5: User Story 3 - Export a saved report for the client (Priority: P3)

**Goal**: A user can open a saved report and download it as a self-contained, client-ready HTML file.

**Independent Test**: Per `spec.md` User Story 3 — save a report, request an export, confirm a downloadable file containing all blocks/comments is produced. Validated end-to-end via `quickstart.md` Scenario 3.

### Implementation for User Story 3

- [X] T036 [US3] Implement the server-side export builder in `backend/app/report_builder/export.py`, rendering a saved report's stored blocks and comments into a single self-contained HTML file (embedded data, inline styles, no external requests) per `research.md` §9 (depends on T022)
- [X] T037 [US3] Implement `GET /api/report-builder/reports/{report_id}/export` in `backend/app/api/routes.py` (file-download response, `Content-Disposition: attachment`) using T036 (depends on T036, T030)
- [X] T038 [US3] Add an Export action to the reopened-report view in `src/reportBuilder/ReportBuilderPage.tsx`, triggering a download from the T037 endpoint (depends on T033, T037)
- [X] T039 [US3] Add unit tests in `tests/test_report_builder.py` for T036's HTML generation from a stored-report fixture (asserts the output is self-contained and includes each block's data and comment) (depends on T036)

**Checkpoint**: All three user stories are independently functional — the full generate → comment → save → reopen → export lifecycle works.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Quality pass across all three stories.

- [X] T040 [P] Add error/loading-state polish across the Report Builder UI (network failures, in-flight indicators for Generate/Save/Export) in `src/reportBuilder/ReportBuilderPage.tsx`
- [X] T041 [P] Add structured logging for generate/save/export operations, following the existing `backend/app/logging_config.py` conventions, in `backend/app/report_builder/service.py` and `export.py`
- [ ] T042 Run all four `quickstart.md` scenarios end-to-end manually and record results
- [X] T043 Run `python -m unittest discover -s tests -v` and fix any failures

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **User Story 1 (Phase 3)**: Depends on Foundational only
- **User Story 2 (Phase 4)**: Depends on Foundational; also depends on US1's `POST /reports` (T022) and generate/comment UI (T025/T026/T027) existing to reopen and re-save into
- **User Story 3 (Phase 5)**: Depends on Foundational; also depends on US1's save endpoint (T022) and US2's reopen flow (T033) as the place the Export action lives
- **Polish (Phase 6)**: Depends on all three user stories being complete

Note: unlike a fully decoupled feature, US2 and US3 here are additive layers on the same saved-report record and the same page component rather than fully parallel tracks — each still has its own independently testable slice (list/reopen/re-save for US2; export for US3), but the frontend work naturally sequences after US1's generate/comment view exists.

### Within Each User Story

- Data-source resolvers before the orchestration that calls them
- Orchestration/service logic before the endpoint that exposes it
- Backend endpoint before the frontend UI that calls it
- Story implementation before that story's tests

### Parallel Opportunities

- T001 and T002 (Setup) — different files
- T005, T006, T007, T010 (Foundational) — different files, no interdependencies
- T013–T019 (US1 data-source resolvers) — seven different files, none depend on each other
- T040 and T041 (Polish) — different files

All other tasks touch a file another task in the same phase also touches (`backend/app/api/routes.py`, `src/reportBuilder/ReportBuilderPage.tsx`, or `tests/test_report_builder.py`) or have a direct dependency, so they are listed sequentially rather than marked `[P]`.

---

## Parallel Example: User Story 1 data sources

```bash
# After T005 (catalog) and T003 (Client model) exist, launch all seven resolvers together:
Task: "Implement static/editorial resolver in backend/app/report_builder/data_sources/static_editorial.py"
Task: "Implement Ahrefs resolver in backend/app/report_builder/data_sources/ahrefs.py"
Task: "Implement GA4 sheet resolver in backend/app/report_builder/data_sources/ga4.py"
Task: "Implement GSC sheet resolver in backend/app/report_builder/data_sources/gsc.py"
Task: "Implement ClickUp resolver in backend/app/report_builder/data_sources/clickup.py"
Task: "Implement SE Ranking resolver in backend/app/report_builder/data_sources/se_ranking.py"
Task: "Implement AI-visibility resolver in backend/app/report_builder/data_sources/ai_visibility.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (blocks everything else)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: run `quickstart.md` Scenario 1 (and Scenario 4 for the unavailable-source path)
5. This alone delivers the core value: build, comment on, and save a client report — replacing the manual script pipeline described in `README~1.MD`

### Incremental Delivery

1. Setup + Foundational → schema, catalog, and page shell exist
2. Add User Story 1 → validate → this is the MVP
3. Add User Story 2 → validate → reports can now be corrected without starting over
4. Add User Story 3 → validate → reports can now actually be handed to the client
5. Polish

### Notes

- `[P]` tasks touch different files and have no unmet same-phase dependency
- `[Story]` labels trace every Phase 3+ task back to its user story
- Because Phases 3–5 share one page component and one save endpoint, commit after each task/checkpoint rather than trying to fully parallelize across stories with multiple developers — the realistic parallelism here is *within* Phase 3 (the seven data-source resolvers), not across stories
- Verify `python -m unittest discover -s tests -v` stays green after each backend task
