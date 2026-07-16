# Phase 0 Research: Client Report Builder

All items below were resolved by reading the existing codebase (`src/App.tsx`, `backend/app/models.py`, `backend/app/run_service.py`, `backend/app/domain.py`, `backend/app/api/routes.py`, `README.md`, `README~1.MD`, `ONEBYO~2.HTM`) rather than external research — this is an integration into a fully-formed existing system, not a greenfield technology choice. No `NEEDS CLARIFICATION` markers remain in the Technical Context.

## 1. Where the new page lives in the frontend

- **Decision**: New page as its own component (`src/reportBuilder/ReportBuilderPage.tsx`), added as a new member of the existing `Page` union type and a new entry in `navItems` in `src/App.tsx`, following the exact pattern already used for `"overview" | "service" | "outputs" | "history"`.
- **Rationale**: `src/App.tsx` is already ~4,560 lines with no router library; the codebase's existing convention is a manual `Page` union + `setPage()`. Matching that convention (rather than introducing a router) keeps the change minimal and consistent. Splitting the new page into its own file avoids growing the monolith further while integrating through the same nav/page-switch mechanism everything else already uses.
- **Alternatives considered**: Introduce a client-side router library — rejected, no router exists today and the feature doesn't need deep-linking; adding one page's worth of value doesn't justify the dependency. Inlining the new page directly into `App.tsx` — rejected, would push an already-large file past 5,000 lines for no benefit.

## 2. Access control

- **Decision**: No new role/permission. The new nav entry and its endpoints are available to any authenticated user, matching `FR-001` and the spec's Assumptions.
- **Rationale**: Confirmed directly with the user during `/speckit-specify`.
- **Alternatives considered**: Admin-only gating (like `service`/`outputs` are hidden from admins today) — rejected per explicit user decision.

## 3. Client entity and how it relates to the existing "project" concept

- **Decision**: A new `Client` entity (name + domain) is introduced. It is a distinct concept from the free-text `project` field already used by the AI-visibility feature (`Run.project`, `RunResult.project`). To pull AI-visibility data into a report, the Client's `name` is matched against the existing `project` field (case-insensitive exact match) — i.e., staff are expected to name a Client the same as the `project` label they already use when running AI-visibility checks for that client.
- **Rationale**: The existing AI-visibility feature has no formal "client" concept — `project` is a free-text tag on `Run`/`RunResult`, scoped per-user unless viewed through the admin's cross-user overview (`run_service.get_overview_summary`). Introducing a second, disconnected identifier would make it impossible to line up a report's AI-visibility blocks with existing AI-visibility history. Matching on name is the simplest bridge that requires no migration of historical data and no change to the existing AI-visibility feature.
- **Alternatives considered**: Add a formal `client_id` foreign key to `Run`/`RunResult` — rejected as out of scope; it would require touching the existing, independently-shipped AI-visibility feature and backfilling historical rows, which is a much larger change than this feature's spec calls for. Require the user to manually pick which `project` string(s) map to a Client — deferred; name-matching is the reasonable default per the spec's Assumptions, and can be revisited if it proves insufficient in practice.

## 4. AI-visibility block data aggregation

- **Decision**: Each of the 8 AI-visibility block variants (2 windows × {all models, GPT, Gemini, Grok}) aggregates `RunResult` rows (joined to `Run` for `project`/`created_at`) where `Run.project` matches the Client's name, across **all users** (not scoped to the current user), for the selected window. "All models" sums/combines the three per-model mention flags; a specific-model variant reads only that model's `*_domain_mention`/`*_brand_mention` flags. This reuses the same filtering/windowing helpers `run_service.py` already has for the admin overview (`_filter_rows_since`, `_build_monthly_overview`) rather than duplicating that logic.
- **Rationale**: A client's reported AI visibility shouldn't depend on which staff member happened to run the checks, so cross-user aggregation (like the existing admin overview) is the correct default — the same reasoning the existing admin overview already encodes. Reusing the existing helpers avoids a second, divergent implementation of "windowed AI-visibility stats."
- **Alternatives considered**: Scope to only the report-builder user's own runs — rejected, would silently under-report visibility data created by colleagues for the same client.

## 5. Block-type catalog representation

- **Decision**: The block-type catalog (14 baseline + 2 bar-chart variants + 8 AI-visibility variants) is a static, in-code registry (`backend/app/report_builder/block_catalog.py`, mirrored by `src/reportBuilder/blockCatalog.ts` for display purposes only) — not a database table.
- **Rationale**: The catalog is not user-editable content; it changes only when a developer adds a new block type, which is a code change either way. A DB table would add migration/seeding overhead for zero behavioral benefit (YAGNI).
- **Alternatives considered**: DB-backed catalog table — rejected as unnecessary; would need to stay hand-in-sync with the fetch-function registry anyway, so the code registry is the single source of truth.

## 6. Chart-style variants (pie vs. bar)

- **Decision**: The two donut-chart baseline blocks (GA4 session-mix-by-channel; GSC branded-vs-non-branded clicks) and their new bar-chart counterparts share one data-fetch function each; the catalog entry only differs by a `render_style: "donut" | "bar"` field consumed by the frontend chart renderer.
- **Rationale**: The underlying dataset is identical (per `README~1.MD` §4's `channels`/`branded` schema) — only the visualization differs. Sharing the fetch avoids duplicating source-integration code for what is purely a presentation choice.
- **Alternatives considered**: Separate fetch implementations per chart style — rejected, pure duplication.

## 7. Per-client data-source configuration (Ahrefs, GA4/GSC sheet, ClickUp, SE Ranking)

- **Decision**: `Client` carries optional, nullable configuration fields for each external source that needs more than a domain to be located (`ga4_sheet_id`, `clickup_list_id`, `se_ranking_target`) — Ahrefs and GSC blocks resolve from the domain alone. If a selected block's required configuration is missing for a given Client, the block is generated as **unavailable** with a "not configured for this client" reason, using the same unavailable-state handling `FR-006` already requires for live source failures. Populating these fields is a separate, outside-this-feature setup step (per the spec's Assumptions) — this feature only reads them.
- **Rationale**: The spec explicitly puts source *setup* out of scope, but the feature still needs *somewhere* to read a per-client source identifier from, or every block would be permanently unavailable for every client. Reusing the existing "unavailable" UX for "not configured" avoids inventing a second error state.
- **Alternatives considered**: A separate source-configuration table/service — rejected as over-engineering for what is currently just a handful of per-client identifiers; can be split out later if source configuration grows more complex.

## 8. Report / report-block persistence

- **Decision**: Two new tables: `Dashboard_ReportBuilder_reports` (one row per saved report: client, period, generated_by, generated_at, saved_at, updated_at) and `Dashboard_ReportBuilder_report_blocks` (one row per included block instance: report_id, block_type key, data snapshot as JSON text, comment text, status `ok`/`unavailable`, unavailable_reason).
- **Rationale**: Matches the existing schema style (`Dashboard_AI_check_outputs` stores per-iteration data as text/JSON-ish columns; `Dashboard_AI_check_drafts.rows_json` already stores a JSON blob as `Text`). Keeping the block snapshot as an opaque JSON blob (rather than a fully normalized per-block-type schema) keeps the table shape stable even as new block types are added to the catalog.
- **Alternatives considered**: One JSON column on `reports` holding all blocks — rejected, would make "list previously saved reports" (`FR-011`) and per-block editing (`FR-012`) awkward; a row-per-block table is closer to how the UI and requirements are structured.

## 9. Export format

- **Decision**: The backend generates the downloadable client-ready file server-side (`backend/app/report_builder/export.py`), producing a single self-contained HTML file (embedded data, inline styles, no external requests) — directly analogous to the existing hand-built `builder_v2.py` → `OnebyOne-SEO-Report-*.html` pipeline described in `README~1.MD`.
- **Rationale**: This repository already has a working, proven precedent for "data + comments → single-file client HTML" in `builder_v2.py`. Generating server-side from the same stored snapshot used for "reopen and edit" means the export always reflects exactly what was saved (no separate client-side re-render path to keep in sync), and keeps a single Python code path testable with the existing `unittest` conventions.
- **Alternatives considered**: Client-side export (serialize the already-rendered DOM, like the current template's `saveReport()` does) — rejected for this integration; the report can be reopened for editing days later in a fresh page load, so the frontend would need to re-fetch and re-render the exact snapshot anyway before it could serialize it, which is more code than doing it once, server-side, from the stored data.

## 10. Testing approach

- **Decision**: Backend logic (block-catalog resolution, per-source fetch functions with mocked clients, AI-visibility aggregation, report/report-block CRUD, export generation) gets unit tests under `tests/test_report_builder.py`, run via the existing `python -m unittest discover -s tests -v`. No new frontend test tooling is introduced; the new page is verified manually per `quickstart.md`.
- **Rationale**: Matches the existing project's actual testing footprint exactly (backend has `unittest`, frontend has none) — introducing a frontend test framework for one page would be scope creep beyond what this feature needs.
- **Alternatives considered**: Add a frontend test framework (Vitest/RTL) — rejected as out of scope; can be proposed separately if the team wants frontend test coverage project-wide.
