import os
import pathlib
import time
import unittest
import uuid

import httpx
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base, build_engine
from backend.app.models import ApiCache, Client, Report, ReportBlock, Run, RunResult
from backend.app.api import routes as api_routes
from backend.app.report_builder import api_cache
from backend.app.report_builder import export as report_export
from backend.app.report_builder import localization
from backend.app.report_builder import secrets_crypto
from backend.app.report_builder import selections_service
from backend.app.report_builder import service as report_service
from backend.app.report_builder import settings_service
from backend.app.report_builder.data_sources import periods
from backend.app.report_builder.data_sources.periods import PeriodSelection
from backend.app.report_builder.block_catalog import BLOCK_CATALOG, get_block
from backend.app.report_builder.data_sources import (
    ahrefs, ai_visibility, clickup, ga4, gsc, se_ranking, static_editorial,
)
from backend.app.report_builder.data_sources import ahrefs_client
from backend.app.report_builder.data_sources.ahrefs_client import AhrefsAccessError, resolve_report_dates
from backend.app.report_builder.data_sources import clickup_client as clickup_client_module
from backend.app.report_builder.data_sources.clickup_client import ClickUpAccessError, find_client_list
from backend.app.report_builder.data_sources.base import ResolveContext
from backend.app.report_builder.data_sources.sheets_client import (
    SheetsAccessError,
    fetch_tab_values,
    find_client_sheet_id,
    resolve_client_sheet_id,
    resolve_periods,
    resolve_tab_name,
    rows_to_dicts,
)
from backend.app.utils import utcnow


def _make_session():
    engine = build_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _client(session, *, name="Acme Co", domain="acme.com", **kwargs) -> Client:
    client = Client(name=name, domain=domain, created_by=uuid.uuid4(), **kwargs)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def _seed_ai_run(session, *, project, created_at, gpt_domain=False, gem_brand=False):
    run = Run(
        user_id=uuid.uuid4(),
        keyword="kw",
        domain="acme.com",
        brand="Acme",
        prompt="p",
        project=project,
        created_at=created_at,
    )
    session.add(run)
    session.flush()
    result = RunResult(
        user_id=run.user_id,
        run_id=run.id,
        project=project,
        gpt_domain_mention=gpt_domain,
        gem_brand_mention=gem_brand,
    )
    session.add(result)
    session.commit()


class BlockCatalogTests(unittest.TestCase):
    def test_catalog_has_expected_size_and_unique_keys(self) -> None:
        self.assertEqual(len(BLOCK_CATALOG), 22)
        keys = [block.key for block in BLOCK_CATALOG]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ai_visibility_blocks_carry_window_and_model(self) -> None:
        ai_blocks = [block for block in BLOCK_CATALOG if block.source == "ai_visibility"]
        self.assertEqual(len(ai_blocks), 8)
        for block in ai_blocks:
            self.assertIn(block.ai_visibility_window, {"last_month", "last_6_months"})
            self.assertIn(block.ai_visibility_model, {"all", "gpt", "gemini", "grok"})

    def test_retired_bar_variants_resolve_but_are_not_offered(self) -> None:
        # Still resolvable, so historical reports and saved selections keep working…
        self.assertIsNotNone(get_block("ga4_session_mix_bar"))
        self.assertIsNotNone(get_block("gsc_branded_bar"))
        # …but no longer selectable: they have no SECTION_BY_KEY entry, so a report
        # that included one dropped it silently and never got a Claude comment.
        offered = {block.key for block in BLOCK_CATALOG}
        self.assertNotIn("ga4_session_mix_bar", offered)
        self.assertNotIn("gsc_branded_bar", offered)

    def test_every_offered_block_can_render_a_section(self) -> None:
        """Guard against re-introducing a phantom block: anything selectable must
        have somewhere in the template to render."""
        for block in BLOCK_CATALOG:
            self.assertIn(
                block.key,
                report_export.SECTION_BY_KEY,
                f"{block.key} has no report section",
            )


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def _context(self, client) -> ResolveContext:
        return ResolveContext(client=client, period_label="2026-06", now=utcnow(), session=self.session)

    def test_static_editorial_block_is_ok(self) -> None:
        client = _client(self.session)
        result = static_editorial.resolve(get_block("intro_header"), self._context(client))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["client"], "Acme Co")

    def test_ga4_block_unavailable_when_not_configured(self) -> None:
        client = _client(self.session)  # no ga4_sheet_id
        fake_settings = MagicMock(google_sheets_client_folder_id=None)
        with patch("backend.app.report_builder.data_sources.sheets_client.get_settings", return_value=fake_settings):
            result = ga4.resolve(get_block("ga4_summary"), self._context(client))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("No GA4 sheet linked", result.unavailable_reason)

    def test_ai_visibility_aggregates_matching_project_all_models(self) -> None:
        client = _client(self.session, name="Acme Co")
        _seed_ai_run(self.session, project="acme co", created_at=utcnow(), gpt_domain=True)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gem_brand=True)
        result = ai_visibility.resolve(get_block("ai_visibility_all_1mo"), self._context(client))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["total_results"], 2)
        self.assertEqual(result.data["domain_matches"], 1)
        self.assertEqual(result.data["brand_matches"], 1)

    def test_ai_visibility_specific_model_scope(self) -> None:
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        gpt_result = ai_visibility.resolve(get_block("ai_visibility_gpt_1mo"), self._context(client))
        grok_result = ai_visibility.resolve(get_block("ai_visibility_grok_1mo"), self._context(client))
        self.assertEqual(gpt_result.data["domain_matches"], 1)
        self.assertEqual(grok_result.data["domain_matches"], 0)

    def test_ai_visibility_window_filtering(self) -> None:
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow() - timedelta(days=60), gpt_domain=True)
        one_month = ai_visibility.resolve(get_block("ai_visibility_all_1mo"), self._context(client))
        six_month = ai_visibility.resolve(get_block("ai_visibility_all_6mo"), self._context(client))
        self.assertEqual(one_month.status, "unavailable")  # older than 30 days
        self.assertEqual(six_month.status, "ok")

    def test_ai_visibility_unavailable_when_no_matching_runs(self) -> None:
        client = _client(self.session, name="Other Client")
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        result = ai_visibility.resolve(get_block("ai_visibility_all_1mo"), self._context(client))
        self.assertEqual(result.status, "unavailable")

    def test_ai_visibility_blocks_share_one_query_per_generate(self) -> None:
        """All eight AI-visibility blocks read the project once, not once each.

        They differ only by window and model, both of which are filters over the
        same rows. Re-querying per block pulled the project's whole history eight
        times over — the report builder's largest source of Supabase egress.
        """
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        context = self._context(client)  # one context = one generate call
        ai_blocks = [block for block in BLOCK_CATALOG if block.source == "ai_visibility"]

        executed = []
        real_execute = context.session.execute

        def _counting_execute(statement, *args, **kwargs):
            executed.append(statement)
            return real_execute(statement, *args, **kwargs)

        with patch.object(context.session, "execute", side_effect=_counting_execute):
            results = [ai_visibility.resolve(block, context) for block in ai_blocks]

        self.assertEqual(len(ai_blocks), 8)
        self.assertEqual(len(executed), 1)
        self.assertTrue(all(result.status == "ok" for result in results))

    def test_ai_visibility_query_selects_no_wide_text_columns(self) -> None:
        """The aggregation needs six booleans, a timestamp and a user id.

        Selecting whole ``RunResult``/``Run`` entities also dragged each run's
        prompt and each result's brand list, citation format and sentiment JSON
        across the wire — kilobytes a row, for nothing.
        """
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        context = self._context(client)

        executed = []
        real_execute = context.session.execute

        def _capturing_execute(statement, *args, **kwargs):
            executed.append(statement)
            return real_execute(statement, *args, **kwargs)

        with patch.object(context.session, "execute", side_effect=_capturing_execute):
            ai_visibility.resolve(get_block("ai_visibility_all_1mo"), context)

        selected = {column.name for column in executed[0].selected_columns}
        self.assertEqual(
            selected,
            {
                "created_at",
                "user_id",
                "gpt_domain_mention",
                "gem_domain_mention",
                "grok_domain_mention",
                "gpt_brand_mention",
                "gem_brand_mention",
                "grok_brand_mention",
            },
        )

    def test_ai_visibility_window_cut_happens_in_sql(self) -> None:
        """Rows outside the widest window must never leave the database."""
        client = _client(self.session)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow(), gpt_domain=True)
        _seed_ai_run(self.session, project="Acme Co", created_at=utcnow() - timedelta(days=400))
        context = self._context(client)

        rows = ai_visibility._load_rows(context, "acme co")
        self.assertEqual(len(rows), 1)  # the 400-day-old run was filtered server-side


def _ga4_sheet_fixture() -> dict[str, list[list[str]]]:
    return {
        "GA4 Summary": [
            ["Period", "Sessions", "Organic Sessions", "Total Users", "New Users", "Returning Users",
             "Engaged Sessions", "Engagement Rate %", "Bounce Rate %", "Avg Session Duration (s)",
             "Page Views", "Pages/Session", "Key Events"],
            ["Jun 2026", "1030014", "59714", "683957", "539757", "52926", "903931", "87.8", "12.2", "128", "2940264", "2.85", "2245822"],
            ["May 2026", "1337409", "88463", "878660", "701292", "58488", "1250690", "93.5", "6.5", "132", "3886750", "2.91", "3895538"],
            ["Jun 2025", "518345", "34000", "325395", "278731", "36200", "509226", "98.2", "1.8", "185", "2399951", "4.63", "2410445"],
        ],
        "GA4 Channels": [
            ["Period", "Channel", "Sessions", "Engaged Sessions", "Users"],
            ["Jun 2026", "Organic Social", "221477", "209988", "168689"],
            ["Jun 2026", "Direct", "162884", "134948", "124067"],
            ["May 2026", "Direct", "150000", "130000", "120000"],
        ],
        "GA4 Daily": [
            ["Period", "Date", "Sessions", "Engaged Sessions", "Users"],
            ["Jun 2026", "20260601", "36948", "35003", "32728"],
            ["Jun 2026", "20260602", "37078", "35215", "31735"],
        ],
        "GA4 Events": [
            ["Period", "Event Name", "Count", "Users"],
            ["Jun 2026", "page_view", "2940260", "653159"],
            ["Jun 2026", "scroll", "950771", "276122"],
        ],
        "GA4 Top Pages": [
            ["Period", "Landing Page", "Sessions", "Engaged Sessions", "Key Events", "Bounce Rate %"],
            ["Jun 2026", "/", "290724", "283522", "939555", "2.5"],
            ["Jun 2026", "/new", "25185", "23762", "22670", "5.7"],
        ],
        "GA4 Ecommerce": [
            ["Period", "Purchases", "Revenue", "Currency", "Add to Carts", "Checkouts"],
            ["Jun 2026", "6058", "22724460.05", "UAH", "36610", "18268"],
            ["May 2026", "8789", "29735694.35", "UAH", "53226", "23902"],
            ["Jun 2025", "4269", "11646456.71", "UAH", "28324", "13994"],
        ],
        "GA4 Ecommerce Organic": [
            ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts", "Channel"],
            ["Jun 2026", "107", "468354.0498", "1239", "449", "Organic Search"],
            ["May 2026", "171", "613939.9701", "1612", "596", "Organic Search"],
            ["Jun 2025", "201", "761002.3602", "1818", "772", "Organic Search"],
        ],
        "GA4 AI Ecommerce": [
            ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts"],
            ["Jun 2026", "42", "185000.50", "260", "70"],
            ["May 2026", "31", "121000.00", "180", "55"],
            ["Jun 2025", "5", "12000.00", "40", "9"],
        ],
        "GA4 AI Summary": [
            ["Period", "Total AI Sessions", "Engaged Sessions", "Engagement Rate %"],
            ["Jun 2026", "1057", "986", "93.3"],
            ["May 2026", "0", "0", "0"],
            ["Jun 2025", "0", "0", "0"],
        ],
        "GA4 AI Traffic": [
            ["Period", "Source", "Sessions", "Engaged Sessions"],
            ["Jun 2026", "chatgpt.com", "1028", "961"],
            ["Jun 2026", "gemini.google.com", "19", "16"],
        ],
        "GA4 AI Top Pages": [
            ["Period", "Landing Page", "Sessions", "Engaged Sessions"],
            ["Jun 2026", "/", "737", "710"],
        ],
    }


def _gsc_sheet_fixture() -> dict[str, list[list[str]]]:
    return {
        "GSC Summary": [
            ["Period", "Clicks", "Impressions", "CTR %", "Avg Position"],
            ["Jun 2026", "35907", "1622018", "2.21", "7.8"],
            ["May 2026", "53637", "2399750", "2.24", "8.1"],
            ["Jun 2025", "26349", "1427275", "1.85", "19"],
        ],
        "GSC Positions": [
            ["Period", "Top-3", "Top-5", "Top-10", "Top-20", "Top-50", "Total Sampled"],
            ["Jun 2026", "568", "1016", "1887", "1983", "1999", "2000"],
            ["May 2026", "559", "1096", "1903", "1994", "2000", "2000"],
            ["Jun 2025", "634", "983", "1624", "1869", "1983", "2000"],
        ],
        "GSC Daily": [
            ["Period", "Date", "Clicks", "Impressions", "CTR %", "Avg Position"],
            ["Jun 2026", "2026-06-01", "1314", "56810", "2.31", "7.3"],
        ],
        "GSC Queries": [
            ["Period", "Query", "Clicks", "Impressions", "CTR %", "Avg Position"],
            ["Jun 2026", "one by one", "7763", "63824", "12.16", "1.4"],
            ["Jun 2026", "onebyone ua", "468", "752", "62.23", "1"],
            ["Jun 2026", "summer dresses", "300", "5000", "6.0", "8.2"],
        ],
        "GSC Top Pages": [
            ["Period", "Page", "Clicks", "Impressions", "CTR %", "Avg Position"],
            ["Jun 2026", "https://onebyone.ua/", "13813", "145868", "9.47", "3.9"],
        ],
    }


class SheetsClientHelperTests(unittest.TestCase):
    def test_rows_to_dicts_pads_short_rows(self) -> None:
        rows = [["A", "B", "C"], ["1", "2"]]
        result = rows_to_dicts(rows)
        self.assertEqual(result, [{"A": "1", "B": "2", "C": ""}])

    def test_rows_to_dicts_empty_input(self) -> None:
        self.assertEqual(rows_to_dicts([]), [])

    def test_resolve_periods_picks_latest_as_current(self) -> None:
        result = resolve_periods(["Jun 2026", "May 2026", "Jun 2025"])
        self.assertEqual(result, {"current": "Jun 2026", "previous": "May 2026", "yoy": "Jun 2025"})

    def test_resolve_periods_missing_previous_or_yoy(self) -> None:
        result = resolve_periods(["Jun 2026"])
        self.assertEqual(result["current"], "Jun 2026")
        self.assertIsNone(result["previous"])
        self.assertIsNone(result["yoy"])

    def test_resolve_periods_ignores_unparseable_labels(self) -> None:
        result = resolve_periods(["Jun 2026", "not-a-period", ""])
        self.assertEqual(result["current"], "Jun 2026")

    def test_num_parses_locale_formatted_sheet_values(self) -> None:
        # Sheets returns formatted values: a UA/EU sheet writes revenue as
        # "12 345,67" and a currency cell keeps its symbol.
        for raw, expected in (
            ("12 345,67", 12345.67),
            ("1 234", 1234.0),
            ("₴12,345.67", 12345.67),
            ("1,234", 1234.0),
            ("12,5", 12.5),
            ("-1 234,5", -1234.5),
            (1234, 1234.0),
            ("", 0.0),
            (None, 0.0),
        ):
            self.assertAlmostEqual(periods.num(raw), expected, msg=repr(raw))


@contextmanager
def _patched_ga4_sheet(fixture=None, tab_titles=None):
    fixture = fixture if fixture is not None else _ga4_sheet_fixture()
    titles = tab_titles if tab_titles is not None else set(fixture.keys())
    with patch("backend.app.report_builder.data_sources.ga4.list_sheet_tabs", return_value=titles), \
         patch("backend.app.report_builder.data_sources.ga4.fetch_tab_values", return_value=fixture):
        yield


@contextmanager
def _patched_gsc_sheet(fixture=None, tab_titles=None):
    fixture = fixture if fixture is not None else _gsc_sheet_fixture()
    titles = tab_titles if tab_titles is not None else set(fixture.keys())
    with patch("backend.app.report_builder.data_sources.gsc.list_sheet_tabs", return_value=titles), \
         patch("backend.app.report_builder.data_sources.gsc.fetch_tab_values", return_value=fixture):
        yield


class GA4SheetResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.client = _client(self.session, name="Acme Co", ga4_sheet_id="sheet-123")

    def _context(self) -> ResolveContext:
        return ResolveContext(client=self.client, period_label="2026-06", now=utcnow(), session=self.session)

    def test_summary_block_parses_kpis_channels_daily_events(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_summary"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["period"], "Jun 2026")
        self.assertEqual(result.data["previous_period"], "May 2026")
        self.assertEqual(result.data["yoy_period"], "Jun 2025")
        self.assertEqual(result.data["kpis"]["current"]["sessions"], 1030014)
        self.assertEqual(result.data["kpis"]["previous"]["sessions"], 1337409)
        self.assertEqual(result.data["kpis"]["yoy"]["sessions"], 518345)
        # channel mix filtered to current period only, sorted by sessions desc
        self.assertEqual(len(result.data["channels"]), 2)
        self.assertEqual(result.data["channels"][0]["channel"], "Organic Social")
        self.assertEqual(len(result.data["daily"]), 2)
        self.assertEqual(result.data["top_events"][0]["event_name"], "page_view")

    def test_session_mix_bar_shares_channel_data(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_session_mix_bar"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.data["channels"]), 2)

    def test_top_pages_sorted_and_capped(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_top_pages"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["pages"][0]["page"], "/")

    def test_monetization_includes_site_wide_and_organic(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_monetization"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["site_wide"]["current"]["purchases"], 6058)
        self.assertEqual(result.data["organic"]["current"]["purchases"], 107)

    def test_monetization_includes_ai_driven_sales(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_monetization"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["ai"]["current"]["purchases"], 42)
        self.assertAlmostEqual(result.data["ai"]["current"]["revenue"], 185000.50)
        self.assertEqual(result.data["ai"]["previous"]["purchases"], 31)
        self.assertEqual(result.data["ai"]["yoy"]["purchases"], 5)

    def test_ai_sales_use_the_rollup_row_not_the_sum_of_every_source(self) -> None:
        """A per-source AI ecommerce tab lists each assistant *and* a total row.

        Summing the window counted every sale twice — once under the assistant,
        once under "ALL AI ASSISTANTS".
        """
        fixture = _ga4_sheet_fixture()
        fixture["GA4 AI Ecommerce"] = [
            ["Period", "Source", "Purchases", "Revenue", "Currency", "Add to Carts", "Checkouts"],
            ["Jun 2026", "ALL AI ASSISTANTS", "42", "185000.50", "USD", "260", "70"],
            ["Jun 2026", "chatgpt.com", "30", "140000.00", "USD", "180", "50"],
            ["Jun 2026", "perplexity.ai", "12", "45000.50", "USD", "80", "20"],
        ]
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_monetization"), self._context())
        current = result.data["ai"]["current"]
        self.assertEqual(current["purchases"], 42)
        self.assertAlmostEqual(current["revenue"], 185000.50)
        self.assertEqual(current["add_to_carts"], 260)
        self.assertEqual(current["checkouts"], 70)

    def test_ai_sales_still_sum_the_rollup_across_a_multi_month_window(self) -> None:
        """One roll-up row per month — those do still add up."""
        rows = [
            {"Period": "Jun 2026", "Source": "ALL AI ASSISTANTS", "Purchases": "42", "Revenue": "100"},
            {"Period": "May 2026", "Source": "ALL AI ASSISTANTS", "Purchases": "31", "Revenue": "50"},
            {"Period": "Jun 2026", "Source": "chatgpt.com", "Purchases": "30", "Revenue": "90"},
        ]
        kpi = ga4._ecommerce_kpi(rows)
        self.assertEqual(kpi["purchases"], 73)
        self.assertAlmostEqual(kpi["revenue"], 150.0)

    def test_a_tab_without_a_source_column_is_summed_as_before(self) -> None:
        rows = [
            {"Period": "Jun 2026", "Purchases": "10", "Revenue": "5"},
            {"Period": "May 2026", "Purchases": "20", "Revenue": "7"},
        ]
        self.assertEqual(ga4._ecommerce_kpi(rows)["purchases"], 30)

    def test_the_rollup_row_is_not_charted_as_an_ai_tool(self) -> None:
        fixture = _ga4_sheet_fixture()
        fixture["GA4 AI Traffic"] = [
            ["Period", "Source", "Sessions", "Engaged Sessions"],
            ["Jun 2026", "ALL AI ASSISTANTS", "1057", "800"],
            ["Jun 2026", "chatgpt.com", "1000", "760"],
            ["Jun 2026", "perplexity.ai", "57", "40"],
        ]
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_ai_traffic"), self._context())
        sources = [tool["source"] for tool in result.data["tools"]]
        self.assertEqual(sources, ["chatgpt.com", "perplexity.ai"])

    def test_a_rollup_only_traffic_tab_still_charts_its_one_row(self) -> None:
        fixture = _ga4_sheet_fixture()
        fixture["GA4 AI Traffic"] = [
            ["Period", "Source", "Sessions", "Engaged Sessions"],
            ["Jun 2026", "ALL AI ASSISTANTS", "1057", "800"],
        ]
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_ai_traffic"), self._context())
        self.assertEqual([t["source"] for t in result.data["tools"]], ["ALL AI ASSISTANTS"])

    def test_monetization_reads_the_currency_off_the_sheet(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_monetization"), self._context())
        self.assertEqual(result.data["currency"], "₴")

    def test_monetization_currency_defaults_to_dollars_without_the_column(self) -> None:
        fixture = _ga4_sheet_fixture()
        for tab in ("GA4 Ecommerce", "GA4 Ecommerce Organic", "GA4 AI Ecommerce"):
            header, *rows = fixture[tab]
            keep = [i for i, name in enumerate(header) if name != "Currency"]
            fixture[tab] = [[row[i] for i in keep] for row in ([header] + rows)]
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_monetization"), self._context())
        self.assertEqual(result.data["currency"], "$")

    def test_monetization_ai_section_empty_when_tab_absent(self) -> None:
        fixture = _ga4_sheet_fixture()
        fixture.pop("GA4 AI Ecommerce")
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_monetization"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.data["ai"]["current"])

    def test_ai_traffic_includes_summary_tools_and_top_pages(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_ai_traffic"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["summary"]["current"]["total_ai_sessions"], 1057)
        self.assertEqual(result.data["tools"][0]["source"], "chatgpt.com")
        self.assertEqual(result.data["top_pages"][0]["page"], "/")

    def test_ai_traffic_carries_ai_sales_without_the_monetization_block(self) -> None:
        """The AI-Traffic section shows AI revenue, so it must not depend on the
        monetization block being selected to supply it."""
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_ai_traffic"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["ecommerce"]["current"]["purchases"], 42)
        self.assertAlmostEqual(result.data["ecommerce"]["current"]["revenue"], 185000.50)

    def test_non_ga4_block_key_is_unavailable(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ai_visibility_all_1mo"), self._context())
        self.assertEqual(result.status, "unavailable")

    def test_sheet_access_error_becomes_unavailable(self) -> None:
        with patch(
            "backend.app.report_builder.data_sources.ga4.list_sheet_tabs",
            side_effect=SheetsAccessError("Access denied — share the sheet with the service account."),
        ):
            result = ga4.resolve(get_block("ga4_summary"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("Access denied", result.unavailable_reason)

    def test_multiple_blocks_share_one_fetch_via_context_cache(self) -> None:
        context = self._context()
        with patch(
            "backend.app.report_builder.data_sources.ga4.list_sheet_tabs",
            return_value=set(_ga4_sheet_fixture().keys()),
        ) as mocked_titles, patch(
            "backend.app.report_builder.data_sources.ga4.fetch_tab_values",
            return_value=_ga4_sheet_fixture(),
        ) as mocked_fetch:
            ga4.resolve(get_block("ga4_summary"), context)
            ga4.resolve(get_block("ga4_top_pages"), context)
        mocked_fetch.assert_called_once()
        mocked_titles.assert_called_once()

    def test_ai_traffic_unavailable_when_the_three_ai_tabs_are_absent(self) -> None:
        # Seen on yamahaonlineparts.com: the sheet carried one "GA4 AI Assistants"
        # tab (Period/AI Source/Medium/Landing Page/Sessions/Users) instead of the
        # three this reads. Real data, wrong name and shape — so the block must say
        # the tabs are missing rather than report a section of zeros.
        fixture = _ga4_sheet_fixture()
        for tab in ["GA4 AI Summary", "GA4 AI Traffic", "GA4 AI Top Pages"]:
            fixture.pop(tab, None)
        fixture["GA4 AI Assistants"] = [
            ["Period", "AI Source", "Medium", "Landing Page", "Sessions", "Users"],
            ["Jun 2026", "chatgpt.com", "ai-assistant", "/", "74", "57"],
        ]
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_ai_traffic"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("GA4 AI Traffic", result.unavailable_reason)

    def test_alias_tab_name_used_when_canonical_missing(self) -> None:
        # This client's sheet uses "GA4 Overview" and "GA4 Key Events" instead
        # of "GA4 Summary" / "GA4 Events" — a real naming variant observed in
        # practice across different client sheets.
        fixture = _ga4_sheet_fixture()
        fixture["GA4 Overview"] = fixture.pop("GA4 Summary")
        fixture["GA4 Key Events"] = fixture.pop("GA4 Events")
        with _patched_ga4_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = ga4.resolve(get_block("ga4_summary"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["kpis"]["current"]["sessions"], 1030014)
        self.assertEqual(result.data["top_events"][0]["event_name"], "page_view")


class GSCSheetResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.client = _client(self.session, name="Onebyone", ga4_sheet_id="sheet-123")

    def _context(self) -> ResolveContext:
        return ResolveContext(client=self.client, period_label="2026-06", now=utcnow(), session=self.session)

    def test_summary_includes_kpis_positions_daily_and_branded(self) -> None:
        with _patched_gsc_sheet():
            result = gsc.resolve(get_block("gsc_summary"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["kpis"]["current"]["clicks"], 35907)
        self.assertEqual(result.data["positions"]["current"]["top10"], 1887)
        self.assertEqual(len(result.data["daily"]), 1)
        # "one by one" and "onebyone ua" both match client name "Onebyone"; "summer dresses" doesn't
        self.assertEqual(result.data["branded"]["branded_clicks"], 7763 + 468)
        self.assertEqual(result.data["branded"]["total_clicks"], 7763 + 468 + 300)

    def test_branded_bar_shares_branded_calc(self) -> None:
        with _patched_gsc_sheet():
            result = gsc.resolve(get_block("gsc_branded_bar"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.data["branded"]["branded_share_pct"], 0)

    def test_top_queries_returns_queries_and_pages(self) -> None:
        with _patched_gsc_sheet():
            result = gsc.resolve(get_block("gsc_top_queries"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["queries"][0]["query"], "one by one")
        self.assertEqual(result.data["pages"][0]["page"], "https://onebyone.ua/")

    def test_unavailable_when_no_sheet_id_and_no_folder_configured(self) -> None:
        client = _client(self.session, name="No Sheet Client")
        context = ResolveContext(client=client, period_label="2026-06", now=utcnow(), session=self.session)
        fake_settings = MagicMock(google_sheets_client_folder_id=None)
        with patch("backend.app.report_builder.data_sources.sheets_client.get_settings", return_value=fake_settings):
            result = gsc.resolve(get_block("gsc_summary"), context)
        self.assertEqual(result.status, "unavailable")

    def test_summary_unavailable_when_period_rows_are_all_zero(self) -> None:
        # Seen on yamahaonlineparts.com: the collector wrote Period rows but the
        # sheet's Search Console property returned nothing, so every metric is 0.
        # Rows existing is not data arriving — the section must say so, not ship zeros.
        fixture = _gsc_sheet_fixture()
        fixture["GSC Summary"] = [
            ["Period", "Clicks", "Impressions", "CTR %", "Avg Position"],
            ["Jun 2026", "0", "0", "", ""],
            ["May 2026", "0", "0", "", ""],
        ]
        with _patched_gsc_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = gsc.resolve(get_block("gsc_summary"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("GSC property", result.unavailable_reason)

    def test_alias_tab_name_used_when_canonical_missing(self) -> None:
        # This client's sheet uses "GSC Overview" and "GSC Top Queries" instead
        # of "GSC Summary" / "GSC Queries" — a real naming variant observed in
        # practice across different client sheets.
        fixture = _gsc_sheet_fixture()
        fixture["GSC Overview"] = fixture.pop("GSC Summary")
        fixture["GSC Top Queries"] = fixture.pop("GSC Queries")
        with _patched_gsc_sheet(fixture=fixture, tab_titles=set(fixture.keys())):
            result = gsc.resolve(get_block("gsc_summary"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["kpis"]["current"]["clicks"], 35907)
        self.assertEqual(result.data["branded"]["total_clicks"], 7763 + 468 + 300)


class SheetsClientRetryTests(unittest.TestCase):
    """A 503 from Google used to empty a section of the finished report."""

    def _response(self, status_code, payload=None):
        response = MagicMock()
        response.status_code = status_code
        response.content = b""
        response.json.return_value = payload or {}
        return response

    @contextmanager
    def _patched(self, responses):
        with patch("backend.app.report_builder.data_sources.sheets_client._get_token", return_value="tok"), \
             patch("backend.app.report_builder.data_sources.sheets_client.time.sleep"), \
             patch("httpx.get", side_effect=responses) as mocked_get:
            yield mocked_get

    def test_a_503_is_retried_and_the_data_still_arrives(self) -> None:
        ok = self._response(200, {"valueRanges": [{"values": [["Period"], ["Jun 2026"]]}]})
        with self._patched([self._response(503), ok]) as mocked_get:
            result = fetch_tab_values("sheet-1", ["GA4 Summary"])
        self.assertEqual(result["GA4 Summary"], [["Period"], ["Jun 2026"]])
        self.assertEqual(mocked_get.call_count, 2)

    def test_a_transport_failure_is_retried_too(self) -> None:
        ok = self._response(200, {"valueRanges": [{"values": [["Period"]]}]})
        with self._patched([httpx.ReadTimeout("timed out"), ok]) as mocked_get:
            result = fetch_tab_values("sheet-1", ["GA4 Summary"])
        self.assertEqual(result["GA4 Summary"], [["Period"]])
        self.assertEqual(mocked_get.call_count, 2)

    def test_it_gives_up_after_three_attempts_and_says_what_happened(self) -> None:
        with self._patched([self._response(503)] * 3) as mocked_get:
            with self.assertRaises(SheetsAccessError) as raised:
                fetch_tab_values("sheet-1", ["GA4 Summary"])
        self.assertIn("503", str(raised.exception))
        self.assertEqual(mocked_get.call_count, 3)

    def test_a_permanent_failure_is_not_retried(self) -> None:
        with self._patched([self._response(403)]) as mocked_get:
            with self.assertRaises(SheetsAccessError):
                fetch_tab_values("sheet-1", ["GA4 Summary"])
        self.assertEqual(mocked_get.call_count, 1)


class SheetsClientDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def _drive_files_response(self, files):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"files": files}
        return response

    def test_find_client_sheet_id_matches_exact_domain_first(self) -> None:
        files = [
            {"id": "id-partsvu", "name": "partsvu"},
            {"id": "id-partsvu-com", "name": "partsvu.com"},
        ]
        with patch("backend.app.report_builder.data_sources.sheets_client._get_token", return_value="tok"), \
             patch("httpx.get", return_value=self._drive_files_response(files)):
            result = find_client_sheet_id("folder-1", name="PartsVu", domain="partsvu.com")
        self.assertEqual(result, "id-partsvu-com")

    def test_find_client_sheet_id_falls_back_to_name_match(self) -> None:
        files = [{"id": "id-partsvu", "name": "partsvu"}]
        with patch("backend.app.report_builder.data_sources.sheets_client._get_token", return_value="tok"), \
             patch("httpx.get", return_value=self._drive_files_response(files)):
            result = find_client_sheet_id("folder-1", name="partsvu", domain="partsvu.io")
        self.assertEqual(result, "id-partsvu")

    def test_find_client_sheet_id_returns_none_when_no_match(self) -> None:
        files = [{"id": "id-x", "name": "some-other-client"}]
        with patch("backend.app.report_builder.data_sources.sheets_client._get_token", return_value="tok"), \
             patch("httpx.get", return_value=self._drive_files_response(files)):
            result = find_client_sheet_id("folder-1", name="Acme", domain="acme.com")
        self.assertIsNone(result)

    def test_resolve_client_sheet_id_uses_existing_value_without_drive_call(self) -> None:
        client = _client(self.session, name="Acme", ga4_sheet_id="already-set")
        context = ResolveContext(client=client, period_label="", now=utcnow(), session=self.session)
        with patch("backend.app.report_builder.data_sources.sheets_client.find_client_sheet_id") as mocked_find:
            result = resolve_client_sheet_id(context)
        self.assertEqual(result, "already-set")
        mocked_find.assert_not_called()

    def test_resolve_client_sheet_id_looks_up_and_persists_to_client(self) -> None:
        client = _client(self.session, name="Onebyone", domain="onebyone.ua")  # no ga4_sheet_id
        context = ResolveContext(client=client, period_label="", now=utcnow(), session=self.session)
        fake_settings = MagicMock(google_sheets_client_folder_id="folder-1")
        with patch("backend.app.report_builder.data_sources.sheets_client.get_settings", return_value=fake_settings), \
             patch("backend.app.report_builder.data_sources.sheets_client.find_client_sheet_id", return_value="discovered-id") as mocked_find:
            result = resolve_client_sheet_id(context)
        self.assertEqual(result, "discovered-id")
        mocked_find.assert_called_once()
        self.assertEqual(client.ga4_sheet_id, "discovered-id")
        # persisted to the DB, not just the in-memory object
        reloaded = self.session.get(type(client), client.id)
        self.assertEqual(reloaded.ga4_sheet_id, "discovered-id")

    def test_resolve_client_sheet_id_caches_within_context_without_recalling_drive(self) -> None:
        client = _client(self.session, name="Onebyone", domain="onebyone.ua")
        context = ResolveContext(client=client, period_label="", now=utcnow(), session=self.session)
        fake_settings = MagicMock(google_sheets_client_folder_id="folder-1")
        with patch("backend.app.report_builder.data_sources.sheets_client.get_settings", return_value=fake_settings), \
             patch("backend.app.report_builder.data_sources.sheets_client.find_client_sheet_id", return_value="discovered-id") as mocked_find:
            resolve_client_sheet_id(context)
            resolve_client_sheet_id(context)
        mocked_find.assert_called_once()

    def test_resolve_tab_name_prefers_first_alias_present(self) -> None:
        available = {"GA4 Overview", "GA4 Channels"}
        self.assertEqual(resolve_tab_name(available, ["GA4 Summary", "GA4 Overview"]), "GA4 Overview")
        self.assertIsNone(resolve_tab_name(available, ["GA4 Ecommerce"]))

    def test_resolve_tab_name_ignores_case_and_stray_spaces(self) -> None:
        """Tab names are typed by hand, so "GA4 AI ecommerce" is the same tab."""
        available = {"GA4 AI ecommerce ", "GA4  Summary"}
        self.assertEqual(resolve_tab_name(available, ["GA4 AI Ecommerce"]), "GA4 AI ecommerce ")
        self.assertEqual(resolve_tab_name(available, ["GA4 Summary"]), "GA4  Summary")

    def test_an_llm_named_tab_resolves_to_the_ai_ecommerce_tab(self) -> None:
        """Sheets write the same tab as "AI" or "LLM"; both feed the AI-revenue
        cards, which rendered nothing at all when the name was not recognised."""
        for name in ("GA4 LLM Ecommerce", "GA4 Ecommerce LLMs", "GA4 llm ecommerce"):
            self.assertEqual(
                resolve_tab_name({name}, ga4._TAB_ALIASES["GA4 AI Ecommerce"]), name
            )


class AhrefsClientDateTests(unittest.TestCase):
    def test_report_dates_use_most_recent_complete_month(self) -> None:
        dates = resolve_report_dates(date(2026, 7, 16))
        self.assertEqual(dates.current, date(2026, 6, 30))
        self.assertEqual(dates.previous, date(2026, 5, 31))
        self.assertEqual(dates.yoy, date(2025, 6, 30))
        self.assertEqual(dates.current_label, "Jun 2026")

    def test_report_dates_cross_year_boundary(self) -> None:
        dates = resolve_report_dates(date(2026, 1, 5))
        self.assertEqual(dates.current, date(2025, 12, 31))
        self.assertEqual(dates.previous, date(2025, 11, 30))
        self.assertEqual(dates.yoy, date(2024, 12, 31))

    def test_trend_window_spans_14_months(self) -> None:
        dates = resolve_report_dates(date(2026, 7, 16))
        # first day of the month 13 months before current (Jun 2026) => May 2025
        self.assertEqual(dates.trend_from, date(2025, 5, 1))


def _ahrefs_responses() -> dict[str, dict]:
    """Keyed by endpoint name, the JSON bodies ahrefs_client.get would return."""
    return {
        "domain-rating": {"domain_rating": {"domain_rating": 32.0, "ahrefs_rank": 3322118}},
        "backlinks-stats": {"metrics": {"live": 16898, "all_time": 24548, "live_refdomains": 595, "all_time_refdomains": 1071}},
        "metrics": {"metrics": {
            "org_keywords": 3611, "paid_keywords": 255, "org_keywords_1_3": 1358,
            "org_traffic": 110210, "org_cost": 691805, "paid_traffic": 6966,
            "paid_cost": 22653, "paid_pages": 34,
        }},
        "metrics-history": {"metrics": [
            {"date": "2025-05-01T00:00:00Z", "org_traffic": 73421},
            {"date": "2026-06-01T00:00:00Z", "org_traffic": 104363},
        ]},
        "top-pages": {"pages": [
            {"url": "https://onebyone.ua/sukni", "sum_traffic": 13631, "sum_traffic_prev": 5374,
             "traffic_diff": 8257, "keywords": 100, "top_keyword": "сукня",
             "top_keyword_volume": 16000, "top_keyword_best_position": 1, "top_keyword_best_position_prev": 5},
        ]},
    }


class AhrefsResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.client = _client(self.session, name="Onebyone", domain="onebyone.ua")
        self._responses = _ahrefs_responses()

    def _context(self) -> ResolveContext:
        return ResolveContext(client=self.client, period_label="", now=utcnow(), session=self.session)

    def _fake_get(self, endpoint, params):
        return self._responses[endpoint]

    def test_domain_analysis_shapes_all_sections(self) -> None:
        with patch("backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get", side_effect=self._fake_get):
            result = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["domain_rating"], 32.0)
        self.assertEqual(result.data["ahrefs_rank"], 3322118)
        self.assertEqual(result.data["backlinks"]["live"], 16898)
        self.assertEqual(result.data["metrics"]["current"]["org_keywords"], 3611)
        self.assertEqual(result.data["metrics"]["current"]["org_keywords_top3"], 1358)
        self.assertEqual(result.data["trend"][0], ["2025-05", 73421])

    def test_every_call_covers_subdomains_not_the_bare_domain(self) -> None:
        """A site served from www.<domain> is a subdomain to Ahrefs.

        With mode=domain, eatlebab.com reported 0 organic visits, 0 keywords, an
        empty trend and 628 of its 20,642 backlinks — while Top movers, already
        on mode=subdomains, listed its www pages. Every call must agree.
        """
        seen: list[dict] = []

        def record(endpoint, params):
            seen.append(params)
            return self._responses[endpoint]

        with patch("backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get", side_effect=record):
            ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
            ahrefs.resolve(get_block("ahrefs_top_movers"), self._context())

        with_mode = [params for params in seen if "mode" in params]
        self.assertTrue(with_mode)
        self.assertEqual({params["mode"] for params in with_mode}, {"subdomains"})

    def test_top_movers_returns_gainers_and_losers(self) -> None:
        with patch("backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get", side_effect=self._fake_get):
            result = ahrefs.resolve(get_block("ahrefs_top_movers"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["gainers"][0]["url"], "https://onebyone.ua/sukni")
        self.assertEqual(result.data["gainers"][0]["position_prev"], 5)
        self.assertIn("losers", result.data)

    def test_a_month_is_pulled_once_and_reused_by_later_reports(self) -> None:
        """The persistent cache: a second report for the same month costs no units.

        Both Ahrefs blocks are snapshots of a finished month, so the eight calls
        (~2,300 API units) must happen once, not on every regenerate.
        """
        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=self._fake_get,
        ) as mocked:
            first_domain = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
            first_movers = ahrefs.resolve(get_block("ahrefs_top_movers"), self._context())
            calls = mocked.call_count
            # A separate generate call — a fresh context, so only the stored
            # payload can answer it.
            again_domain = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
            again_movers = ahrefs.resolve(get_block("ahrefs_top_movers"), self._context())

        self.assertEqual(calls, 8)
        self.assertEqual(mocked.call_count, 8)
        self.assertEqual(again_domain.data, first_domain.data)
        self.assertEqual(again_movers.data, first_movers.data)

    def test_another_client_never_gets_this_ones_numbers(self) -> None:
        """The key carries the domain. Sharing one entry across clients would put
        somebody else's traffic in this client's report — the worst outcome the
        cache can produce, and invisible once the report is sent."""
        other = _client(self.session, name="Other Co", domain="other.com")
        other_context = ResolveContext(
            client=other, period_label="", now=utcnow(), session=self.session
        )
        targets: list[str] = []

        def record(endpoint, params):
            targets.append(params.get("target"))
            return self._responses[endpoint]

        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=record,
        ) as mocked:
            ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
            first_call_count = mocked.call_count
            ahrefs.resolve(get_block("ahrefs_domain_analysis"), other_context)

        self.assertEqual(mocked.call_count, first_call_count * 2)
        self.assertIn("other.com", targets)

    def test_a_different_month_is_pulled_again(self) -> None:
        """Each month is its own snapshot, so last month's entry must not answer
        for this one — the report would silently repeat the previous figures."""
        def context_at(now):
            return ResolveContext(
                client=self.client, period_label="", now=now, session=self.session
            )

        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=self._fake_get,
        ) as mocked:
            ahrefs.resolve(
                get_block("ahrefs_domain_analysis"),
                context_at(datetime(2026, 7, 15, tzinfo=timezone.utc)),  # → Jun 2026
            )
            first_call_count = mocked.call_count
            ahrefs.resolve(
                get_block("ahrefs_domain_analysis"),
                context_at(datetime(2026, 6, 15, tzinfo=timezone.utc)),  # → May 2026
            )
        self.assertEqual(mocked.call_count, first_call_count * 2)

    def test_an_expired_entry_is_pulled_again(self) -> None:
        """The TTL is what lets Ahrefs' revisions to a recent month land."""
        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=self._fake_get,
        ) as mocked:
            ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
            first_call_count = mocked.call_count

        rows = list(self.session.execute(select(ApiCache)).scalars())
        self.assertTrue(rows, "the pull should have been stored")
        for row in rows:
            row.expires_at = utcnow() - timedelta(days=1)
        self.session.commit()

        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=self._fake_get,
        ) as mocked_again:
            result = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(mocked_again.call_count, first_call_count)

    def test_a_failed_pull_is_not_cached(self) -> None:
        """A rate limit must not be stored as this month's answer."""
        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=AhrefsAccessError("rate limited", retryable=True),
        ):
            failed = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
        self.assertEqual(failed.status, "unavailable")

        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=self._fake_get,
        ) as mocked:
            retried = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
        self.assertEqual(retried.status, "ok")
        self.assertTrue(mocked.call_count)

    def test_unavailable_without_domain(self) -> None:
        client = _client(self.session, name="No Domain", domain="")
        context = ResolveContext(client=client, period_label="", now=utcnow(), session=self.session)
        result = ahrefs.resolve(get_block("ahrefs_domain_analysis"), context)
        self.assertEqual(result.status, "unavailable")

    def test_anchor_never_asks_ahrefs_for_an_unfinished_month(self) -> None:
        # A range ending in the current month used to anchor on the month after
        # it, and Ahrefs answers 400 "bad date" for a month that hasn't ended.
        now = utcnow()
        selection = SimpleNamespace(start=date(now.year, 1, 1), end=date(now.year, now.month, 28))
        context = ResolveContext(
            client=self.client, period_label="", now=now, session=self.session,
            period_selection=selection,
        )
        self.assertLessEqual(ahrefs._anchor_date(context), now.date())

    def test_api_error_becomes_unavailable(self) -> None:
        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=AhrefsAccessError("Ahrefs API rejected the token (401)."),
        ):
            result = ahrefs.resolve(get_block("ahrefs_domain_analysis"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("401", result.unavailable_reason)

    def test_domain_analysis_cached_within_context(self) -> None:
        context = self._context()
        with patch(
            "backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get",
            side_effect=self._fake_get,
        ) as mocked:
            ahrefs.resolve(get_block("ahrefs_domain_analysis"), context)
            first_call_count = mocked.call_count
            ahrefs.resolve(get_block("ahrefs_domain_analysis"), context)
            # second resolve of the same block reuses the cache — no new API calls
            self.assertEqual(mocked.call_count, first_call_count)


class SeRankingResolverTests(unittest.TestCase):
    """The tracked-keywords table reports only keywords the site actually ranks
    for, split into position bands and sorted by search volume inside each."""

    # July 2026 → the report's current month is June 2026, previous May 2026.
    NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.session = _make_session()
        # A numeric target is taken as the project id, so no /sites call is made.
        self.client = _client(self.session, name="Acme Co", domain="acme.com",
                              se_ranking_target="4242")

    def _context(self) -> ResolveContext:
        return ResolveContext(client=self.client, period_label="", now=self.NOW,
                              session=self.session)

    @staticmethod
    def _keyword(name, volume, current_pos, previous_pos):
        return {"name": name, "volume": volume, "positions": [
            {"date": "2026-05-20", "pos": previous_pos},
            {"date": "2026-06-20", "pos": current_pos},
        ]}

    def _resolve(self, keywords):
        with patch(
            "backend.app.report_builder.data_sources.se_ranking_client.get_positions",
            return_value=[{"site_engine_id": 1, "keywords": keywords}],
        ):
            return se_ranking.resolve(get_block("se_ranking_keywords"), self._context())

    @staticmethod
    def _band(result, key):
        return next(b for b in result.data["buckets"] if b["key"] == key)

    def test_unranked_keywords_are_left_out(self) -> None:
        result = self._resolve([
            self._keyword("never ranked", 900, 0, 0),        # 0 = outside tracked depth
            self._keyword("page eleven", 800, 101, 99),      # past the reported range
            self._keyword("hundredth", 700, 100, 0),         # the last reported position
            self._keyword("best", 50, 3, 4),
        ])
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [row[0] for row in result.data["keywords"]],
            ["best", "hundredth"],
            "positions 1-100 only",
        )
        # [name, volume, position, previous position]
        self.assertEqual(self._band(result, "top3")["rows"][0], ["best", 50, 3, 4])
        self.assertIn("1–100", result.data["note"])

    def test_each_band_holds_its_own_positions_sorted_by_volume(self) -> None:
        result = self._resolve([
            self._keyword("pos 2 small", 10, 2, 5),
            self._keyword("pos 1 big", 900, 1, 2),
            self._keyword("pos 7", 500, 7, 9),
            self._keyword("pos 25", 400, 25, 30),
            self._keyword("pos 40", 300, 40, 44),
            self._keyword("pos 80", 200, 80, 90),
        ])
        bands = {b["key"]: [row[0] for row in b["rows"]] for b in result.data["buckets"]}
        self.assertEqual(bands, {
            "top3": ["pos 1 big", "pos 2 small"],   # volume order, not position order
            "top10": ["pos 7"],
            "top30": ["pos 25"],
            "top50": ["pos 40"],
            "top100": ["pos 80"],
        })
        self.assertEqual(
            [b["label"] for b in result.data["buckets"]],
            ["Top 3 (1–3)", "Top 10 (4–10)", "Top 30 (11–30)", "Top 50 (31–50)", "Top 100 (51–100)"],
        )

    def test_the_cap_applies_per_band_and_keeps_the_most_searched(self) -> None:
        # 150 keywords in the Top 3 band alone, plus one further down: the cap
        # must not eat the smaller band, and must keep the biggest volumes.
        result = self._resolve(
            [self._keyword(f"kw{i}", i, (i % 3) + 1, 5) for i in range(150)]
            + [self._keyword("lonely", 1, 75, 80)],
        )
        top3 = self._band(result, "top3")
        self.assertEqual(top3["count"], 150, "the band reports its true size")
        self.assertEqual(len(top3["rows"]), 100, "and carries at most 100 rows")
        self.assertEqual([row[1] for row in top3["rows"]], sorted((row[1] for row in top3["rows"]), reverse=True))
        self.assertEqual(top3["rows"][0][1], 149, "most-searched first")
        self.assertEqual([row[0] for row in self._band(result, "top100")["rows"]], ["lonely"])

    def test_no_ranked_keywords_still_resolves_ok(self) -> None:
        result = self._resolve([self._keyword("never ranked", 900, 0, 0)])
        self.assertEqual(result.status, "ok", "an empty table is not a failed block")
        self.assertEqual(result.data["keywords"], [])
        self.assertEqual([b["count"] for b in result.data["buckets"]], [0, 0, 0, 0, 0])

    def test_missing_target_is_unavailable(self) -> None:
        self.client.se_ranking_target = ""
        result = se_ranking.resolve(get_block("se_ranking_keywords"), self._context())
        self.assertEqual(result.status, "unavailable")


class ServiceGenerateWithSheetsTests(unittest.TestCase):
    def test_generate_prefers_sheet_period_over_wallclock_default(self) -> None:
        session = _make_session()
        client = _client(session, name="Acme Co", ga4_sheet_id="sheet-123")
        with _patched_ga4_sheet():
            result = report_service.generate(session, client_id=client.id, block_keys=["ga4_summary"])
        self.assertEqual(result["period_label"], "Jun 2026")

    def test_generate_propagates_sheet_period_to_later_blocks(self) -> None:
        # ga4_summary resolves before work_completed in catalog order, so once
        # GA4 reports the real period ("Jun 2026") ClickUp must filter DONE
        # tasks against that month too — not the wall-clock month generate()
        # started with.
        session = _make_session()
        user_id = uuid.uuid4()
        client = _client(session, name="Acme Co", domain="acme.com", ga4_sheet_id="sheet-123")
        settings_service.set_clickup_token(session, user_id, "pk_x")
        task_done_in_june = [{
            "id": "1", "name": "Ship redesign", "url": "https://app.clickup.com/t/1",
            "status": {"status": "done", "type": "done"}, "date_done": "1781481600000",  # 2026-06-15
            "assignees": [],
        }]
        june_time = [{"user": {"username": "Denys"},
                      "intervals": [{"start": "1781481600000", "time": "3600000"}]}]
        with _patched_ga4_sheet(), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "acme"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=task_done_in_june,
        ), patch(
            "backend.app.report_builder.data_sources.clickup_client.fetch_task_time",
            return_value=june_time,
        ):
            result = report_service.generate(
                session,
                client_id=client.id,
                block_keys=["ga4_summary", "work_completed"],
                user_id=user_id,
            )
        self.assertEqual(result["period_label"], "Jun 2026")
        by_key = {block["block_type_key"]: block for block in result["blocks"]}
        self.assertEqual(by_key["work_completed"]["status"], "ok")
        self.assertEqual(by_key["work_completed"]["data"]["count"], 1)

    def test_clickup_statuses_match_regardless_of_spacing(self) -> None:
        """ClickUp's own default status is "to do" — with a space.

        Matching the raw label meant a list on the stock workflow produced no
        planned works at all, and any list writing "To-Do" or "DONE" the same.
        """
        for label in ("todo", "to do", "To Do", "TO-DO", "to_do"):
            self.assertEqual(
                clickup._status_name({"status": {"status": label}}),
                clickup._TODO_STATUS_NAME,
                f"{label!r} should count as the Todo stage",
            )
        for label in ("done", "Done", "DONE", " done "):
            self.assertEqual(
                clickup._status_name({"status": {"status": label}}),
                clickup._DONE_STATUS_NAME,
                f"{label!r} should count as the Done stage",
            )
        # Neighbouring stages must still be excluded — "Complete" is the closed
        # archive stage, not the reportable "Done" one.
        for label in ("doing", "in progress", "Complete", "backlog", ""):
            name = clickup._status_name({"status": {"status": label}})
            self.assertNotIn(name, (clickup._TODO_STATUS_NAME, clickup._DONE_STATUS_NAME))

    @staticmethod
    def _done_tasks_fixture():
        """Three DONE tasks spread across two years, plus one never marked done."""
        return [
            {"id": "t1", "name": "Guest post writing", "url": "https://app.clickup.com/t/t1",
             "status": {"status": "done", "type": "done"}, "date_done": "1783641600000",
             "time_spent": "12600000", "assignees": []},           # 2026-07-10
            {"id": "t2", "name": "Old technical audit", "url": "https://app.clickup.com/t/t2",
             "status": {"status": "done", "type": "done"}, "date_done": "1741046400000",
             "time_spent": "3600000", "assignees": []},            # 2025-03-04
            {"id": "t3", "name": "August retainer work", "url": "https://app.clickup.com/t/t3",
             "status": {"status": "done", "type": "done"}, "date_done": "1786060800000",
             "assignees": []},                                     # 2026-08-07
            {"id": "t4", "name": "Still in progress", "url": "https://app.clickup.com/t/t4",
             "status": {"status": "doing", "type": "custom"}, "assignees": []},
        ]

    # Tracked time per task: t1 worked in Jul 2026, t2 in Mar 2025, and t3 for a
    # single two-minute entry running from 23:59 on 31 Jul into 1 Aug 2026.
    _TIME_ENTRIES = {
        "t1": [{"user": {"username": "Denys"},
                "intervals": [{"start": "1783641600000", "time": "3600000"}]}],
        "t2": [{"user": {"username": "Denys"},
                "intervals": [{"start": "1741046400000", "time": "3600000"}]}],
        "t3": [{"user": {"username": "Denys"},
                "intervals": [{"start": "1785542340000", "time": "120000"}]}],
    }

    def _resolve_completed(self, tasks, period_label="Jul 2026", entries=None):
        entries = self._TIME_ENTRIES if entries is None else entries
        session = _make_session()
        user_id = uuid.uuid4()
        client = _client(session, name="Acme Co", domain="acme.com")
        settings_service.set_clickup_token(session, user_id, "pk_x")
        context = ResolveContext(
            client=client, period_label=period_label,
            now=datetime(2026, 8, 6, tzinfo=timezone.utc), session=session, user_id=user_id,
        )
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "acme"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=tasks,
        ), patch(
            "backend.app.report_builder.data_sources.clickup_client.fetch_task_time",
            side_effect=lambda token, task_id: entries.get(task_id, []),
        ) as task_time:
            return clickup.resolve(get_block("work_completed"), context), task_time

    def test_work_completed_lists_only_done_tasks_worked_on_in_the_month(self) -> None:
        """Tracked time decides the month, not the completion date: "August
        retainer work" is marked done in August but was worked on 31 July, so it
        belongs in the July report — and the 2025 task does not."""
        result, _ = self._resolve_completed(self._done_tasks_fixture(), period_label="Jul 2026")
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [t["name"] for t in result.data["tasks"]],
            ["August retainer work", "Guest post writing"],
            "most recently completed first",
        )
        self.assertEqual(result.data["count"], 2)

    def test_two_minutes_of_tracked_time_place_a_task_in_both_months(self) -> None:
        """"Even partly" is literal: the entry straddling midnight on 1 August
        counts for August as well, however short it is."""
        result, _ = self._resolve_completed(self._done_tasks_fixture(), period_label="Aug 2026")
        self.assertEqual([t["name"] for t in result.data["tasks"]], ["August retainer work"])

    def test_a_done_task_with_no_tracked_time_is_not_listed(self) -> None:
        """No working time means no month places it, so the report leaves it out
        rather than falling back on the completion date."""
        result, _ = self._resolve_completed(self._done_tasks_fixture(), entries={})
        self.assertEqual(result.data["count"], 0)
        self.assertEqual(result.status, "ok", "an empty section is not a failed block")

    def test_work_completed_reads_each_done_task_time_once(self) -> None:
        """One time-entry call per DONE task — the in-progress one is never asked
        about, and the cache keeps a task from being read twice."""
        _, task_time = self._resolve_completed(self._done_tasks_fixture())
        self.assertEqual(sorted(call.args[1] for call in task_time.call_args_list), ["t1", "t2", "t3"])

    def test_a_failed_time_lookup_makes_the_block_unavailable(self) -> None:
        """Tracked time decides the month, so a failed read can't be shrugged off
        into "no work this month" in a client-facing report."""
        def _boom(token, task_id):
            raise ClickUpAccessError("rate limited")

        session = _make_session()
        user_id = uuid.uuid4()
        client = _client(session, name="Acme Co", domain="acme.com")
        settings_service.set_clickup_token(session, user_id, "pk_x")
        context = ResolveContext(
            client=client, period_label="Jul 2026",
            now=datetime(2026, 8, 6, tzinfo=timezone.utc), session=session, user_id=user_id,
        )
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "acme"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=self._done_tasks_fixture(),
        ), patch(
            "backend.app.report_builder.data_sources.clickup_client.fetch_task_time",
            side_effect=_boom,
        ):
            result = clickup.resolve(get_block("work_completed"), context)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("tracked time", result.unavailable_reason)

    def test_work_completed_takes_tracked_time_from_the_task_payload(self) -> None:
        """ClickUp's own aggregate rides along with the list fetch; a task with
        none, or an unparseable value, reads as zero rather than raising."""
        tasks = self._done_tasks_fixture()
        tasks[2]["time_spent"] = "not-a-number"
        result, _ = self._resolve_completed(tasks)
        by_name = {t["name"]: t["time_spent_ms"] for t in result.data["tasks"]}
        self.assertEqual(by_name["Guest post writing"], 12600000)
        self.assertEqual(by_name["August retainer work"], 0)

    def test_clickup_get_rides_out_one_rate_limit(self) -> None:
        """One 429 is retried after the rate-limit window rather than failing the
        call, since a generate can burst several list reads into the limit."""
        limited = MagicMock(status_code=429, content=b"", headers={"Retry-After": "0"})
        okay = MagicMock(status_code=200, content=b"{}", headers={})
        okay.json.return_value = {"data": []}
        with patch("backend.app.report_builder.data_sources.clickup_client.httpx.get",
                   side_effect=[limited, okay]) as get, \
                patch("backend.app.report_builder.data_sources.clickup_client.time.sleep") as sleep:
            self.assertEqual(clickup_client_module.fetch_task_time("pk_x", "t1"), [])
        self.assertEqual(get.call_count, 2, "the 429 must be retried once")
        sleep.assert_called_once()

    def test_clickup_get_gives_up_after_persistent_rate_limit(self) -> None:
        limited = MagicMock(status_code=429, content=b"", headers={"Retry-After": "0"})
        with patch("backend.app.report_builder.data_sources.clickup_client.httpx.get",
                   return_value=limited), \
                patch("backend.app.report_builder.data_sources.clickup_client.time.sleep"):
            with self.assertRaises(ClickUpAccessError) as caught:
                clickup_client_module.fetch_task_time("pk_x", "t1")
        self.assertIn("429", str(caught.exception))

    def test_planned_works_picks_up_stock_clickup_todo_status(self) -> None:
        session = _make_session()
        user_id = uuid.uuid4()
        client = _client(session, name="Acme Co", domain="acme.com")
        settings_service.set_clickup_token(session, user_id, "pk_x")
        tasks = [
            {"name": "Rewrite category copy", "status": {"status": "to do", "type": "open"},
             "assignees": []},
            {"name": "Mid-flight work", "status": {"status": "doing", "type": "custom"},
             "assignees": []},
        ]
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "acme"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=tasks,
        ):
            result = report_service.generate(
                session, client_id=client.id, block_keys=["planned_works"], user_id=user_id,
            )
        planned = result["blocks"][0]
        self.assertEqual(planned["status"], "ok")
        self.assertEqual(planned["data"]["count"], 1, "the 'to do' task must be picked up")
        self.assertEqual(planned["data"]["tasks"][0]["name"], "Rewrite category copy")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def test_generate_marks_unknown_key_unavailable_without_failing_others(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["intro_header", "does_not_exist"],
        )
        by_key = {block["block_type_key"]: block for block in result["blocks"]}
        self.assertEqual(by_key["intro_header"]["status"], "ok")
        self.assertEqual(by_key["does_not_exist"]["status"], "unavailable")

    def test_generate_keeps_the_specialist_block_order(self) -> None:
        """The specialist's drag order (block_keys) is the order the blocks come
        back in, and the order the export renders the sections in."""
        client = _client(self.session)
        wanted = ["summary", "search_industry", "intro_header"]
        result = report_service.generate(self.session, client_id=client.id, block_keys=wanted)
        self.assertEqual([block["block_type_key"] for block in result["blocks"]], wanted)
        data = report_export._build_data(
            period_label="Jun 2026",
            default_comparison="mom",
            prepared="2026-07-01",
            blocks=result["blocks"],
            client_name="Acme Co",
            client_domain="acme.test",
        )
        self.assertEqual(data["report"]["order"], ["b14", "b2", "b1"])

    def test_generate_rejects_empty_selection(self) -> None:
        client = _client(self.session)
        with self.assertRaises(ValueError):
            report_service.generate(self.session, client_id=client.id, block_keys=[])

    def test_save_persists_report_and_blocks(self) -> None:
        client = _client(self.session)
        user_id = uuid.uuid4()
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {"client": "Acme Co"}, "comment": "hi"},
                {"block_type_key": "ga4_summary", "status": "unavailable", "data": None, "comment": "", "unavailable_reason": "Not configured"},
            ],
            generated_by=user_id,
        )
        _, blocks = report_service.get_report(self.session, report.id)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].block_type_key, "intro_header")
        self.assertEqual(blocks[0].comment, "hi")

    def test_update_edits_in_place_without_duplicating(self) -> None:
        client = _client(self.session)
        user_id = uuid.uuid4()
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {"x": 1}, "comment": "first"}],
            generated_by=user_id,
        )
        report_service.update_report(
            self.session,
            report_id=report.id,
            period_label="2026-06",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {"x": 1}, "comment": "second"}],
            generated_by=user_id,
        )
        reports = report_service.list_reports_for_client(self.session, client.id)
        self.assertEqual(len(reports), 1)  # no duplicate created
        _, blocks = report_service.get_report(self.session, report.id)
        self.assertEqual(blocks[0].comment, "second")

    def test_delete_removes_the_report_and_its_blocks(self) -> None:
        client = _client(self.session)
        keep = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-05",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": "keep"}],
            generated_by=uuid.uuid4(),
        )
        drop = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": "drop"},
                {"block_type_key": "ga4_summary", "status": "ok", "data": {"x": 1}, "comment": ""},
            ],
            generated_by=uuid.uuid4(),
        )

        report_service.delete_report(self.session, drop.id)

        remaining = report_service.list_reports_for_client(self.session, client.id)
        self.assertEqual([report.id for report in remaining], [keep.id])
        # Blocks have no cascade, so an incomplete delete would leave orphans.
        orphans = self.session.execute(
            select(ReportBlock).where(ReportBlock.report_id == drop.id)
        ).scalars().all()
        self.assertEqual(orphans, [])
        # The untouched report keeps its own blocks.
        _, kept_blocks = report_service.get_report(self.session, keep.id)
        self.assertEqual([block.comment for block in kept_blocks], ["keep"])

    def test_delete_unknown_report_raises_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            report_service.delete_report(self.session, uuid.uuid4())

    def test_save_allows_empty_comments(self) -> None:
        client = _client(self.session)
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
        )
        self.assertIsInstance(report.id, uuid.UUID)


class ExportTests(unittest.TestCase):
    def _export(self):
        import json as _json
        import re as _re

        session = _make_session()
        client = _client(session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""},
                {"block_type_key": "search_industry", "status": "ok", "data": {}, "comment": "Watch the June core update."},
                {"block_type_key": "ga4_summary", "status": "ok", "comment": "", "data": {
                    "period": "Jun 2026", "previous_period": "May 2026", "yoy_period": "Jun 2025",
                    "kpis": {
                        "current": {"sessions": 1000, "organic_sessions": 500, "total_users": 800,
                                    "new_users": 600, "returning_users": 200, "engaged_sessions": 900,
                                    "engagement_rate": 90.0, "bounce_rate": 10.0,
                                    "avg_session_duration_seconds": 120, "page_views": 3000,
                                    "pages_per_session": 3.0, "key_events": 2000},
                        "previous": {"sessions": 1200}, "yoy": {"sessions": 500},
                    },
                    "channels": [{"channel": "Organic Search", "sessions": 500, "engaged_sessions": 450, "users": 400}],
                    "daily": [{"date": "20260601", "sessions": 33, "engaged_sessions": 30, "users": 28}],
                    "top_events": [{"event_name": "page_view", "count": 3000, "users": 800}],
                }},
                {"block_type_key": "work_completed", "status": "unavailable", "data": None,
                 "comment": "", "unavailable_reason": "No ClickUp API key connected."},
            ],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(session, report.id)
        doc = report_export.build_report_html(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com"
        )
        data_match = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL)
        raw = data_match.group(1).replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        return doc, _json.loads(raw)

    def test_export_is_full_styled_html_document(self) -> None:
        doc, _ = self._export()
        self.assertTrue(doc.startswith("<!doctype html>"))
        self.assertNotIn("{{", doc)  # all markup tokens filled
        self.assertNotIn("__DATA_JSON__", doc)  # placeholder replaced
        self.assertIn("RANKBERRY · <b>Acme Co</b> SEO Report", doc)
        self.assertIn("June 2026", doc)
        # script tags balanced — the injected DATA can't break out of <script>
        self.assertEqual(doc.count("<script>"), doc.count("</script>"))

    def test_export_is_self_contained(self) -> None:
        doc, _ = self._export()
        # no external resource loading (CSS/JS/fonts/images are all inline)
        self.assertNotIn("<script src=", doc)
        self.assertNotIn("<link ", doc)
        self.assertNotIn("cdn", doc.lower())

    def test_export_data_maps_stored_blocks(self) -> None:
        _, data = self._export()
        cur = data["meta"]["cur"]
        self.assertEqual(data["meta"]["client"], "Acme Co")
        self.assertEqual(data["meta"]["periodLong"], "June 2026")
        self.assertEqual(data["ga4"][cur]["sessions"], 1000)
        self.assertEqual(data["ga4"][cur]["organic"], 500)

    def test_export_report_chrome_marks_selection_and_availability(self) -> None:
        _, data = self._export()
        blocks = data["report"]["blocks"]
        self.assertTrue(blocks["b5"]["selected"])
        self.assertEqual(blocks["b5"]["status"], "ok")
        self.assertEqual(blocks["b12"]["status"], "unavailable")
        self.assertIn("ClickUp", blocks["b12"]["reason"])
        # unselected blocks (e.g. Ahrefs b3) are absent from chrome -> hidden
        self.assertNotIn("b3", blocks)

    def test_export_injects_comment_into_block_with_notes_box(self) -> None:
        _, data = self._export()
        # search_industry -> b2 has a specialist-notes box
        self.assertIn("b2", data["report"]["comments"])
        self.assertIn("Watch the June core update.", data["report"]["comments"]["b2"])


class SecretsCryptoTests(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self) -> None:
        enc = secrets_crypto.encrypt("pk_secret_token_123")
        self.assertTrue(enc.startswith("enc:"))
        self.assertNotIn("pk_secret_token_123", enc)  # not stored in the clear
        self.assertEqual(secrets_crypto.decrypt(enc), "pk_secret_token_123")

    def test_decrypt_passthrough_for_legacy_plaintext(self) -> None:
        self.assertEqual(secrets_crypto.decrypt("pk_legacy"), "pk_legacy")

    def test_decrypt_none_is_none(self) -> None:
        self.assertIsNone(secrets_crypto.decrypt(None))

    def test_hint_masks_token(self) -> None:
        self.assertEqual(secrets_crypto.hint("pk_936_abcd1234"), "pk_…1234")
        self.assertIsNone(secrets_crypto.hint(None))


class SettingsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.user_id = uuid.uuid4()

    def test_set_get_roundtrip_and_encrypted_at_rest(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_abc_123456")
        self.assertEqual(settings_service.get_clickup_token(self.session, self.user_id), "pk_abc_123456")
        # stored column must be ciphertext, never the raw token
        from backend.app.models import UserSettings
        from sqlalchemy import select

        row = self.session.execute(
            select(UserSettings).where(UserSettings.user_id == self.user_id)
        ).scalar_one()
        self.assertTrue(row.clickup_token_encrypted.startswith("enc:"))
        self.assertNotIn("pk_abc_123456", row.clickup_token_encrypted)

    def test_set_updates_existing_row(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_first")
        settings_service.set_clickup_token(self.session, self.user_id, "pk_second")
        self.assertEqual(settings_service.get_clickup_token(self.session, self.user_id), "pk_second")

    def test_clear_token(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        settings_service.clear_clickup_token(self.session, self.user_id)
        self.assertIsNone(settings_service.get_clickup_token(self.session, self.user_id))

    def test_status_reflects_configured_state_without_exposing_token(self) -> None:
        self.assertEqual(
            settings_service.get_status(self.session, self.user_id),
            {"clickup_configured": False, "clickup_token_hint": None},
        )
        settings_service.set_clickup_token(self.session, self.user_id, "pk_936_abcd1234")
        status = settings_service.get_status(self.session, self.user_id)
        self.assertTrue(status["clickup_configured"])
        self.assertEqual(status["clickup_token_hint"], "pk_…1234")
        self.assertNotIn("pk_936_abcd1234", str(status))

    def test_empty_token_rejected(self) -> None:
        with self.assertRaises(ValueError):
            settings_service.set_clickup_token(self.session, self.user_id, "   ")


class ClickUpClientMatchTests(unittest.TestCase):
    def _lists(self):
        return [
            {"id": "1", "name": "General", "folder": None},
            {"id": "2", "name": "onebyone (30)", "folder": None},
            {"id": "3", "name": "Acme Corp Tasks", "folder": "Clients"},
        ]

    def test_matches_list_by_domain_root_label(self) -> None:
        with patch(
            "backend.app.report_builder.data_sources.clickup_client._iter_all_lists",
            return_value=iter(self._lists()),
        ):
            match = find_client_list("tok", name="OneByOne", domain="onebyone.ua")
        self.assertEqual(match, {"id": "2", "name": "onebyone (30)"})

    def test_matches_list_by_client_name(self) -> None:
        with patch(
            "backend.app.report_builder.data_sources.clickup_client._iter_all_lists",
            return_value=iter(self._lists()),
        ):
            match = find_client_list("tok", name="Acme Corp", domain="acme.com")
        self.assertEqual(match["id"], "3")

    def test_no_match_returns_none(self) -> None:
        with patch(
            "backend.app.report_builder.data_sources.clickup_client._iter_all_lists",
            return_value=iter(self._lists()),
        ):
            self.assertIsNone(find_client_list("tok", name="Unrelated", domain="nowhere.io"))

    # -- names that drifted apart between the dashboard and ClickUp ------------

    def _real_lists(self):
        """Names taken from the live workspace, including the ones that broke."""
        return [
            {"id": "a", "name": "CONTENT", "folder": None},
            {"id": "b", "name": "PBN", "folder": None},
            {"id": "c", "name": "List", "folder": None},
            {"id": "d", "name": "onebyone (30)", "folder": "Oleksiy"},
            {"id": "e", "name": "tarsco (30)", "folder": "Oleksiy"},
            {"id": "f", "name": "Premiumplate (40)", "folder": "Oleksiy"},
            {"id": "g", "name": "factorydirectblinds.com(30)", "folder": "Oleksiy"},
            {"id": "h", "name": "onebyoneshop (23)", "folder": None},
            {"id": "i", "name": "LampConcept.se (25)", "folder": None},
        ]

    def _find(self, name, domain, lists=None):
        with patch(
            "backend.app.report_builder.data_sources.clickup_client._iter_all_lists",
            return_value=iter(lists if lists is not None else self._real_lists()),
        ):
            return find_client_list("tok", name=name, domain=domain)

    def test_a_shorter_list_name_matches_a_longer_client_name(self) -> None:
        """The reported failure: dashboard "premiumplatesupply", list
        "Premiumplate (40)". The old matcher only looked for the client name
        *inside* the list name, so this pulled no tasks at all."""
        match = self._find("premiumplatesupply", "premiumplatesupply.com")
        self.assertEqual(match["name"], "Premiumplate (40)")

    def test_spaces_case_and_punctuation_are_ignored(self) -> None:
        for name in ("Premium Plate Supply", "PREMIUM-PLATE_SUPPLY", "  premium plate supply  "):
            self.assertEqual(
                self._find(name, "premiumplatesupply.com")["name"], "Premiumplate (40)", name
            )

    def test_an_exact_name_beats_a_longer_list_that_merely_contains_it(self) -> None:
        """"onebyone" must not land on "onebyoneshop (23)" just because that
        list comes first in the workspace — and vice versa."""
        self.assertEqual(self._find("onebyone", "onebyone.ua")["name"], "onebyone (30)")
        self.assertEqual(self._find("onebyoneshop", "onebyoneshop.com")["name"], "onebyoneshop (23)")

    def test_a_dotted_list_name_still_matches_the_domain(self) -> None:
        self.assertEqual(
            self._find("Factory Direct Blinds", "factorydirectblinds.com")["name"],
            "factorydirectblinds.com(30)",
        )
        self.assertEqual(
            self._find("LampConcept.se", "lampconcept.se")["name"], "LampConcept.se (25)"
        )

    def test_a_short_list_name_never_claims_a_client(self) -> None:
        """"PBN", "List" and "CONTENT" are shared working lists, not any
        client's — and their letters do appear inside plausible client names.
        Caught for real while writing this: "CONTENT" claimed "Contentful"."""
        self.assertIsNone(self._find("Publisher BN Group", "pbnlisting.com"))
        self.assertIsNone(self._find("Contentful", "contentful.io"))
        self.assertIsNone(self._find("Listopia", "listopia.com"))

    def test_the_loose_direction_needs_a_substantial_list_name(self) -> None:
        """Documents the floor, so lowering it fails here rather than in a
        report that quietly loaded another client's tasks."""
        lists = [{"id": "x", "name": "Content", "folder": None}]  # 7 characters
        self.assertIsNone(self._find("Contentful", "contentful.io", lists=lists))
        lists = [{"id": "y", "name": "Contented", "folder": None}]  # 9 characters
        self.assertIsNotNone(self._find("Contentedly", "contentedly.io", lists=lists))

    def test_a_client_with_no_list_still_matches_nothing(self) -> None:
        self.assertIsNone(self._find("eatlebab", "eatlebab.com"))
        self.assertIsNone(self._find("Totally Unrelated", "nowhere-at-all.io"))


class ClickUpTransportRetryTests(unittest.TestCase):
    """A flaky connection gets one more go; a real refusal does not.

    From a live report: "Could not reach ClickUp: _ssl.c:1120: The handshake
    operation timed out" killed Work completed and Planned works at once. Only
    429 was retried, so one bad connection out of dozens lost the section.
    """

    def _ok(self, body=None):
        return SimpleNamespace(
            status_code=200, content=b"{}", text="{}", headers={},
            json=lambda: (body if body is not None else {"teams": []}),
        )

    def _call(self, responses):
        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            result = responses[calls["n"]]
            calls["n"] += 1
            if isinstance(result, Exception):
                raise result
            return result

        with patch("httpx.get", side_effect=fake_get), \
             patch.object(clickup_client_module.time, "sleep"):
            try:
                return clickup_client_module._get("tok", "team"), calls["n"]
            except ClickUpAccessError as error:
                return error, calls["n"]

    def test_a_handshake_timeout_is_retried_once_and_can_succeed(self) -> None:
        out, calls = self._call([
            httpx.ConnectTimeout("_ssl.c:1120: The handshake operation timed out"),
            self._ok({"teams": [{"id": "1"}]}),
        ])
        self.assertEqual(out, {"teams": [{"id": "1"}]})
        self.assertEqual(calls, 2)

    def test_a_read_timeout_is_retried_once(self) -> None:
        out, calls = self._call([
            httpx.ReadTimeout("The read operation timed out"),
            self._ok(),
        ])
        self.assertEqual(out, {"teams": []})
        self.assertEqual(calls, 2)

    def test_it_gives_up_after_the_retry_and_says_what_happened(self) -> None:
        out, calls = self._call([
            httpx.ReadTimeout("The read operation timed out"),
            httpx.ReadTimeout("The read operation timed out"),
        ])
        self.assertIsInstance(out, ClickUpAccessError)
        self.assertIn("Could not reach ClickUp", str(out))
        self.assertEqual(calls, 2)

    def test_a_bad_token_is_not_retried(self) -> None:
        bad = SimpleNamespace(status_code=401, content=b"", text="", headers={}, json=lambda: {})
        out, calls = self._call([bad])
        self.assertIsInstance(out, ClickUpAccessError)
        self.assertEqual(calls, 1)

    def test_a_timeout_then_a_rate_limit_both_get_their_own_retry(self) -> None:
        """The two budgets are separate: one flaky connection must not spend the
        429 retry that a burst still needs."""
        limited = SimpleNamespace(
            status_code=429, content=b"", text="", headers={"Retry-After": "1"}, json=lambda: {}
        )
        out, calls = self._call([
            httpx.ReadTimeout("timed out"),
            limited,
            self._ok({"teams": [{"id": "9"}]}),
        ])
        self.assertEqual(out, {"teams": [{"id": "9"}]})
        self.assertEqual(calls, 3)


class ClickUpIterAllListsTests(unittest.TestCase):
    """_iter_all_lists must also surface lists shared with the token via the
    "Shared with me" hierarchy, which lives outside the team's own space tree."""

    def _fake_get(self, token, path, params=None):
        responses = {
            "team": {"teams": [{"id": "T1"}]},
            "team/T1/space": {"spaces": [{"id": "S1"}]},
            "space/S1/folder": {"folders": [
                {"name": "Clients", "lists": [{"id": "1", "name": "Owned List"}]},
            ]},
            "space/S1/list": {"lists": [{"id": "2", "name": "Folderless List"}]},
            "team/T1/shared": {"shared": {
                "folders": [{"name": "Oleksiy", "lists": [{"id": "3", "name": "onebyone (30)"}]}],
                "lists": [{"id": "4", "name": "Shared Folderless"},
                          {"id": "1", "name": "Owned List"}],  # duplicate id -> suppressed
            }},
        }
        return responses[path]

    def test_yields_owned_and_shared_lists_deduped(self) -> None:
        with patch(
            "backend.app.report_builder.data_sources.clickup_client._get",
            side_effect=self._fake_get,
        ):
            found = list(clickup_client_module._iter_all_lists("tok"))
        by_id = {item["id"]: item for item in found}
        self.assertEqual(sorted(by_id), ["1", "2", "3", "4"])  # no duplicate "1"
        self.assertEqual(by_id["3"], {"id": "3", "name": "onebyone (30)", "folder": "Oleksiy"})
        self.assertEqual(len(found), 4)

    def test_shared_endpoint_failure_is_non_fatal(self) -> None:
        def _get(token, path, params=None):
            if path == "team/T1/shared":
                raise ClickUpAccessError("shared not available")
            return self._fake_get(token, path, params)

        with patch(
            "backend.app.report_builder.data_sources.clickup_client._get",
            side_effect=_get,
        ):
            found = list(clickup_client_module._iter_all_lists("tok"))
        self.assertEqual(sorted(i["id"] for i in found), ["1", "2"])


def _clickup_tasks_fixture():
    return [
        {"id": "1", "name": "Publish blog post", "url": "https://app.clickup.com/t/1",
         "status": {"status": "done", "type": "done"}, "date_done": "1750000000000",
         "assignees": [{"username": "Denys"}]},
        {"id": "2", "name": "Fix meta tags", "url": "https://app.clickup.com/t/2",
         "status": {"status": "complete", "type": "closed"}, "date_done": "1750100000000", "assignees": []},
        {"id": "3", "name": "Build backlinks", "url": "https://app.clickup.com/t/3",
         "status": {"status": "todo", "type": "open"}, "due_date": "1752000000000", "assignees": []},
        {"id": "4", "name": "Keyword research", "url": "https://app.clickup.com/t/4",
         "status": {"status": "doing", "type": "custom"}, "assignees": [{"username": "Bohdan"}]},
    ]


# The fixture's DONE task ("Publish blog post") was worked on 15 June 2025 —
# that one time entry is what places it in a report month.
_CLICKUP_TIME_FIXTURE = {
    "1": [{"user": {"username": "Denys"},
           "intervals": [{"start": "1750000000000", "time": "3600000"}]}],
}


def _patched_clickup_time(entries=None):
    entries = _CLICKUP_TIME_FIXTURE if entries is None else entries
    return patch(
        "backend.app.report_builder.data_sources.clickup_client.fetch_task_time",
        side_effect=lambda token, task_id: entries.get(task_id, []),
    )


class ClickUpResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.user_id = uuid.uuid4()
        self.client = _client(self.session, name="Onebyone", domain="onebyone.ua")

    def _context(self, period_label: str = "2025-06") -> ResolveContext:
        return ResolveContext(
            client=self.client, period_label=period_label, now=utcnow(),
            session=self.session, user_id=self.user_id,
        )

    def test_unavailable_when_user_has_no_token(self) -> None:
        result = clickup.resolve(get_block("work_completed"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("No ClickUp API key", result.unavailable_reason)

    def test_unavailable_when_no_list_matches(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        with patch("backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list", return_value=None):
            result = clickup.resolve(get_block("work_completed"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("No ClickUp list found", result.unavailable_reason)

    def test_done_lists_completed_tasks_todo_lists_open_tasks(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        context = self._context(period_label="2025-06")
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "onebyone (30)"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=_clickup_tasks_fixture(),
        ) as mocked_fetch, _patched_clickup_time():
            completed = clickup.resolve(get_block("work_completed"), context)
            planned = clickup.resolve(get_block("planned_works"), context)

        # DONE: only the "Done"-status task — "complete" (closed/archived) is a
        # different stage and doesn't count.
        self.assertEqual(completed.status, "ok")
        self.assertEqual(completed.data["count"], 1)
        self.assertEqual({t["name"] for t in completed.data["tasks"]}, {"Publish blog post"})
        self.assertEqual(completed.data["tasks"][0]["date_done"], "2025-06-15")

        # TODO: only the "Todo"-status task — "doing" (in progress) doesn't count.
        self.assertEqual(planned.status, "ok")
        self.assertEqual(planned.data["count"], 1)
        self.assertEqual({t["name"] for t in planned.data["tasks"]}, {"Build backlinks"})

        # both blocks share a single tasks fetch via the context cache
        mocked_fetch.assert_called_once()

    def test_done_drops_a_task_with_no_tracked_time_in_the_month(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        # Reporting July 2025 — the fixture's DONE task was worked on in June, so
        # it belongs in June's report, not this one. Planned works is not scoped
        # by period at all and still lists its task.
        context = self._context(period_label="2025-07")
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "onebyone (30)"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=_clickup_tasks_fixture(),
        ), _patched_clickup_time():
            completed = clickup.resolve(get_block("work_completed"), context)
            planned = clickup.resolve(get_block("planned_works"), context)

        self.assertEqual(completed.status, "ok")
        self.assertEqual(completed.data["count"], 0)
        self.assertEqual(planned.data["count"], 1)

    def test_api_error_becomes_unavailable(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            side_effect=ClickUpAccessError("ClickUp token is invalid or expired (401)."),
        ):
            result = clickup.resolve(get_block("work_completed"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("401", result.unavailable_reason)


class PeriodWindowTests(unittest.TestCase):
    def test_default_selection_is_latest_single_month(self) -> None:
        windows = periods.resolve_windows(["Jun 2026", "May 2026", "Jun 2025"], None)
        self.assertEqual(windows.current.labels, ["Jun 2026"])
        self.assertEqual(windows.current.display, "Jun 2026")
        self.assertEqual(windows.previous.labels, ["May 2026"])
        self.assertEqual(windows.yoy.labels, ["Jun 2025"])

    def test_range_selection_builds_current_previous_yoy(self) -> None:
        selection = PeriodSelection(start=date(2026, 5, 1), end=date(2026, 6, 1))
        windows = periods.resolve_windows(
            ["Jun 2026", "May 2026", "Apr 2026", "Jun 2025", "May 2025"], selection
        )
        self.assertEqual(windows.current.labels, ["May 2026", "Jun 2026"])
        self.assertEqual(windows.current.display, "May 2026 – Jun 2026")
        # previous = the equally long span immediately before (Mar+Apr); only Apr present
        self.assertEqual(windows.previous.labels, ["Apr 2026"])
        self.assertEqual(windows.previous.display, "Mar 2026 – Apr 2026")
        self.assertEqual(windows.yoy.labels, ["May 2025", "Jun 2025"])

    def test_yearly_selection_display_is_the_year(self) -> None:
        selection = periods.parse_selection("2026-01-01", "2026-12-31", "yearly")
        self.assertIsNotNone(selection)
        self.assertEqual(periods.selection_display(selection), "2026")

    def test_parse_selection_requires_both_bounds(self) -> None:
        self.assertIsNone(periods.parse_selection("2026-01-01", None))
        self.assertIsNone(periods.parse_selection(None, None))

    def test_one_date_on_its_own_is_refused_not_quietly_dropped(self) -> None:
        """Falling back to the latest month here handed back a different month
        than the caller asked for, and said nothing about it."""
        with self.assertRaises(ValueError):
            report_service.resolve_timeframe(utcnow(), date_from="2026-01-01")
        with self.assertRaises(ValueError):
            report_service.resolve_timeframe(utcnow(), date_to="2026-01-31")

    def test_no_dates_at_all_still_means_the_latest_month(self) -> None:
        selection, _ = report_service.resolve_timeframe(utcnow())
        self.assertIsNone(selection)

    def test_window_latest_picks_most_recent_label(self) -> None:
        selection = PeriodSelection(start=date(2026, 1, 1), end=date(2026, 6, 1))
        windows = periods.resolve_windows(["Jan 2026", "Mar 2026", "Jun 2026"], selection)
        self.assertEqual(windows.current.latest, "Jun 2026")


class GA4RangeAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.client = _client(self.session, name="Acme Co", ga4_sheet_id="sheet-123")

    def _context(self, selection) -> ResolveContext:
        return ResolveContext(
            client=self.client,
            period_label="",
            now=utcnow(),
            session=self.session,
            period_selection=selection,
        )

    def test_summary_sums_kpis_across_the_range(self) -> None:
        selection = PeriodSelection(start=date(2026, 5, 1), end=date(2026, 6, 1))
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_summary"), self._context(selection))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["period"], "May 2026 – Jun 2026")
        # May + Jun 2026 sessions summed
        self.assertEqual(result.data["kpis"]["current"]["sessions"], 1030014 + 1337409)
        # yoy window is May+Jun 2025; only Jun 2025 present in the fixture
        self.assertEqual(result.data["kpis"]["yoy"]["sessions"], 518345)
        # channels aggregated across both months: Direct = 162884 (Jun) + 150000 (May)
        by_channel = {c["channel"]: c["sessions"] for c in result.data["channels"]}
        self.assertEqual(by_channel["Direct"], 162884 + 150000)
        self.assertEqual(result.data["channels"][0]["channel"], "Direct")  # sorted desc


class SelectionsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.user_id = uuid.uuid4()
        self.client_id = uuid.uuid4()

    def test_defaults_when_nothing_saved(self) -> None:
        selection = selections_service.get_selection(self.session, self.user_id, self.client_id)
        self.assertEqual(selection["block_keys"], [])
        self.assertEqual(selection["report_type"], "monthly")

    def test_save_and_restore_roundtrip(self) -> None:
        selections_service.save_selection(
            self.session,
            self.user_id,
            self.client_id,
            block_keys=["ga4_summary", "gsc_summary"],
            report_type="yearly",
            date_from="2026-01-01",
            date_to="2026-12-31",
        )
        selection = selections_service.get_selection(self.session, self.user_id, self.client_id)
        self.assertEqual(selection["block_keys"], ["ga4_summary", "gsc_summary"])
        self.assertEqual(selection["report_type"], "yearly")
        self.assertEqual(selection["date_from"], "2026-01-01")

    def test_save_updates_existing_row(self) -> None:
        selections_service.save_selection(
            self.session, self.user_id, self.client_id, block_keys=["ga4_summary"]
        )
        selections_service.save_selection(
            self.session, self.user_id, self.client_id, block_keys=["gsc_summary"]
        )
        selection = selections_service.get_selection(self.session, self.user_id, self.client_id)
        self.assertEqual(selection["block_keys"], ["gsc_summary"])

    def test_selection_is_scoped_per_user_and_client(self) -> None:
        other_client = uuid.uuid4()
        selections_service.save_selection(
            self.session, self.user_id, self.client_id, block_keys=["ga4_summary"]
        )
        other = selections_service.get_selection(self.session, self.user_id, other_client)
        self.assertEqual(other["block_keys"], [])

    def test_period_and_comparisons_roundtrip(self) -> None:
        selections_service.save_selection(
            self.session,
            self.user_id,
            self.client_id,
            block_keys=["ga4_summary"],
            period_preset="last_3_months",
            comparisons=["yoy", "mom"],
        )
        selection = selections_service.get_selection(self.session, self.user_id, self.client_id)
        self.assertEqual(selection["period_preset"], "last_3_months")
        self.assertEqual(selection["comparisons"], ["yoy", "mom"])

    def test_legacy_stored_preset_restores_as_period_and_comparisons(self) -> None:
        # a selection saved before the period/comparison split
        selections_service.save_selection(
            self.session,
            self.user_id,
            self.client_id,
            block_keys=["ga4_summary"],
            comparison="last_month_vs_year",
        )
        selection = selections_service.get_selection(self.session, self.user_id, self.client_id)
        self.assertEqual(selection["period_preset"], "last_month")
        self.assertEqual(selection["comparisons"], ["yoy"])

    def test_advanced_timeframe_stores_no_period_preset(self) -> None:
        selections_service.save_selection(
            self.session,
            self.user_id,
            self.client_id,
            block_keys=["ga4_summary"],
            report_type="yearly",
            date_from="2026-01-01",
            date_to="2026-12-31",
        )
        selection = selections_service.get_selection(self.session, self.user_id, self.client_id)
        self.assertIsNone(selection["period_preset"])
        self.assertEqual(selection["comparisons"], [])


class ClickUpRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.user_id = uuid.uuid4()
        self.client = _client(self.session, name="Onebyone", domain="onebyone.ua")
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")

    def _context(self, selection) -> ResolveContext:
        return ResolveContext(
            client=self.client,
            period_label="",
            now=utcnow(),
            session=self.session,
            user_id=self.user_id,
            period_selection=selection,
        )

    def _resolve_completed(self, selection):
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "onebyone (30)"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=_clickup_tasks_fixture(),
        ), _patched_clickup_time():
            return clickup.resolve(get_block("work_completed"), self._context(selection))

    def test_done_task_counts_when_the_range_covers_its_tracked_month(self) -> None:
        # the fixture's DONE task was worked on 2025-06-15
        selection = PeriodSelection(start=date(2025, 5, 1), end=date(2025, 7, 1))
        result = self._resolve_completed(selection)
        self.assertEqual(result.data["count"], 1)

    def test_done_task_drops_out_when_the_range_misses_its_tracked_month(self) -> None:
        selection = PeriodSelection(start=date(2025, 8, 1), end=date(2025, 9, 1))
        result = self._resolve_completed(selection)
        self.assertEqual(result.data["count"], 0)


class ComparisonPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        # 15 Jul 2026 -> last completed month is Jun 2026.
        self.now = datetime(2026, 7, 15, tzinfo=timezone.utc)

    def test_last_month_vs_prev_is_single_month_mom(self) -> None:
        selection, mode = periods.parse_comparison("last_month_vs_prev", self.now)
        self.assertEqual(selection.start, date(2026, 6, 1))
        self.assertEqual(selection.end, date(2026, 6, 1))
        self.assertEqual(mode, "mom")

    def test_last_month_vs_year_is_single_month_yoy(self) -> None:
        selection, mode = periods.parse_comparison("last_month_vs_year", self.now)
        self.assertEqual(selection.start, date(2026, 6, 1))
        self.assertEqual(selection.end, date(2026, 6, 1))
        self.assertEqual(mode, "yoy")

    def test_last_3_months_vs_year_spans_three_months_yoy(self) -> None:
        selection, mode = periods.parse_comparison("last_3_months_vs_year", self.now)
        self.assertEqual(selection.start, date(2026, 4, 1))
        self.assertEqual(selection.end, date(2026, 6, 1))
        self.assertEqual(mode, "yoy")
        self.assertEqual(periods.selection_display(selection), "Apr 2026 – Jun 2026")

    def test_unknown_preset_returns_none(self) -> None:
        self.assertIsNone(periods.parse_comparison("nope", self.now))
        self.assertIsNone(periods.parse_comparison(None, self.now))

    def test_preset_crosses_year_boundary(self) -> None:
        selection, _ = periods.parse_comparison("last_3_months_vs_year", datetime(2026, 1, 10, tzinfo=timezone.utc))
        # last completed month is Dec 2025; three-month window is Oct–Dec 2025
        self.assertEqual(selection.start, date(2025, 10, 1))
        self.assertEqual(selection.end, date(2025, 12, 1))


class PeriodPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        # 15 Jul 2026 -> last completed month is Jun 2026.
        self.now = datetime(2026, 7, 15, tzinfo=timezone.utc)

    def test_last_month_is_the_last_completed_month(self) -> None:
        selection = periods.parse_period_preset("last_month", self.now)
        self.assertEqual(selection.start, date(2026, 6, 1))
        self.assertEqual(selection.end, date(2026, 6, 1))

    def test_last_3_months_spans_three_whole_months(self) -> None:
        selection = periods.parse_period_preset("last_3_months", self.now)
        self.assertEqual(selection.start, date(2026, 4, 1))
        self.assertEqual(selection.end, date(2026, 6, 1))
        self.assertEqual(periods.selection_display(selection), "Apr 2026 – Jun 2026")

    def test_unknown_period_preset_returns_none(self) -> None:
        self.assertIsNone(periods.parse_period_preset("last_decade", self.now))
        self.assertIsNone(periods.parse_period_preset(None, self.now))

    def test_normalize_comparisons_keeps_order_and_drops_noise(self) -> None:
        self.assertEqual(periods.normalize_comparisons(["yoy", "mom", "yoy"]), ["yoy", "mom"])
        self.assertEqual(periods.normalize_comparisons(["MoM"]), ["mom"])
        self.assertEqual(periods.normalize_comparisons(["nope"]), ["mom"])
        self.assertEqual(periods.normalize_comparisons([]), ["mom"])

    def test_legacy_preset_maps_onto_period_and_comparisons(self) -> None:
        self.assertEqual(periods.legacy_comparison_preset("last_month_vs_year"), ("last_month", ["yoy"]))
        self.assertEqual(
            periods.legacy_comparison_preset("last_3_months_vs_year"), ("last_3_months", ["yoy"])
        )
        self.assertIsNone(periods.legacy_comparison_preset("nope"))


class GenerateComparisonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def test_generate_sets_default_comparison_from_legacy_preset(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["intro_header"],
            comparison="last_3_months_vs_year",
        )
        self.assertEqual(result["default_comparison"], "yoy")

    def test_generate_keeps_every_chosen_comparison_in_order(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["intro_header"],
            period_preset="last_3_months",
            comparisons=["yoy", "mom"],
        )
        # first chosen comparison is the one the report opens on
        self.assertEqual(result["default_comparison"], "yoy,mom")

    def test_generate_drops_unknown_comparisons(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["intro_header"],
            period_preset="last_month",
            comparisons=["nonsense", "yoy", "yoy"],
        )
        self.assertEqual(result["default_comparison"], "yoy")

    def test_generate_offers_both_comparisons_without_a_preset(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["intro_header"],
        )
        self.assertEqual(result["default_comparison"], "mom,yoy")

    def test_save_persists_default_comparison(self) -> None:
        client = _client(self.session)
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
            default_comparison="yoy",
        )
        summary = report_service.serialize_report_summary(report)
        self.assertEqual(summary["default_comparison"], "yoy")


class PlannedWorkManualTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def test_manual_planned_work_skips_clickup_and_stores_text(self) -> None:
        client = _client(self.session)
        result = report_service.generate(
            self.session,
            client_id=client.id,
            block_keys=["planned_works"],
            user_id=uuid.uuid4(),  # no ClickUp token — manual must not need one
            planned_work_mode="manual",
            planned_work_text="Ship the new landing pages and refresh meta titles.",
        )
        block = {b["block_type_key"]: b for b in result["blocks"]}["planned_works"]
        self.assertEqual(block["status"], "ok")
        self.assertEqual(block["data"]["mode"], "manual")
        self.assertIn("landing pages", block["data"]["text"])


class TaskTableExportTests(unittest.TestCase):
    def test_task_rows_carry_only_the_title_and_id(self) -> None:
        import json as _json
        import re as _re

        session = _make_session()
        client = _client(session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""},
                {"block_type_key": "work_completed", "status": "ok", "comment": "", "data": {
                    "list_name": "acme", "count": 2, "total_time_spent_ms": 12600000, "tasks": [
                        {"name": "Publish blog post", "description": "Wrote and published the spring guide.",
                         "url": "https://app.clickup.com/t/abc", "time_spent_ms": 12600000},
                        {"name": "Fix meta tags", "description": "", "url": "https://app.clickup.com/t/def"},
                    ]},
                },
            ],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(session, report.id)
        doc = report_export.build_report_html(report_row, blocks, client_name="Acme Co", client_domain="acme.com")
        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        # The browser-tab title is this report's, not the client the template was
        # first drawn for.
        self.assertIn("<title>Acme Co — SEO Report — June 2026</title>", doc)
        data = _json.loads(raw)
        rows = data["workDone"]
        # [task, id] and nothing else: the ClickUp description and the tracked
        # time are deliberately not reported in this section.
        self.assertEqual(rows[0], ["Publish blog post", "abc"])
        self.assertEqual(rows[1], ["Fix meta tags", "def"])
        self.assertNotIn("workDoneTotal", data)

    @staticmethod
    def _work_blocks():
        """Two completed and two planned ClickUp tasks, one wrong one in each."""
        return [
            {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""},
            {"block_type_key": "work_completed", "status": "ok", "comment": "", "data": {
                "list_name": "acme", "count": 2, "total_time_spent_ms": 16200000, "tasks": [
                    {"name": "Publish blog post", "description": "",
                     "url": "https://app.clickup.com/t/abc", "time_spent_ms": 12600000},
                    {"name": "Wrong task", "description": "",
                     "url": "https://app.clickup.com/t/bad", "time_spent_ms": 3600000},
                ]},
            },
            {"block_type_key": "planned_works", "status": "ok", "comment": "", "data": {
                "list_name": "acme", "count": 2, "tasks": [
                    {"name": "Next month plan", "description": "",
                     "url": "https://app.clickup.com/t/plan1", "assignees": []},
                    {"name": "Wrong plan", "description": "",
                     "url": "https://app.clickup.com/t/plan2", "assignees": []},
                ]},
            },
        ]

    def _work_report(self, session):
        client = _client(session, name="Acme Co", domain="acme.com")
        return report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=self._work_blocks(),
            generated_by=uuid.uuid4(),
        )

    @staticmethod
    def _preview_data(doc):
        import json as _json
        import re as _re

        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        return _json.loads(raw)

    def test_excluded_tasks_are_dropped_from_the_client_report(self) -> None:
        """A task struck off in the preview must not reach the client, in either
        ClickUp section."""
        session = _make_session()
        report = self._work_report(session)
        report_row, blocks = report_service.get_report(session, report.id)
        doc = report_export.build_report_html(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com",
            customization={"excludedTasks": {
                "work_completed": ["bad"], "planned_works": ["plan2"]}},
        )
        data = self._preview_data(doc)
        self.assertEqual([r[1] for r in data["workDone"]], ["abc"])
        self.assertEqual([t["taskId"] for t in data["workPlanned"]], ["plan1"])

    def test_excluded_tasks_survive_in_the_editable_preview(self) -> None:
        """The editable preview keeps every task in the payload so "Restore all"
        has something to put back; the template hides them from the exclusion
        list, which the preview also carries."""
        doc = report_export.build_preview_html(
            period_label="Jun 2026",
            default_comparison="mom",
            blocks=self._work_blocks(),
            client_name="Acme Co",
            client_domain="acme.com",
            customization={"excludedTasks": {
                "work_completed": ["bad"], "planned_works": ["plan2"]}},
            editable=True,
        )
        data = self._preview_data(doc)
        self.assertEqual([r[1] for r in data["workDone"]], ["abc", "bad"])
        self.assertEqual([t["taskId"] for t in data["workPlanned"]], ["plan1", "plan2"])
        self.assertEqual(
            data["customization"]["excludedTasks"],
            {"work_completed": ["bad"], "planned_works": ["plan2"]},
        )

    def test_excluded_tasks_blob_is_sanitized(self) -> None:
        norm = report_export._normalize_excluded_tasks
        self.assertEqual(norm({"work_completed": ["a", "a", " b ", "", None]}),
                         {"work_completed": ["a", "b"]}, "ids are trimmed and deduped")
        self.assertEqual(norm({"work_completed": []}), {}, "empty lists carry no exclusion")
        for junk in (None, "abc", 7, {"work_completed": "abc"}):
            self.assertEqual(norm(junk), {})
        # The blob reaches the template through the customization contract.
        self.assertEqual(report_export._normalize_customization({})["excludedTasks"], {})

    def test_excluded_task_leaves_the_markdown_export(self) -> None:
        session = _make_session()
        report = self._work_report(session)
        report_row, blocks = report_service.get_report(session, report.id)
        md = report_export.build_report_markdown(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com",
            customization={"excludedTasks": {"work_completed": ["bad"]}},
        )
        self.assertIn("Publish blog post", md)
        self.assertNotIn("Wrong task", md)
        self.assertNotIn("Total time tracked", md)

    def test_manual_planned_work_exports_as_text(self) -> None:
        import json as _json
        import re as _re

        session = _make_session()
        client = _client(session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""},
                {"block_type_key": "planned_works", "status": "ok", "comment": "", "data": {
                    "mode": "manual", "text": "Launch the summer campaign.", "tasks": []}},
            ],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(session, report.id)
        doc = report_export.build_report_html(report_row, blocks, client_name="Acme Co", client_domain="acme.com")
        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        data = _json.loads(raw)
        self.assertIn("Launch the summer campaign.", data["workPlannedManual"])
        self.assertEqual(data["workPlanned"], [])


class CleanExportTests(unittest.TestCase):
    def _doc(self):
        session = _make_session()
        client = _client(session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""},
                {"block_type_key": "search_industry", "status": "ok", "data": {}, "comment": "Watch the core update."},
                {"block_type_key": "ga4_summary", "status": "ok", "comment": "", "data": {
                    "period": "Jun 2026", "previous_period": "May 2026", "yoy_period": "Jun 2025",
                    "kpis": {"current": {"sessions": 1000}, "previous": {"sessions": 900}, "yoy": {"sessions": 500}},
                }},
            ],
            generated_by=uuid.uuid4(),
            default_comparison="yoy",
        )
        report_row, blocks = report_service.get_report(session, report.id)
        return report_export.build_report_html(report_row, blocks, client_name="Acme Co", client_domain="acme.com")

    def test_no_save_or_print_controls(self) -> None:
        doc = self._doc()
        self.assertNotIn("Save report", doc)
        self.assertNotIn("window.print()", doc)
        self.assertNotIn("Print / PDF", doc)

    def test_comment_label_replaces_specialist_notes(self) -> None:
        doc = self._doc()
        self.assertNotIn("Specialist", doc)
        self.assertNotIn("✎ Notes", doc)
        self.assertIn(">Comment<", doc)  # card-comment label

    def test_default_comparison_carried_into_meta(self) -> None:
        import json as _json
        import re as _re

        doc = self._doc()
        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        data = _json.loads(raw)
        self.assertEqual(data["meta"]["defaultMode"], "yoy")


class ComparisonToggleExportTests(unittest.TestCase):
    """The report offers one toggle per comparison the specialist chose."""

    def _data(self, default_comparison: str, *, yoy: bool = True):
        import json as _json
        import re as _re

        session = _make_session()
        client = _client(session, name="Acme Co", domain="acme.com")
        ga4 = {
            "period": "Jun 2026", "previous_period": "May 2026",
            "kpis": {"current": {"sessions": 1000}, "previous": {"sessions": 900}},
        }
        if yoy:
            ga4["yoy_period"] = "Jun 2025"
            ga4["kpis"]["yoy"] = {"sessions": 500}
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "ga4_summary", "status": "ok", "comment": "", "data": ga4}],
            generated_by=uuid.uuid4(),
            default_comparison=default_comparison,
        )
        report_row, blocks = report_service.get_report(session, report.id)
        doc = report_export.build_report_html(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com"
        )
        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        return doc, _json.loads(raw)

    def test_both_chosen_comparisons_become_toggles(self) -> None:
        _, data = self._data("mom,yoy")
        modes = data["meta"]["modes"]
        self.assertEqual([mode["id"] for mode in modes], ["mom", "yoy"])
        self.assertEqual(modes[0]["label"], "Jun 2026 vs May 2026 · MoM")
        self.assertEqual(modes[1]["label"], "Jun 2026 vs Jun 2025 · YoY")
        self.assertEqual(data["meta"]["defaultMode"], "mom")

    def test_chosen_order_decides_which_comparison_opens(self) -> None:
        _, data = self._data("yoy,mom")
        self.assertEqual([mode["id"] for mode in data["meta"]["modes"]], ["yoy", "mom"])
        self.assertEqual(data["meta"]["defaultMode"], "yoy")

    def test_single_chosen_comparison_is_the_only_toggle(self) -> None:
        _, data = self._data("yoy")
        self.assertEqual([mode["id"] for mode in data["meta"]["modes"]], ["yoy"])

    def test_comparison_without_data_is_dropped(self) -> None:
        # no yoy period in the sheet -> the yoy toggle would compare against nothing
        _, data = self._data("mom,yoy", yoy=False)
        self.assertEqual([mode["id"] for mode in data["meta"]["modes"]], ["mom"])

    def test_multi_month_window_compares_against_the_previous_period(self) -> None:
        labels = report_export._mode_label("Apr 2026 – Jun 2026", "Jan 2026 – Mar 2026", "mom")
        self.assertEqual(labels, "Apr 2026 – Jun 2026 vs Jan 2026 – Mar 2026 · Prev. period")
        self.assertTrue(report_export._mode_label("2026", "2025", "yoy").endswith("· YoY"))

    def test_report_header_carries_the_period(self) -> None:
        doc, _ = self._data("mom")
        # the pinned top bar names the client and the period it covers
        self.assertIn('<span class="cb-period">June 2026</span>', doc)
        self.assertIn("position:sticky", doc)


class CommentLinkTests(unittest.TestCase):
    """URLs a specialist types into a comment must be live links in the report."""

    def test_plain_url_becomes_an_anchor(self) -> None:
        out = report_export._comment_html("See https://example.com/report?a=1&b=2 for details.")
        self.assertIn('href="https://example.com/report?a=1&amp;b=2"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_bare_www_link_gets_a_scheme(self) -> None:
        self.assertIn('href="https://www.example.com"', report_export._comment_html("www.example.com"))

    def test_trailing_sentence_punctuation_stays_outside_the_link(self) -> None:
        out = report_export._comment_html("Ranking page: https://example.com/page.")
        self.assertIn('>https://example.com/page</a>.', out)

    def test_bracket_inside_the_url_is_kept(self) -> None:
        out = report_export._comment_html("https://en.wikipedia.org/wiki/SEO_(marketing)")
        self.assertIn(">https://en.wikipedia.org/wiki/SEO_(marketing)</a>", out)

    def test_surrounding_text_is_still_escaped(self) -> None:
        out = report_export._comment_html("<b>bold</b> https://example.com\nnext line")
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", out)
        self.assertIn("<br>", out)
        self.assertEqual(out.count("<a "), 1)

    def test_comment_links_survive_into_the_exported_report(self) -> None:
        session = _make_session()
        client = _client(session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[
                {"block_type_key": "search_industry", "status": "ok", "data": {},
                 "comment": "Details: https://example.com/audit"},
            ],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(session, report.id)
        doc = report_export.build_report_html(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com"
        )
        self.assertIn("https://example.com/audit", doc)
        self.assertIn("\\u003ca href=", doc)  # anchor, escaped for the <script> block


class CustomizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()

    def _data_from_doc(self, doc):
        import json as _json
        import re as _re

        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        return _json.loads(raw)

    def test_normalize_customization_fills_defaults(self) -> None:
        norm = report_export._normalize_customization(None)
        self.assertIsNone(norm["accent"])
        self.assertEqual(norm["charts"], {})
        self.assertEqual(norm["panels"], {})

    def test_normalize_customization_rejects_bad_values(self) -> None:
        norm = report_export._normalize_customization(
            {"accent": "  ", "charts": {"ga4_mix": "bar"},
             "panels": {"ga4_summary": {"scale": "huge", "headingWeight": "black"}}}
        )
        self.assertIsNone(norm["accent"])
        self.assertEqual(norm["charts"], {"ga4_mix": "bar"})
        # invalid per-panel values fall back to defaults, resolved for the template
        panel = norm["panels"]["ga4_summary"]
        self.assertEqual(panel["scale"], "normal")
        self.assertEqual(panel["fontScale"], 1.0)
        self.assertEqual(panel["headingWeight"], "700")

    def test_normalize_panel_resolves_scale_and_weights(self) -> None:
        norm = report_export._normalize_customization(
            {"panels": {"ga4_summary": {"scale": "large", "headingWeight": "normal", "bodyWeight": "bold"}}}
        )
        panel = norm["panels"]["ga4_summary"]
        self.assertEqual(panel["scale"], "large")
        self.assertEqual(panel["fontScale"], 1.14)
        self.assertEqual(panel["headingWeight"], "400")
        self.assertEqual(panel["bodyWeight"], "700")

    def test_preview_render_embeds_customization_and_editable(self) -> None:
        client = _client(self.session, name="Acme Co", domain="acme.com")
        doc = report_export.build_preview_html(
            period_label="Jun 2026",
            default_comparison="mom",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            client_name=client.name,
            client_domain=client.domain,
            customization={"accent": "#123456", "charts": {"ga4_mix": "bar"},
                           "panels": {"ga4_summary": {"scale": "large"}}},
            editable=True,
        )
        data = self._data_from_doc(doc)
        self.assertTrue(data["editable"])
        self.assertEqual(data["customization"]["accent"], "#123456")
        self.assertEqual(data["customization"]["charts"]["ga4_mix"], "bar")
        self.assertEqual(data["customization"]["panels"]["ga4_summary"]["scale"], "large")

    def test_export_is_not_editable(self) -> None:
        client = _client(self.session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            self.session, client_id=client.id, period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(self.session, report.id)
        doc = report_export.build_report_html(report_row, blocks, client_name="Acme Co", client_domain="acme.com")
        self.assertFalse(self._data_from_doc(doc)["editable"])

    def test_save_and_export_roundtrip_customization(self) -> None:
        client = _client(self.session, name="Acme Co", domain="acme.com")
        custom = {"accent": "#ABCDEF", "charts": {"gsc_branded": "bar"},
                  "panels": {"ga4_summary": {"scale": "large"}}}
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
            customization=custom,
        )
        # serialized summary exposes it for the frontend to restore
        summary = report_service.serialize_report_summary(report)
        self.assertEqual(summary["customization"]["accent"], "#ABCDEF")
        # export reads report.customization when none is passed explicitly
        report_row, blocks = report_service.get_report(self.session, report.id)
        doc = report_export.build_report_html(report_row, blocks, client_name="Acme Co", client_domain="acme.com")
        data = self._data_from_doc(doc)
        self.assertEqual(data["customization"]["accent"], "#ABCDEF")
        self.assertEqual(data["customization"]["panels"]["ga4_summary"]["scale"], "large")
        self.assertEqual(data["customization"]["charts"]["gsc_branded"], "bar")

    def test_update_changes_customization(self) -> None:
        client = _client(self.session, name="Acme Co", domain="acme.com")
        report = report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
            customization={"accent": "#111111"},
        )
        report_service.update_report(
            self.session,
            report_id=report.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
            customization={"accent": "#222222"},
        )
        updated = self.session.get(report_service.Report, report.id)
        summary = report_service.serialize_report_summary(updated)
        self.assertEqual(summary["customization"]["accent"], "#222222")


class ReportCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        # Module-global cache: a report saved by one test must never be visible
        # to the next, and these tests assert on hit/miss counts.
        report_service.invalidate_report_cache()
        self.addCleanup(report_service.invalidate_report_cache)

    def _saved_report(self, *, comment="first"):
        client = _client(self.session)
        return report_service.save_report(
            self.session,
            client_id=client.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "data": {"rows": [1, 2, 3]}, "comment": comment}],
            generated_by=uuid.uuid4(),
        )

    def test_second_read_does_not_touch_the_database(self) -> None:
        report = self._saved_report()

        first_report, first_blocks = report_service.get_report(self.session, report.id)
        with patch.object(self.session, "execute") as execute, patch.object(self.session, "get") as get:
            second_report, second_blocks = report_service.get_report(self.session, report.id)

        execute.assert_not_called()
        get.assert_not_called()
        self.assertEqual(second_report.id, first_report.id)
        self.assertEqual(second_report.period_label, "Jun 2026")
        self.assertEqual(len(second_blocks), len(first_blocks))
        self.assertEqual(second_blocks[0].data_json, first_blocks[0].data_json)
        self.assertEqual(second_blocks[0].comment, "first")

    def test_cached_blocks_serialize_identically(self) -> None:
        report = self._saved_report()

        fresh = report_service.serialize_report_detail(*report_service.get_report(self.session, report.id))
        cached = report_service.serialize_report_detail(*report_service.get_report(self.session, report.id))

        self.assertEqual(fresh, cached)
        self.assertEqual(cached["blocks"][0]["data"], {"rows": [1, 2, 3]})

    def test_update_invalidates_so_the_next_read_sees_the_new_blocks(self) -> None:
        report = self._saved_report(comment="first")
        report_service.get_report(self.session, report.id)  # populate

        report_service.update_report(
            self.session,
            report_id=report.id,
            period_label="Jul 2026",
            blocks=[{"block_type_key": "intro_header", "data": {"rows": [9]}, "comment": "second"}],
            generated_by=uuid.uuid4(),
        )

        _, blocks = report_service.get_report(self.session, report.id)
        self.assertEqual(blocks[0].comment, "second")
        self.assertEqual(blocks[0].data_json, '{"rows": [9]}')

    def test_delete_invalidates_so_the_next_read_raises(self) -> None:
        report = self._saved_report()
        report_service.get_report(self.session, report.id)  # populate

        report_service.delete_report(self.session, report.id)

        with self.assertRaises(LookupError):
            report_service.get_report(self.session, report.id)

    def test_expired_entry_is_re_read(self) -> None:
        report = self._saved_report()
        report_service.get_report(self.session, report.id)

        # Jump past the TTL rather than sleeping through it.
        with patch(
            "backend.app.report_builder.service.time.monotonic",
            return_value=time.monotonic() + report_service.REPORT_CACHE_TTL_SECONDS + 1,
        ):
            with patch.object(self.session, "get", side_effect=self.session.get) as get:
                report_service.get_report(self.session, report.id)

        get.assert_called_once()

    def test_cache_never_grows_past_its_ceiling(self) -> None:
        for _ in range(report_service._REPORT_CACHE_MAX_ENTRIES + 5):
            saved = self._saved_report()
            report_service.get_report(self.session, saved.id)

        self.assertLessEqual(
            len(report_service._REPORT_CACHE),
            report_service._REPORT_CACHE_MAX_ENTRIES,
        )


class RevenueCurrencyTests(unittest.TestCase):
    """Revenue is printed in the sheet's currency, not a hardcoded ₴."""

    def _data(self, blocks):
        import json as _json
        import re as _re

        doc = report_export.build_preview_html(
            period_label="Jun 2026",
            default_comparison="mom",
            blocks=blocks,
            client_name="Acme Co",
            client_domain="acme.com",
        )
        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        return _json.loads(raw)

    def _monetization(self, **data):
        return {
            "block_type_key": "ga4_monetization",
            "status": "ok",
            "comment": "",
            "data": {"period": "Jun 2026", "previous_period": "May 2026", **data},
        }

    def test_symbol_lookup_maps_codes_and_passes_through_symbols(self) -> None:
        self.assertEqual(localization.currency_symbol("USD"), "$")
        self.assertEqual(localization.currency_symbol("uah"), "₴")
        self.assertEqual(localization.currency_symbol("€"), "€")
        # Not in the table — a sheet that typed "zł" should print "zł".
        self.assertEqual(localization.currency_symbol("zł"), "zł")

    def test_sheet_currency_reaches_the_template(self) -> None:
        data = self._data([self._monetization(currency="$")])
        self.assertEqual(data["meta"]["currency"], "$")

    def test_currency_column_is_read_off_the_ecommerce_tabs(self) -> None:
        rows = [{"Period": "Jun 2026", "Revenue": "100", "Currency": "EUR"}]
        self.assertEqual(ga4._currency_of(rows), "€")
        # Blank cells and older sheets without the column fall through.
        self.assertEqual(ga4._currency_of([{"Period": "Jun 2026", "Currency": " "}]), "$")
        self.assertEqual(ga4._currency_of([]), "$")

    def test_a_sheet_without_the_column_falls_back_to_us_dollars(self) -> None:
        data = self._data([{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}])
        self.assertEqual(data["meta"]["currency"], "$")

    def test_the_ai_traffic_block_supplies_the_currency_on_its_own(self) -> None:
        block = {
            "block_type_key": "ga4_ai_traffic",
            "status": "ok",
            "comment": "",
            "data": {
                "period": "Jun 2026",
                "previous_period": "May 2026",
                "currency": "₴",
                "summary": {"current": {"total_ai_sessions": 5, "engaged_sessions": 4, "engagement_rate": 80}},
                "tools": [],
                "top_pages": [],
            },
        }
        self.assertEqual(self._data([block])["meta"]["currency"], "₴")


class AiRevenueWithoutMonetizationTests(unittest.TestCase):
    """The AI-Traffic section's revenue cards read DATA.aiEcom, which used to be
    filled only by the monetization block — so a report without that block showed
    no AI revenue at all."""

    def _ai_ecom(self, blocks):
        import json as _json
        import re as _re

        doc = report_export.build_preview_html(
            period_label="Jun 2026",
            default_comparison="mom",
            blocks=blocks,
            client_name="Acme Co",
            client_domain="acme.com",
        )
        raw = _re.search(r"window\.DATA=(\{.*?\});</script>", doc, _re.DOTALL).group(1)
        raw = raw.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
        return _json.loads(raw).get("aiEcom") or {}

    def _ai_traffic_block(self):
        return {
            "block_type_key": "ga4_ai_traffic",
            "status": "ok",
            "comment": "",
            "data": {
                "period": "Jun 2026",
                "previous_period": "May 2026",
                "summary": {
                    "current": {"total_ai_sessions": 1057, "engaged_sessions": 800, "engagement_rate": 75.7},
                    "previous": {"total_ai_sessions": 900, "engaged_sessions": 700, "engagement_rate": 77.8},
                },
                "tools": [],
                "top_pages": [],
                "ecommerce": {
                    "current": {"purchases": 42, "revenue": 185000.5, "add_to_carts": 90, "checkouts": 60},
                    "previous": {"purchases": 31, "revenue": 120000.0, "add_to_carts": 70, "checkouts": 50},
                },
            },
        }

    def test_ai_sales_render_from_the_ai_traffic_block_alone(self) -> None:
        ai_ecom = self._ai_ecom([self._ai_traffic_block()])
        self.assertEqual(ai_ecom["2026-06"]["purchases"], 42)
        self.assertAlmostEqual(ai_ecom["2026-06"]["revenue"], 185000.5)
        self.assertEqual(ai_ecom["2026-05"]["purchases"], 31)

    def test_the_monetization_block_still_wins_when_both_are_present(self) -> None:
        monetization = {
            "block_type_key": "ga4_monetization",
            "status": "ok",
            "comment": "",
            "data": {
                "period": "Jun 2026",
                "previous_period": "May 2026",
                "site_wide": {"current": {"purchases": 6058, "revenue": 9e6, "add_to_carts": 1, "checkouts": 1}},
                "ai": {"current": {"purchases": 7, "revenue": 100.0, "add_to_carts": 1, "checkouts": 1}},
            },
        }
        ai_ecom = self._ai_ecom([monetization, self._ai_traffic_block()])
        self.assertEqual(ai_ecom["2026-06"]["purchases"], 7)


class ClientDomainCleaningTests(unittest.TestCase):
    """A pasted address becomes the bare host the report builds links from."""

    def setUp(self) -> None:
        self.session = _make_session()

    def test_a_pasted_url_becomes_a_bare_host(self) -> None:
        for typed, expected in (
            ("eatlebab.com", "eatlebab.com"),
            ("www.eatlebab.com", "www.eatlebab.com"),      # www is part of the host
            ("https://eatlebab.com", "eatlebab.com"),
            ("http://eatlebab.com/", "eatlebab.com"),
            ("https://www.eatlebab.com/venue-menus", "www.eatlebab.com"),
            ("eatlebab.com/", "eatlebab.com"),
            ("eatlebab.com?utm_source=x", "eatlebab.com"),
            ("eatlebab.com#top", "eatlebab.com"),
            ("  EatLebab.COM  ", "eatlebab.com"),
            ("eatlebab.com.", "eatlebab.com"),
            ("", ""),
        ):
            self.assertEqual(report_service.clean_domain(typed), expected, typed)

    def test_create_client_stores_the_cleaned_domain(self) -> None:
        client = report_service.create_client(
            self.session,
            name="eatlebab",
            domain="https://www.eatlebab.com/venue-menus",
            created_by=uuid.uuid4(),
        )
        self.assertEqual(client.domain, "www.eatlebab.com")

    def test_a_url_with_no_host_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            report_service.create_client(
                self.session, name="Acme", domain="https://", created_by=uuid.uuid4()
            )

    def test_the_cleaned_domain_builds_a_working_report_link(self) -> None:
        """The bug this exists for: "https://" + domain in the report template."""
        cleaned = report_service.clean_domain("https://eatlebab.com")
        self.assertEqual(f"https://{cleaned}/kebab-queen", "https://eatlebab.com/kebab-queen")
        # …and the top-movers URL shortens to a path again.
        self.assertEqual(
            report_export._url_path("https://www.eatlebab.com/kebab-queen", cleaned),
            "/kebab-queen",
        )

    def test_the_migration_sql_agrees_with_clean_domain(self) -> None:
        """Existing rows are cleaned by SQL, new ones by Python — same answer.

        Run against SQLite here, which has no regexp_replace, so the SQL's steps
        are applied as the equivalent Python. This guards the *steps* drifting
        apart, which is what would leave old rows broken after a deploy.
        """
        sql = (
            pathlib.Path(__file__).resolve().parents[1]
            / "backend/app/migrations/sql/020_normalize_client_domains.sql"
        ).read_text(encoding="utf-8")
        for fragment in ("^[A-Za-z][A-Za-z0-9+.-]*://", "[/?#].*$", "btrim", "lower"):
            self.assertIn(fragment, sql)


class AhrefsRetryTests(unittest.TestCase):
    """One retry, and only for the failures a second attempt could fix."""

    def _response(self, status: int, body: dict | None = None):
        return SimpleNamespace(
            status_code=status,
            content=b"{}",
            text="{}",
            json=lambda: (body if body is not None else {}),
        )

    def _get(self, responses):
        calls = {"n": 0}

        def fake_get(*args, **kwargs):
            result = responses[calls["n"]]
            calls["n"] += 1
            if isinstance(result, Exception):
                raise result
            return result

        with patch("httpx.get", side_effect=fake_get), \
             patch("backend.app.report_builder.data_sources.ahrefs_client._token", return_value="tok"), \
             patch("backend.app.report_builder.data_sources.ahrefs_client.time.sleep") as slept:
            try:
                return ahrefs_client.get("metrics", {"target": "acme.com"}), calls["n"], slept
            except AhrefsAccessError as error:
                return error, calls["n"], slept

    def test_a_rate_limit_is_retried_once_and_can_succeed(self) -> None:
        out, calls, slept = self._get([
            self._response(429),
            self._response(200, {"metrics": {"org_traffic": 5910}}),
        ])
        self.assertEqual(out, {"metrics": {"org_traffic": 5910}})
        self.assertEqual(calls, 2)
        slept.assert_called_once()

    def test_a_dropped_connection_is_retried_once(self) -> None:
        out, calls, _ = self._get([
            httpx.ConnectError("connection reset"),
            self._response(200, {"metrics": {}}),
        ])
        self.assertEqual(out, {"metrics": {}})
        self.assertEqual(calls, 2)

    def test_it_gives_up_after_the_second_attempt(self) -> None:
        out, calls, _ = self._get([self._response(503), self._response(503)])
        self.assertIsInstance(out, AhrefsAccessError)
        self.assertEqual(calls, 2)

    def test_a_rejected_token_is_not_retried(self) -> None:
        out, calls, slept = self._get([self._response(401)])
        self.assertIsInstance(out, AhrefsAccessError)
        self.assertEqual(calls, 1)
        slept.assert_not_called()

    def test_a_bad_request_is_not_retried(self) -> None:
        """400 "bad date" returns the same thing every time."""
        out, calls, _ = self._get([self._response(400)])
        self.assertIsInstance(out, AhrefsAccessError)
        self.assertEqual(calls, 1)


class ReportShareLinkTests(unittest.TestCase):
    """The public /r/<token> page: opt-in, revocable, and 404 for anything else."""

    def setUp(self) -> None:
        self.session = _make_session()
        self.client = _client(self.session, name="Acme Co", domain="acme.com")

    def _report(self):
        return report_service.save_report(
            self.session,
            client_id=self.client.id,
            period_label="Jun 2026",
            blocks=[{"block_type_key": "intro_header", "status": "ok", "data": {}, "comment": ""}],
            generated_by=uuid.uuid4(),
        )

    def test_a_new_report_is_not_shared(self) -> None:
        report = self._report()
        self.assertIsNone(report.share_token)
        self.assertIsNone(report_service.serialize_report_summary(report)["share_token"])

    def test_sharing_mints_a_token_and_finds_the_report_by_it(self) -> None:
        report = self._report()
        token = report_service.set_report_share(
            self.session, report_id=report.id, shared=True
        )
        self.assertTrue(token)
        self.assertGreater(len(token), 30)
        found, blocks = report_service.get_shared_report(self.session, token)
        self.assertEqual(found.id, report.id)
        self.assertEqual(len(blocks), 1)

    def test_sharing_twice_keeps_the_link_already_sent_working(self) -> None:
        report = self._report()
        first = report_service.set_report_share(self.session, report_id=report.id, shared=True)
        second = report_service.set_report_share(self.session, report_id=report.id, shared=True)
        self.assertEqual(first, second)

    def test_revoking_kills_the_link_and_a_new_one_differs(self) -> None:
        report = self._report()
        first = report_service.set_report_share(self.session, report_id=report.id, shared=True)
        self.assertIsNone(
            report_service.set_report_share(self.session, report_id=report.id, shared=False)
        )
        with self.assertRaises(LookupError):
            report_service.get_shared_report(self.session, first)
        again = report_service.set_report_share(self.session, report_id=report.id, shared=True)
        self.assertNotEqual(again, first)

    def test_an_unknown_or_empty_token_is_a_lookup_error(self) -> None:
        for token in ("", "   ", "not-a-real-token"):
            with self.assertRaises(LookupError):
                report_service.get_shared_report(self.session, token)

    def test_revoking_clears_the_cached_report_so_the_link_dies_at_once(self) -> None:
        report = self._report()
        token = report_service.set_report_share(self.session, report_id=report.id, shared=True)
        report_service.get_shared_report(self.session, token)  # warms the cache
        report_service.set_report_share(self.session, report_id=report.id, shared=False)
        with self.assertRaises(LookupError):
            report_service.get_shared_report(self.session, token)

    def test_sharing_an_unknown_report_is_a_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            report_service.set_report_share(self.session, report_id=uuid.uuid4(), shared=True)


class ExportFilenameTests(unittest.TestCase):
    """The download name goes into a Content-Disposition header, which is
    latin-1 and delimited by quotes — so nothing else may reach it."""

    def test_quotes_and_newlines_cannot_reach_the_header(self) -> None:
        safe = api_routes._filename_safe('Acme "Co"\r\nX-Injected: yes-Jun 2026-report')
        self.assertNotIn('"', safe)
        self.assertNotIn("\r", safe)
        self.assertNotIn("\n", safe)

    def test_a_cyrillic_client_name_does_not_500_the_export(self) -> None:
        safe = api_routes._filename_safe("Клієнт-Jun 2026-report")
        safe.encode("latin-1")  # what Starlette does with a header value
        self.assertIn("Jun", safe)

    def test_a_name_made_only_of_punctuation_still_has_something_left(self) -> None:
        self.assertEqual(api_routes._filename_safe("!!! ???"), "report")


class ApiCacheTransactionTests(unittest.TestCase):
    """Storing a pull must not commit whatever else the caller had open."""

    def setUp(self) -> None:
        self.session = _make_session()
        self.client = _client(self.session, name="Acme Co", domain="acme.com")

    def test_the_callers_unfinished_work_is_left_alone(self) -> None:
        self.client.name = "Half-typed name"  # dirty, not committed
        api_cache.get_or_fetch(self.session, "k1", lambda: {"v": 1})
        self.session.rollback()
        self.assertEqual(self.client.name, "Acme Co")

    def test_a_pull_on_a_clean_session_is_committed_on_its_own(self) -> None:
        api_cache.get_or_fetch(self.session, "k2", lambda: {"v": 2})
        self.session.rollback()  # would drop the row if it had not been committed
        self.assertIsNotNone(self.session.get(ApiCache, "k2"))

    def test_a_failing_pull_stores_nothing(self) -> None:
        def boom() -> dict:
            raise RuntimeError("rate limited")

        with self.assertRaises(RuntimeError):
            api_cache.get_or_fetch(self.session, "k3", boom)
        self.assertIsNone(self.session.get(ApiCache, "k3"))


if __name__ == "__main__":
    unittest.main()
