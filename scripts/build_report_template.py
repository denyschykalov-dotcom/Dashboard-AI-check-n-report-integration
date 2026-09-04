"""One-time build: turn the example report (ONEBYO~2.HTM) into a generalized,
data-driven template committed at backend/app/report_builder/report_template.html.

The template keeps the example's exact CSS, markup and render JS, but:
  * the embedded window.DATA is replaced with a __DATA_JSON__ placeholder;
  * hardcoded client/domain/period strings become {{TOKENS}} (markup) or read
    DATA.meta (JS);
  * P / LBL come from DATA.meta;
  * a per-report chrome layer (applyReportChrome) shows only selected blocks,
    marks unavailable ones, and injects specialist comments;
  * renderAll is gated per block and guarded so a missing/failed block never
    breaks the rest of the page.

Run:  python scripts/build_report_template.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ONEBYO~2.HTM"
OUT = ROOT / "backend" / "app" / "report_builder" / "report_template.html"


def _split_data(html: str) -> tuple[str, str]:
    idx = html.find("window.DATA=")
    if idx < 0:
        raise SystemExit("could not find window.DATA= in source")
    start = html.find("{", idx)
    depth = 0
    in_str = False
    esc = False
    end = None
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise SystemExit("could not find end of DATA object")
    return html[:idx], html[end + 1:]


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected exactly 1 occurrence of {label!r}, found {count}")
    return text.replace(old, new)


def _replace_n(text: str, old: str, new: str, label: str, expected: int) -> str:
    count = text.count(old)
    if count != expected:
        raise SystemExit(f"expected {expected} of {label!r}, found {count}")
    return text.replace(old, new)


NEW_SUMMARY = r"""function renderSummary(){
  const el=document.getElementById('summary'); if(el.dataset.edited) return;
  const cmt=(DATA.report&&DATA.report.comments&&DATA.report.comments.b14)||'';
  if(cmt){ el.innerHTML=cmt; return; }
  const m=DATA.meta;
  const g4=(DATA.ga4&&DATA.ga4[P.cur])||null, gs=(DATA.gsc&&DATA.gsc[P.cur])||null,
        ai=(DATA.aiSummary&&DATA.aiSummary[P.cur])||null, ec=(DATA.ecom&&DATA.ecom[P.cur])||null;
  let parts=[`<b>${esc(m.client)}</b> — performance summary for <span class="hl">${esc(m.periodLong)}</span>.`];
  if(g4) parts.push(` Total sessions <span class="hl">${fnum(g4.sessions)}</span>, organic <span class="hl">${fnum(g4.organic)}</span>, engagement <span class="hl">${g4.engRate.toFixed(1)}%</span>.`);
  if(ai&&ai.sessions) parts.push(` AI Assistant sessions: <b>${fnum(ai.sessions)}</b> at ${ai.engRate.toFixed(1)}% engagement.`);
  if(gs) parts.push(` Search Console clicks <b>${fnum(gs.clicks)}</b>, avg position ${gs.position.toFixed(1)}.`);
  if(ec) parts.push(` Commerce: <b>${fnum(ec.purchases)}</b> purchases, <b>${fabbr(ec.revenue)}</b> revenue (all channels).`);
  parts.push(` <span class="muted">Editable — adjust before sending; your text is kept on Save.</span>`);
  el.innerHTML=parts.join('');
}
"""

NEW_RENDERALL = r"""const SECRENDER={b1:renderIntro,b2:renderIndustry,b3:renderAhrefs,b4:renderAhMovers,b5:renderGA4,b6:renderLanding,b7:renderEcom,b8:renderAI,b9:renderGSC,b10:renderGscTables,b11:renderSER,b12:()=>taskTable('doneTable',DATA.workDone||[]),b13:()=>taskTable('planTable',DATA.workPlanned||[]),b14:renderSummary};
function blockState(secId){ return (DATA.report&&DATA.report.blocks&&DATA.report.blocks[secId])||null; }
function applyReportChrome(){
  if(!DATA.report) return;
  Object.keys(SECRENDER).forEach(secId=>{
    const sec=document.getElementById(secId); if(!sec) return;
    const st=blockState(secId);
    if(!st||!st.selected){ sec.style.display='none'; return; }
    if(st.status!=='ok'){
      sec.querySelectorAll('.scoregrid,.card').forEach(el=>el.remove());
      const w=document.createElement('div'); w.className='warnbox'; w.style.marginTop='10px';
      w.textContent='⚠ '+(st.reason||'Data unavailable for this block.');
      const t=sec.querySelector('.blk-title'); if(t) t.insertAdjacentElement('afterend',w);
    }
    if(secId!=='b14'){ const c=(DATA.report.comments||{})[secId];
      if(c){ const box=sec.querySelector('.cmt'); if(box) box.innerHTML=c; } }
  });
}
function renderAll(){ paintLabels();
  Object.keys(SECRENDER).forEach(secId=>{ const st=blockState(secId);
    if(st&&st.selected&&st.status==='ok'){ try{ SECRENDER[secId](); }catch(e){ console.error('render '+secId,e); } }});
}
function wireTabs(){"""


def main() -> None:
    html = SOURCE.read_text(encoding="utf-8")
    prefix, suffix = _split_data(html)

    # --- markup tokens (in prefix) ---
    prefix = _replace_once(prefix, "RANKBERRY · <b>OnebyOne</b> SEO Report",
                           "RANKBERRY · <b>{{CLIENT}}</b> SEO Report", "topbar brand")
    prefix = _replace_once(prefix, "<button data-mode=\"mom\" class=\"on\">Jun'26 vs May'26 · MoM</button>",
                           "<button data-mode=\"mom\" class=\"on\">{{MOM_LABEL}}</button>", "mom button")
    prefix = _replace_once(prefix, "<button data-mode=\"yoy\">Jun'26 vs Jun'25 · YoY</button>",
                           "<button data-mode=\"yoy\">{{YOY_LABEL}}</button>", "yoy button")
    prefix = _replace_once(prefix, "across onebyone.ua.", "across {{DOMAIN}}.", "hero lead domain")
    prefix = _replace_once(prefix, "Search industry — June 2026", "Search industry — {{PERIOD_LONG}}", "b2 title")
    prefix = _replace_once(prefix, "Work completed — June 2026", "Work completed — {{PERIOD_LONG}}", "b12 title")
    prefix = _replace_once(prefix, "Planned works — July 2026", "Planned works — {{NEXT_PERIOD_LONG}}", "b13 title")
    prefix = _replace_once(prefix, "Summary — June 2026", "Summary — {{PERIOD_LONG}}", "b14 title")
    prefix = _replace_once(prefix, "· onebyone.ua</div>", "· {{DOMAIN}}</div>", "foot domain")

    # --- JS generalizations (in suffix) ---
    suffix = _replace_once(
        suffix,
        "const P = {cur:'2026-06', prev:'2026-05', yoy:'2025-06'};",
        "const P = DATA.meta.P;", "P const")
    suffix = _replace_once(
        suffix,
        "const LBL = {'2026-06':\"Jun 2026\",'2026-05':\"May 2026\",'2025-06':\"Jun 2025\"};",
        "const LBL = DATA.meta.LBL;", "LBL const")
    suffix = _replace_once(
        suffix,
        "`OnebyOne — SEO &amp; Visibility Report — <span class=\"mo\">June 2026</span>`",
        "`${m.client} — SEO &amp; Visibility Report — <span class=\"mo\">${m.periodLong}</span>`",
        "hero title")
    # the single-quoted replace() must use string concat, not a template literal
    suffix = _replace_once(
        suffix, "r.page.replace('https://onebyone.ua','')",
        "r.page.replace('https://'+DATA.meta.domain,'')", "gsc page strip")
    # remaining onebyone.ua refs live inside backtick literals -> interpolate
    suffix = _replace_n(
        suffix, "https://onebyone.ua${", "https://${DATA.meta.domain}${", "mover/link prefixes", 3)
    # industry editorial: drop the onebyone-specific AI sentence
    suffix = _replace_once(
        suffix,
        "'onebyone.ua received 1,057 AI Assistant sessions in June 2026 — 97.3% from ChatGPT. First measurable AI-driven traffic for the brand.'",
        "'AI Assistant traffic is now measurable in GA4 — see the GA4 — AI Traffic block for this month\\'s figures.'",
        "industry ai line")
    # branded-sample note: drop the onebyone-specific brand terms
    suffix = _replace_once(
        suffix,
        "Share computed from the GSC top-50 query sample (queries containing “one by one / onebyone”).",
        "Share computed from the current-period GSC query sample (queries containing the client's brand name).",
        "branded note")
    # save filename
    suffix = _replace_once(
        suffix,
        "a.download='OnebyOne-SEO-Report-June-2026.html';",
        "a.download=(DATA.meta.client+'-SEO-Report-'+DATA.meta.periodLong).replace(/[^A-Za-z0-9._-]+/g,'-')+'.html';",
        "save filename")

    # replace renderSummary (from its def up to 'function paintLabels(){')
    anchor_a = "function renderSummary(){"
    anchor_b = "\nfunction paintLabels(){"
    i = suffix.index(anchor_a)
    j = suffix.index(anchor_b)
    suffix = suffix[:i] + NEW_SUMMARY.rstrip("\n") + suffix[j:]

    # replace renderAll (from its def up to 'function wireTabs(){')
    ra = "function renderAll(){"
    rb = "function wireTabs(){"
    i = suffix.index(ra)
    j = suffix.index(rb)
    suffix = suffix[:i] + NEW_RENDERALL + suffix[j + len(rb):]

    # init order: run chrome after renderAll
    suffix = _replace_once(
        suffix, "renderAll(); wireTabs(); wireEditing();",
        "renderAll(); applyReportChrome(); wireTabs(); wireEditing();", "init line")

    template = prefix + "window.DATA=__DATA_JSON__" + suffix
    OUT.write_text(template, encoding="utf-8")
    print(f"wrote {OUT} ({len(template)} chars)")


if __name__ == "__main__":
    main()
