"""Build a self-contained, client-ready HTML report from a saved report.

Renders through the generalized ``report_template.html`` (derived from the
OnebyOne example) so the exported file matches that design exactly — the same
CSS, KPI cards, inline-SVG charts, MoM/YoY toggle, print and re-save controls,
and editable specialist-notes boxes.

This module's job is to translate the stored per-block data (in this feature's
own shapes) into the ``window.DATA`` object those render functions expect, plus
a ``report`` chrome object (which blocks are selected/available, and the saved
comments), then inject it into the template.
"""

from __future__ import annotations

import typing

import html
import json
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from backend.app.models import Report, ReportBlock
from backend.app.report_builder import localization
from backend.app.report_builder.block_catalog import get_block


_TEMPLATE_PATH = Path(__file__).resolve().parent / "report_template.html"

# block_type_key -> template section id. Public: a block only has somewhere to
# show a comment if it appears here (ai_commentary reads this to decide which
# sections get a generated comment).
#
# The 8 ai_visibility_* blocks all share one section (b15) — a specialist can
# select several model/window variants at once, and they render as tabs inside
# that single section rather than one section each.
SECTION_BY_KEY = {
    "intro_header": "b1",
    "search_industry": "b2",
    "ahrefs_domain_analysis": "b3",
    "ahrefs_top_movers": "b4",
    "ga4_summary": "b5",
    "ga4_top_pages": "b6",
    "ga4_monetization": "b7",
    "ga4_ai_traffic": "b8",
    "gsc_summary": "b9",
    "gsc_top_queries": "b10",
    "se_ranking_keywords": "b11",
    "work_completed": "b12",
    "planned_works": "b13",
    "summary": "b14",
    "ai_visibility_all_1mo": "b15",
    "ai_visibility_gpt_1mo": "b15",
    "ai_visibility_gemini_1mo": "b15",
    "ai_visibility_grok_1mo": "b15",
    "ai_visibility_all_6mo": "b15",
    "ai_visibility_gpt_6mo": "b15",
    "ai_visibility_gemini_6mo": "b15",
    "ai_visibility_grok_6mo": "b15",
}

_AI_VISIBILITY_MODEL_ORDER = ["all", "gpt", "gemini", "grok"]
_AI_VISIBILITY_MODEL_LABELS = {"all": "All models", "gpt": "GPT", "gemini": "Gemini", "grok": "Grok"}
_AI_VISIBILITY_WINDOW_LABELS = {"last_month": "Last month", "last_6_months": "Last 6 months"}

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@lru_cache(maxsize=1)
def _template() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


# --- period helpers ----------------------------------------------------------

def _parse_label(label: typing.Optional[str]) -> typing.Optional[date]:
    if not label:
        return None
    try:
        return datetime.strptime(label.strip(), "%b %Y").date().replace(day=1)
    except (ValueError, AttributeError):
        return None


def _key(label: typing.Optional[str]) -> typing.Optional[str]:
    d = _parse_label(label)
    return f"{d.year:04d}-{d.month:02d}" if d else (label or None)


def _long(label: typing.Optional[str]) -> str:
    d = _parse_label(label)
    if not d:
        return label or ""
    full = ["January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"]
    return f"{full[d.month - 1]} {d.year}"


def _mode_label(cur_label: typing.Optional[str], other_label: typing.Optional[str], mode: str) -> str:
    """The toggle caption for one comparison, e.g. "Jun 2026 vs May 2026 · MoM".

    "MoM" only reads right for a single-month report; a multi-month window (which
    never parses as a month label) compares against the previous *period*.
    """
    cur = cur_label or ""
    if mode == "yoy":
        suffix = "YoY"
    else:
        suffix = "MoM" if _parse_label(cur) else "Prev. period"
    return f"{cur} vs {other_label} · {suffix}" if other_label else f"{cur} · {suffix}"


def _comparison_modes(value: typing.Optional[str]) -> list[str]:
    """The stored comparison field ("mom", "yoy", "mom,yoy") → ordered mode list."""
    out: list[str] = []
    for raw in (value or "").split(","):
        mode = raw.strip().lower()
        if mode in ("mom", "yoy") and mode not in out:
            out.append(mode)
    return out or ["mom"]


def _next_long(label: typing.Optional[str]) -> str:
    d = _parse_label(label)
    if not d:
        return ""
    ny, nm = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    full = ["January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"]
    return f"{full[nm - 1]} {ny}"


# --- data mapping ------------------------------------------------------------

def _num(value: typing.Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _zero_ga4() -> dict:
    return {k: 0 for k in ["sessions", "organic", "users", "newUsers", "returning", "engaged",
                           "engRate", "bounce", "duration", "pageViews", "pps", "keyEvents"]}


def _ga4_kpi(k: typing.Optional[dict]) -> dict:
    if not k:
        return _zero_ga4()
    return {
        "sessions": _num(k.get("sessions")), "organic": _num(k.get("organic_sessions")),
        "users": _num(k.get("total_users")), "newUsers": _num(k.get("new_users")),
        "returning": _num(k.get("returning_users")), "engaged": _num(k.get("engaged_sessions")),
        "engRate": _num(k.get("engagement_rate")), "bounce": _num(k.get("bounce_rate")),
        "duration": _num(k.get("avg_session_duration_seconds")), "pageViews": _num(k.get("page_views")),
        "pps": _num(k.get("pages_per_session")), "keyEvents": _num(k.get("key_events")),
    }


def _ecom_kpi(k: typing.Optional[dict]) -> dict:
    if not k:
        return {"purchases": 0, "revenue": 0, "addToCart": 0, "checkouts": 0}
    return {
        "purchases": _num(k.get("purchases")), "revenue": _num(k.get("revenue")),
        "addToCart": _num(k.get("add_to_carts")), "checkouts": _num(k.get("checkouts")),
    }


def _ai_kpi(k: typing.Optional[dict]) -> dict:
    if not k:
        return {"sessions": 0, "engaged": 0, "engRate": 0}
    return {
        "sessions": _num(k.get("total_ai_sessions")), "engaged": _num(k.get("engaged_sessions")),
        "engRate": _num(k.get("engagement_rate")),
    }


def _gsc_kpi(k: typing.Optional[dict]) -> dict:
    if not k:
        return {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
    return {
        "clicks": _num(k.get("clicks")), "impressions": _num(k.get("impressions")),
        "ctr": _num(k.get("ctr")), "position": _num(k.get("avg_position")),
    }


def _gsc_pos(k: typing.Optional[dict]) -> dict:
    if not k:
        return {"top3": 0, "top5": 0, "top10": 0, "top20": 0, "top50": 0, "total": 0}
    return {
        "top3": _num(k.get("top3")), "top5": _num(k.get("top5")), "top10": _num(k.get("top10")),
        "top20": _num(k.get("top20")), "top50": _num(k.get("top50")), "total": _num(k.get("total_sampled")),
    }


def _url_path(url: typing.Optional[str], domain: str) -> str:
    if not url:
        return ""
    for pref in (f"https://{domain}", f"http://{domain}", f"https://www.{domain}", f"http://www.{domain}"):
        if url.startswith(pref):
            return url[len(pref):] or "/"
    return url


def _task_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1] if url else ""


def _build_data(
    *,
    period_label: str,
    default_comparison: str,
    prepared: str,
    blocks: list[dict],
    client_name: str,
    client_domain: str,
    customization: typing.Optional[dict] = None,
    editable: bool = False,
    language: str = localization.DEFAULT_LANGUAGE,
) -> dict:
    ok = {b["block_type_key"]: (b.get("data") or {}) for b in blocks if b.get("status") == "ok"}
    data: dict[str, typing.Any] = {}

    # -- period resolution: prefer a metric block carrying cur/prev/yoy labels --
    cur_label = period_label
    prev_label = yoy_label = None
    for key in ("ga4_summary", "gsc_summary", "ahrefs_domain_analysis", "ga4_monetization", "ga4_ai_traffic"):
        d = ok.get(key)
        if d and d.get("period"):
            cur_label = d.get("period") or cur_label
            prev_label = d.get("previous_period") or prev_label
            yoy_label = d.get("yoy_period") or yoy_label
            if prev_label and yoy_label:
                break

    cur_k, prev_k, yoy_k = _key(cur_label), _key(prev_label), _key(yoy_label)
    lbl = {}
    P = {}
    if cur_k:
        P["cur"] = cur_k
        lbl[cur_k] = cur_label
    if prev_k:
        P["prev"] = prev_k
        lbl[prev_k] = prev_label
    if yoy_k:
        P["yoy"] = yoy_k
        lbl[yoy_k] = yoy_label

    # The comparisons the specialist chose, in order — each becomes a toggle in
    # the report and the first is the one it opens on. A comparison whose window
    # didn't resolve is dropped, unless that would leave no toggle at all.
    mode_specs = {
        "mom": {"id": "mom", "cmp": prev_k, "label": _mode_label(cur_label, prev_label, "mom")},
        "yoy": {"id": "yoy", "cmp": yoy_k, "label": _mode_label(cur_label, yoy_label, "yoy")},
    }
    chosen = [mode_specs[mode] for mode in _comparison_modes(default_comparison)]
    modes = [mode for mode in chosen if mode["cmp"]] or chosen[:1]

    # Period strings are assembled from month names, so they are localized here
    # rather than by the template's post-render pass — the Markdown export has no
    # JS to run, and the <title>/header substitutions happen server-side.
    lang = localization.normalize_language(language)
    # Labels that end up as *values* inside DATA (rather than as text in the
    # template's own markup) are translated here — the post-render pass only sees
    # rendered text nodes, and it must not rewrite data the report is reporting on.
    _t = localization.translator(lang)

    def _period(text: str) -> str:
        return localization.localize_period_label(text, lang)

    for mode in modes:
        mode["label"] = _period(mode.get("label") or "")
    lbl = {key: _period(value) for key, value in lbl.items()}

    data["meta"] = {
        "client": client_name,
        "domain": client_domain,
        "period": _period(_long(cur_label)),
        "periodLong": _period(_long(cur_label)),
        "nextPeriodLong": _period(_next_long(cur_label)),
        "prepared": prepared,
        "cur": cur_k, "prev": prev_k, "yoy": yoy_k,
        "P": P, "LBL": lbl,
        # The comparison toggles the report offers, and which one it opens on.
        "modes": modes,
        "defaultMode": modes[0]["id"] if modes else "mom",
    }

    # -- GA4 (b5) --
    if "ga4_summary" in ok:
        d = ok["ga4_summary"]
        kpis = d.get("kpis") or {}
        data["ga4"] = {}
        if cur_k:
            data["ga4"][cur_k] = _ga4_kpi(kpis.get("current"))
        if prev_k:
            data["ga4"][prev_k] = _ga4_kpi(kpis.get("previous"))
        if yoy_k:
            data["ga4"][yoy_k] = _ga4_kpi(kpis.get("yoy"))
        def _channels(rows):
            return [
                {"channel": c.get("channel", ""), "sessions": _num(c.get("sessions")),
                 "engaged": _num(c.get("engaged_sessions")), "users": _num(c.get("users"))}
                for c in (rows or [])
            ]

        def _daily(rows):
            return [
                {"sessions": _num(x.get("sessions")), "engaged": _num(x.get("engaged_sessions")),
                 "users": _num(x.get("users"))}
                for x in (rows or [])
            ]

        def _events(rows):
            out = {}
            for e in rows or []:
                out[e.get("event_name", "")] = {"count": _num(e.get("count")), "users": _num(e.get("users"))}
            return out

        # current + (when the resolver provides them) the comparison periods, so the
        # daily area charts draw a dashed prev/yoy line and the channel/event tables
        # show a real delta that responds to the MoM/YoY toggle.
        data["channels"] = {}
        data["ga4daily"] = {}
        data["events"] = {}
        for pk, cur_key, dly_key, ev_key in (
            ("cur", "channels", "daily", "top_events"),
            ("prev", "channels_previous", "daily_previous", "top_events_previous"),
            ("yoy", "channels_yoy", "daily_yoy", "top_events_yoy"),
        ):
            k = P.get(pk)
            if not k:
                continue
            if cur_key in d:
                data["channels"][k] = _channels(d.get(cur_key))
            if dly_key in d:
                data["ga4daily"][k] = _daily(d.get(dly_key))
            if ev_key in d:
                data["events"][k] = _events(d.get(ev_key))

    # -- GA4 top pages (b6) --
    if "ga4_top_pages" in ok and cur_k:
        data["ga4TopPages"] = {cur_k: [
            {"page": p.get("page", ""), "sessions": _num(p.get("sessions")),
             "engaged": _num(p.get("engaged_sessions")), "keyEvents": _num(p.get("key_events")),
             "bounce": _num(p.get("bounce_rate"))}
            for p in ok["ga4_top_pages"].get("pages", [])
        ]}

    # -- GA4 monetization (b7) --
    if "ga4_monetization" in ok:
        sw = ok["ga4_monetization"].get("site_wide") or {}
        ai_ec = ok["ga4_monetization"].get("ai") or {}
        data["ecom"] = {}
        data["aiEcom"] = {}
        for pk, sub in (("cur", "current"), ("prev", "previous"), ("yoy", "yoy")):
            k = P.get(pk)
            if not k:
                continue
            data["ecom"][k] = _ecom_kpi(sw.get(sub))
            data["aiEcom"][k] = _ecom_kpi(ai_ec.get(sub))

    # -- GA4 AI traffic (b8) --
    if "ga4_ai_traffic" in ok:
        d = ok["ga4_ai_traffic"]
        summ = d.get("summary") or {}
        data["aiSummary"] = {}
        if cur_k:
            data["aiSummary"][cur_k] = _ai_kpi(summ.get("current"))
        if prev_k:
            data["aiSummary"][prev_k] = _ai_kpi(summ.get("previous"))
        if yoy_k:
            data["aiSummary"][yoy_k] = _ai_kpi(summ.get("yoy"))
        data["aiTools"] = [
            {"source": t.get("source", ""), "sessions": _num(t.get("sessions")),
             "engaged": _num(t.get("engaged_sessions"))}
            for t in d.get("tools", [])
        ]
        data["aiTopPages"] = [
            {"page": p.get("page", ""), "sessions": _num(p.get("sessions")),
             "engaged": _num(p.get("engaged_sessions"))}
            for p in d.get("top_pages", [])
        ]

    # -- GSC (b9) --
    if "gsc_summary" in ok:
        d = ok["gsc_summary"]
        kpis = d.get("kpis") or {}
        pos = d.get("positions") or {}
        data["gsc"] = {}
        data["gscPos"] = {}
        for pk, sub in (("cur", "current"), ("prev", "previous"), ("yoy", "yoy")):
            k = P.get(pk)
            if k:
                data["gsc"][k] = _gsc_kpi(kpis.get(sub))
                data["gscPos"][k] = _gsc_pos(pos.get(sub))
        def _gsc_daily(rows):
            return [
                {"clicks": _num(x.get("clicks")), "impressions": _num(x.get("impressions")),
                 "ctr": _num(x.get("ctr")), "position": _num(x.get("avg_position"))}
                for x in (rows or [])
            ]

        data["gscDaily"] = {}
        for pk, dly_key in (("cur", "daily"), ("prev", "daily_previous"), ("yoy", "daily_yoy")):
            k = P.get(pk)
            if k and dly_key in d:
                data["gscDaily"][k] = _gsc_daily(d.get(dly_key))
        b = d.get("branded") or {}
        data["branded"] = {}
        if cur_k:
            data["branded"][cur_k] = {"branded": _num(b.get("branded_clicks")),
                                      "total": _num(b.get("total_clicks")),
                                      "share": _num(b.get("branded_share_pct"))}
        # prev/yoy branded not computed by the resolver — zero rows so the table renders
        for k in (prev_k, yoy_k):
            if k:
                data["branded"][k] = {"branded": 0, "total": 0, "share": 0}

    # -- GSC queries/pages (b10) --
    if "gsc_top_queries" in ok and cur_k:
        d = ok["gsc_top_queries"]
        data["gscQueries"] = {cur_k: [
            {"query": q.get("query", ""), "clicks": _num(q.get("clicks")),
             "impressions": _num(q.get("impressions")), "ctr": _num(q.get("ctr")),
             "position": _num(q.get("avg_position"))}
            for q in d.get("queries", [])
        ]}
        data["gscTopPages"] = {cur_k: [
            {"page": p.get("page", ""), "clicks": _num(p.get("clicks")),
             "impressions": _num(p.get("impressions")), "ctr": _num(p.get("ctr")),
             "position": _num(p.get("avg_position"))}
            for p in d.get("pages", [])
        ]}

    # -- Ahrefs (b3) --
    if "ahrefs_domain_analysis" in ok:
        d = ok["ahrefs_domain_analysis"]
        m = d.get("metrics") or {}

        def ah(sub):
            k = m.get(sub) or {}
            return {
                "orgKw": _num(k.get("org_keywords")), "orgKw13": _num(k.get("org_keywords_top3")),
                "paidKw": _num(k.get("paid_keywords")), "orgTraffic": _num(k.get("org_traffic")),
                "orgCost": _num(k.get("org_cost_cents")), "paidTraffic": _num(k.get("paid_traffic")),
                "paidCost": _num(k.get("paid_cost_cents")), "paidPages": _num(k.get("paid_pages")),
            }
        bl = d.get("backlinks") or {}
        metrics = {}
        for pk, sub in (("cur", "current"), ("prev", "previous"), ("yoy", "yoy")):
            k = P.get(pk)
            if k:
                metrics[k] = ah(sub)
        data["ahrefs"] = {
            "domainRating": _num(d.get("domain_rating")), "ahrefsRank": _num(d.get("ahrefs_rank")),
            "backlinks": {"live": _num(bl.get("live")), "allTime": _num(bl.get("all_time")),
                          "liveRefdomains": _num(bl.get("live_refdomains")),
                          "allTimeRefdomains": _num(bl.get("all_time_refdomains"))},
            "metrics": metrics,
            "trend": d.get("trend", []),
        }

    # -- Ahrefs movers (b4) --
    if "ahrefs_top_movers" in ok:
        d = ok["ahrefs_top_movers"]

        def mover(rows):
            return [[
                _url_path(r.get("url", ""), client_domain), _num(r.get("traffic")),
                _num(r.get("traffic_prev")), _num(r.get("traffic_diff")), _num(r.get("keywords")),
                r.get("top_keyword", ""), _num(r.get("top_keyword_volume")),
                _num(r.get("position")), _num(r.get("position_prev")),
            ] for r in rows]
        data["ahrefsGainers"] = mover(d.get("gainers", []))
        data["ahrefsLosers"] = mover(d.get("losers", []))

    # -- ClickUp work (b12/b13) --
    # Each completed-work row is [task, task_id]: the task title is what the client
    # reads, and the ClickUp id links back to it. The Summary/description and
    # tracked-time columns were dropped from this block — tracked time is still
    # what decides which month a task belongs to (see clickup._done_tasks), it just
    # isn't reported.
    # Tasks the specialist struck off in the preview. Dropping them here rather
    # than in the template is what makes the client HTML, the PDF and the
    # Markdown export agree on one list.
    #
    # The *editable* preview is the exception: it keeps every task in the payload
    # and lets the template decide what to draw, because a removal has to stay
    # undoable — filtering them out server-side would leave "Restore all" with
    # nothing to put back.
    excluded_tasks = _normalize_excluded_tasks((customization or {}).get("excludedTasks"))

    def _kept(source_key, rows):
        if editable:
            return list(rows)
        dropped = set(excluded_tasks.get(source_key) or ())
        return [t for t in rows if _task_id(t.get("url", "")) not in dropped]

    def tasks(source_key):
        d = ok.get(source_key) or {}
        return [
            [t.get("name", ""), _task_id(t.get("url", ""))]
            for t in _kept(source_key, d.get("tasks", []))
        ]
    # Planned works (the ClickUp "Todo" stage) renders as a numbered plan rather
    # than a table, and reads as the mirror of Work completed: the task and its
    # due date, nothing else. The internal ClickUp description and the assignee
    # are deliberately dropped — the client is told what is planned, not who on
    # the team owns it or what the internal ticket notes say.
    def planned_items(source_key):
        d = ok.get(source_key) or {}
        out = []
        for t in _kept(source_key, d.get("tasks", [])):
            out.append({
                "name": t.get("name", ""),
                "taskId": _task_id(t.get("url", "")),
                "due": t.get("due_date") or "",
            })
        return out
    if "work_completed" in ok:
        data["workDone"] = tasks("work_completed")
    if "planned_works" in ok:
        planned = ok["planned_works"] or {}
        if planned.get("mode") == "manual":
            # Manually typed plan for the upcoming period (no ClickUp tasks).
            data["workPlannedManual"] = _comment_html(planned.get("text") or "")
            data["workPlanned"] = []
        else:
            data["workPlanned"] = planned_items("planned_works")

    # -- SE Ranking (b11) --
    sr = ok.get("se_ranking_keywords")
    data["seranking"] = {
        "status": "ok" if sr else "unavailable",
        "note": (sr or {}).get("note", ""),
        # One tab per position band (Top 3 / 10 / 30 / 50 / 100), each already
        # sorted by search volume by the resolver.
        "buckets": (sr or {}).get("buckets", []),
        "keywords": (sr or {}).get("keywords", []),
    }

    # -- AI Visibility (b15) — one section, a tab per selected model, each tab
    # carrying whichever window(s) (last month / last 6 months) were selected
    # for that model. A model/window combo that resolved unavailable still gets
    # an entry (with a reason) so its tab can say so instead of just being blank.
    models: dict[str, dict] = {}
    for b in blocks:
        if SECTION_BY_KEY.get(b.get("block_type_key")) != "b15":
            continue
        block_type = get_block(b.get("block_type_key") or "")
        if block_type is None or block_type.ai_visibility_model is None or block_type.ai_visibility_window is None:
            continue
        model = block_type.ai_visibility_model
        window = block_type.ai_visibility_window
        entry = models.setdefault(
            model,
            {
                "key": model,
                "label": _t(_AI_VISIBILITY_MODEL_LABELS.get(model, model)),
                "windows": {},
            },
        )
        if b.get("status") == "ok":
            d = b.get("data") or {}
            entry["windows"][window] = {
                "status": "ok",
                "window_label": _t(
                    d.get("window_label") or _AI_VISIBILITY_WINDOW_LABELS.get(window, window)
                ),
                "total_results": d.get("total_results", 0),
                "brand_matches": d.get("brand_matches", 0),
                "domain_matches": d.get("domain_matches", 0),
                "brand_match_rate": d.get("brand_match_rate", 0),
                "domain_match_rate": d.get("domain_match_rate", 0),
                "users": d.get("users", 0),
            }
        else:
            entry["windows"][window] = {
                "status": "unavailable",
                "window_label": _t(_AI_VISIBILITY_WINDOW_LABELS.get(window, window)),
                "reason": b.get("unavailable_reason") or "No data.",
            }
    data["aiVisibility"] = {
        "models": [models[key] for key in _AI_VISIBILITY_MODEL_ORDER if key in models]
    }

    # -- report chrome: which blocks are selected/available + comments --
    block_states: dict[str, dict] = {}
    comments: dict[str, str] = {}
    comments_raw: dict[str, str] = {}
    for b in blocks:
        sec = SECTION_BY_KEY.get(b.get("block_type_key"))
        if not sec:
            continue
        # Several ai_visibility_* blocks can share section b15 — once any of
        # them resolves ok, a later unavailable one must not downgrade it back.
        existing = block_states.get(sec)
        if existing is None or existing["status"] != "ok":
            block_states[sec] = {
                "selected": True,
                "status": b.get("status"),
                "reason": b.get("unavailable_reason"),
                "key": b.get("block_type_key"),
            }
        if b.get("comment"):
            comments[sec] = _comment_html(b["comment"])
            comments_raw[sec] = b["comment"]
    data["report"] = {"blocks": block_states, "comments": comments, "commentsRaw": comments_raw}
    data["customization"] = _normalize_customization(customization)
    data["editable"] = bool(editable)
    data["language"] = lang
    # The English->target vocabulary the template's post-render pass swaps in.
    # Empty for English, and empty until the cache is warmed — in both cases the
    # report simply renders in English.
    data["i18n"] = localization.load_ui_translations(lang)

    return data


# --- customization -----------------------------------------------------------

_TEXT_SCALES = {"small": 0.9, "normal": 1.0, "large": 1.14}
_TEXT_WEIGHTS = {"normal": "400", "bold": "700"}


def _normalize_panel(raw: typing.Optional[dict]) -> dict:
    """One panel's text config → the resolved shape the template applies."""
    raw = raw if isinstance(raw, dict) else {}
    scale = str(raw.get("scale", "normal"))
    if scale not in _TEXT_SCALES:
        scale = "normal"
    heading_weight = str(raw.get("headingWeight", "bold"))
    if heading_weight not in _TEXT_WEIGHTS:
        heading_weight = "bold"
    body_weight = str(raw.get("bodyWeight", "normal"))
    if body_weight not in _TEXT_WEIGHTS:
        body_weight = "normal"
    return {
        "scale": scale,
        "fontScale": _TEXT_SCALES[scale],
        "headingWeight": _TEXT_WEIGHTS[heading_weight],
        "bodyWeight": _TEXT_WEIGHTS[body_weight],
    }


def _normalize_customization(raw: typing.Optional[dict]) -> dict:
    """Sanitize the stored customization blob into the exact shape the template
    reads, filling defaults so the template never has to guard for missing keys.

    Shape: ``accent`` (report-wide), ``charts`` (per chart-slot variant),
    ``panels`` (per-block text config: size + heading/body weight),
    ``excludedTasks`` (ClickUp task ids the specialist struck off a block) and
    ``aiVisibilityShot`` (the dashboard overview screenshot as a data URL)."""
    raw = raw if isinstance(raw, dict) else {}

    accent = raw.get("accent")
    accent = accent if isinstance(accent, str) and accent.strip() else None

    charts = raw.get("charts") if isinstance(raw.get("charts"), dict) else {}
    charts = {str(k): str(v) for k, v in charts.items() if v}

    panels_raw = raw.get("panels") if isinstance(raw.get("panels"), dict) else {}
    panels = {str(k): _normalize_panel(v) for k, v in panels_raw.items() if isinstance(v, dict)}

    # The AI-visibility overview screenshot, an inline JPEG the report builder
    # captured from the dashboard. Only a data: image URL is accepted — the value
    # is written straight into an <img src>, so a remote or javascript: URL has no
    # business being there.
    shot = raw.get("aiVisibilityShot")
    if not (isinstance(shot, str) and shot.startswith("data:image/")):
        shot = None

    return {
        "accent": accent,
        "charts": charts,
        "panels": panels,
        "excludedTasks": _normalize_excluded_tasks(raw.get("excludedTasks")),
        "aiVisibilityShot": shot,
    }


def _normalize_excluded_tasks(raw: typing.Any) -> dict[str, list[str]]:
    """``{block_key: [clickup_task_id, ...]}`` of tasks struck off in the preview.

    Stored as an exclusion list rather than by editing the block's data so the
    removal is reversible and a regenerate doesn't silently resurrect it — the
    task stays in the block payload and is simply not rendered.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for block_key, ids in raw.items():
        if not isinstance(ids, (list, tuple)):
            continue
        cleaned = [str(i).strip() for i in ids if str(i or "").strip()]
        if cleaned:
            out[str(block_key)] = list(dict.fromkeys(cleaned))  # dedupe, keep order
    return out


def _block_to_dict(block: ReportBlock) -> dict:
    data = None
    if block.data_json:
        try:
            data = json.loads(block.data_json)
        except (ValueError, TypeError):
            data = None
    return {
        "block_type_key": block.block_type_key,
        "status": block.status,
        "data": data,
        "comment": block.comment or "",
        "unavailable_reason": block.unavailable_reason,
    }


_URL_RE = re.compile(r"\b((?:https?://|www\.)[^\s<>\"']+)", re.IGNORECASE)
# Punctuation that ends a sentence rather than the URL it follows.
_URL_TRAILING = ".,;:!?\"')]}"


def _link_html(url: str) -> str:
    """One matched URL → an anchor, with sentence punctuation left outside it."""
    trailing = ""
    while url and url[-1] in _URL_TRAILING:
        # a closing bracket that pairs with one inside the URL belongs to the URL
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        trailing = url[-1] + trailing
        url = url[:-1]
    if not url:
        return html.escape(trailing)
    href = url if url.lower().startswith(("http://", "https://")) else f"https://{url}"
    return (
        f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
        f"{html.escape(url)}</a>{html.escape(trailing)}"
    )


def _comment_html(text: str) -> str:
    """A specialist's comment as report HTML: escaped, line breaks kept, and any
    URL turned into a real clickable link (the report is read in a browser)."""
    text = text or ""
    parts: list[str] = []
    cursor = 0
    for match in _URL_RE.finditer(text):
        parts.append(html.escape(text[cursor:match.start()]))
        parts.append(_link_html(match.group(1)))
        cursor = match.end()
    parts.append(html.escape(text[cursor:]))
    return "".join(parts).replace("\n", "<br>")


def _load_json(raw: typing.Optional[str]) -> typing.Optional[dict]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def build_report_html(
    report: Report,
    blocks: list[ReportBlock],
    *,
    client_name: str,
    client_domain: str,
    customization: typing.Optional[dict] = None,
    language: str = localization.DEFAULT_LANGUAGE,
) -> str:
    prepared = (report.updated_at or report.created_at or datetime.utcnow()).date().isoformat()
    return _render_document(
        period_label=report.period_label,
        default_comparison=report.default_comparison,
        prepared=prepared,
        blocks=[_block_to_dict(block) for block in blocks],
        client_name=client_name,
        client_domain=client_domain,
        customization=customization if customization is not None else _load_json(report.customization),
        language=language,
    )


def build_preview_html(
    *,
    period_label: str,
    default_comparison: str,
    blocks: list[dict],
    client_name: str,
    client_domain: str,
    customization: typing.Optional[dict] = None,
    editable: bool = False,
    language: str = localization.DEFAULT_LANGUAGE,
) -> str:
    """Render a report from unsaved block payloads (the generate response shape)
    for the in-dashboard live preview. With ``editable`` the report carries the
    in-panel note editors and config toolbars; the final export does not."""
    return _render_document(
        period_label=period_label,
        default_comparison=default_comparison,
        prepared=datetime.utcnow().date().isoformat(),
        blocks=blocks,
        client_name=client_name,
        client_domain=client_domain,
        customization=customization,
        editable=editable,
        language=language,
    )


class PdfRenderError(RuntimeError):
    """Raised for any expected, handled failure to render a report to PDF."""


# Headless Chrome/Chromium binary names to look for, in order. Different distros
# and versions package this under different names (and one CLI flag renamed
# across Chrome versions, so both spellings are passed below).
_CHROME_BINARY_CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]


def _find_chrome_binary() -> str:
    for name in _CHROME_BINARY_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise PdfRenderError(
        "No Chrome/Chromium binary found on this server for PDF export. Install "
        "google-chrome, google-chrome-stable, chromium, or chromium-browser."
    )


def build_report_pdf(
    report: Report,
    blocks: list[ReportBlock],
    *,
    client_name: str,
    client_domain: str,
    customization: typing.Optional[dict] = None,
    language: str = localization.DEFAULT_LANGUAGE,
) -> bytes:
    """Render the same document as :func:`build_report_html`, then print it to
    PDF with headless Chrome — the report is JS-rendered (charts, tabs, KPI
    grids all populate from ``window.DATA`` on load), so a static HTML-to-PDF
    converter would only ever see the empty template shell.
    """
    document = build_report_html(
        report,
        blocks,
        client_name=client_name,
        client_domain=client_domain,
        customization=customization,
        language=language,
    )
    chrome = _find_chrome_binary()

    with tempfile.TemporaryDirectory(prefix="report-pdf-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        html_path = tmp_path / "report.html"
        pdf_path = tmp_path / "report.pdf"
        html_path.write_text(document, encoding="utf-8")

        command = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={tmp_path / 'chrome-profile'}",
            # Flag was renamed across Chrome versions; passing both is harmless.
            "--print-to-pdf-no-header",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=45)
        except subprocess.TimeoutExpired as error:
            raise PdfRenderError("Rendering the report to PDF timed out.") from error
        except OSError as error:
            raise PdfRenderError(f"Could not run Chrome for PDF export: {error}") from error

        if result.returncode != 0 or not pdf_path.exists():
            stderr = result.stderr.decode("utf-8", errors="replace").strip()[:500]
            raise PdfRenderError(f"Chrome failed to render the PDF{f': {stderr}' if stderr else '.'}")

        return pdf_path.read_bytes()


# --- Markdown export ----------------------------------------------------------
#
# Charts (trend lines, donuts) have no useful Markdown form, so those sections
# carry the same underlying numbers as tables instead. Otherwise this mirrors
# the HTML export section-for-section and skips whatever isn't selected or
# didn't resolve, same as the final client HTML/PDF export.

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    return _HTML_TAG_RE.sub("", html.unescape(value or "")).strip()


def _md_escape(value: typing.Any) -> str:
    text = str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").strip()
    return text or "—"


def _md_num(value: typing.Any) -> str:
    n = _num(value)
    return f"{int(n):,}" if n == int(n) else f"{n:,.1f}"


def _md_pct(value: typing.Any) -> str:
    return f"{_num(value):.1f}%"


def _md_table(headers: list[str], rows: list[list[typing.Any]]) -> str:
    if not rows:
        return "_No data._"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_md_escape(cell) for cell in row) + " |")
    return "\n".join(lines)


def _period_columns(data: dict) -> list[tuple[str, str]]:
    meta = data.get("meta") or {}
    p = meta.get("P") or {}
    lbl = meta.get("LBL") or {}
    return [(p[pk], lbl.get(p[pk], p[pk])) for pk in ("cur", "prev", "yoy") if p.get(pk)]


def _md_ahrefs_domain(data: dict) -> str:
    d = data.get("ahrefs") or {}
    bl = d.get("backlinks") or {}
    cols = _period_columns(data)
    metrics = d.get("metrics") or {}
    headers = ["Metric"] + [label for _, label in cols]
    rows = [
        [label] + [_md_num((metrics.get(key) or {}).get(field)) for key, _ in cols]
        for label, field in (
            ("Organic keywords", "orgKw"), ("Organic keywords (top 3)", "orgKw13"),
            ("Paid keywords", "paidKw"), ("Organic traffic", "orgTraffic"),
            ("Paid traffic", "paidTraffic"), ("Paid pages", "paidPages"),
        )
    ]
    return (
        f"**Domain Rating:** {_md_num(d.get('domainRating'))} · **Ahrefs Rank:** {_md_num(d.get('ahrefsRank'))}\n\n"
        f"**Backlinks:** {_md_num(bl.get('live'))} live / {_md_num(bl.get('allTime'))} all-time · "
        f"**Referring domains:** {_md_num(bl.get('liveRefdomains'))} live / {_md_num(bl.get('allTimeRefdomains'))} all-time\n\n"
        f"{_md_table(headers, rows)}"
    )


def _md_ahrefs_movers(data: dict) -> str:
    headers = ["URL", "Traffic", "Prev", "Δ", "Keywords", "Top keyword", "Volume", "Position", "Prev pos."]

    def rows_for(items: list[list]) -> list[list]:
        return [
            [r[0], _md_num(r[1]), _md_num(r[2]), _md_num(r[3]), _md_num(r[4]), r[5], _md_num(r[6]), _md_num(r[7]), _md_num(r[8])]
            for r in items
        ]

    gainers = _md_table(headers, rows_for(data.get("ahrefsGainers") or []))
    losers = _md_table(headers, rows_for(data.get("ahrefsLosers") or []))
    return f"**Top gainers**\n\n{gainers}\n\n**Top losers**\n\n{losers}"


def _md_ga4_summary(data: dict) -> str:
    cols = _period_columns(data)
    ga4 = data.get("ga4") or {}
    headers = ["Metric"] + [label for _, label in cols]
    fields = [
        ("Sessions", "sessions", _md_num), ("Organic sessions", "organic", _md_num),
        ("Total users", "users", _md_num), ("New users", "newUsers", _md_num),
        ("Returning users", "returning", _md_num), ("Engaged sessions", "engaged", _md_num),
        ("Engagement rate", "engRate", _md_pct), ("Bounce rate", "bounce", _md_pct),
        ("Avg. session duration (s)", "duration", _md_num), ("Page views", "pageViews", _md_num),
        ("Pages / session", "pps", _md_num), ("Key events", "keyEvents", _md_num),
    ]
    rows = [[label] + [fmt((ga4.get(key) or {}).get(field)) for key, _ in cols] for label, field, fmt in fields]
    parts = [_md_table(headers, rows)]
    cur_key = cols[0][0] if cols else ""
    channels = (data.get("channels") or {}).get(cur_key) or []
    if channels:
        parts.append("\n**Channels (current period)**\n")
        parts.append(_md_table(
            ["Channel", "Sessions", "Engaged", "Users"],
            [[c.get("channel", ""), _md_num(c.get("sessions")), _md_num(c.get("engaged")), _md_num(c.get("users"))] for c in channels],
        ))
    return "\n".join(parts)


def _md_ga4_top_pages(data: dict) -> str:
    cols = _period_columns(data)
    cur_key = cols[0][0] if cols else ""
    pages = (data.get("ga4TopPages") or {}).get(cur_key) or []
    return _md_table(
        ["Page", "Sessions", "Engaged", "Key events", "Bounce rate"],
        [[p.get("page", ""), _md_num(p.get("sessions")), _md_num(p.get("engaged")), _md_num(p.get("keyEvents")), _md_pct(p.get("bounce"))] for p in pages],
    )


def _md_ga4_monetization(data: dict) -> str:
    cols = _period_columns(data)
    headers = ["Metric"] + [label for _, label in cols]

    def table_for(section_key: str) -> str:
        section = data.get(section_key) or {}
        rows = [
            [label] + [_md_num((section.get(key) or {}).get(field)) for key, _ in cols]
            for label, field in (
                ("Purchases", "purchases"), ("Revenue", "revenue"),
                ("Add to cart", "addToCart"), ("Checkouts", "checkouts"),
            )
        ]
        return _md_table(headers, rows)

    return f"**Site-wide**\n\n{table_for('ecom')}\n\n**AI-referred traffic**\n\n{table_for('aiEcom')}"


def _md_ga4_ai_traffic(data: dict) -> str:
    cols = _period_columns(data)
    headers = ["Metric"] + [label for _, label in cols]
    summary = data.get("aiSummary") or {}
    rows = [
        ["AI sessions"] + [_md_num((summary.get(key) or {}).get("sessions")) for key, _ in cols],
        ["Engaged sessions"] + [_md_num((summary.get(key) or {}).get("engaged")) for key, _ in cols],
        ["Engagement rate"] + [_md_pct((summary.get(key) or {}).get("engRate")) for key, _ in cols],
    ]
    parts = [_md_table(headers, rows)]
    tools = data.get("aiTools") or []
    if tools:
        parts.append("\n**Traffic by AI tool**\n")
        parts.append(_md_table(
            ["Source", "Sessions", "Engaged"],
            [[t.get("source", ""), _md_num(t.get("sessions")), _md_num(t.get("engaged"))] for t in tools],
        ))
    pages = data.get("aiTopPages") or []
    if pages:
        parts.append("\n**Top landing pages from AI**\n")
        parts.append(_md_table(
            ["Page", "Sessions", "Engaged"],
            [[p.get("page", ""), _md_num(p.get("sessions")), _md_num(p.get("engaged"))] for p in pages],
        ))
    return "\n".join(parts)


def _md_ai_visibility(data: dict) -> str:
    models = (data.get("aiVisibility") or {}).get("models") or []
    if not models:
        return "_No AI-visibility data selected._"
    headers = ["Window", "Results checked", "Brand mentions", "Brand %", "Domain mentions", "Domain %"]
    parts = []
    for m in models:
        rows = []
        for window_key in ("last_month", "last_6_months"):
            w = (m.get("windows") or {}).get(window_key)
            if not w:
                continue
            if w.get("status") == "ok":
                rows.append([
                    w.get("window_label", window_key), _md_num(w.get("total_results")),
                    _md_num(w.get("brand_matches")), _md_pct(w.get("brand_match_rate")),
                    _md_num(w.get("domain_matches")), _md_pct(w.get("domain_match_rate")),
                ])
            else:
                rows.append([w.get("window_label", window_key), f"No data — {w.get('reason', '')}", "—", "—", "—", "—"])
        parts.append(f"**{m.get('label', m.get('key'))}**\n\n{_md_table(headers, rows)}")
    return "\n\n".join(parts)


def _md_gsc_summary(data: dict) -> str:
    cols = _period_columns(data)
    headers = ["Metric"] + [label for _, label in cols]
    gsc = data.get("gsc") or {}
    rows = [
        ["Clicks"] + [_md_num((gsc.get(key) or {}).get("clicks")) for key, _ in cols],
        ["Impressions"] + [_md_num((gsc.get(key) or {}).get("impressions")) for key, _ in cols],
        ["CTR"] + [_md_pct((gsc.get(key) or {}).get("ctr")) for key, _ in cols],
        ["Avg. position"] + [_md_num((gsc.get(key) or {}).get("position")) for key, _ in cols],
    ]
    parts = [_md_table(headers, rows)]
    pos = data.get("gscPos") or {}
    pos_rows = [
        [label] + [_md_num((pos.get(key) or {}).get(field)) for key, _ in cols]
        for label, field in (
            ("Top 3", "top3"), ("Top 5", "top5"), ("Top 10", "top10"),
            ("Top 20", "top20"), ("Top 50", "top50"), ("Total tracked", "total"),
        )
    ]
    parts.append("\n**Position distribution**\n")
    parts.append(_md_table(headers, pos_rows))
    cur_key = cols[0][0] if cols else ""
    b = (data.get("branded") or {}).get(cur_key) or {}
    parts.append("\n**Branded vs non-branded (current period)**\n")
    parts.append(f"Branded clicks: {_md_num(b.get('branded'))} of {_md_num(b.get('total'))} total ({_md_pct(b.get('share'))})")
    return "\n".join(parts)


def _md_gsc_queries(data: dict) -> str:
    cols = _period_columns(data)
    cur_key = cols[0][0] if cols else ""
    queries = (data.get("gscQueries") or {}).get(cur_key) or []
    pages = (data.get("gscTopPages") or {}).get(cur_key) or []
    headers = ["Item", "Clicks", "Impressions", "CTR", "Position"]
    q_table = _md_table(headers, [
        [q.get("query", ""), _md_num(q.get("clicks")), _md_num(q.get("impressions")), _md_pct(q.get("ctr")), _md_num(q.get("position"))]
        for q in queries
    ])
    p_table = _md_table(headers, [
        [p.get("page", ""), _md_num(p.get("clicks")), _md_num(p.get("impressions")), _md_pct(p.get("ctr")), _md_num(p.get("position"))]
        for p in pages
    ])
    return f"**Top queries**\n\n{q_table}\n\n**Top pages**\n\n{p_table}"


def _md_se_ranking(data: dict) -> str:
    sr = data.get("seranking") or {}
    note = (sr.get("note") or "").strip()
    headers = ["Keyword", "Volume", "Position", "Previous position"]
    parts = []
    for bucket in sr.get("buckets") or []:
        rows = [[kw[0], _md_num(kw[1]), _md_num(kw[2]), _md_num(kw[3])] for kw in bucket.get("rows") or []]
        if not rows:
            continue
        parts.append(f"**{bucket.get('label', '')}**\n\n" + _md_table(headers, rows))
    body = "\n\n".join(parts) or "_No tracked keywords ranked in this period._"
    return (f"_{note}_\n\n" if note else "") + body


def _md_work_done(data: dict) -> str:
    rows = [[r[0], r[1] if len(r) > 1 else ""] for r in (data.get("workDone") or [])]
    return _md_table(["Task", "ID"], rows)


def _md_planned_work(data: dict) -> str:
    manual = data.get("workPlannedManual")
    if manual:
        return _strip_html(manual)
    items = data.get("workPlanned") or []
    if not items:
        return "_No planned work for the next period._"
    lines = []
    for item in items:
        due = f" — due {item.get('due')}" if item.get("due") else ""
        lines.append(f"- **{item.get('name', '')}**{due} [#{item.get('taskId', '')}]")
    return "\n".join(lines)


# (section id, title, body builder — None means "raw specialist comment text
# is the section's own content", used by the two editorial blocks).
_MD_SECTIONS: list[tuple[str, str, typing.Optional[typing.Callable[[dict], str]]]] = [
    ("b14", "Summary", None),
    ("b2", "Search industry", None),
    ("b3", "Ahrefs — Domain analysis", _md_ahrefs_domain),
    ("b4", "Ahrefs — Top movers (pages & keywords)", _md_ahrefs_movers),
    ("b5", "Google Analytics 4", _md_ga4_summary),
    ("b6", "GA4 — Top landing pages", _md_ga4_top_pages),
    ("b7", "GA4 — Monetization", _md_ga4_monetization),
    ("b8", "GA4 — AI Traffic", _md_ga4_ai_traffic),
    ("b15", "AI Visibility", _md_ai_visibility),
    ("b9", "Google Search Console", _md_gsc_summary),
    ("b10", "GSC — Top queries & pages", _md_gsc_queries),
    ("b11", "SE Ranking — Tracked keywords", _md_se_ranking),
    ("b12", "Work completed", _md_work_done),
    ("b13", "Planned works", _md_planned_work),
]


def build_report_markdown(
    report: Report,
    blocks: list[ReportBlock],
    *,
    client_name: str,
    client_domain: str,
    customization: typing.Optional[dict] = None,
    language: str = localization.DEFAULT_LANGUAGE,
) -> str:
    """A Markdown rendering of the report's data and comments — same section
    selection/availability rules as the HTML/PDF export (an unselected or
    unavailable section is simply omitted)."""
    prepared = (report.updated_at or report.created_at or datetime.utcnow()).date().isoformat()
    data = _build_data(
        period_label=report.period_label,
        default_comparison=report.default_comparison,
        prepared=prepared,
        blocks=[_block_to_dict(block) for block in blocks],
        client_name=client_name,
        client_domain=client_domain,
        customization=customization if customization is not None else _load_json(report.customization),
        editable=False,
        language=language,
    )
    # Markdown has no template JS to run the post-render pass, so its section
    # titles are localized here.
    t = localization.translator(language)
    meta = data.get("meta") or {}
    report_chrome = data.get("report") or {}
    block_states = report_chrome.get("blocks") or {}
    comments_raw = report_chrome.get("commentsRaw") or {}

    lines = [
        f"# {meta.get('client', client_name)} — SEO & Visibility Report — {meta.get('periodLong', '')}",
        "",
        f"Domain: {meta.get('domain', client_domain)} · Prepared: {meta.get('prepared', prepared)}",
        "",
    ]
    for sec_id, title, builder in _MD_SECTIONS:
        state = block_states.get(sec_id)
        if not state or not state.get("selected") or state.get("status") != "ok":
            continue
        lines.append(f"## {t(title)}")
        lines.append("")
        comment = (comments_raw.get(sec_id) or "").strip()
        if builder is None:
            lines.append(comment or "_No content was written for this section._")
        else:
            lines.append(builder(data))
            if comment:
                lines.append("")
                lines.append(f"> {comment}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _render_document(
    *,
    period_label: str,
    default_comparison: str,
    prepared: str,
    blocks: list[dict],
    client_name: str,
    client_domain: str,
    customization: typing.Optional[dict],
    editable: bool = False,
    language: str = localization.DEFAULT_LANGUAGE,
) -> str:
    data = _build_data(
        period_label=period_label,
        default_comparison=default_comparison,
        prepared=prepared,
        blocks=blocks,
        client_name=client_name,
        client_domain=client_domain,
        customization=customization,
        editable=editable,
        language=language,
    )

    data_json = json.dumps(data, ensure_ascii=False)
    # make the JSON safe to embed inside a <script> tag
    data_json = data_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    meta = data["meta"]
    document = _template().replace("__DATA_JSON__", data_json)
    document = (
        document
        .replace("{{CLIENT}}", html.escape(client_name))
        .replace("{{DOMAIN}}", html.escape(client_domain))
        .replace("{{PERIOD_LONG}}", html.escape(meta["periodLong"]))
        .replace("{{NEXT_PERIOD_LONG}}", html.escape(meta["nextPeriodLong"]))
    )
    return document
