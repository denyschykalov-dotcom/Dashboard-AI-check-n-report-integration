"""Build a self-contained, client-ready HTML file from a saved report.

The output embeds all data and comments inline with no external requests — the
same spirit as the original single-file OnebyOne report template, generated here
server-side from the stored snapshot so the export always matches what was saved.
"""

from __future__ import annotations

import html
import json

from backend.app.models import Report, ReportBlock
from backend.app.report_builder.block_catalog import get_block


def _display_name(block: ReportBlock) -> str:
    catalog_entry = get_block(block.block_type_key)
    return catalog_entry.display_name if catalog_entry else block.block_type_key


def _render_block(block: ReportBlock) -> str:
    title = html.escape(_display_name(block))
    parts = [f'<section class="block"><h2>{title}</h2>']
    if block.status == "unavailable":
        reason = html.escape(block.unavailable_reason or "Data unavailable.")
        parts.append(f'<p class="unavailable">⚠ {reason}</p>')
    elif block.data_json:
        try:
            data = json.loads(block.data_json)
            pretty = html.escape(json.dumps(data, indent=2, ensure_ascii=False))
        except (ValueError, TypeError):
            pretty = html.escape(block.data_json)
        parts.append(f'<pre class="data">{pretty}</pre>')
    else:
        parts.append('<p class="unavailable">No data.</p>')

    comment = (block.comment or "").strip()
    if comment:
        parts.append(f'<div class="comment"><strong>Specialist notes</strong><p>{html.escape(comment)}</p></div>')
    parts.append("</section>")
    return "".join(parts)


def build_report_html(report: Report, blocks: list[ReportBlock], *, client_name: str, client_domain: str) -> str:
    title = html.escape(f"{client_name} — SEO Report — {report.period_label}")
    blocks_html = "".join(_render_block(block) for block in blocks)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
         max-width: 900px; margin: 0 auto; padding: 32px 20px; color: #1a1a2e; background: #fff; }}
  header {{ border-bottom: 2px solid #eee; margin-bottom: 24px; }}
  header h1 {{ margin: 0 0 4px; font-size: 26px; }}
  header p {{ margin: 0; color: #666; }}
  .block {{ border: 1px solid #eee; border-radius: 10px; padding: 18px 20px; margin: 16px 0; }}
  .block h2 {{ font-size: 17px; margin: 0 0 12px; }}
  pre.data {{ background: #f7f7fb; border-radius: 8px; padding: 12px; overflow-x: auto;
             font-size: 12px; line-height: 1.5; }}
  .unavailable {{ color: #a06000; background: #fff6e5; padding: 8px 12px; border-radius: 6px; }}
  .comment {{ border-left: 3px solid #6c5ce7; padding: 8px 14px; margin-top: 12px; background: #f6f5ff; border-radius: 4px; }}
  .comment p {{ margin: 4px 0 0; white-space: pre-wrap; }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(client_name)}</h1>
  <p>{html.escape(client_domain)} · Reporting period: {html.escape(report.period_label)}</p>
</header>
{blocks_html}
</body>
</html>"""
