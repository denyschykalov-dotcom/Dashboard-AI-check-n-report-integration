"""The collector's site list: who it lists, and who may read it.

The endpoint exists so the Apps Script collector stops carrying its own copy of
which clients to pull and what to pull from. It is token-authenticated rather
than session-authenticated because the collector runs on a monthly trigger with
no logged-in user — which makes the token check a trust boundary worth testing
directly.
"""

from __future__ import annotations

import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base, build_engine, get_db_session
from backend.app.main import app
from backend.app.models import Client
from backend.app.report_builder import service as report_service

TOKEN = "collector-secret"
ENDPOINT = "/api/report-builder/collector-sites"


class _FakeSettings:
    def __init__(self, token, folder_id="folder-1"):
        self.collector_token = token
        self.google_sheets_client_folder_id = folder_id


class CollectorSitesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = build_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine, expire_on_commit=False)()

        self.configured = Client(
            name="Yamaha", domain="yamahaonlineparts.com",
            ga4_property_id="509009564", gsc_property=None, created_by=uuid.uuid4(),
        )
        self.unconfigured = Client(
            name="No Property", domain="noproperty.com", created_by=uuid.uuid4(),
        )
        self.session.add_all([self.configured, self.unconfigured])
        self.session.commit()

        app.dependency_overrides[get_db_session] = lambda: self.session
        self.addCleanup(app.dependency_overrides.clear)
        self.http = TestClient(app)

    def _get(self, token=TOKEN, configured_token=TOKEN):
        headers = {"X-Collector-Token": token} if token is not None else {}
        settings = _FakeSettings(configured_token)
        with patch("backend.app.auth.get_settings", return_value=settings), \
             patch("backend.app.api.routes.get_settings", return_value=settings):
            return self.http.get(ENDPOINT, headers=headers)

    def test_valid_token_returns_sites_and_the_folder(self) -> None:
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["folder_id"], "folder-1")
        by_domain = {site["domain"]: site for site in body["sites"]}
        self.assertEqual(
            by_domain["yamahaonlineparts.com"]["ga4_property_id"], "509009564"
        )
        # None, not "", so the collector knows to probe for the working form.
        self.assertIsNone(by_domain["yamahaonlineparts.com"]["gsc_property"])
        self.assertTrue(by_domain["yamahaonlineparts.com"]["collect"])

    def test_unconfigured_client_is_listed_with_a_reason_not_omitted(self) -> None:
        # Silently dropping it would hide the misconfiguration; the collector
        # logs this reason so an unset property id is visible in the run log.
        sites = {site["domain"]: site for site in self._get().json()["sites"]}
        entry = sites["noproperty.com"]
        self.assertFalse(entry["collect"])
        self.assertIn("GA4 property", entry["skip_reason"])

    def test_wrong_token_is_rejected(self) -> None:
        self.assertEqual(self._get(token="wrong").status_code, 401)

    def test_missing_token_is_rejected(self) -> None:
        self.assertEqual(self._get(token=None).status_code, 401)

    def test_endpoint_refuses_when_no_token_is_configured(self) -> None:
        # Fails closed: the list names every client's GA4 and Search Console
        # properties, so an unset backend token must not mean "open to anyone".
        response = self._get(token="anything", configured_token="")
        self.assertEqual(response.status_code, 503)


class Ga4PropertyIdParsingTests(unittest.TestCase):
    """Specialists paste whatever GA4 shows them; the collector needs digits."""

    def test_accepted_forms_all_reduce_to_the_bare_id(self) -> None:
        for raw in [
            "509009564",
            "  509009564  ",
            "properties/509009564",
            "https://analytics.google.com/analytics/web/#/p509009564/reports",
            "https://analytics.google.com/analytics/web/?p=509009564",
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(report_service._extract_ga4_property_id(raw), "509009564")

    def test_empty_stays_empty_so_the_field_can_be_cleared(self) -> None:
        self.assertEqual(report_service._extract_ga4_property_id(""), "")
        self.assertEqual(report_service._extract_ga4_property_id("   "), "")


if __name__ == "__main__":
    unittest.main()
