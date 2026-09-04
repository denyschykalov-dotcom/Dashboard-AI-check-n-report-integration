"""Audit a client's report end to end against the client sheet.

The client sheet is the source of truth. This script walks the whole path:

    sheet tab  ->  resolver payload  ->  window.DATA  ->  rendered HTML

and checks each hop, because a wrong number can enter at any of them (the
branded-share bug was in the resolver; the zeroed prev/yoy comparison next to
it was in the export mapping; four silently blank sections were in the render).

Two kinds of check:

1. *Numbers.* Every figure in ``window.DATA`` is recomputed straight from the
   sheet rows it came from and diffed. No tolerance for real differences.
2. *Render.* The report is loaded in headless Chrome for several data shapes
   (both comparisons, one comparison, no previous month, no year-ago month,
   current month only, single blocks). ``renderAll`` wraps each section in a
   try/catch that swallows a throw into ``console.error``, so a crashed section
   just comes out blank with no visible error — the probe hooks console.error
   to catch exactly that.

Usage:
    python scripts/audit_report_data.py <sheet_id> [--name NAME] [--domain DOMAIN]
    python scripts/audit_report_data.py --all-clients      # every client in the DB

Needs the Google Sheets credentials the app itself uses (GOOGLE_SHEETS_* in
.env) and google-chrome on PATH.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import subprocess
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.report_builder.block_catalog import get_block  # noqa: E402
from backend.app.report_builder.data_sources import ga4, gsc, periods  # noqa: E402
from backend.app.report_builder.data_sources.base import ResolveContext  # noqa: E402
from backend.app.report_builder.data_sources.sheets_client import (  # noqa: E402
    fetch_tab_values,
    list_sheet_tabs,
)
from backend.app.report_builder.export import _build_data, build_preview_html  # noqa: E402

SHEET_BLOCKS = ["ga4_summary", "ga4_top_pages", "ga4_monetization", "ga4_ai_traffic",
                "gsc_summary", "gsc_top_queries"]

# Sheet tabs the report design deliberately does not read. Listed so a reviewer
# can tell "unused by design" from "we forgot to wire it up".
UNUSED_TABS = {"GA4 Countries", "GA4 Devices", "GSC Countries", "GSC Devices",
               "Monthly History", "Test Log", "Sheet1", "GA4 AI Summary"}


# --- hop 1: the sheet ---------------------------------------------------------

def fetch_sheet(sheet_id: str) -> dict[str, list[dict[str, str]]]:
    titles = sorted(list_sheet_tabs(sheet_id))
    raw = fetch_tab_values(sheet_id, titles)
    out = {}
    for tab, rows in raw.items():
        if not rows:
            out[tab] = []
            continue
        header = rows[0]
        out[tab] = [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]
    return out


def tab_rows(sheet, tab, period):
    return [r for r in sheet.get(tab, []) if (r.get("Period") or "").strip() == period]


def one_row(sheet, tab, period):
    rows = tab_rows(sheet, tab, period)
    return rows[0] if rows else None


# --- hop 2+3: resolvers and the export mapping --------------------------------

def resolve_blocks(sheet_id: str, name: str, domain: str) -> dict:
    client = SimpleNamespace(name=name, domain=domain, ga4_sheet_id=sheet_id)
    context = ResolveContext(client=client, period_label="", now=datetime.now(timezone.utc),
                             session=None, cache={})
    out = {}
    for key in SHEET_BLOCKS:
        module = ga4 if key.startswith("ga4") else gsc
        result = module.resolve(get_block(key), context)
        out[key] = {"status": result.status, "data": result.data,
                    "reason": result.unavailable_reason}
    return out


def current_label(resolved: dict) -> str:
    """The report's own period label, as the app would store it."""
    for value in resolved.values():
        period = (value.get("data") or {}).get("period")
        if period:
            return period
    return ""


def build_window_data(resolved: dict, name: str, domain: str) -> dict:
    return _build_data(
        period_label=current_label(resolved), default_comparison="mom,yoy",
        prepared=datetime.now(timezone.utc).date().isoformat(),
        blocks=[{"block_type_key": k, "status": v["status"], "data": v["data"],
                 "comment": None, "unavailable_reason": v["reason"], "position": i}
                for i, (k, v) in enumerate(resolved.items())],
        client_name=name, client_domain=domain,
    )


# --- the number check ---------------------------------------------------------

# (DATA key, sheet tab, [(DATA field, sheet column)]) for the flat KPI buckets.
KPI_SPECS = [
    ("ga4", "GA4 Summary", [
        ("sessions", "Sessions"), ("organic", "Organic Sessions"),
        ("users", "Total Users"), ("newUsers", "New Users"),
        ("returning", "Returning Users"), ("engaged", "Engaged Sessions"),
        ("engRate", "Engagement Rate %"), ("bounce", "Bounce Rate %"),
        ("duration", "Avg Session Duration (s)"), ("pageViews", "Page Views"),
        ("pps", "Pages/Session"), ("keyEvents", "Key Events")]),
    ("gsc", "GSC Summary", [
        ("clicks", "Clicks"), ("impressions", "Impressions"),
        ("ctr", "CTR %"), ("position", "Avg Position")]),
    ("gscPos", "GSC Positions", [
        ("top3", "Top-3"), ("top5", "Top-5"), ("top10", "Top-10"),
        ("top20", "Top-20"), ("top50", "Top-50"), ("total", "Total Sampled")]),
    ("ecom", "GA4 Ecommerce", [
        ("purchases", "Purchases"), ("revenue", "Revenue"),
        ("addToCart", "Add to Carts"), ("checkouts", "Checkouts")]),
    ("aiSummary", "GA4 AI Summary", [
        ("sessions", "Total AI Sessions"), ("engaged", "Engaged Sessions"),
        ("engRate", "Engagement Rate %")]),
]

# (DATA key, sheet tab, sheet column, DATA field) for the per-period row lists.
LIST_SPECS = [
    ("ga4daily", "GA4 Daily", "Sessions", "sessions"),
    ("channels", "GA4 Channels", "Sessions", "sessions"),
    ("gscDaily", "GSC Daily", "Clicks", "clicks"),
]


def check_numbers(sheet: dict, data: dict) -> list[str]:
    """Recompute every DATA figure from the sheet. Returns the mismatches."""
    bad: list[str] = []
    meta = data["meta"]
    lbl, pmap = meta["LBL"], meta["P"]
    windows = [(pmap[pk], lbl[pmap[pk]]) for pk in ("cur", "prev", "yoy") if pmap.get(pk)]

    def diff(where, sheet_value, got, tol=0.6):
        if abs(periods.num(sheet_value) - periods.num(got)) > tol:
            bad.append(f"{where}: sheet={sheet_value!r} report={got!r}")

    for data_key, tab, fields in KPI_SPECS:
        bucket = data.get(data_key)
        if bucket is None:  # block not in this report
            continue
        for key, label in windows:
            row, got = one_row(sheet, tab, label), bucket.get(key)
            if row is None or got is None:
                if bool(row) != bool(got):
                    bad.append(f"{data_key}[{label}]: sheet row={bool(row)} DATA={bool(got)}")
                continue
            for field, column in fields:
                diff(f"{data_key}[{label}].{field}", row.get(column), got.get(field))

    for data_key, tab, column, field in LIST_SPECS:
        bucket = data.get(data_key)
        if bucket is None:
            continue
        for key, label in windows:
            rows, got = tab_rows(sheet, tab, label), bucket.get(key)
            if got is None:
                bad.append(f"{data_key}[{label}] absent from DATA (sheet has {len(rows)} rows)")
                continue
            diff(f"{data_key}[{label}] row count", len(rows), len(got), tol=0)
            diff(f"{data_key}[{label}] {column} sum",
                 sum(periods.num(r.get(column)) for r in rows),
                 sum(periods.num(x.get(field)) for x in got))

    # branded share: the total must be the window's GSC Queries clicks, and the
    # share must be that total's own arithmetic (this is where it went wrong).
    for key, label in windows:
        got = (data.get("branded") or {}).get(key)
        rows = tab_rows(sheet, "GSC Queries", label)
        if got is None:
            if "branded" in data:
                bad.append(f"branded[{label}] absent from DATA")
            continue
        diff(f"branded[{label}].total", sum(periods.num(r.get("Clicks")) for r in rows),
             got.get("total"))
        if periods.num(got.get("total")):
            diff(f"branded[{label}].share",
                 round(periods.num(got.get("branded")) / periods.num(got.get("total")) * 100, 1),
                 got.get("share"), tol=0.15)

    # top tables: the #1 row must be the sheet's best row for that window
    for data_key, tab, id_col, id_field, sort_col in (
        ("gscQueries", "GSC Queries", "Query", "query", "Clicks"),
        ("gscTopPages", "GSC Top Pages", "Page", "page", "Clicks"),
        ("ga4TopPages", "GA4 Top Pages", "Landing Page", "page", "Sessions"),
    ):
        bucket = data.get(data_key)
        if not bucket or not pmap.get("cur"):
            continue
        label = lbl[pmap["cur"]]
        rows = sorted(tab_rows(sheet, tab, label), key=lambda r: -periods.num(r.get(sort_col)))
        got = bucket.get(pmap["cur"]) or []
        if not rows or not got:
            continue
        if str(rows[0].get(id_col)).strip() != str(got[0].get(id_field)).strip():
            bad.append(f"{data_key} #1: sheet={rows[0].get(id_col)!r} DATA={got[0].get(id_field)!r}")
    return bad


# --- the render check ---------------------------------------------------------

PROBE_HEAD = """<script>
window.__errs=[];
window.onerror=function(m,u,l,c){window.__errs.push(String(m)+' @'+l+':'+c);};
var _ce=console.error.bind(console);
console.error=function(){window.__errs.push(Array.prototype.map.call(arguments,String).join(' '));_ce.apply(null,arguments);};
</script>"""

PROBE_BODY = """<script>window.addEventListener('load',function(){
var s={};document.querySelectorAll('section[id^="b"]').forEach(function(x){
 s[x.id]={visible:x.offsetParent!==null&&x.style.display!=='none',
          chars:(x.innerText||'').replace(/\\s+/g,' ').trim().length};});
var d=document.createElement('div');d.id='__probe';
d.textContent=JSON.stringify({errors:window.__errs,sections:s});
document.body.appendChild(d);});</script>"""


def drop_comparison(blocks: list[dict], which: list[str]) -> list[dict]:
    """Simulate a sheet missing the previous and/or year-ago month."""
    kill = {"prev": ("previous_period", "_previous", "previous"),
            "yoy": ("yoy_period", "_yoy", "yoy")}
    blocks = json.loads(json.dumps(blocks))
    for block in blocks:
        payload = block.get("data")
        if not isinstance(payload, dict):
            continue
        for name in which:
            label_key, suffix, sub = kill[name]
            payload[label_key] = None
            for field in [f for f in payload if f.endswith(suffix)]:
                payload.pop(field)
            for group in ("kpis", "positions", "site_wide", "organic", "ai", "summary"):
                if isinstance(payload.get(group), dict):
                    payload[group].pop(sub, None)
    return blocks


def render_probe(blocks: list[dict], comparison: str, name: str, domain: str, out_dir: Path,
                 case: str, period_label: str) -> dict:
    html = build_preview_html(period_label=period_label, default_comparison=comparison, blocks=blocks,
                              client_name=name, client_domain=domain, editable=False)
    html = html.replace("<head>", "<head>" + PROBE_HEAD, 1)
    html = html.replace("</body>", PROBE_BODY + "</body>", 1)
    path = out_dir / (re.sub(r"[^a-z0-9]+", "_", case.lower()) + ".html")
    path.write_text(html, encoding="utf-8")
    dom = subprocess.run(
        ["google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
         "--virtual-time-budget=6000", "--dump-dom", f"file://{path}"],
        capture_output=True, text=True, timeout=180).stdout
    found = re.search(r'<div id="__probe">(.*?)</div>', dom, re.S)
    if not found:
        return {"errors": ["page never finished rendering"], "sections": {}}
    return json.loads(html_mod.unescape(found.group(1)))


def render_cases(resolved: dict, name: str, domain: str, out_dir: Path) -> list[str]:
    period_label = current_label(resolved)
    base = [{"block_type_key": k, "status": v["status"], "data": v["data"], "comment": None,
             "unavailable_reason": v["reason"], "position": i}
            for i, (k, v) in enumerate(resolved.items())]
    cases = {
        "all blocks, mom+yoy": (base, "mom,yoy"),
        "mom only": (base, "mom"),
        "yoy only": (base, "yoy"),
        "no previous month": (drop_comparison(base, ["prev"]), "mom,yoy"),
        "no year-ago month": (drop_comparison(base, ["yoy"]), "mom,yoy"),
        "current month only": (drop_comparison(base, ["prev", "yoy"]), "mom,yoy"),
    }
    for key in SHEET_BLOCKS:
        cases[f"{key} alone"] = ([b for b in base if b["block_type_key"] == key], "mom,yoy")

    failures = []
    for case, (blocks, comparison) in cases.items():
        probe = render_probe(blocks, comparison, name, domain, out_dir, case, period_label)
        errors = probe["errors"]
        thin = [s for s, v in probe["sections"].items() if v["visible"] and v["chars"] < 40]
        status = "FAIL" if errors else "ok"
        print(f"    [{status:4s}] {case}")
        for error in errors:
            print(f"           ! {error}")
            failures.append(f"{case}: {error}")
        if thin:
            print(f"           visible but near-empty: {thin}")
    return failures


# --- driver -------------------------------------------------------------------

def audit(sheet_id: str, name: str, domain: str, out_dir: Path) -> int:
    print(f"\n=== {name} ({domain}) sheet {sheet_id} ===")
    sheet = fetch_sheet(sheet_id)
    unread = sorted(set(sheet) - UNUSED_TABS - _tabs_the_resolvers_read())
    if unread:
        print(f"  note: sheet tabs no resolver reads: {unread}")

    resolved = resolve_blocks(sheet_id, name, domain)
    for key, value in resolved.items():
        print(f"  {key:20s} {value['status']}"
              + (f" — {value['reason']}" if value["reason"] else ""))

    data = build_window_data(resolved, name, domain)
    print("  numbers: sheet -> window.DATA")
    mismatches = check_numbers(sheet, data)
    if mismatches:
        for line in mismatches:
            print(f"    MISMATCH {line}")
    else:
        print("    all figures match the sheet")

    print("  render: headless Chrome, per data shape")
    failures = render_cases(resolved, name, domain, out_dir)
    return len(mismatches) + len(failures)


def _tabs_the_resolvers_read() -> set[str]:
    names = set()
    for module in (ga4, gsc):
        for aliases in module._TAB_ALIASES.values():
            names.update(aliases)
    return names


def load_clients(source: str) -> list[tuple[str, str, str]]:
    """Client rows to audit. Defaults to the local SQLite file: this project's
    application database is Supabase and reading it costs egress, so that needs
    asking for by name."""
    if source == "app":
        from backend.app.db import SessionLocal
        from backend.app.models import Client
        with SessionLocal() as session:
            return [(c.ga4_sheet_id, c.name, c.domain)
                    for c in session.query(Client).all() if c.ga4_sheet_id]
    import sqlite3
    path = Path(source)
    if not path.exists():
        raise SystemExit(f"no such SQLite file: {path} (pass --clients-db, or 'app')")
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "select ga4_sheet_id, name, domain from Dashboard_ReportBuilder_clients "
        "where ga4_sheet_id is not null and ga4_sheet_id != ''").fetchall()
    connection.close()
    return [(sheet_id, name, domain) for sheet_id, name, domain in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sheet_id", nargs="?", help="Google Sheet id of the client sheet")
    parser.add_argument("--name", default="Audit Client", help="client name (drives brand matching)")
    parser.add_argument("--domain", default="audit.example", help="client domain")
    parser.add_argument("--all-clients", action="store_true",
                        help="audit every client that has a sheet linked, read from --clients-db")
    parser.add_argument("--clients-db", default="local_dev.db",
                        help="SQLite file to read the client list from (default: local_dev.db). "
                             "Pass 'app' to use the configured application database instead — "
                             "that is Supabase in this project, and reading it adds to the egress "
                             "bill, so only do it deliberately.")
    parser.add_argument("--out", default=None, help="directory for the rendered HTML (default: temp)")
    args = parser.parse_args()

    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="report-audit-"))
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = []
    if args.all_clients:
        targets.extend(load_clients(args.clients_db))
    elif args.sheet_id:
        targets.append((args.sheet_id, args.name, args.domain))
    else:
        parser.error("give a sheet_id or --all-clients")

    problems = sum(audit(sheet_id, name, domain, out_dir) for sheet_id, name, domain in targets)
    print(f"\nrendered reports in {out_dir}")
    print("AUDIT CLEAN" if not problems else f"AUDIT FOUND {problems} PROBLEM(S)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
