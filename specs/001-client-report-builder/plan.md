# Implementation Plan: Client Report Builder

**Branch**: `main` | **Date**: 2026-07-16 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-client-report-builder/spec.md`

## Summary

Add a new Dashboard page where a user selects an existing client (or creates one with a name + domain), picks any subset of a block catalog (the 14 baseline blocks from the existing `OnebyOne` report template, plus bar-chart variants for the two blocks that use a donut chart, plus an 8-variant AI-visibility block family sourced from this dashboard's own AI-check data), clicks **Generate Report** to pull current data for the selected blocks, adds a comment under each block, and clicks **Save** to persist the report against the client. A saved report can be reopened and its comments re-edited at any time, and exported as a self-contained client-ready HTML file. Technically this is additive to the existing FastAPI + SQLAlchemy/Postgres backend and the existing React/TypeScript single-page Dashboard frontend — new tables, new endpoints, and a new page component following the codebase's current conventions (no new frameworks).

## Technical Context

**Language/Version**: TypeScript (frontend, ES2020/strict via `tsconfig.app.json`); Python 3.9 (backend, per `npm run start`/`migrate` scripts)

**Primary Dependencies**: React 18 + Vite 5 + `@supabase/supabase-js` (frontend, no router library — pages are a manual `useState` union like the existing `Page` type in `src/App.tsx`); FastAPI + SQLAlchemy + Pydantic (backend, per `backend/app/api/routes.py`, `models.py`, `schemas.py`)

**Storage**: PostgreSQL via Supabase, same instance as the existing `Dashboard_AI_check_*` tables (`backend/app/models.py`); new tables follow the same `Dashboard_<Feature>_<table>` naming convention

**Testing**: `python -m unittest discover -s tests -v` (existing backend convention, see `tests/test_domain_logic.py` etc.); the frontend has no test harness today and this feature does not introduce one — verified manually per `quickstart.md`

**Target Platform**: Linux server behind Nginx + systemd (per `README.md`), browser clients (Chrome/Firefox/Safari/Edge)

**Project Type**: Web application (existing single frontend + single backend, not a monorepo split — see Project Structure below)

**Performance Goals**: Generating a full-catalog report (all ~24 blocks) completes in well under the 15-minute end-to-end budget from `SC-001`; individual block fetches should not block the others (a slow/failed source must not stall unrelated blocks per `FR-006`)

**Constraints**: Must reuse the existing Supabase-backed bearer-token auth (`backend/app/auth.py`), the existing FastAPI app/router structure, and the existing frontend's manual page-switching pattern — no new auth scheme, no new frontend framework or router library

**Scale/Scope**: Internal tool; tens of clients, a handful of concurrent staff users, each report holding up to ~24 block instances

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` in this repository is still the unfilled template (bracketed placeholders only) — no project constitution has been ratified. There are no principles or gates to check this plan against. **Gate result: PASS (no constitution in force).**

## Project Structure

### Documentation (this feature)

```text
specs/001-client-report-builder/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md         # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   └── report-builder-api.md
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

This repository does not use the templated `frontend/`+`backend/` monorepo split — it already has a single frontend at `src/` and a single backend at `backend/app/` at the repository root. This feature follows that existing layout rather than introducing a new one:

```text
src/
├── App.tsx                     # existing shell: sidebar nav, Page union, routes to page components
├── api.ts                      # existing apiRequest() helper — reused as-is
├── reportBuilder/               # NEW: this feature's frontend code
│   ├── ReportBuilderPage.tsx    # page component wired into App.tsx's Page union + nav
│   ├── blockCatalog.ts          # NEW: static catalog of selectable block types (display names, source, chart-style/AI-visibility variants) — mirrors backend registry
│   └── types.ts                 # NEW: Client / Report / ReportBlockInstance frontend types

backend/app/
├── models.py                   # add Client, Report, ReportBlockInstance ORM models
├── schemas.py                  # add request/response Pydantic models for this feature
├── report_builder/              # NEW: this feature's backend package
│   ├── block_catalog.py         # NEW: static registry of block types + their data-source resolver
│   ├── data_sources.py          # NEW: per-source fetch functions (Ahrefs, GA4/GSC sheet, ClickUp, SE Ranking, internal AI-visibility)
│   ├── service.py                # NEW: generate/save/list/export orchestration, mirrors run_service.py's style
│   └── export.py                 # NEW: builds the self-contained client-ready HTML file from a saved Report
├── api/routes.py                # add report-builder endpoints (see contracts/report-builder-api.md)
└── migrations/sql/               # add migration(s) for the new tables

tests/
└── test_report_builder.py       # NEW: unit tests for block_catalog, data_sources resolution, service orchestration, export — follows existing test_domain_logic.py style
```

**Structure Decision**: Extend the existing single-frontend (`src/`) + single-backend (`backend/app/`) layout in place. The new page is its own component under `src/reportBuilder/` (rather than growing the already-4500-line `App.tsx` further) and is wired into `App.tsx`'s existing `Page` union and sidebar `navItems`. The backend gets a new `backend/app/report_builder/` package mirroring the existing `run_service.py`/`domain.py` separation of orchestration vs. pure logic, plus new ORM models in the existing `models.py` and new routes appended to the existing `routes.py` router.

## Complexity Tracking

*No constitution is in force (see Constitution Check above), so there are no gate violations to justify. This section is intentionally empty.*
