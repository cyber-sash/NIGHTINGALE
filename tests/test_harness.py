"""Smoke tests that run offline — no network, no scraping.

These lock the harness's invariants: taxonomy mapping, ToysReloved
products.json parsing, and report rendering. Run with `python -m pytest`
or `python -m unittest tests.test_harness`.
"""

from __future__ import annotations

import json
import unittest

from nightingale.collect import ToysRelovedCollector
from nightingale.report import render
from nightingale.taxonomy import CANONICAL_CATEGORIES, normalize_category


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
