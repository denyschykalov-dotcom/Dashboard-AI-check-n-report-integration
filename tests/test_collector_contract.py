"""The collector script and the sheet resolvers must agree on tab names and headers.

The report-builder resolvers match sheet tabs by exact title and columns by exact
header text, so a rename on either side degrades silently: the block resolves,
reads nothing, and the client gets an empty section. Real drift already shipped
this way — clients' sheets were missing "Users", "CTR %" and every AI tab while
every block still reported "ok". This test reads the Apps Script collector and
fails if it stops writing what the resolvers read.
"""

from __future__ import annotations

import pathlib
import re
import unittest

COLLECTOR = pathlib.Path(__file__).resolve().parents[1] / "apps_script" / "collector.gs"

# canonical tab -> columns the resolvers read, from ga4.py and gsc.py.
REQUIRED: dict[str, list[str]] = {
    "GA4 Summary": [
        "Period", "Sessions", "Organic Sessions", "Total Users", "New Users",
        "Returning Users", "Engaged Sessions", "Engagement Rate %", "Bounce Rate %",
        "Avg Session Duration (s)", "Page Views", "Pages/Session", "Key Events",
    ],
    "GA4 Channels": ["Period", "Channel", "Sessions", "Engaged Sessions", "Users"],
    "GA4 Daily": ["Period", "Date", "Sessions", "Engaged Sessions", "Users"],
    "GA4 Events": ["Period", "Event Name", "Count", "Users"],
    "GA4 Top Pages": [
        "Period", "Landing Page", "Sessions", "Engaged Sessions", "Key Events", "Bounce Rate %",
    ],
    "GA4 Ecommerce": ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts"],
    "GA4 Ecommerce Organic": ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts"],
    "GA4 AI Ecommerce": ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts"],
    "GA4 AI Summary": ["Period", "Total AI Sessions", "Engaged Sessions", "Engagement Rate %"],
    "GA4 AI Traffic": ["Period", "Source", "Sessions", "Engaged Sessions"],
    "GA4 AI Top Pages": ["Period", "Landing Page", "Sessions", "Engaged Sessions"],
    "GSC Summary": ["Period", "Clicks", "Impressions", "CTR %", "Avg Position"],
    "GSC Positions": [
        "Period", "Top-3", "Top-5", "Top-10", "Top-20", "Top-50", "Total Sampled",
    ],
    "GSC Daily": ["Period", "Date", "Clicks", "Impressions", "CTR %", "Avg Position"],
    "GSC Queries": ["Period", "Query", "Clicks", "Impressions", "CTR %", "Avg Position"],
    "GSC Top Pages": ["Period", "Page", "Clicks", "Impressions", "CTR %", "Avg Position"],
}

# A writeTab_ header is either an inline array literal or an identifier bound to
# one (the three ecommerce tabs share a single header constant).
_WRITE_TAB = re.compile(
    r"writeTab_\(\s*ss\s*,\s*'([^']+)'\s*,\s*(\[.*?\]|\w+)\s*,", re.DOTALL
)
_HEADER_CONST = re.compile(r"const\s+(\w+)\s*=\s*(\[[^\]]*\])\s*;")
# The GSC per-dimension tabs share one writeTab_ call; their titles and label
# column come from the gscTabs spec list instead.
_GSC_SPEC = re.compile(r"tab:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'")
_STRINGS = re.compile(r"'([^']*)'")


def _collector_headers() -> dict[str, list[str]]:
    source = COLLECTOR.read_text()
    constants = {
        name: _STRINGS.findall(literal) for name, literal in _HEADER_CONST.findall(source)
    }

    headers: dict[str, list[str]] = {}
    for tab, header in _WRITE_TAB.findall(source):
        headers[tab] = (
            _STRINGS.findall(header) if header.startswith("[") else constants.get(header, [])
        )
    # Rebuild the loop-written GSC tabs from their spec entries.
    for tab, label in _GSC_SPEC.findall(source):
        headers[tab] = ["Period", label, "Clicks", "Impressions", "CTR %", "Avg Position"]
    return headers


class CollectorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.headers = _collector_headers()

    def test_collector_writes_every_tab_the_resolvers_read(self) -> None:
        missing = sorted(set(REQUIRED) - set(self.headers))
        self.assertEqual(missing, [], f"collector.gs never writes: {missing}")

    def test_collector_headers_cover_every_column_read(self) -> None:
        for tab, columns in REQUIRED.items():
            written = self.headers[tab]
            missing = [column for column in columns if column not in written]
            self.assertEqual(missing, [], f"{tab!r} header is missing {missing} (has {written})")

    def test_period_labels_use_english_months(self) -> None:
        # The resolvers parse labels with datetime.strptime(..., "%b %Y"), which
        # only accepts English abbreviations. A locale-formatted month is dropped
        # silently, taking every metric with it.
        source = COLLECTOR.read_text()
        self.assertIn("MONTHS_EN", source)
        # Scan code only: the header comment names the pattern it warns against.
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.DOTALL)
        localised = re.search(r"formatDate\([^)]*'[^']*MMM", code)
        self.assertIsNone(localised, f"locale-formatted month label: {localised}")


if __name__ == "__main__":
    unittest.main()
