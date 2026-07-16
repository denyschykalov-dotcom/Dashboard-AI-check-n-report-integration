# Quickstart: Validating the Client Report Builder

This is a manual end-to-end validation guide (no frontend test harness exists in this repo — see `research.md` §10). Each scenario maps back to an acceptance scenario in `spec.md`.

## Prerequisites

- Local `.env` populated per the repo `README.md` (Supabase URL/keys at minimum; `OPENAI_API_KEY`/`GEMINI_API_KEY`/`GROK_API_KEY` only needed if you also want to exercise the existing AI-visibility feature to produce data for the AI-visibility blocks).
- Database migrated with this feature's new tables applied: `npm run migrate`.
- Backend running: `npm run start` (serves `/api` on `127.0.0.1:3001`).
- Frontend running: `npm run dev` equivalent for this repo (Vite dev server; proxies `/api` to the backend per `vite.config.ts`).
- Logged in as any authenticated Dashboard user (no admin requirement — `FR-001`).

## Scenario 1 — Generate and save a report for a new client (User Story 1, P1)

1. Open the Dashboard and navigate to the new **Report Builder** nav entry.
2. Choose "Create new client", enter a name and domain, submit — confirm the client is now selected and block selection is available (Acceptance Scenario 4).
3. Select a mix of blocks, including at least one baseline block, the bar-chart variant of a donut block, and one AI-visibility variant. Confirm at least one block is required — deselect everything and confirm **Generate Report** is blocked (Acceptance Scenario 7).
4. Re-select blocks and click **Generate Report**. Confirm every selected block renders — either populated with data, or clearly marked unavailable with a reason (never silently blank) (Acceptance Scenario 1; `SC-002`).
5. If both a donut and bar variant of the same data were selected, confirm both appear as independent blocks with matching underlying numbers (Acceptance Scenario 2).
6. If multiple AI-visibility variants were selected, confirm each is scoped to its own stated window/model (Acceptance Scenario 3).
7. Type a comment under at least one block, leave another blank, click **Save**. Confirm the save succeeds regardless of blank comments (Acceptance Scenario 6) and the report is now associated with the client (Acceptance Scenario 5).

## Scenario 2 — Reopen and edit a saved report (User Story 2, P2)

1. From the Report Builder, browse saved reports for the client used in Scenario 1. Confirm the list shows enough to identify the report (date/period/author) (Acceptance Scenario, User Story 2 #1).
2. Open it, edit one comment, click **Save** again.
3. Reload the page and reopen the same report. Confirm the edited comment persisted and no second/duplicate report was created for the same generation (Acceptance Scenario, User Story 2 #2; `SC-003`).

## Scenario 3 — Export a saved report (User Story 3, P3)

1. Open the report from Scenario 2 and request an export.
2. Confirm a downloadable HTML file is produced, and that opening it in a browser shows the same block data and comments as the in-dashboard view (Acceptance Scenario, User Story 3 #1).

## Scenario 4 — Source-unavailable handling (Edge Cases)

1. Generate a report for a client that has no `se_ranking_target`/`clickup_list_id`/`ga4_sheet_id` configured (a brand-new client, per Scenario 1, has none by default).
2. Select the corresponding blocks and generate. Confirm each renders as unavailable with a specific reason, and that unrelated selected blocks (e.g., a static or AI-visibility block) still populate normally in the same run.

## Backend unit tests

Run `python -m unittest discover -s tests -v` — `tests/test_report_builder.py` should cover: block-catalog contents/shape, per-source fetch resolution (mocked clients) including the "missing config ⇒ unavailable" path, AI-visibility windowed aggregation against seeded `Run`/`RunResult` rows, report save/update-in-place semantics, and export HTML generation from a stored report.
