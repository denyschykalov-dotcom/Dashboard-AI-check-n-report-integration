/**
 * Rankberry — Monthly SEO Report collector (v6, multi-site).
 *
 * ONE standalone Apps Script for every client. Per client it finds (or creates)
 * the spreadsheet in the shared Drive folder, deletes tabs that are not current
 * collector output, creates the ones that are missing, and fills them from GA4
 * and Search Console.
 *
 * Self-contained: no backend, no API calls out, no tokens. Edit SITES below.
 *
 * Differences from v5 (the per-sheet bound script):
 *   - Multi-site and standalone, instead of one CONFIG bound to one sheet.
 *   - Creates the client spreadsheet in FOLDER_ID when it does not exist yet.
 *   - Adds the two tabs the report reads but v5 never wrote:
 *       "GA4 Ecommerce Organic" and "GA4 AI Ecommerce".
 *   - Auto-detects the working Search Console property string, which is what
 *     silently produced 0 clicks for yamahaonlineparts.com: a wrong-but-readable
 *     property answers HTTP 200 with zero rows rather than an error.
 *   - Period labels are built from English month names, never Utilities
 *     .formatDate('MMM'), whose output follows the script locale. The report
 *     parses them with Python "%b %Y" and would drop a localised month.
 *   - One batched setValues() write per tab instead of appendRow() per row.
 *   - Owns the whole sheet: after a run, every "GA4 …" / "GSC …" tab is one this
 *     script authored. Leftovers from older versions under names nothing reads
 *     are removed, so a hand-made sheet stops being something to audit.
 *
 * SETUP
 *   1. Fill ga4PropertyId for each entry in SITES below.
 *   2. Services → add "Google Analytics Data API" (identifier: AnalyticsData).
 *   3. The account running this needs read access to every GA4 property and
 *      every Search Console property listed.
 *   4. Run testConnections(), read the log, then setupMonthlyTrigger().
 *
 * Run runAll() for every site, or runSite('domain.com') for one.
 */

// ═══════════════════════════════════════════════════════
//  CONFIG
// ═══════════════════════════════════════════════════════

/** The Drive folder the report reads client sheets from.
 *  Same value as GOOGLE_SHEETS_CLIENT_FOLDER_ID in the backend .env. */
const FOLDER_ID = '1yYQ_b603YmYGSHF2BJ-w29chWrplSEFa';

/**
 * One entry per client. This is the whole configuration.
 *
 *   domain         the spreadsheet is named after it, and that name is how the
 *                  report finds the sheet — so it must match the client's domain.
 *   ga4PropertyId  numeric GA4 property id. Admin → Property details.
 *   gscProperty    leave '' and the script probes "sc-domain:<domain>",
 *                  "https://<domain>/" and the www form, then keeps whichever
 *                  returns clicks. Better than pinning a string: a wrong one
 *                  returns zeros instead of failing, so it breaks silently.
 */
const SITES = [
  { domain: 'yamahaonlineparts.com', ga4PropertyId: '355243910', gscProperty: '' },
  { domain: 'tarscoboltedtank.com',  ga4PropertyId: '509009564', gscProperty: '' },
  { domain: 'partsvu.com',           ga4PropertyId: 'FILL_ME',   gscProperty: '' },
  { domain: 'onebyone.ua',           ga4PropertyId: 'FILL_ME',   gscProperty: '' },
];

/** Which tabs to collect. Global, not per site: every tab the report backend
 *  reads is cheap to fill, and a site without ecommerce simply records zeros —
 *  one less thing to configure wrong. GA4 Page Paths stays off because no
 *  report block reads it. */
const FEATURES = {
  // GA4
  CHANNELS:          true,
  NEW_VS_RETURNING:  true,
  EVENTS:            true,
  ECOMMERCE:         true,
  TOP_LANDING_PAGES: true,
  TOP_PAGE_PATHS:    false,
  DAILY_SESSIONS:    true,
  DEVICES:           true,
  COUNTRIES:         true,
  // GSC
  GSC_QUERIES:       true,
  GSC_TOP_PAGES:     true,
  GSC_DAILY_CLICKS:  true,
  GSC_POSITION_DIST: true,
  GSC_DEVICES:       true,
  GSC_COUNTRIES:     true,
  // AI
  AI_TRAFFIC:        true,
};

const TOP_LIMIT    = 50;
const QUERY_SAMPLE = 2000;

// ponytail: one mutable global holds the site being collected, so the ~25 fetch
// helpers below keep their (start, end) signatures untouched from v5. Safe only
// because Apps Script runs one execution at a time. Thread a context object
// through if this ever runs concurrently.
let CONFIG = null;

// ═══════════════════════════════════════════════════════
//  PERIODS
// ═══════════════════════════════════════════════════════

// Hardcoded English months: the backend parses these labels with Python
// "%b %Y". Utilities.formatDate(..., 'MMM yyyy') follows the script locale and
// would emit e.g. "лип 2026", which the backend silently drops.
const MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                   'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function getPeriods() {
  const now = new Date();
  const fmt = d => Utilities.formatDate(d, 'UTC', 'yyyy-MM-dd');
  const lbl = d => MONTHS_EN[d.getUTCMonth()] + ' ' + d.getUTCFullYear();

  const cur   = new Date(Date.UTC(now.getFullYear(),     now.getMonth() - 1, 1));
  const curE  = new Date(Date.UTC(now.getFullYear(),     now.getMonth(),     0));
  const prev  = new Date(Date.UTC(now.getFullYear(),     now.getMonth() - 2, 1));
  const prevE = new Date(Date.UTC(now.getFullYear(),     now.getMonth() - 1, 0));
  const yoy   = new Date(Date.UTC(now.getFullYear() - 1, now.getMonth() - 1, 1));
  const yoyE  = new Date(Date.UTC(now.getFullYear() - 1, now.getMonth(),     0));

  return {
    current:  { label: lbl(cur),  start: fmt(cur),  end: fmt(curE)  },
    previous: { label: lbl(prev), start: fmt(prev), end: fmt(prevE) },
    yoy:      { label: lbl(yoy),  start: fmt(yoy),  end: fmt(yoyE)  },
  };
}

// ═══════════════════════════════════════════════════════
//  SHEET / DRIVE HELPERS
// ═══════════════════════════════════════════════════════

/** The client's spreadsheet in FOLDER_ID, created there if absent.
 *
 *  Reuses an existing exact-name match rather than creating a second sheet:
 *  the backend caches the resolved sheet id on the client row, and a duplicate
 *  name makes its lookup ambiguous — it would keep reading the old one. */
function getOrCreateSpreadsheet_(domain) {
  const folder = DriveApp.getFolderById(FOLDER_ID);
  const found  = folder.getFilesByName(domain);
  if (found.hasNext()) return SpreadsheetApp.open(found.next());

  const ss = SpreadsheetApp.create(domain);
  DriveApp.getFileById(ss.getId()).moveTo(folder);
  Logger.log('created spreadsheet "' + domain + '" (' + ss.getId() + ')');
  return ss;
}

// Tabs this run authored, per site. The sweep below uses it to tell current
// output from a previous script version's leftovers.
let writtenTabs = null;

/** Replace a tab's contents in one write.
 *
 *  Rows are padded/trimmed to the header width because setValues rejects a
 *  ragged array, and the backend maps values onto header cells by position. */
function writeTab_(ss, name, header, rows) {
  if (writtenTabs) writtenTabs[name] = true;
  let sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  sh.clear();

  const width = header.length;
  const data  = [header];
  (rows || []).forEach(row => {
    const cells = row.slice(0, width);
    while (cells.length < width) cells.push('');
    data.push(cells);
  });
  sh.getRange(1, 1, data.length, width).setValues(data);
  return sh;
}

/**
 * Delete tabs this run did not author, so the sheet is entirely script-owned.
 *
 * The point of this: a sheet set up by hand carries tabs from older script
 * versions under names nothing reads — a real one held "GA4 AI Assistants"
 * (Period/AI Source/Medium/Landing Page/Sessions/Users) where the report needs
 * three differently-shaped tabs. Leaving it there means the next person cannot
 * tell which tab is live, and the report silently used neither.
 *
 * Deliberately narrow about what it removes:
 *   - a "GA4 …" or "GSC …" tab this run did not write is stale collector output
 *   - a completely empty tab is the default sheet a new spreadsheet ships with
 *     (its name is locale-dependent, so match on emptiness, not on "Sheet1")
 *   - anything else — a colleague's notes tab — is kept and logged
 *
 * "Monthly History" is never touched: it is append-only and holds months that
 * Search Console (16-month window) can no longer return.
 */
function sweepUnmanagedTabs_(ss) {
  const removed = [];
  const kept = [];
  ss.getSheets().forEach(sh => {
    const name = sh.getName();
    if (writtenTabs[name] || name === 'Monthly History') return;
    const isStaleCollectorTab = name.indexOf('GA4 ') === 0 || name.indexOf('GSC ') === 0;
    const isEmpty = sh.getLastRow() === 0;
    if (isStaleCollectorTab || isEmpty) {
      // getSheets() gives a snapshot, so deleting while iterating it is safe.
      ss.deleteSheet(sh);
      removed.push(name);
    } else {
      kept.push(name);
    }
  });
  if (removed.length) Logger.log('  removed stale tabs: ' + removed.join(', '));
  if (kept.length) Logger.log('  left alone (not collector output): ' + kept.join(', '));
  return removed;
}

/** GA4's `date` dimension returns "20260701"; emit "2026-07-01" so GA4 Daily
 *  and GSC Daily carry the same shape. String sort order is unchanged. */
function ymd_(value) {
  const raw = String(value || '');
  return /^\d{8}$/.test(raw)
    ? raw.slice(0, 4) + '-' + raw.slice(4, 6) + '-' + raw.slice(6, 8)
    : raw;
}

function rowsOrEmpty(r) { return r.rows || []; }

// ═══════════════════════════════════════════════════════
//  API HELPERS
// ═══════════════════════════════════════════════════════

function ga4Report(body) {
  return AnalyticsData.Properties.runReport(
    body, 'properties/' + CONFIG.GA4_PROPERTY_ID
  );
}

function gscFetch_(property, payload) {
  const url = 'https://www.googleapis.com/webmasters/v3/sites/' +
    encodeURIComponent(property) + '/searchAnalytics/query';
  const resp = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  });
  return { code: resp.getResponseCode(), body: JSON.parse(resp.getContentText() || '{}') };
}

function gscPost(payload) {
  return gscFetch_(CONFIG.GSC_PROPERTY, payload).body;
}

/**
 * The Search Console property string that actually returns data for a domain.
 *
 * A wrong-but-valid string is the failure this exists for: Search Console
 * answers HTTP 200 with zero rows for a property you can read but that holds no
 * data, so the collector logged a cheerful "✅ GSC — clicks: 0" and the report
 * shipped an empty section. Prefer a candidate with clicks; fall back to the
 * first readable one so a genuinely quiet month still resolves.
 */
function resolveGscProperty_(domain, configured) {
  const bare = String(domain).replace(/^https?:\/\//, '').replace(/^sc-domain:/, '').replace(/\/$/, '');
  const candidates = [];
  if (configured) candidates.push(configured);
  ['sc-domain:' + bare, 'https://' + bare + '/', 'https://www.' + bare + '/']
    .forEach(c => { if (candidates.indexOf(c) === -1) candidates.push(c); });

  const period = getPeriods().current;
  const notes  = [];
  let readable = null;

  for (let i = 0; i < candidates.length; i++) {
    const prop = candidates[i];
    let result;
    try {
      result = gscFetch_(prop, { startDate: period.start, endDate: period.end, dimensions: [] });
    } catch (e) {
      notes.push('✗ ' + prop + ' → ' + e.message);
      continue;
    }
    if (result.code !== 200) {
      const message = result.body.error ? result.body.error.message : 'HTTP ' + result.code;
      notes.push('✗ ' + prop + ' → ' + message);
      continue;
    }
    const clicks = result.body.rows && result.body.rows.length ? result.body.rows[0].clicks : 0;
    notes.push((clicks > 0 ? '✓ ' : '· ') + prop + ' → clicks ' + clicks);
    if (clicks > 0) return { property: prop, clicks: clicks, notes: notes };
    if (!readable) readable = prop;
  }
  return { property: readable, clicks: 0, notes: notes };
}

// ═══════════════════════════════════════════════════════
//  GA4 FETCHERS
// ═══════════════════════════════════════════════════════

function ga4Summary(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    metrics: [
      { name: 'sessions' }, { name: 'totalUsers' }, { name: 'newUsers' },
      { name: 'engagedSessions' }, { name: 'engagementRate' }, { name: 'bounceRate' },
      { name: 'averageSessionDuration' }, { name: 'screenPageViews' },
      { name: 'screenPageViewsPerSession' }, { name: 'keyEvents' },
    ],
  });
  const vals = r.rows && r.rows[0] ? r.rows[0].metricValues.map(m => m.value) : Array(10).fill(0);
  return {
    sessions: Number(vals[0]), totalUsers: Number(vals[1]), newUsers: Number(vals[2]),
    engagedSessions: Number(vals[3]), engagementRate: Number(vals[4]), bounceRate: Number(vals[5]),
    avgSessionDuration: Number(vals[6]), pageViews: Number(vals[7]),
    pagesPerSession: Number(vals[8]), keyEvents: Number(vals[9]),
  };
}

function channelFilter_(channelGroup) {
  return { filter: { fieldName: 'sessionDefaultChannelGroup',
                     stringFilter: { value: channelGroup } } };
}

function ga4OrganicSessions(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'sessionDefaultChannelGroup' }],
    metrics: [{ name: 'sessions' }],
    dimensionFilter: channelFilter_('Organic Search'),
  });
  return r.rows ? Number(r.rows[0].metricValues[0].value) : 0;
}

function ga4Channels(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'sessionDefaultChannelGroup' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }, { name: 'totalUsers' }],
    orderBys: [{ metric: { metricName: 'sessions' }, desc: true }],
  });
  return rowsOrEmpty(r).map(row => ({
    channel: row.dimensionValues[0].value,
    sessions: Number(row.metricValues[0].value),
    engagedSessions: Number(row.metricValues[1].value),
    users: Number(row.metricValues[2].value),
  }));
}

function ga4NewVsReturning(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'newVsReturning' }],
    metrics: [{ name: 'sessions' }, { name: 'totalUsers' }],
  });
  const result = { newSessions: 0, returningSessions: 0, newUsers: 0, returningUsers: 0 };
  rowsOrEmpty(r).forEach(row => {
    const type = row.dimensionValues[0].value;
    const sessions = Number(row.metricValues[0].value);
    const users    = Number(row.metricValues[1].value);
    if (type === 'new') { result.newSessions = sessions; result.newUsers = users; }
    else                { result.returningSessions = sessions; result.returningUsers = users; }
  });
  return result;
}

function ga4Events(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'eventName' }],
    metrics: [{ name: 'eventCount' }, { name: 'totalUsers' }],
    orderBys: [{ metric: { metricName: 'eventCount' }, desc: true }],
  });
  return rowsOrEmpty(r).map(row => ({
    eventName: row.dimensionValues[0].value,
    count: Number(row.metricValues[0].value),
    users: Number(row.metricValues[1].value),
  }));
}

/** Ecommerce KPIs, optionally restricted to one default channel group.
 *  channelGroup null → site-wide; 'Organic Search' and 'AI Assistant' feed the
 *  "GA4 Ecommerce Organic" and "GA4 AI Ecommerce" tabs the backend reads. */
function ga4Ecommerce(start, end, channelGroup) {
  const body = {
    dateRanges: [{ startDate: start, endDate: end }],
    metrics: [
      { name: 'ecommercePurchases' }, { name: 'purchaseRevenue' },
      { name: 'addToCarts' }, { name: 'checkouts' },
    ],
  };
  if (channelGroup) {
    body.dimensions = [{ name: 'sessionDefaultChannelGroup' }];
    body.dimensionFilter = channelFilter_(channelGroup);
  }
  const r = ga4Report(body);
  const v = r.rows && r.rows[0] ? r.rows[0].metricValues : [];
  const at = i => Number((v[i] && v[i].value) || 0);
  return { purchases: at(0), revenue: at(1), addToCarts: at(2), checkouts: at(3) };
}

function ga4TopPages(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'landingPage' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }, { name: 'keyEvents' }, { name: 'bounceRate' }],
    orderBys: [{ metric: { metricName: 'sessions' }, desc: true }],
    limit: TOP_LIMIT,
  });
  return rowsOrEmpty(r).map(row => ({
    page: row.dimensionValues[0].value,
    sessions: Number(row.metricValues[0].value),
    engagedSessions: Number(row.metricValues[1].value),
    keyEvents: Number(row.metricValues[2].value),
    bounceRate: Number(row.metricValues[3].value),
  }));
}

function ga4TopPagePaths(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'pagePath' }],
    metrics: [{ name: 'screenPageViews' }, { name: 'totalUsers' }, { name: 'averageSessionDuration' }],
    orderBys: [{ metric: { metricName: 'screenPageViews' }, desc: true }],
    limit: TOP_LIMIT,
  });
  return rowsOrEmpty(r).map(row => ({
    path: row.dimensionValues[0].value,
    pageViews: Number(row.metricValues[0].value),
    users: Number(row.metricValues[1].value),
    avgTime: Number(row.metricValues[2].value),
  }));
}

function ga4Daily(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'date' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }, { name: 'totalUsers' }],
    orderBys: [{ dimension: { dimensionName: 'date' } }],
  });
  return rowsOrEmpty(r).map(row => ({
    date: ymd_(row.dimensionValues[0].value),
    sessions: Number(row.metricValues[0].value),
    engagedSessions: Number(row.metricValues[1].value),
    users: Number(row.metricValues[2].value),
  }));
}

function ga4Devices(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'deviceCategory' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }, { name: 'totalUsers' }],
    orderBys: [{ metric: { metricName: 'sessions' }, desc: true }],
  });
  return rowsOrEmpty(r).map(row => ({
    device: row.dimensionValues[0].value,
    sessions: Number(row.metricValues[0].value),
    engagedSessions: Number(row.metricValues[1].value),
    users: Number(row.metricValues[2].value),
  }));
}

function ga4Countries(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'country' }],
    metrics: [{ name: 'sessions' }, { name: 'totalUsers' }, { name: 'engagedSessions' }],
    orderBys: [{ metric: { metricName: 'sessions' }, desc: true }],
    limit: 20,
  });
  return rowsOrEmpty(r).map(row => ({
    country: row.dimensionValues[0].value,
    sessions: Number(row.metricValues[0].value),
    users: Number(row.metricValues[1].value),
    engagedSessions: Number(row.metricValues[2].value),
  }));
}

function ga4AITrafficSummary(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'sessionDefaultChannelGroup' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }],
    dimensionFilter: channelFilter_('AI Assistant'),
  });
  if (!r.rows || !r.rows.length) return { sessions: 0, engagedSessions: 0 };
  return {
    sessions: Number(r.rows[0].metricValues[0].value),
    engagedSessions: Number(r.rows[0].metricValues[1].value),
  };
}

function ga4AITrafficDetail(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'sessionSource' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }],
    dimensionFilter: channelFilter_('AI Assistant'),
    orderBys: [{ metric: { metricName: 'sessions' }, desc: true }],
  });
  return rowsOrEmpty(r).map(row => ({
    source: row.dimensionValues[0].value,
    sessions: Number(row.metricValues[0].value),
    engagedSessions: Number(row.metricValues[1].value),
  }));
}

function ga4AITopPages(start, end) {
  const r = ga4Report({
    dateRanges: [{ startDate: start, endDate: end }],
    dimensions: [{ name: 'landingPage' }],
    metrics: [{ name: 'sessions' }, { name: 'engagedSessions' }],
    dimensionFilter: channelFilter_('AI Assistant'),
    orderBys: [{ metric: { metricName: 'sessions' }, desc: true }],
    limit: 10,
  });
  return rowsOrEmpty(r).map(row => ({
    page: row.dimensionValues[0].value,
    sessions: Number(row.metricValues[0].value),
    engagedSessions: Number(row.metricValues[1].value),
  }));
}

// ═══════════════════════════════════════════════════════
//  GSC FETCHERS
// ═══════════════════════════════════════════════════════

function gscSummary(start, end) {
  const body = gscPost({ startDate: start, endDate: end, dimensions: [] });
  if (!body.rows || !body.rows.length) return { clicks: 0, impressions: 0, ctr: 0, position: 0 };
  const r = body.rows[0];
  return { clicks: r.clicks, impressions: r.impressions, ctr: r.ctr, position: r.position };
}

function gscByDimension_(start, end, dimension, limit) {
  const payload = { startDate: start, endDate: end, dimensions: [dimension] };
  if (limit) payload.rowLimit = limit;
  const body = gscPost(payload);
  return (body.rows || []).map(r => ({
    key: r.keys[0], clicks: r.clicks, impressions: r.impressions,
    ctr: r.ctr, position: r.position,
  }));
}

function byClicksDesc_(rows) { return rows.sort((a, b) => b.clicks - a.clicks); }

function gscPositionDist(start, end) {
  const body = gscPost({
    startDate: start, endDate: end,
    dimensions: ['query'], rowLimit: QUERY_SAMPLE,
  });
  if (!body.rows) return { top3: 0, top5: 0, top10: 0, top20: 0, top50: 0, total: 0 };
  let top3 = 0, top5 = 0, top10 = 0, top20 = 0, top50 = 0;
  body.rows.forEach(r => {
    const p = r.position;
    if (p <= 3)  top3++;
    if (p <= 5)  top5++;
    if (p <= 10) top10++;
    if (p <= 20) top20++;
    if (p <= 50) top50++;
  });
  return { top3, top5, top10, top20, top50, total: body.rows.length };
}

// ═══════════════════════════════════════════════════════
//  MONTHLY HISTORY — append-only
// ═══════════════════════════════════════════════════════

function updateHistory_(ss, periods, histData) {
  const header = [
    'Month', 'Total Sessions', 'Organic Sessions', 'Total Users', 'New Users', 'Returning Users',
    'Engaged Sessions', 'Engagement Rate %', 'Bounce Rate %',
    'Avg Session Duration (s)', 'Page Views', 'Pages/Session', 'Key Events',
    'GSC Clicks', 'GSC Impressions', 'GSC CTR', 'Avg Position',
    'Top-3 Queries', 'Top-5 Queries', 'Top-10 Queries',
    'AI Sessions', 'AI Engaged Sessions',
    'Ecommerce Revenue', 'Ecommerce Purchases',
  ];
  let sh = ss.getSheetByName('Monthly History');
  if (!sh) {
    sh = ss.insertSheet('Monthly History');
    sh.appendRow(header);
  }

  const label = periods.current.label;
  if (sh.getDataRange().getValues().some(row => row[0] === label)) {
    Logger.log('Monthly History: ' + label + ' already recorded — skipped.');
    return;
  }

  const d  = histData;
  const nr = d.newVsReturning || {};
  const gs = d.gscSummary     || {};
  const pd = d.posDist        || {};
  const ec = d.ecommerce      || {};
  const ai = d.aiSummary      || {};

  sh.appendRow([
    label,
    d.summary.sessions, d.organicSessions, d.summary.totalUsers,
    nr.newUsers || '', nr.returningUsers || '',
    d.summary.engagedSessions,
    (d.summary.engagementRate * 100).toFixed(1),
    (d.summary.bounceRate * 100).toFixed(1),
    d.summary.avgSessionDuration.toFixed(0),
    d.summary.pageViews, d.summary.pagesPerSession.toFixed(2), d.summary.keyEvents,
    gs.clicks || '', gs.impressions || '',
    gs.ctr ? (gs.ctr * 100).toFixed(2) + '%' : '',
    gs.position ? gs.position.toFixed(1) : '',
    pd.top3 || '', pd.top5 || '', pd.top10 || '',
    ai.sessions || 0, ai.engagedSessions || 0,
    ec.revenue || '', ec.purchases || '',
  ]);
}

// ═══════════════════════════════════════════════════════
//  COLLECTION — one site
// ═══════════════════════════════════════════════════════

/** Rows for a metric collected once per period, flattened with the period label
 *  in column A — the shape every tab uses and the backend filters on. */
function perPeriod_(periods, keys, rowsFor) {
  const out = [];
  keys.forEach(key => {
    const period = periods[key];
    rowsFor(period, key).forEach(row => out.push([period.label].concat(row)));
  });
  return out;
}

function collectSite_(site) {
  const periods = getPeriods();
  const keys    = ['current', 'previous', 'yoy'];
  const F       = CONFIG.FEATURES;
  const ss      = getOrCreateSpreadsheet_(site.domain);
  const notes   = [];
  writtenTabs   = {};

  // ── GA4 Summary ─────────────────────────────────
  const curP       = periods.current;
  const curSum     = ga4Summary(curP.start, curP.end);
  const curOrganic = ga4OrganicSessions(curP.start, curP.end);

  writeTab_(ss, 'GA4 Summary', [
    'Period', 'Sessions', 'Organic Sessions', 'Total Users', 'New Users', 'Returning Users',
    'Engaged Sessions', 'Engagement Rate %', 'Bounce Rate %',
    'Avg Session Duration (s)', 'Page Views', 'Pages/Session', 'Key Events',
  ], perPeriod_(periods, keys, (p, key) => {
    const s  = (key === 'current') ? curSum     : ga4Summary(p.start, p.end);
    const o  = (key === 'current') ? curOrganic : ga4OrganicSessions(p.start, p.end);
    const nr = F.NEW_VS_RETURNING ? ga4NewVsReturning(p.start, p.end)
                                  : { newUsers: '', returningUsers: '' };
    return [[
      s.sessions, o, s.totalUsers, nr.newUsers, nr.returningUsers,
      s.engagedSessions,
      (s.engagementRate * 100).toFixed(1),
      (s.bounceRate * 100).toFixed(1),
      s.avgSessionDuration.toFixed(0),
      s.pageViews, s.pagesPerSession.toFixed(2), s.keyEvents,
    ]];
  }));
  notes.push('GA4 sessions ' + curP.label + ': ' + curSum.sessions);

  // ── GA4 Channels ────────────────────────────────
  if (F.CHANNELS) {
    writeTab_(ss, 'GA4 Channels',
      ['Period', 'Channel', 'Sessions', 'Engaged Sessions', 'Users'],
      perPeriod_(periods, keys, p => ga4Channels(p.start, p.end)
        .map(r => [r.channel, r.sessions, r.engagedSessions, r.users])));
  }

  // ── GA4 Events ──────────────────────────────────
  if (F.EVENTS) {
    writeTab_(ss, 'GA4 Events',
      ['Period', 'Event Name', 'Count', 'Users'],
      perPeriod_(periods, keys, p => {
        const events = ga4Events(p.start, p.end);
        return events.length ? events.map(e => [e.eventName, e.count, e.users])
                             : [['(none)', 0, 0]];
      }));
  }

  // ── GA4 Ecommerce: site-wide, organic, AI ───────
  // All three tabs are read by the backend's monetization block. Organic and AI
  // were never written by v5, which is why those columns rendered empty.
  if (F.ECOMMERCE) {
    const ecommerceHeader = ['Period', 'Purchases', 'Revenue', 'Add to Carts', 'Checkouts'];
    const ecommerceRows = channelGroup => perPeriod_(periods, keys, p => {
      const e = ga4Ecommerce(p.start, p.end, channelGroup);
      return [[e.purchases, e.revenue, e.addToCarts, e.checkouts]];
    });
    writeTab_(ss, 'GA4 Ecommerce',         ecommerceHeader, ecommerceRows(null));
    writeTab_(ss, 'GA4 Ecommerce Organic', ecommerceHeader, ecommerceRows('Organic Search'));
    writeTab_(ss, 'GA4 AI Ecommerce',      ecommerceHeader, ecommerceRows('AI Assistant'));
  }

  // ── GA4 Top Landing Pages ───────────────────────
  if (F.TOP_LANDING_PAGES) {
    writeTab_(ss, 'GA4 Top Pages',
      ['Period', 'Landing Page', 'Sessions', 'Engaged Sessions', 'Key Events', 'Bounce Rate %'],
      perPeriod_(periods, keys, p => ga4TopPages(p.start, p.end).map(r =>
        [r.page, r.sessions, r.engagedSessions, r.keyEvents, (r.bounceRate * 100).toFixed(1)])));
  }

  // ── GA4 Page Paths ──────────────────────────────
  if (F.TOP_PAGE_PATHS) {
    writeTab_(ss, 'GA4 Page Paths',
      ['Period', 'Page Path', 'Page Views', 'Users', 'Avg Time on Page (s)'],
      perPeriod_(periods, keys, p => ga4TopPagePaths(p.start, p.end).map(r =>
        [r.path, r.pageViews, r.users, r.avgTime.toFixed(0)])));
  }

  // ── GA4 Daily ───────────────────────────────────
  if (F.DAILY_SESSIONS) {
    writeTab_(ss, 'GA4 Daily',
      ['Period', 'Date', 'Sessions', 'Engaged Sessions', 'Users'],
      perPeriod_(periods, keys, p => ga4Daily(p.start, p.end).map(d =>
        [d.date, d.sessions, d.engagedSessions, d.users])));
  }

  // ── GA4 Devices ─────────────────────────────────
  if (F.DEVICES) {
    writeTab_(ss, 'GA4 Devices',
      ['Period', 'Device', 'Sessions', 'Engaged Sessions', 'Users'],
      perPeriod_(periods, keys, p => ga4Devices(p.start, p.end).map(r =>
        [r.device, r.sessions, r.engagedSessions, r.users])));
  }

  // ── GA4 Countries ───────────────────────────────
  if (F.COUNTRIES) {
    writeTab_(ss, 'GA4 Countries',
      ['Period', 'Country', 'Sessions', 'Users', 'Engaged Sessions'],
      perPeriod_(periods, keys, p => ga4Countries(p.start, p.end).map(r =>
        [r.country, r.sessions, r.users, r.engagedSessions])));
  }

  // ── GA4 AI Traffic ──────────────────────────────
  let curAiSummary = null;
  if (F.AI_TRAFFIC) {
    writeTab_(ss, 'GA4 AI Summary',
      ['Period', 'Total AI Sessions', 'Engaged Sessions', 'Engagement Rate %'],
      perPeriod_(periods, keys, (p, key) => {
        const s = ga4AITrafficSummary(p.start, p.end);
        if (key === 'current') curAiSummary = s;
        const rate = s.sessions > 0 ? (s.engagedSessions / s.sessions * 100).toFixed(1) : '0';
        return [[s.sessions, s.engagedSessions, rate]];
      }));

    writeTab_(ss, 'GA4 AI Traffic',
      ['Period', 'Source', 'Sessions', 'Engaged Sessions'],
      perPeriod_(periods, keys, p => {
        const rows = ga4AITrafficDetail(p.start, p.end);
        return rows.length ? rows.map(r => [r.source, r.sessions, r.engagedSessions])
                           : [['(no AI traffic detected)', 0, 0]];
      }));

    writeTab_(ss, 'GA4 AI Top Pages',
      ['Period', 'Landing Page', 'Sessions', 'Engaged Sessions'],
      perPeriod_(periods, keys, p => {
        const rows = ga4AITopPages(p.start, p.end);
        return rows.length ? rows.map(r => [r.page, r.sessions, r.engagedSessions])
                           : [['(no AI traffic detected)', 0, 0]];
      }));
  }

  // ── GSC Summary ─────────────────────────────────
  let curGsc = null;
  writeTab_(ss, 'GSC Summary',
    ['Period', 'Clicks', 'Impressions', 'CTR %', 'Avg Position'],
    perPeriod_(periods, keys, (p, key) => {
      const s = gscSummary(p.start, p.end);
      if (key === 'current') curGsc = s;
      return [[
        s.clicks, s.impressions,
        s.ctr ? (s.ctr * 100).toFixed(2) : '',
        s.position ? s.position.toFixed(1) : '',
      ]];
    }));
  notes.push('GSC clicks ' + curP.label + ': ' + (curGsc ? curGsc.clicks : 0) +
             ' (property ' + CONFIG.GSC_PROPERTY + ')');

  // ── GSC Positions ───────────────────────────────
  let curPosDist = null;
  if (F.GSC_POSITION_DIST) {
    writeTab_(ss, 'GSC Positions',
      ['Period', 'Top-3', 'Top-5', 'Top-10', 'Top-20', 'Top-50', 'Total Sampled'],
      perPeriod_(periods, keys, (p, key) => {
        const pd = gscPositionDist(p.start, p.end);
        if (key === 'current') curPosDist = pd;
        return [[pd.top3, pd.top5, pd.top10, pd.top20, pd.top50, pd.total]];
      }));
  }

  // ── GSC per-dimension tabs ──────────────────────
  const gscTabs = [
    { on: F.GSC_QUERIES,      tab: 'GSC Queries',   label: 'Query',   dim: 'query',   limit: TOP_LIMIT, sort: true  },
    { on: F.GSC_TOP_PAGES,    tab: 'GSC Top Pages', label: 'Page',    dim: 'page',    limit: TOP_LIMIT, sort: true  },
    { on: F.GSC_DAILY_CLICKS, tab: 'GSC Daily',     label: 'Date',    dim: 'date',    limit: 0,         sort: false },
    { on: F.GSC_DEVICES,      tab: 'GSC Devices',   label: 'Device',  dim: 'device',  limit: 0,         sort: false },
    { on: F.GSC_COUNTRIES,    tab: 'GSC Countries', label: 'Country', dim: 'country', limit: 20,        sort: true  },
  ];
  gscTabs.forEach(spec => {
    if (!spec.on) return;
    writeTab_(ss, spec.tab,
      ['Period', spec.label, 'Clicks', 'Impressions', 'CTR %', 'Avg Position'],
      perPeriod_(periods, keys, p => {
        let rows = gscByDimension_(p.start, p.end, spec.dim, spec.limit);
        if (spec.sort) rows = byClicksDesc_(rows);
        return rows.map(r => [
          r.key, r.clicks, r.impressions,
          (r.ctr * 100).toFixed(2),
          r.position.toFixed(1),
        ]);
      }));
  });

  // ── Monthly History ─────────────────────────────
  updateHistory_(ss, periods, {
    summary:         curSum,
    organicSessions: curOrganic,
    newVsReturning:  F.NEW_VS_RETURNING ? ga4NewVsReturning(curP.start, curP.end) : null,
    gscSummary:      curGsc,
    posDist:         curPosDist,
    ecommerce:       F.ECOMMERCE ? ga4Ecommerce(curP.start, curP.end, null) : null,
    aiSummary:       curAiSummary,
  });

  // ── Test Log ────────────────────────────────────
  // The record that answers "did this month actually collect?" — including a
  // zero-click warning, because a green run with 0 clicks is the failure that
  // reached the client as an empty Search Console section.
  if (curGsc && !curGsc.clicks && !curGsc.impressions) {
    notes.push('⚠ Search Console returned no data. Check the property and that ' +
               'the collector account can read it.');
  }
  writeTab_(ss, 'Test Log',
    ['Timestamp', 'Note'],
    notes.map(n => [Utilities.formatDate(new Date(), 'UTC', 'yyyy-MM-dd HH:mm:ss'), n]));

  // Last, and only on a run that got this far: a half-finished run must not
  // strip the tabs it never reached.
  const removed = sweepUnmanagedTabs_(ss);

  return {
    domain: site.domain, sheetId: ss.getId(), period: curP.label,
    notes: notes, removedTabs: removed,
  };
}

/** Load one SITES entry into the CONFIG global. Returns null when unusable. */
function activate_(site) {
  if (!site.ga4PropertyId || site.ga4PropertyId === 'FILL_ME') {
    Logger.log('⏭ ' + site.domain + ' skipped — ga4PropertyId not set in SITES.');
    return null;
  }
  const resolved = resolveGscProperty_(site.domain, site.gscProperty);
  Logger.log(site.domain + ' GSC probe:\n  ' + resolved.notes.join('\n  '));
  if (!resolved.property) {
    Logger.log('⏭ ' + site.domain + ' skipped — no readable Search Console property.');
    return null;
  }
  CONFIG = {
    GA4_PROPERTY_ID: site.ga4PropertyId,
    GSC_PROPERTY:    resolved.property,
    FEATURES:        FEATURES,
  };
  return CONFIG;
}

// ═══════════════════════════════════════════════════════
//  ENTRY POINTS
// ═══════════════════════════════════════════════════════

/** Collect every site in SITES. One failure does not stop the rest.
 *
 *  ponytail: a plain sequential loop. Roughly 1–2 minutes per site, so this fits
 *  the 30-minute Workspace execution limit up to ~12 sites. Past that, split
 *  into one trigger per site or add a continuation trigger. */
function runAll() {
  const summary = [];
  Logger.log('Collecting ' + SITES.length + ' site(s).');
  SITES.forEach(site => {
    if (!activate_(site)) { summary.push('⏭ ' + site.domain + ' skipped'); return; }
    try {
      const result = collectSite_(site);
      summary.push('✅ ' + result.domain + ' → ' + result.period + ' (' + result.sheetId + ')');
    } catch (e) {
      summary.push('❌ ' + site.domain + ' → ' + e.message);
      Logger.log('❌ ' + site.domain + ': ' + e.stack);
    }
  });
  Logger.log('runAll finished:\n' + summary.join('\n'));
  return summary;
}

/** Collect one site by domain — what to use after fixing a single client. */
function runSite(domain) {
  const site = SITES.filter(s => s.domain === domain)[0];
  if (!site) throw new Error('No SITES entry for domain "' + domain + '".');
  if (!activate_(site)) throw new Error('Cannot collect ' + domain + ' — see the log.');
  const result = collectSite_(site);
  Logger.log('✅ ' + result.domain + ' → ' + result.period + '\n  ' + result.notes.join('\n  '));
  return result;
}

/** Monthly trigger target. Never throws, so one bad month cannot break the
 *  trigger — read the execution log for detail. */
function collectMonthlyData() {
  try {
    runAll();
  } catch (e) {
    Logger.log('❌ collectMonthlyData error: ' + e.stack);
  }
}

/** Reachability check for every site. Writes no report data — safe to re-run.
 *  Logs only: a standalone script has no UI to alert into. */
function testConnections() {
  const periods = getPeriods();
  const results = [];
  SITES.forEach(site => {
    const lines = ['── ' + site.domain];
    if (!site.ga4PropertyId || site.ga4PropertyId === 'FILL_ME') {
      lines.push('  ❌ GA4: ga4PropertyId not set in SITES');
    } else {
      CONFIG = { GA4_PROPERTY_ID: site.ga4PropertyId, GSC_PROPERTY: '', FEATURES: FEATURES };
      try {
        const r = ga4Report({
          dateRanges: [{ startDate: periods.current.start, endDate: periods.current.end }],
          metrics: [{ name: 'sessions' }],
        });
        lines.push('  ✅ GA4 sessions ' + periods.current.label + ': ' +
                   (r.rows ? r.rows[0].metricValues[0].value : '0'));
      } catch (e) {
        lines.push('  ❌ GA4: ' + e.message);
      }
    }
    const probe = resolveGscProperty_(site.domain, site.gscProperty);
    probe.notes.forEach(n => lines.push('  ' + n));
    lines.push(probe.clicks > 0
      ? '  ✅ GSC property: ' + probe.property
      : '  ⚠ GSC: no property returned clicks — report will have an empty Search Console section');
    results.push(lines.join('\n'));
  });
  Logger.log('testConnections — ' + periods.current.label +
             '\n  folder: ' + FOLDER_ID + '\n\n' + results.join('\n\n'));
  return results;
}

/** Run ONCE. Collects on the 1st of each month at 6am UTC. */
function setupMonthlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'collectMonthlyData') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('collectMonthlyData').timeBased().onMonthDay(1).atHour(6).create();
  Logger.log('✅ Monthly trigger set: collectMonthlyData, day 1, 06:00 UTC.');
}

function removeMonthlyTrigger() {
  let removed = 0;
  ScriptApp.getProjectTriggers().forEach(t => {
    if (t.getHandlerFunction() === 'collectMonthlyData') { ScriptApp.deleteTrigger(t); removed++; }
  });
  Logger.log(removed > 0 ? '✅ Trigger removed.' : 'ℹ️ No trigger found.');
}
