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
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from backend.app.models import Report, ReportBlock


_TEMPLATE_PATH = Path(__file__).resolve().parent / "report_template.html"

# block_type_key -> template section id
_SECTION_BY_KEY = {
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
}

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

    data["meta"] = {
        "client": client_name,
        "domain": client_domain,
        "period": _long(cur_label),
        "periodLong": _long(cur_label),
        "nextPeriodLong": _next_long(cur_label),
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
    # Each row is [summary, task, task_id]: the Summary column leads with a brief
    # description (the task's ClickUp description, falling back to its title), the
    # Task column keeps the title, and the Category column has been dropped.
    def tasks(source_key):
        d = ok.get(source_key) or {}
        out = []
        for t in d.get("tasks", []):
            name = t.get("name", "")
            description = (t.get("description") or "").strip()
            summary = description or name
            out.append([summary, name, _task_id(t.get("url", ""))])
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
            data["workPlanned"] = tasks("planned_works")

    # -- SE Ranking (b11) placeholder --
    data["seranking"] = {
        "status": "pending",
        "note": "SE Ranking integration will be added later.",
        "keywords": [],
    }

    # -- report chrome: which blocks are selected/available + comments --
    block_states: dict[str, dict] = {}
    comments: dict[str, str] = {}
    comments_raw: dict[str, str] = {}
    for b in blocks:
        sec = _SECTION_BY_KEY.get(b.get("block_type_key"))
        if not sec:
            continue
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

    Shape: ``accent`` (report-wide), ``charts`` (per chart-slot variant), and
    ``panels`` (per-block text config: size + heading/body weight)."""
    raw = raw if isinstance(raw, dict) else {}

    accent = raw.get("accent")
    accent = accent if isinstance(accent, str) and accent.strip() else None

    charts = raw.get("charts") if isinstance(raw.get("charts"), dict) else {}
    charts = {str(k): str(v) for k, v in charts.items() if v}

    panels_raw = raw.get("panels") if isinstance(raw.get("panels"), dict) else {}
    panels = {str(k): _normalize_panel(v) for k, v in panels_raw.items() if isinstance(v, dict)}

    return {
        "accent": accent,
        "charts": charts,
        "panels": panels,
    }


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
    )


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
