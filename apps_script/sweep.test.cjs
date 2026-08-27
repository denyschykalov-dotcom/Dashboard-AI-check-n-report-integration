/**
 * Check for sweepUnmanagedTabs_ — the one destructive branch in collector.gs.
 *
 * Run: node apps_script/sweep.test.cjs
 *
 * It loads the real collector.gs into a sandbox with stub Apps Script globals,
 * so the behaviour tested is the shipped code, not a copy of it. The sweep
 * deletes sheets, so the cases that matter are the ones it must NOT touch.
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadCollector() {
  const source = fs.readFileSync(path.join(__dirname, 'collector.gs'), 'utf8');
  const logs = [];
  // Note: collector.gs declares its state with `let`, which lives in the
  // context's lexical scope rather than on the sandbox object — so it is set
  // via runInContext below, not by assigning a property.
  const sandbox = {
    Logger: { log: (m) => logs.push(String(m)) },
    Utilities: { formatDate: () => '2026-08-27 00:00:00' },
    PropertiesService: undefined,
    SpreadsheetApp: undefined,
    DriveApp: undefined,
    UrlFetchApp: undefined,
    ScriptApp: undefined,
    AnalyticsData: undefined,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return { sandbox, logs };
}

/** A spreadsheet stub that records which sheets were deleted. */
function fakeSpreadsheet(tabs) {
  const sheets = tabs.map(([name, lastRow]) => ({
    getName: () => name,
    getLastRow: () => lastRow,
  }));
  const deleted = [];
  return {
    ss: {
      getSheets: () => sheets.slice(),
      deleteSheet: (sh) => deleted.push(sh.getName()),
    },
    deleted,
  };
}

function run(tabs, written) {
  const { sandbox } = loadCollector();
  sandbox.__written = written;
  vm.runInContext('writtenTabs = __written;', sandbox);
  const { ss, deleted } = fakeSpreadsheet(tabs);
  const removed = sandbox.sweepUnmanagedTabs_(ss);
  return { deleted, removed };
}

// ── The case this exists for: a stale tab from an older script version.
{
  const { deleted } = run(
    [['GA4 Summary', 4], ['GA4 AI Assistants', 101], ['GSC Summary', 4]],
    { 'GA4 Summary': true, 'GSC Summary': true },
  );
  assert.deepStrictEqual(deleted, ['GA4 AI Assistants'],
    'a GA4/GSC tab this run did not write is stale and must go');
}

// ── Monthly History is irreplaceable: Search Console only keeps 16 months.
{
  const { deleted } = run(
    [['Monthly History', 14], ['GA4 Summary', 4]],
    { 'GA4 Summary': true },
  );
  assert.deepStrictEqual(deleted, [], 'Monthly History with rows must never be deleted');
}

// ── The same tab while still empty — someone added it by hand and it has no
// rows yet. Only the explicit name guard saves it here; the "keep tabs with
// content" rule does not apply, and the empty-sheet rule would delete it.
{
  const { deleted } = run(
    [['Monthly History', 0], ['GA4 Summary', 4]],
    { 'GA4 Summary': true },
  );
  assert.deepStrictEqual(deleted, [],
    'an empty Monthly History must survive — it is the tab we can never rebuild');
}

// ── A colleague's own tab is not collector output — keep it.
{
  const { deleted } = run(
    [['My notes', 12], ['Client questions', 3], ['GA4 Summary', 4]],
    { 'GA4 Summary': true },
  );
  assert.deepStrictEqual(deleted, [], 'non-collector tabs with content are kept');
}

// ── The default sheet a new spreadsheet ships with: empty, locale-named.
{
  const { deleted } = run(
    [['Аркуш1', 0], ['GA4 Summary', 4]],
    { 'GA4 Summary': true },
  );
  assert.deepStrictEqual(deleted, ['Аркуш1'],
    'an empty leftover default sheet is removed whatever the locale named it');
}

// ── Everything written this run survives, including Test Log.
{
  const written = { 'GA4 Summary': true, 'GA4 AI Traffic': true, 'Test Log': true };
  const { deleted } = run(
    [['GA4 Summary', 4], ['GA4 AI Traffic', 9], ['Test Log', 3]],
    written,
  );
  assert.deepStrictEqual(deleted, [], 'tabs authored this run are never swept');
}

// ── A feature turned off leaves its tab behind; it is unused, so remove it.
{
  const { deleted } = run(
    [['GA4 Page Paths', 150], ['GA4 Summary', 4]],
    { 'GA4 Summary': true },
  );
  assert.deepStrictEqual(deleted, ['GA4 Page Paths'],
    'a tab for a disabled feature is stale output, not live data');
}

// ═══════════════════════════════════════════════════════
//  getOrCreateSpreadsheet_ — the "client has no sheet yet" path
// ═══════════════════════════════════════════════════════

/** Drive + SpreadsheetApp stubs recording what got created and moved where. */
function fakeDrive(existingNames) {
  const events = [];
  const folder = {
    getFilesByName: (name) => {
      const hit = existingNames.indexOf(name) !== -1;
      let served = false;
      return {
        hasNext: () => hit && !served,
        next: () => { served = true; return { id: 'existing-' + name, name: name }; },
      };
    },
  };
  return {
    events,
    DriveApp: {
      getFolderById: (id) => { events.push('getFolderById:' + id); return folder; },
      getFileById: (id) => ({ moveTo: (f) => events.push('moveTo:' + id) }),
    },
    SpreadsheetApp: {
      create: (name) => { events.push('create:' + name); return { getId: () => 'new-' + name }; },
      open: (file) => { events.push('open:' + file.id); return { getId: () => file.id }; },
    },
  };
}

function runGetOrCreate(domain, existingNames) {
  const { sandbox } = loadCollector();
  const drive = fakeDrive(existingNames);
  sandbox.DriveApp = drive.DriveApp;
  sandbox.SpreadsheetApp = drive.SpreadsheetApp;
  const ss = sandbox.getOrCreateSpreadsheet_(domain);
  return { id: ss.getId(), events: drive.events };
}

// ── No spreadsheet in the folder: create one, named exactly the domain, and
// move it into the folder the report backend scans. Naming it anything else
// would leave the backend unable to find it by name.
{
  const { id, events } = runGetOrCreate('newclient.com', []);
  assert.strictEqual(id, 'new-newclient.com');
  assert.ok(events.indexOf('create:newclient.com') !== -1, 'must create the spreadsheet');
  assert.ok(events.some((e) => e.indexOf('moveTo:') === 0), 'must move it into the folder');
}

// ── A spreadsheet with that exact name already there: reuse it, never create a
// second one. A duplicate name makes the backend's lookup ambiguous, and it
// caches the id it picked — so it would keep reading whichever it guessed.
{
  const { id, events } = runGetOrCreate('yamahaonlineparts.com', ['yamahaonlineparts.com']);
  assert.strictEqual(id, 'existing-yamahaonlineparts.com');
  assert.ok(!events.some((e) => e.indexOf('create:') === 0),
    'an existing sheet must be reused, not duplicated');
}

console.log('sweep.test.cjs — all 9 checks passed');
