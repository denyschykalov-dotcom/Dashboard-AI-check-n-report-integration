"""Report-language tests: the catalog, the cache, and the lookup.

All pure — the translating callable is injected, so nothing here touches Claude.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.report_builder import localization as L
from backend.app.report_builder.block_catalog import BLOCK_CATALOG, RETIRED_BLOCK_KEYS
from backend.app.report_builder.export import SECTION_BY_KEY


class LanguageNormalizationTests(unittest.TestCase):
    def test_supported_codes_pass_through(self) -> None:
        self.assertEqual(L.normalize_language("en"), "en")
        self.assertEqual(L.normalize_language("uk"), "uk")

    def test_case_and_region_tags_are_tolerated(self) -> None:
        self.assertEqual(L.normalize_language("UK"), "uk")
        self.assertEqual(L.normalize_language("uk-UA"), "uk")
        self.assertEqual(L.normalize_language("uk_UA"), "uk")
        self.assertEqual(L.normalize_language(" En "), "en")

    def test_unknown_and_empty_fall_back_to_english(self) -> None:
        # A report in the wrong language is a nuisance; one that fails to render
        # is an outage — so unknown input degrades rather than raising.
        for value in ("fr", "klingon", "", "   ", None):
            self.assertEqual(L.normalize_language(value), "en")

    def test_english_never_needs_translation(self) -> None:
        self.assertFalse(L.needs_translation("en"))
        self.assertFalse(L.needs_translation("bogus"))
        self.assertTrue(L.needs_translation("uk"))

    def test_language_name(self) -> None:
        self.assertEqual(L.language_name("uk"), "Ukrainian")
        self.assertEqual(L.language_name("en"), "English")
        self.assertEqual(L.language_name("nope"), "English")


class _CacheTestCase(unittest.TestCase):
    """Isolates the on-disk cache so tests never touch the real translations."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(L, "_CACHE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        L._memo.clear()
        self.addCleanup(L._memo.clear)


class TranslatorTests(_CacheTestCase):
    def test_english_translator_is_identity(self) -> None:
        t = L.translator("en")
        self.assertEqual(t("Clicks"), "Clicks")

    def test_missing_translation_falls_back_to_english(self) -> None:
        L._store_ui_translations("uk", {"Clicks": "Кліки"})
        t = L.translator("uk")
        self.assertEqual(t("Clicks"), "Кліки")
        self.assertEqual(t("Impressions"), "Impressions")
        self.assertEqual(t(""), "")

    def test_surrounding_whitespace_is_preserved(self) -> None:
        """Labels arrive padded from markup; the padding must survive the swap.

        Composite forms ("Top events —", "<Label> — <period>") are the template
        pass's job — server-side lookup is exact-key plus whitespace, and the
        dash-suffixed captions are catalog entries in their own right.
        """
        L._store_ui_translations("uk", {"Top events": "Топ події"})
        t = L.translator("uk")
        self.assertEqual(t("  Top events  "), "  Топ події  ")
        self.assertEqual(t("\n Top events\n"), "\n Топ події\n")
        self.assertIn("Top events —", L.UI_STRINGS)

    def test_period_label_translates_months_and_keeps_digits(self) -> None:
        L._store_ui_translations("uk", {"Jun": "Чер", "July": "Липень"})
        self.assertEqual(L.localize_period_label("Jun 2026", "uk"), "Чер 2026")
        self.assertEqual(L.localize_period_label("July 2026", "uk"), "Липень 2026")
        # English is a no-op, and an untranslated month is left alone.
        self.assertEqual(L.localize_period_label("Jun 2026", "en"), "Jun 2026")
        self.assertEqual(L.localize_period_label("Mar 2026", "uk"), "Mar 2026")


class CatalogCacheTests(_CacheTestCase):
    def test_missing_reports_the_whole_catalog_when_cache_is_empty(self) -> None:
        self.assertEqual(len(L.missing_ui_strings("uk")), len(set(L.UI_STRINGS)))
        # English has no catalog gap because it needs no translation.
        self.assertEqual(L.missing_ui_strings("en"), [])

    def test_ensure_only_requests_the_gap(self) -> None:
        L._store_ui_translations("uk", {"Clicks": "Кліки"})
        seen: list[list[str]] = []

        def fake_translate(texts, language):
            seen.append(list(texts))
            return {text: f"<{text}>" for text in texts}

        merged = L.ensure_ui_translations("uk", fake_translate)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("Clicks", seen[0])          # already cached
        self.assertIn("Impressions", seen[0])
        self.assertEqual(merged["Clicks"], "Кліки")  # pre-existing entry survives
        self.assertEqual(merged["Impressions"], "<Impressions>")

    def test_ensure_is_a_no_op_once_complete(self) -> None:
        calls = []

        def fake_translate(texts, language):
            calls.append(len(texts))
            return {text: f"<{text}>" for text in texts}

        L.ensure_ui_translations("uk", fake_translate)
        L.ensure_ui_translations("uk", fake_translate)
        self.assertEqual(len(calls), 1, "second call should be served from cache")
        self.assertEqual(L.missing_ui_strings("uk"), [])

    def test_ensure_persists_to_disk(self) -> None:
        L.ensure_ui_translations("uk", lambda texts, lang: {t: f"<{t}>" for t in texts})
        payload = json.loads((Path(self._tmp.name) / "uk.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["Clicks"], "<Clicks>")

    def test_translation_failure_leaves_the_report_in_english(self) -> None:
        def boom(texts, language):
            raise RuntimeError("Claude is down")

        # Must not propagate: labels are cosmetic next to shipping the report.
        self.assertEqual(L.ensure_ui_translations("uk", boom), {})
        self.assertEqual(L.translator("uk")("Clicks"), "Clicks")

    def test_blank_translations_are_discarded(self) -> None:
        L.ensure_ui_translations(
            "uk", lambda texts, lang: {t: ("" if t == "Clicks" else f"<{t}>") for t in texts}
        )
        t = L.translator("uk")
        self.assertEqual(t("Clicks"), "Clicks", "a blank translation must fall back")
        self.assertEqual(t("Impressions"), "<Impressions>")

    def test_english_never_writes_a_cache(self) -> None:
        L.ensure_ui_translations("en", lambda texts, lang: {t: "x" for t in texts})
        self.assertEqual(list(Path(self._tmp.name).glob("*.json")), [])


class CatalogCoverageTests(unittest.TestCase):
    def test_every_selectable_block_name_is_translatable(self) -> None:
        """A block a client can see must have its section title in the catalog.

        Guards the gap that made the bar-chart variants invisible: a block whose
        title never reaches the vocabulary would render in English forever.
        """
        catalog = set(L.UI_STRINGS)
        for block in BLOCK_CATALOG:
            if block.source == "ai_visibility":
                continue  # these share one section, titled "AI Visibility"
            if block.key in ("intro_header",):
                continue  # the hero has its own copy, not a section title
            self.assertIn(
                block.display_name,
                catalog,
                f"{block.key}: '{block.display_name}' is missing from UI_STRINGS",
            )

    def test_retired_blocks_are_not_selectable(self) -> None:
        offered = {block.key for block in BLOCK_CATALOG}
        self.assertFalse(offered & RETIRED_BLOCK_KEYS)
        for key in RETIRED_BLOCK_KEYS:
            self.assertNotIn(key, SECTION_BY_KEY)

    def test_catalog_has_no_blank_entries(self) -> None:
        for text in L.UI_STRINGS:
            self.assertTrue(text.strip(), "UI_STRINGS must not contain blanks")


if __name__ == "__main__":
    unittest.main()
