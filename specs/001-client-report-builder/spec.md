# Feature Specification: Client Report Builder

**Feature Branch**: `001-client-report-builder`

**Created**: 2026-07-16

**Status**: Draft

**Input**: User description: "you will see the ready project in the working folder, it is the thing Where we will integrate the feature. The feature is described in this 2 files \"ONEBYO~2.HTM\" and \"README~1.MD\" It meant to show what needs to be created but I will specify a bit more. It have to be a new page in the Dashboard(initial project) where user will firstly choose 14 blocks with data as in the files (the data will be taken from different sources) then after click the button \"Generate Report\" it takes all selected data from sources and than user have to be able to write a comment under each block and after that a button \"Save\" that will save an already made Report for the client"

## Clarifications

### Session 2026-07-16

- Q: When a user creates a brand-new client from this page (instead of picking an existing one), what should be required at creation time? → A: Name + domain only — the minimum needed to identify the client and later match it against domain-based sources like Ahrefs/GSC; any source-specific setup (GA4 property, ClickUp list, etc.) happens separately, outside this feature.
- Q: How should the dashboard's own AI-visibility data (brand/domain mention checks) be incorporated into the report blocks? → A: The catalog of selectable blocks is not capped at 14 — the 14 from the source template are only the baseline set. It must include more block options: (a) wherever a block's data is presented as a circle/pie chart, an equivalent bar-chart variant of the same underlying data must also exist as a separate selectable block, and (b) AI visibility must appear as multiple distinct blocks rather than one, not merged into the existing GA4 AI Traffic block or the Summary block.
- Q: For the AI-visibility block family, which time windows and model scopes should be offered as selectable block variants? → A: Two time windows — last month and last 6 months — each combined with a scope of either all AI models combined or one specific model (GPT, Gemini, or Grok). This yields up to 8 distinct AI-visibility blocks (2 windows × (1 all-models + 3 per-model)), each independently selectable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Generate and save a client report (Priority: P1)

A dashboard user opens the new report-builder page, picks the client the report is for, chooses which of the available report blocks to include — from a catalog that covers the 14 baseline blocks plus additional chart-style and AI-visibility variants — clicks **Generate Report** to pull in current data for those blocks from their respective sources, adds a short comment under each block, and clicks **Save** to store the finished report against that client.

**Why this priority**: This is the entire point of the feature — without it, nothing else matters. It replaces today's manual, multi-script process of assembling a client report by hand.

**Independent Test**: Can be fully tested by picking a client, selecting a subset of blocks, generating, commenting, and saving — and confirming the saved report contains the selected blocks' data and comments, delivering a usable report on its own.

**Acceptance Scenarios**:

1. **Given** a user on the report-builder page, **When** they select a client and a subset of the available blocks and click Generate Report, **Then** each selected block is populated with current data from its associated source and no unselected block appears in the report.
2. **Given** a user selects both the pie-chart and bar-chart variant of the same underlying data (e.g., session mix by channel), **When** they click Generate Report, **Then** both variants are populated side by side as distinct blocks in the report.
3. **Given** a user selects more than one AI-visibility block variant (e.g., "all models — last month" and "GPT — last 6 months"), **When** they click Generate Report, **Then** each selected AI-visibility variant is populated independently with data scoped to its own time window and model selection.
4. **Given** a user on the report-builder page whose client is not yet in the Dashboard, **When** they choose to create a new client and provide a name and domain, **Then** the new client becomes available to select and proceed with block selection immediately.
5. **Given** a generated report with all selected blocks populated, **When** the user types a comment under a block and clicks Save, **Then** the report is stored and associated with the chosen client, including that comment.
6. **Given** a generated report, **When** the user leaves one or more comments empty and clicks Save, **Then** the report still saves successfully with those comments blank.
7. **Given** a user selects zero blocks, **When** they attempt to click Generate Report, **Then** the system prevents generation and explains that at least one block must be selected.

---

### User Story 2 - Reopen and edit a saved report (Priority: P2)

A user browses previously saved reports for a client, reopens one, revises a comment (e.g., to correct a mistake or add context noticed later), and saves the changes back to the same report.

**Why this priority**: Reports are prepared ahead of sending to a client and often need a correction pass; without this, any mistake would force starting over from scratch.

**Independent Test**: Can be fully tested by saving a report, reopening it, changing a comment, saving again, and confirming the update replaced the prior content on the same report rather than creating a duplicate.

**Acceptance Scenarios**:

1. **Given** a client with at least one saved report, **When** the user opens the list of saved reports for that client, **Then** they see each saved report with enough information (e.g., date, period, author) to identify it.
2. **Given** a previously saved report is open, **When** the user edits a comment and saves, **Then** the same report record reflects the new comment and no new duplicate report is created.

---

### User Story 3 - Export a saved report for the client (Priority: P3)

A user opens a saved report and downloads it as a client-ready file to send to the client.

**Why this priority**: Delivering the report is the ultimate purpose of building it, but the in-dashboard generate/comment/save flow already delivers value (a persisted, reviewable report) even before export is available.

**Independent Test**: Can be fully tested by saving a report, requesting an export, and confirming a downloadable file is produced containing the report's blocks and comments.

**Acceptance Scenarios**:

1. **Given** a saved report, **When** the user requests an export, **Then** the system produces a downloadable, client-ready file containing all of that report's blocks and comments.

---

### Edge Cases

- What happens when a selected block's data source has no data available for the current period, or is unreachable/unauthorized (e.g., an expired subscription)? The block should clearly show it could not be populated rather than appearing blank with no explanation, and this must not prevent the other selected blocks from generating.
- What happens if the user changes the block selection and clicks Generate Report again before saving? The report view should reflect only the latest selection.
- What happens if a user tries to Save before ever clicking Generate Report? Save should be unavailable until a report has been generated.
- How does the system behave if a client already has a saved report for the same period — is a new save always a distinct report, or can it overwrite one from the same period? (See Assumptions.)
- What happens if a block's source data changes between Generate Report and Save (e.g., re-generated later)? The report should save exactly what was last generated and shown to the user, not silently re-fetch newer data.
- What happens if a user selects an AI-visibility block scoped to a specific model (e.g., Grok) for a client that has no AI-visibility data recorded for that model yet? The block should show it has no data for that scope rather than an error, and this must not block the other selected blocks.
- What happens if a user selects both a chart-style variant and its counterpart (e.g., the pie-chart and bar-chart version of the same data) for the same report? Both must generate as independent blocks; selecting one must not exclude or alter the other.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The Dashboard MUST provide a new page for building client reports, reachable from the main navigation, accessible to any authenticated dashboard user.
- **FR-002**: Users MUST be able to select which existing client/project a report is being built for before choosing blocks, or create a new client by providing a name and domain if the client is not yet in the Dashboard.
- **FR-003**: The system MUST present a catalog of selectable report blocks that includes at least the 14 baseline blocks defined in the existing report template, and is not limited to exactly 14 — additional block variants (chart-style alternates and the AI-visibility family) MUST also be offered as first-class, independently selectable catalog entries.
- **FR-003a**: For any baseline block whose data is presented as a circle/pie chart (e.g., session mix by channel, branded vs. non-branded clicks), the catalog MUST also offer a bar-chart variant of that same underlying data as a separate selectable block.
- **FR-003b**: The catalog MUST offer the dashboard's own AI-visibility (brand/domain mention) data as its own distinct blocks — not merged into the existing GA4 AI Traffic block or the Summary block — covering two time windows (last month, last 6 months) each combined with either all AI models combined or one specific model (GPT, Gemini, or Grok).
- **FR-004**: Users MUST be able to select any subset of the available catalog blocks, including multiple chart-style or AI-visibility variants at once, and MUST select at least one block before generating.
- **FR-005**: On **Generate Report**, the system MUST retrieve current data for each selected block from that block's associated data source and display it within the report, without fetching or displaying data for unselected blocks.
- **FR-006**: If a selected block's data cannot be retrieved, the system MUST show that block as unavailable/failed with a clear reason rather than omitting it silently, and MUST still generate the remaining selected blocks.
- **FR-007**: Once a report is generated, the system MUST provide an editable comment field under each included block.
- **FR-008**: The system MUST allow a report to be saved regardless of whether some or all comments are left empty.
- **FR-009**: The **Save** action MUST persist the generated report — its selected blocks' data as displayed and all comments — as a record associated with the chosen client.
- **FR-010**: The system MUST record who generated/saved a report, when it was saved, and which reporting period the data covers.
- **FR-011**: Users MUST be able to see a list of previously saved reports for a given client, sufficient to identify and reopen a specific one.
- **FR-012**: Users MUST be able to reopen a previously saved report, edit its comments, and save those changes back to the same report record.
- **FR-013**: Users MUST be able to produce a downloadable, client-ready file from a saved report, containing that report's block data and comments.
- **FR-014**: The **Save** button MUST be disabled or unavailable until a report has been generated for the current session.

### Key Entities

- **Client/Project**: A client the dashboard tracks, identified by name and domain; owns zero or more saved reports. Selecting one scopes which data sources' data is pulled in. Can either be picked from existing clients or created on the spot from this page with just a name and domain.
- **Report Block Type**: One catalog entry a user can select (e.g., "Ahrefs — Domain analysis", "GA4 — Top landing pages", "Session mix by channel (bar chart)", "AI visibility — GPT — last 6 months", "Summary") — has a display name and an associated data source. The catalog includes the 14 baseline entries from the existing report template, a bar-chart variant for each baseline entry whose data is shown as a circle/pie chart, and the AI-visibility block family (one entry per time-window/model-scope combination).
- **Report**: A saved, generated report for one client covering one reporting period; contains the set of block instances the user selected, who saved it, and when.
- **Report Block Instance**: The data snapshot and comment for one Report Block Type within a specific Report; may be marked unavailable if its source failed at generation time.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user can go from opening the report-builder page to a saved report, selecting every available block in the catalog, in under 15 minutes without leaving the page or using any external tool.
- **SC-002**: 100% of blocks a user selects either populate with current source data or clearly indicate why they could not, with zero silent omissions.
- **SC-003**: A saved report reopened later displays the exact same block data and comments that were present at save time, every time.
- **SC-004**: Preparing a client-ready report through this page takes measurably less time than the current manual process of exporting data, running scripts, and hand-editing a file.
- **SC-005**: Users successfully save a report with at least one comment filled in, in 90%+ of report-building sessions that reach the Generate Report step.

## Assumptions

- Clients/projects may already exist in the Dashboard, or be created directly from this page by supplying a name and domain; creating a client here does not require configuring its underlying data-source connections (Ahrefs target, GA4 property, ClickUp list, etc.), which remain a separate, outside-this-feature setup step.
- The 14 baseline block types and their source mapping match the catalog in the existing report template (`ONEBYO~2.HTM` / `README~1.MD`): intro/header, search industry, Ahrefs domain analysis, Ahrefs top movers, GA4 summary, GA4 top landing pages, GA4 monetization, GA4 AI traffic, Search Console summary, Search Console top queries/pages, SE Ranking keywords, work completed, planned works, and summary. This set is a floor, not a ceiling — the selectable catalog is expected to grow beyond 14 (see FR-003/003a/003b).
- The circle/pie-chart-to-bar-chart duplication applies to the two baseline blocks that currently use a donut chart in the source template: GA4 session mix by channel, and branded-vs-non-branded GSC clicks.
- The dashboard's own AI-visibility data (GPT/Gemini/Grok brand & domain mention checks) is already collected and stored by the dashboard's existing AI-visibility feature for each client/project; this feature reads that existing data rather than collecting it itself.
- The external data-source connections (Ahrefs, GA4/Search Console exports, ClickUp, SE Ranking) are already configured and authorized outside this feature; this feature only consumes data that is already reachable, and a source being unreachable (e.g., an expired subscription) is treated as an expected, handled condition rather than a system failure.
- Any authenticated dashboard user may build, save, edit, and export client reports — there is no additional role restriction for this feature.
- A saved report has no locked/final state — it can be reopened and its comments edited at any time, and each save updates that same report record.
- Saving a new report for a client always creates a distinct report record, even if one already exists for the same period; the builder does not attempt to detect or merge duplicates for the same period.
- The downloadable client-ready file is a self-contained artifact analogous in spirit to today's single-file HTML report; its exact format is left open for the planning phase.
- Each report is tied to one reporting period (the period its underlying data was pulled for), consistent with how the current source data is period-based (current/previous/year-over-year).
