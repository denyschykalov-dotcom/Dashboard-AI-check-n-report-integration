# OnebyOne — Monthly SEO Report Template (Handoff)

Self-contained, single-file HTML report. Opens in any browser, works offline, and
can be sent to a client as **one file**. This document is the handoff for the next
specialist: how it's built, where the data comes from, and how to reproduce it.

---

## 1. What the deliverable is

- **`OnebyOne-SEO-Report-June-2026-v2.html`** — the report. One file, no external
  dependencies (no CDN, no fonts, no images pulled from the web). All data is embedded
  as a JSON object, all charts are hand-drawn inline SVG, all styling is inline `<style>`.
- Opens by double-click in Chrome / Safari / Firefox / Edge.
- Two top-bar controls:
  - **MoM / YoY toggle** — re-renders every number, delta and chart against either the
    previous month (May 2026) or the same month last year (Jun 2025).
  - **Print / PDF** — `window.print()`; a print stylesheet hides controls and expands content.
  - **Save report** — serialises the *current* DOM (including specialist comments, pasted
    images and the edited summary) back into a new single HTML file for the client.

### Editing workflow (per client cycle)
1. Open the report.
2. In each block there is a **“Specialist notes”** box (dashed, editable). Type text,
   paste links, or **paste an image** (Ctrl/Cmd+V) — images are stored inline as base64
   so they survive saving.
3. Edit the **Summary** block directly (it is pre-filled but editable; your text is kept).
4. Click **Save report** → send the downloaded file to the client.

---

## 2. Block map & data sources

| # | Block | Source | Status |
|---|-------|--------|--------|
| 1 | Intro / header | `DATA.meta` | static |
| 2 | Search industry | editorial (hard-coded starter items) | **fill manually** |
| 3 | Ahrefs — Domain analysis | Ahrefs API (`site-explorer-metrics`, `domain-rating`, `backlinks-stats`, `metrics-history`) | **live** |
| 4 | Ahrefs — Top movers | Ahrefs API (`site-explorer-top-pages`, date vs date_compared) | **live** |
| 5 | Google Analytics 4 | spreadsheet tabs `GA4 Summary / Channels / Daily / Events` | **live (sheet)** |
| 6 | GA4 Top landing pages | `GA4 Top Pages` | **live (sheet)** |
| 7 | GA4 Monetization | `GA4 Ecommerce` | **live (sheet), all-channels** ⚠ see §5 |
| 8 | GA4 AI Traffic | `GA4 AI Summary / AI Traffic / AI Top Pages` | **live (sheet)** |
| 9 | Google Search Console | `GSC Summary / Positions / Daily` + branded calc | **live (sheet)** |
| 10 | GSC Top queries & pages | `GSC Queries / Top Pages` | **live (sheet)** |
| 11 | SE Ranking — keywords | SE Ranking API | **placeholder** ⚠ subscription expired |
| 12 | Work completed | ClickUp list `onebyone (30)` (id `901002494824`) | **live** |
| 13 | Planned works | ClickUp (TODO / backlog) | **live** |
| 14 | Summary | auto-generated from the numbers, editable | editable |

Comparison periods embedded for all metric blocks: **current = Jun 2026**,
**previous = May 2026**, **YoY = Jun 2025**.

---

## 3. How the data flows

```
Apps Script collector (Code_v5_universal.gs, in the client sheet)
      │  runs monthly → fills tabs in the "onebyone.ua" Google Sheet
      ▼
Google Sheet  ──► prep_data.py  ──►  DATA.json   (single normalised data object)
Ahrefs API ─────────┘                    │
ClickUp API ────────┘                    ▼
                                    builder_v2.py  ──►  OnebyOne-SEO-Report-June-2026-v2.html
```

- **`prep_data.py`** — reads the exported sheet (`onebyone_data.json`) and writes a clean
  `DATA.json`. It also carries the Ahrefs snapshot, the ClickUp task lists and the SE Ranking
  placeholder (these are pasted in from the connector responses).
- **`builder_v2.py`** — injects `DATA.json` into the HTML template (CSS + markup + JS) and
  writes the final report. No templating engine; data is embedded as
  `window.DATA = {...}` and everything renders client-side.

### Regenerating for a new month
1. Re-run the collector so the sheet holds the new current/previous/YoY periods.
2. Export the sheet to `onebyone_data.json` and refresh the Ahrefs / ClickUp snapshots.
3. Update the period constants (`CUR / PREV / YOY`) in `prep_data.py`.
4. Run `python3 prep_data.py` → `python3 builder_v2.py`.

### Regenerating for a new project
- Point the collector at the new GA4 property / GSC domain (see the client-sheet setup
  instruction doc). Everything else is data-driven; only `DATA.meta` (client, domain,
  period) and the Ahrefs/ClickUp targets change.

---

## 4. `DATA` object schema (embedded in the HTML)

```
meta          {client, domain, period, prepared, cur, prev, yoy}
ga4           {"2026-06": {sessions, organic, users, newUsers, returning, engaged,
                           engRate, bounce, duration, pageViews, pps, keyEvents}, ...}
channels      {period: [{channel, sessions, engaged, users}]}
ga4daily      {period: [{day, sessions, engaged, users}]}
events        {period: {eventName: {count, users}}}
ecom          {period: {purchases, revenue, addToCart, checkouts}}
ga4TopPages   {period: [{page, sessions, engaged, keyEvents, bounce}]}
aiSummary     {period: {sessions, engaged, engRate}}
aiTools       [{source, sessions, engaged}]        // current month
aiTopPages    [{page, sessions, engaged}]
gsc           {period: {clicks, impressions, ctr, position}}
gscPos        {period: {top3, top5, top10, top20, top50, total}}
gscDaily      {period: [{day, clicks, impressions, ctr, position}]}
gscQueries    {period: [{query, clicks, impressions, ctr, position}]}
gscTopPages   {period: [{page, clicks, impressions, ctr, position}]}
branded       {period: {branded, total, share}}    // from top-50 query sample
ahrefs        {domainRating, ahrefsRank, backlinks{...}, metrics{period{...}}, trend[[ym,val]]}
ahrefsGainers [[url, traffic, prev, diff, kw, topKw, vol, pos, posPrev]]  // 20
ahrefsLosers  [[...]]                                                      // 20
workDone      [[task, summary, CATEGORY, clickupId]]
workPlanned   [[...]]
seranking     {status, note, keywords:[[keyword, volume, position, prevPosition]]}
```

Rendering entry point: `renderAll()` in the final `<script>`. Each block has its own
`render*()` function; the MoM/YoY toggle simply calls `renderAll()` again with the other
comparison key.

---

## 5. Open TODOs / known limitations

1. **Organic-only Monetization (block 7).** The collector's `ga4Ecommerce()` pulls
   **site-wide** e-commerce (all channels). The report labels this clearly. To show
   organic-only revenue, add a channel-filtered variant to the collector, e.g.:
   ```js
   dimensionFilter: { filter: { fieldName: 'sessionDefaultChannelGroup',
     stringFilter: { value: 'Organic Search' } } }
   ```
   write it to a new `GA4 Ecommerce Organic` tab, and read it in `prep_data.py`.
2. **SE Ranking (block 11)** is a placeholder — the API subscription is expired
   (`status: expired`). Once the key/subscription is renewed, pull tracked keyword
   positions (keyword, volume, latest position, MoM change) and drop them into
   `DATA.seranking.keywords`; the block renders automatically.
3. **Block 2 (Search industry)** and the **Summary** are editorial — verify against the
   month's actual algorithm updates and client context before sending.
4. Branded share (block 9) is computed from the **top-50** GSC query sample, not the full
   query set — good enough for a trend, note the sampling in client conversations.

---

## 6. Files in this handoff

- `OnebyOne-SEO-Report-June-2026-v2.html` — the report (share this / edit + Save).
- `README-report-template.md` — this document.
- `prep_data.py` — sheet/connectors → `DATA.json`.
- `builder_v2.py` — `DATA.json` → final HTML.
- `DATA.json` — the normalised data object for June 2026.
- `onebyone_data.json` — raw export of the client Google Sheet tabs.
