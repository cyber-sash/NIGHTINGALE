"""Smoke tests that run offline — no network, no scraping.

These lock the harness's invariants: taxonomy mapping, ToysReloved
products.json parsing, and report rendering. Run with `python -m pytest`
or `python -m unittest tests.test_harness`.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("NIGHTINGALE_JITTER", "0")

from nightingale.collect import (  # noqa: E402 — env flag set first
    StuffleCollector,
    ToysRelovedCollector,
)
from nightingale.report import render  # noqa: E402
from nightingale.taxonomy import CANONICAL_CATEGORIES, normalize_category  # noqa: E402


class TaxonomyTests(unittest.TestCase):
    def test_exact_label_maps(self) -> None:
        mapping = {"Puppen": "Dolls & Soft Toys"}
        self.assertEqual(normalize_category("Puppen", mapping), "Dolls & Soft Toys")

    def test_substring_fallback(self) -> None:
        mapping = {"LEGO": "LEGO & Building Sets"}
        self.assertEqual(
            normalize_category("LEGO Technic 42100", mapping),
            "LEGO & Building Sets",
        )

    def test_unknown_label_is_other(self) -> None:
        self.assertEqual(
            normalize_category("quantum plushie", {"Puppen": "Dolls & Soft Toys"}),
            "Other / Uncategorized",
        )


class ConfigDrivenUrlTests(unittest.TestCase):
    def test_site_config_overrides_default_urls(self) -> None:
        collector = StuffleCollector(
            site_config={
                "toys_url": "https://stuffle.com/override",
                "fallback_urls": ["https://stuffle.com/fallback"],
                "category_map": {},
            },
            cache_dir=None,
        )
        self.assertEqual(collector.toys_url, "https://stuffle.com/override")
        self.assertEqual(collector.fallback_urls, ["https://stuffle.com/fallback"])

    def test_defaults_apply_when_config_omits_urls(self) -> None:
        collector = StuffleCollector(site_config={}, cache_dir=None)
        self.assertTrue(collector.toys_url.startswith("https://stuffle.com/"))
        self.assertTrue(len(collector.fallback_urls) >= 1)


class FallbackUrlRotationTests(unittest.TestCase):
    def test_first_working_url_wins(self) -> None:
        collector = StuffleCollector(
            site_config={
                "toys_url": "https://primary.invalid/",
                "fallback_urls": [
                    "https://secondary.invalid/",
                    "https://tertiary.invalid/",
                ],
                "category_map": {},
            },
            cache_dir=None,
        )
        calls: list[str] = []

        def fake_fetch(url: str) -> str:
            calls.append(url)
            if url == "https://secondary.invalid/":
                return "<html>123 Artikel</html>"
            raise urllib_error_http(403, url)

        with patch.object(collector, "_fetch", side_effect=fake_fetch):
            body, winner = collector._fetch_first_working(
                [collector.toys_url, *collector.fallback_urls]
            )
        self.assertEqual(winner, "https://secondary.invalid/")
        self.assertEqual(len(calls), 2)  # primary failed, secondary won


def urllib_error_http(code: int, url: str):
    import urllib.error

    return urllib.error.HTTPError(url, code, "blocked", {}, None)


class ProductsJsonParseTests(unittest.TestCase):
    def test_parses_shopify_products_json_offline(self) -> None:
        body = json.dumps(
            {
                "products": [
                    {
                        "id": 1,
                        "title": "LEGO City 60380",
                        "handle": "lego-city",
                        "product_type": "LEGO",
                        "vendor": "LEGO",
                        "tags": "good condition, wie neu",
                        "variants": [{"price": "34.90"}],
                    },
                    {
                        "id": 2,
                        "title": "Playmobil knight",
                        "handle": "playmobil",
                        "product_type": "Figuren",
                        "vendor": "Playmobil",
                        "tags": ["gut"],
                        "variants": [{"price": "8.00"}],
                    },
                ]
            }
        )
        collector = ToysRelovedCollector(site_config={}, cache_dir=None)  # type: ignore[arg-type]
        parsed = collector._parse_products_json(body)
        self.assertEqual(parsed["total_listings"], 2)
        self.assertEqual(parsed["categories"].get("LEGO"), 1)
        self.assertEqual(parsed["categories"].get("Figuren"), 1)
        brand_names = {b["name"] for b in parsed["brands_top10"]}
        self.assertIn("LEGO", brand_names)
        self.assertIn("Playmobil", brand_names)
        self.assertEqual(parsed["price_buckets_eur"]["25-50"], 1)
        self.assertEqual(parsed["price_buckets_eur"]["0-10"], 1)


class ShopifyPaginationTests(unittest.TestCase):
    def test_paginates_until_short_page(self) -> None:
        collector = ToysRelovedCollector(
            site_config={
                "toys_url": "https://toysreloved.de/products.json?limit=250&page=1",
                "category_map": {"LEGO": "LEGO & Building Sets"},
            },
            cache_dir=None,
        )

        def product(i: int) -> dict:
            return {
                "id": i,
                "title": f"Toy {i}",
                "handle": f"toy-{i}",
                "product_type": "LEGO",
                "vendor": "LEGO",
                "tags": "good",
                "variants": [{"price": "12.00"}],
            }

        # page 1: 250 items (full), page 2: 7 items (short ⇒ stop)
        pages = {
            1: json.dumps({"products": [product(i) for i in range(250)]}),
            2: json.dumps({"products": [product(i) for i in range(250, 257)]}),
        }
        captured: list[str] = []

        def fake_fetch(url: str) -> str:
            captured.append(url)
            page = 1
            if "page=2" in url:
                page = 2
            return pages[page]

        with patch.object(collector, "_fetch", side_effect=fake_fetch):
            record = collector.collect("2026-04-15")

        self.assertEqual(record.status, "ok")
        self.assertEqual(record.total_listings, 257)
        self.assertEqual(len(captured), 2)  # stopped at short page
        # Aggregated brand counts
        lego = next((b for b in record.brands_top10 if b["name"] == "LEGO"), None)
        self.assertIsNotNone(lego)
        self.assertEqual(lego["count"], 257)


class ReportRenderTests(unittest.TestCase):
    def test_blocked_snapshot_renders_cleanly(self) -> None:
        snap = {
            "date": "2026-04-15",
            "sites": {
                "toysreloved": {
                    "site": "toysreloved",
                    "fetched_at": "2026-04-15T00:00:00+00:00",
                    "status": "blocked",
                    "total_listings": None,
                    "categories": {},
                    "error": "HTTP 403",
                },
                "stuffle": {"site": "stuffle", "status": "blocked"},
                "sellpy": {"site": "sellpy", "status": "blocked"},
                "tildi": {"site": "tildi", "status": "blocked"},
            },
        }
        md = render("2026-04-15", snap, previous=None)
        self.assertIn("Competitor Toy-Inventory Snapshot — 2026-04-15", md)
        self.assertIn("BLOCKED", md)
        for cat in CANONICAL_CATEGORIES:
            self.assertIn(cat, md)

    def test_delta_row_shows_day_over_day(self) -> None:
        snap = {
            "date": "2026-04-15",
            "sites": {
                "toysreloved": {
                    "status": "ok",
                    "total_listings": 1100,
                    "categories": {},
                },
                "stuffle": {"status": "ok", "total_listings": 500, "categories": {}},
                "sellpy": {"status": "blocked"},
                "tildi": {"status": "blocked"},
            },
        }
        prev = {
            "sites": {
                "toysreloved": {"total_listings": 1000},
                "stuffle": {"total_listings": 499},
            }
        }
        md = render("2026-04-15", snap, previous=prev)
        self.assertIn("+10.0%", md)       # toysreloved grew 10%
        self.assertNotIn("+0.2%", md.split("## Signals")[1])  # sub-5% not signalled


if __name__ == "__main__":
    unittest.main()
