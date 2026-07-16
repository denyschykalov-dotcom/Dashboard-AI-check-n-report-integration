# Contract: Report Builder API

All endpoints are appended to the existing FastAPI router (`backend/app/api/routes.py`, prefix `/api`), require the same bearer-token auth as every other route (`Depends(get_current_user)`, see `backend/app/auth.py`), and are usable by **any** authenticated user (no admin gate — `FR-001`). Request/response bodies are new Pydantic models in `backend/app/schemas.py`, following the existing naming style (`XRequest` / `XResponse`).

## `GET /api/report-builder/block-catalog`

Returns the full block-type catalog (data-model.md § Report Block Type) so the frontend can render the selection UI without hard-coding it.

**Response** `200`:
```json
{
  "blocks": [
    { "key": "ahrefs_domain_analysis", "display_name": "Ahrefs — Domain analysis", "source": "ahrefs", "render_style": "table" },
    { "key": "ga4_session_mix_donut", "display_name": "GA4 — Session mix by channel", "source": "ga4_sheet", "render_style": "donut" },
    { "key": "ga4_session_mix_bar", "display_name": "GA4 — Session mix by channel (bar chart)", "source": "ga4_sheet", "render_style": "bar" },
    { "key": "ai_visibility_all_1mo", "display_name": "AI visibility — All models — Last month", "source": "ai_visibility", "render_style": "table", "ai_visibility_window": "last_month", "ai_visibility_model": "all" }
  ]
}
```

## `GET /api/report-builder/clients`

List existing clients (for selection before block-picking — `FR-002`).

**Response** `200`: `{ "clients": [{ "id": "...", "name": "...", "domain": "..." }] }`

## `POST /api/report-builder/clients`

Create a new client (`FR-002`).

**Request**: `{ "name": "Acme Co", "domain": "acme.com" }`

**Response** `201`: `{ "id": "...", "name": "Acme Co", "domain": "acme.com" }`

**Errors**: `422` if `name` or `domain` is blank.

## `POST /api/report-builder/generate`

Fetch current data for the selected blocks for a client (`FR-005`, `FR-006`). Does not persist anything — this is the "preview" step before `Save`.

**Request**:
```json
{ "client_id": "...", "block_keys": ["ahrefs_domain_analysis", "ai_visibility_gpt_1mo"] }
```

**Response** `200`:
```json
{
  "period_label": "2026-06",
  "blocks": [
    { "block_type_key": "ahrefs_domain_analysis", "status": "ok", "data": { "...": "..." } },
    { "block_type_key": "ai_visibility_gpt_1mo", "status": "unavailable", "unavailable_reason": "No AI-visibility runs found for this client in the selected window." }
  ]
}
```

**Errors**: `400` if `block_keys` is empty (edge case: at least one block required); `404` if `client_id` doesn't exist.

**Behavior note**: A single block's source failing (network error, missing per-client config, expired subscription) MUST NOT fail the whole request — each block in the response is independently `ok` or `unavailable` (`FR-006`).

## `POST /api/report-builder/reports`

Save a newly generated report (`FR-009`). Body carries exactly what was shown after `Generate Report`, plus the user's comments — the backend does not re-fetch (edge case: save exactly what was last generated).

**Request**:
```json
{
  "client_id": "...",
  "period_label": "2026-06",
  "blocks": [
    { "block_type_key": "ahrefs_domain_analysis", "status": "ok", "data": { "...": "..." }, "comment": "Backlinks up 12% MoM." },
    { "block_type_key": "ai_visibility_gpt_1mo", "status": "unavailable", "unavailable_reason": "...", "comment": "" }
  ]
}
```

**Response** `201`: `{ "id": "...", "client_id": "...", "period_label": "2026-06", "created_at": "...", "updated_at": "..." }`

## `PUT /api/report-builder/reports/{report_id}`

Update an existing report's comments (and, if re-generated, its block data) in place — never creates a duplicate (`FR-012`, User Story 2). Same request shape as `POST`.

**Response** `200`: same shape as the `POST` response, with `updated_at` refreshed.

**Errors**: `404` if `report_id` doesn't exist.

## `GET /api/report-builder/clients/{client_id}/reports`

List previously saved reports for a client, for reopening (`FR-011`).

**Response** `200`:
```json
{ "reports": [{ "id": "...", "period_label": "2026-06", "generated_by": "...", "updated_at": "..." }] }
```

## `GET /api/report-builder/reports/{report_id}`

Fetch a saved report's full block data + comments for reopening/editing (`FR-012`).

**Response** `200`: same shape as the generate/save block list, plus `client_id`, `period_label`, timestamps.

## `GET /api/report-builder/reports/{report_id}/export`

Produce the downloadable, client-ready file (`FR-013`, research.md §9).

**Response** `200`, `Content-Type: text/html`, `Content-Disposition: attachment; filename="<client>-<period>-report.html"` — a single self-contained HTML file built server-side from the saved report's stored blocks and comments.

**Errors**: `404` if `report_id` doesn't exist.
