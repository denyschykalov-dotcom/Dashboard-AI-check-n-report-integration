import os
import unittest
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy.orm import sessionmaker

from backend.app.db import Base, build_engine
from backend.app.models import Client, Report, ReportBlock, Run, RunResult
from backend.app.report_builder import export as report_export
from backend.app.report_builder import secrets_crypto
from backend.app.report_builder import service as report_service
from backend.app.report_builder import settings_service
from backend.app.report_builder.block_catalog import BLOCK_CATALOG, get_block
from backend.app.report_builder.data_sources import ahrefs, ai_visibility, clickup, ga4, gsc, static_editorial
from backend.app.report_builder.data_sources.ahrefs_client import AhrefsAccessError, resolve_report_dates
from backend.app.report_builder.data_sources.clickup_client import ClickUpAccessError, find_client_list
from backend.app.report_builder.data_sources.base import ResolveContext
from backend.app.report_builder.data_sources.sheets_client import (
    SheetsAccessError,
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
        self.assertEqual(len(BLOCK_CATALOG), 24)
        keys = [block.key for block in BLOCK_CATALOG]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ai_visibility_blocks_carry_window_and_model(self) -> None:
        ai_blocks = [block for block in BLOCK_CATALOG if block.source == "ai_visibility"]
        self.assertEqual(len(ai_blocks), 8)
        for block in ai_blocks:
            self.assertIn(block.ai_visibility_window, {"last_month", "last_6_months"})
            self.assertIn(block.ai_visibility_model, {"all", "gpt", "gemini", "grok"})

    def test_bar_variants_exist_for_donut_blocks(self) -> None:
        self.assertIsNotNone(get_block("ga4_session_mix_bar"))
        self.assertIsNotNone(get_block("gsc_branded_bar"))


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
            ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts"],
            ["Jun 2026", "6058", "22724460.05", "36610", "18268"],
            ["May 2026", "8789", "29735694.35", "53226", "23902"],
            ["Jun 2025", "4269", "11646456.71", "28324", "13994"],
        ],
        "GA4 Ecommerce Organic": [
            ["Period", "Purchases", "Revenue", "Add to Carts", "Checkouts", "Channel"],
            ["Jun 2026", "107", "468354.0498", "1239", "449", "Organic Search"],
            ["May 2026", "171", "613939.9701", "1612", "596", "Organic Search"],
            ["Jun 2025", "201", "761002.3602", "1818", "772", "Organic Search"],
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

    def test_ai_traffic_includes_summary_tools_and_top_pages(self) -> None:
        with _patched_ga4_sheet():
            result = ga4.resolve(get_block("ga4_ai_traffic"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["summary"]["current"]["total_ai_sessions"], 1057)
        self.assertEqual(result.data["tools"][0]["source"], "chatgpt.com")
        self.assertEqual(result.data["top_pages"][0]["page"], "/")

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

    def test_top_movers_returns_gainers_and_losers(self) -> None:
        with patch("backend.app.report_builder.data_sources.ahrefs.ahrefs_client.get", side_effect=self._fake_get):
            result = ahrefs.resolve(get_block("ahrefs_top_movers"), self._context())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["gainers"][0]["url"], "https://onebyone.ua/sukni")
        self.assertEqual(result.data["gainers"][0]["position_prev"], 5)
        self.assertIn("losers", result.data)

    def test_unavailable_without_domain(self) -> None:
        client = _client(self.session, name="No Domain", domain="")
        context = ResolveContext(client=client, period_label="", now=utcnow(), session=self.session)
        result = ahrefs.resolve(get_block("ahrefs_domain_analysis"), context)
        self.assertEqual(result.status, "unavailable")

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


class ServiceGenerateWithSheetsTests(unittest.TestCase):
    def test_generate_prefers_sheet_period_over_wallclock_default(self) -> None:
        session = _make_session()
        client = _client(session, name="Acme Co", ga4_sheet_id="sheet-123")
        with _patched_ga4_sheet():
            result = report_service.generate(session, client_id=client.id, block_keys=["ga4_summary"])
        self.assertEqual(result["period_label"], "Jun 2026")


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
    def test_export_html_is_self_contained_and_includes_data_and_comment(self) -> None:
        session = _make_session()
        client = _client(session)
        report = report_service.save_report(
            session,
            client_id=client.id,
            period_label="2026-06",
            blocks=[
                {"block_type_key": "intro_header", "status": "ok", "data": {"headline": "Great month"}, "comment": "Client is happy"},
            ],
            generated_by=uuid.uuid4(),
        )
        report_row, blocks = report_service.get_report(session, report.id)
        html_doc = report_export.build_report_html(
            report_row, blocks, client_name="Acme Co", client_domain="acme.com"
        )
        self.assertIn("<!doctype html>", html_doc)
        self.assertIn("Great month", html_doc)
        self.assertIn("Client is happy", html_doc)
        self.assertIn("Acme Co", html_doc)
        # self-contained: no external resource references
        self.assertNotIn("http://", html_doc)
        self.assertNotIn("https://", html_doc)


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


def _clickup_tasks_fixture():
    return [
        {"name": "Publish blog post", "url": "https://app.clickup.com/t/1",
         "status": {"status": "done", "type": "done"}, "date_done": "1750000000000",
         "assignees": [{"username": "Denys"}]},
        {"name": "Fix meta tags", "url": "https://app.clickup.com/t/2",
         "status": {"status": "complete", "type": "closed"}, "date_done": "1750100000000", "assignees": []},
        {"name": "Build backlinks", "url": "https://app.clickup.com/t/3",
         "status": {"status": "todo", "type": "open"}, "due_date": "1752000000000", "assignees": []},
        {"name": "Keyword research", "url": "https://app.clickup.com/t/4",
         "status": {"status": "doing", "type": "custom"}, "assignees": [{"username": "Bohdan"}]},
    ]


class ClickUpResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _make_session()
        self.user_id = uuid.uuid4()
        self.client = _client(self.session, name="Onebyone", domain="onebyone.ua")

    def _context(self) -> ResolveContext:
        return ResolveContext(
            client=self.client, period_label="", now=utcnow(),
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

    def test_work_completed_and_planned_split_by_status_type(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        context = self._context()
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            return_value={"id": "list-1", "name": "onebyone (30)"},
        ), patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.fetch_tasks",
            return_value=_clickup_tasks_fixture(),
        ) as mocked_fetch:
            completed = clickup.resolve(get_block("work_completed"), context)
            planned = clickup.resolve(get_block("planned_works"), context)

        self.assertEqual(completed.status, "ok")
        self.assertEqual(completed.data["count"], 2)
        self.assertEqual({t["name"] for t in completed.data["tasks"]}, {"Publish blog post", "Fix meta tags"})
        self.assertEqual(completed.data["tasks"][0]["date_done"], "2025-06-15")

        self.assertEqual(planned.status, "ok")
        self.assertEqual(planned.data["count"], 2)
        self.assertEqual({t["name"] for t in planned.data["tasks"]}, {"Build backlinks", "Keyword research"})

        # both blocks share a single tasks fetch via the context cache
        mocked_fetch.assert_called_once()

    def test_api_error_becomes_unavailable(self) -> None:
        settings_service.set_clickup_token(self.session, self.user_id, "pk_x")
        with patch(
            "backend.app.report_builder.data_sources.clickup.clickup_client.find_client_list",
            side_effect=ClickUpAccessError("ClickUp token is invalid or expired (401)."),
        ):
            result = clickup.resolve(get_block("work_completed"), self._context())
        self.assertEqual(result.status, "unavailable")
        self.assertIn("401", result.unavailable_reason)


if __name__ == "__main__":
    unittest.main()
