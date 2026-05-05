"""HTTP collectors for each marketplace.

Design notes
------------
* Stdlib-only (urllib) so the harness runs in minimal cron environments.
* Each adapter returns a normalized `SnapshotRecord` dict. Failures are
  captured as `status="blocked"` or `status="error"` rather than raising —
  one bad site should not abort the whole daily run.
* Raw HTML is persisted under data/raw/<site>/<date>/ so an adapter can be
  re-parsed offline when selectors change without re-hitting the site.
* Adapters read per-site config (URLs, category-label mapping) from
  config/sites.json. Selectors live in code because they change under
  deploys and are easier to version in git.

Each adapter has a single extension point: `_parse(html)` → dict of
normalized toy metrics. The base class handles fetch, retries, caching,
and error capture.
"""

from __future__ import annotations

import gzip
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .taxonomy import CANONICAL_CATEGORIES, normalize_category

# Cloudflare / Akamai / DataDome uniformly 403 self-identifying bots, so
# the default UA is a real Chrome build. Override via NIGHTINGALE_UA for
# environments where a polite announce-yourself UA is preferred (and
# whitelisted by the target sites).
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
USER_AGENT = os.environ.get("NIGHTINGALE_UA", DEFAULT_UA)
DEFAULT_TIMEOUT = 20  # seconds
RETRY_BACKOFF = (2, 4, 8, 16)
# Tests set NIGHTINGALE_JITTER=0 to keep the suite fast.
_JITTER_DISABLED = os.environ.get("NIGHTINGALE_JITTER", "").strip() == "0"
JITTER_BETWEEN_PAGES = (0.0, 0.0) if _JITTER_DISABLED else (0.4, 1.2)


@dataclass
class SnapshotRecord:
    site: str
    fetched_at: str
    status: str  # "ok" | "blocked" | "error"
    total_listings: int | None = None
    categories: dict[str, int] = field(default_factory=dict)
    brands_top10: list[dict[str, Any]] = field(default_factory=list)
    price_buckets_eur: dict[str, int] = field(default_factory=dict)
    conditions: dict[str, int] = field(default_factory=dict)
    sample_listings: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    raw_cache_path: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _TextExtractor(HTMLParser):
    """Tiny helper to pull visible text from an HTML blob."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False
        self._title_chunks: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = True
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip():
            self._title_chunks.append(data.strip())
        if not self._skip and data.strip():
            self._chunks.append(data.strip())

    @property
    def text(self) -> str:
        return " ".join(self._chunks)

    @property
    def title(self) -> str:
        return " ".join(self._title_chunks)


def _extract_diagnostics(html: str, winning_url: str) -> dict[str, Any]:
    """Capture debugging info from an HTML response. Written into the
    snapshot JSON so we can debug parser misses without downloading raw HTML."""
    ext = _TextExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    text_preview = ext.text[:800] if ext.text else ""
    platform = "unknown"
    if "__NEXT_DATA__" in html:
        platform = "nextjs"
    elif "__NUXT__" in html or "nuxt" in html[:2000].lower():
        platform = "nuxt"
    elif "ng-app" in html or "ng-version" in html:
        platform = "angular"
    elif '"shopify"' in html.lower() or "cdn.shopify.com" in html:
        platform = "shopify"
    elif "wp-content" in html or "wordpress" in html.lower():
        platform = "wordpress"
    return {
        "winning_url": winning_url,
        "response_bytes": len(html.encode("utf-8")),
        "page_title": ext.title or None,
        "text_preview": text_preview or None,
        "detected_platform": platform,
    }


class BaseCollector:
    """Base class. Subclasses override `site_key` and `_parse`.

    URLs live in `config/sites.json` — `toys_url` is tried first, then
    each entry in `fallback_urls` until one responds 200. This lets an
    operator pivot to a sitemap fallback (or a new category slug) by
    editing config and redeploying, without touching Python.
    """

    site_key: str = ""
    default_toys_url: str = ""
    default_fallback_urls: tuple[str, ...] = ()

    def __init__(self, site_config: dict[str, Any], cache_dir: Path | None) -> None:
        self.site_config = site_config
        self.cache_dir = cache_dir
        self.category_mapping: dict[str, str] = site_config.get("category_map", {})
        self.toys_url: str = site_config.get("toys_url") or self.default_toys_url
        self.fallback_urls: list[str] = list(
            site_config.get("fallback_urls") or self.default_fallback_urls
        )

    # ---- HTTP -----------------------------------------------------------

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8,sv;q=0.7",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        last_exc: Exception | None = None
        for attempt, delay in enumerate((0,) + RETRY_BACKOFF):
            if delay:
                time.sleep(delay)
            try:
                with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return raw.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                # 403/429/5xx get retried; 404 does not.
                if e.code in (404, 410):
                    raise
                last_exc = e
            except (urllib.error.URLError, TimeoutError) as e:
                last_exc = e
        assert last_exc is not None
        raise last_exc

    def _cache_raw(self, html: str, date: str, suffix: str = "toys.html") -> Path | None:
        if self.cache_dir is None:
            return None
        path = self.cache_dir / self.site_key / date / suffix
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return path

    def _jitter(self) -> None:
        lo, hi = JITTER_BETWEEN_PAGES
        time.sleep(random.uniform(lo, hi))

    def _fetch_first_working(self, urls: list[str]) -> tuple[str, str]:
        """Try urls in order. Return (body, winning_url). Re-raise the
        last exception if none succeed."""
        last_exc: Exception | None = None
        for i, url in enumerate(urls):
            if i > 0:
                self._jitter()
            try:
                return self._fetch(url), url
            except Exception as e:  # noqa: BLE001 — we try every fallback
                last_exc = e
        assert last_exc is not None
        raise last_exc

    # ---- Public entry point --------------------------------------------

    def collect(self, date: str) -> SnapshotRecord:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        urls = [self.toys_url, *self.fallback_urls]
        try:
            html, winning_url = self._fetch_first_working(urls)
        except urllib.error.HTTPError as e:
            status = "blocked" if e.code in (401, 403, 429) else "error"
            return SnapshotRecord(
                site=self.site_key,
                fetched_at=now,
                status=status,
                error=f"HTTP {e.code}; tried {len(urls)} URL(s), last: {urls[-1]}",
            )
        except Exception as e:  # network, TLS, parse-upstream
            return SnapshotRecord(
                site=self.site_key,
                fetched_at=now,
                status="error",
                error=f"{type(e).__name__}: {e}; tried {len(urls)} URL(s)",
            )

        cache_path = self._cache_raw(html, date)
        diag = _extract_diagnostics(html, winning_url)
        try:
            parsed = self._parse(html)
        except Exception as e:
            return SnapshotRecord(
                site=self.site_key,
                fetched_at=now,
                status="error",
                error=f"parse failure: {type(e).__name__}: {e}",
                raw_cache_path=str(cache_path),
                diagnostics=diag,
            )

        # Normalize category labels against canonical taxonomy.
        raw_cats: dict[str, int] = parsed.get("categories", {})
        canonical_cats: dict[str, int] = {c: 0 for c in CANONICAL_CATEGORIES}
        for label, count in raw_cats.items():
            canonical_cats[normalize_category(label, self.category_mapping)] += count

        return SnapshotRecord(
            site=self.site_key,
            fetched_at=now,
            status="ok",
            total_listings=parsed.get("total_listings"),
            categories={k: v for k, v in canonical_cats.items() if v},
            brands_top10=parsed.get("brands_top10", []),
            price_buckets_eur=parsed.get("price_buckets_eur", {}),
            conditions=parsed.get("conditions", {}),
            sample_listings=parsed.get("sample_listings", []),
            raw_cache_path=str(cache_path),
            diagnostics=diag,
        )

    # ---- Extension point ------------------------------------------------

    def _parse(self, html: str) -> dict[str, Any]:
        """Return dict with keys: total_listings, categories (raw labels),
        brands_top10, price_buckets_eur, conditions, sample_listings."""
        raise NotImplementedError


# ---- Shared parse helpers -----------------------------------------------


_COUNT_PATTERNS = (
    # Visible text: "12,345 Artikel", "12.345 Treffer", "12345 items"
    re.compile(r"([\d.,]{2,})\s*(?:Artikel|Treffer|Angebote|Produkte|Ergebnisse|items?|results?|listings?|annonce[rn]?|objets?|produkter)", re.I),
    # JSON keys in embedded state (various naming conventions)
    re.compile(r"\"(?:totalCount|total_count|totalResults|total_results|total_items|totalItems|nbHits|hits|numFound|count|itemCount|numberOfItems|totalHits|total)\"\s*:\s*(\d+)"),
    # data-* attributes
    re.compile(r"data-(?:total|count|results|items|hits)(?:-count|-results)?=\"(\d+)\""),
    # Parenthesised count in a heading/nav like "Toys (1 234)"
    re.compile(r"(?:Toys?|Spielzeug|Leksaker|Jouets?)\s*\(\s*([\d\s.,]+)\s*\)", re.I),
    # "Showing N of M" or "N results" patterns
    re.compile(r"(?:showing|visar|zeige)\s+\d+\s+(?:of|von|av)\s+([\d.,]+)", re.I),
    # meta content: <meta name="..." content="... 1234 items ...">
    re.compile(r'content="[^"]*?([\d.,]{2,})\s*(?:items?|products?|results?|Produkte|Angebote)[^"]*"', re.I),
    # og:title or page title with count
    re.compile(r"<title>[^<]*?([\d.,]{2,})\s*(?:items?|products?|results?|Produkte|Angebote)[^<]*</title>", re.I),
)


def _first_int(html: str, patterns: tuple = _COUNT_PATTERNS) -> int | None:
    for pat in patterns:
        m = pat.search(html)
        if m:
            raw = re.sub(r"[\s.,]", "", m.group(1))
            if raw.isdigit() and int(raw) > 0:
                return int(raw)
    return None


def _deep_search_json(html: str, keys: set[str]) -> int | None:
    """Search ALL <script> tags for JSON blobs containing any of `keys`.
    Returns the first integer value found for a matching key."""
    for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        blob = m.group(1).strip()
        if not blob or blob[0] not in ("{", "["):
            continue
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, RecursionError):
            continue
        result = _walk_for_keys(obj, keys, depth=0)
        if result is not None:
            return result
    return None


def _walk_for_keys(obj: Any, keys: set[str], depth: int) -> int | None:
    if depth > 12:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (int, float)) and v > 0:
                return int(v)
            found = _walk_for_keys(v, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj[:50]:
            found = _walk_for_keys(item, keys, depth + 1)
            if found is not None:
                return found
    return None


# =========================================================================
# Site adapters
# =========================================================================


class StuffleCollector(BaseCollector):
    """stuffle.com — German mobile-first C2C marketplace."""

    site_key = "stuffle"
    default_toys_url = "https://stuffle.com/search?category=toys"
    default_fallback_urls = (
        "https://stuffle.com/c/spielzeug",
        "https://stuffle.com/suche?kategorie=spielzeug",
        "https://stuffle.com/sitemap.xml",
    )

    _TOTAL_KEYS = {"totalCount", "total_count", "totalResults", "nbHits", "total", "count", "hits", "numFound"}

    def _parse(self, html: str) -> dict[str, Any]:
        total = _first_int(html)
        cats: dict[str, int] = {}

        if total is None:
            total = _deep_search_json(html, self._TOTAL_KEYS)

        # Extract subcategory facets from embedded JSON state or HTML
        for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
            blob = m.group(1).strip()
            if not blob or blob[0] not in ("{", "["):
                continue
            try:
                obj = json.loads(blob)
            except (json.JSONDecodeError, RecursionError):
                continue
            cats.update(_extract_facets_from_json(obj, "categor"))

        # HTML facets: <a ...>Label (123)</a> or <span class="count">123</span>
        for match in re.finditer(
            r'<(?:a|li|span)[^>]*>\s*([^<]{2,50}?)\s*\(\s*(\d+)\s*\)\s*</(?:a|li|span)>',
            html, re.I,
        ):
            label, count = match.group(1).strip(), int(match.group(2))
            if count > 0:
                cats[label] = count

        if total is None and cats:
            total = sum(cats.values())

        return {"total_listings": total, "categories": cats}


class SellpyCollector(BaseCollector):
    """sellpy.com — Swedish second-hand (H&M Group). Strong SPA; the HTML
    shell includes a JSON-LD `ItemList` and the public /api/v2 returns
    category aggregates.
    """

    site_key = "sellpy"
    default_toys_url = "https://www.sellpy.com/c/kids/toys"
    default_fallback_urls = (
        "https://www.sellpy.com/api/v2/search?categoryId=toys&pageSize=0",
        "https://www.sellpy.com/c/barn/leksaker",
        "https://www.sellpy.com/search?query=toys&category=kids",
    )

    _JSON_LD_RE = re.compile(
        r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I
    )
    _TOTAL_KEYS = {"totalCount", "total_count", "totalResults", "nbHits", "total", "count", "numberOfItems", "numFound", "itemCount", "totalHits"}

    def _parse(self, html: str) -> dict[str, Any]:
        # If the response is raw JSON (from the API fallback), parse directly.
        stripped = html.lstrip()
        if stripped and stripped[0] in ("{", "["):
            return self._parse_api_json(stripped)

        total = _first_int(html)
        cats: dict[str, int] = {}
        brands: list[dict[str, Any]] = []

        # Pull JSON-LD ItemList if present.
        for blob in self._JSON_LD_RE.findall(html):
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            candidates = obj if isinstance(obj, list) else [obj]
            for c in candidates:
                if isinstance(c, dict) and c.get("@type") == "ItemList":
                    if total is None and isinstance(c.get("numberOfItems"), int):
                        total = c["numberOfItems"]

        # Search ALL embedded JSON blobs for total count.
        if total is None:
            total = _deep_search_json(html, self._TOTAL_KEYS)

        # Facet counts are embedded as a JSON payload in __NEXT_DATA__.
        m = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                data = None
            if data:
                # Walk multiple possible paths — Sellpy's __NEXT_DATA__
                # structure varies across deploys.
                for path in (
                    ("props", "pageProps", "facets"),
                    ("props", "pageProps", "initialData", "facets"),
                    ("props", "pageProps", "searchResult", "facets"),
                    ("props", "pageProps", "data", "facets"),
                ):
                    facets = _dig(data, path)
                    if facets:
                        break
                else:
                    facets = {}

                if isinstance(facets, dict):
                    for facet in facets.get("categories", []) or []:
                        name = facet.get("name") or facet.get("label")
                        count = facet.get("count") or facet.get("docCount")
                        if name and isinstance(count, int):
                            cats[name] = count
                    for b in (facets.get("brands") or [])[:10]:
                        if b.get("name"):
                            brands.append({"name": b["name"], "count": b.get("count", 0)})

                # Also look for total in __NEXT_DATA__ directly.
                if total is None:
                    for tpath in (
                        ("props", "pageProps", "totalCount"),
                        ("props", "pageProps", "searchResult", "totalCount"),
                        ("props", "pageProps", "initialData", "totalCount"),
                        ("props", "pageProps", "data", "total"),
                    ):
                        val = _dig(data, tpath)
                        if isinstance(val, int) and val > 0:
                            total = val
                            break

        if total is None and cats:
            total = sum(cats.values())

        return {
            "total_listings": total,
            "categories": cats,
            "brands_top10": brands,
        }

    def _parse_api_json(self, body: str) -> dict[str, Any]:
        """Parse a raw JSON response from /api/v2/search."""
        data = json.loads(body)
        total = None
        cats: dict[str, int] = {}
        brands: list[dict[str, Any]] = []
        for key in ("totalCount", "total", "nbHits", "count", "totalResults"):
            if isinstance(data.get(key), int):
                total = data[key]
                break
        for facet in (data.get("facets", {}) or {}).get("categories", []) or []:
            name = facet.get("name") or facet.get("label")
            count = facet.get("count") or facet.get("docCount")
            if name and isinstance(count, int):
                cats[name] = count
        for b in ((data.get("facets", {}) or {}).get("brands") or [])[:10]:
            if b.get("name"):
                brands.append({"name": b["name"], "count": b.get("count", 0)})
        if total is None and cats:
            total = sum(cats.values())
        return {"total_listings": total, "categories": cats, "brands_top10": brands}


class TildiCollector(BaseCollector):
    """tildi.com — European kids' second-hand marketplace."""

    site_key = "tildi"
    default_toys_url = "https://tildi.com/de/kategorie/spielzeug"
    default_fallback_urls = (
        "https://tildi.com/de/spielzeug",
        "https://tildi.com/kategorie/spielzeug",
        "https://tildi.com/c/spielzeug",
        "https://tildi.com/toys",
        "https://tildi.com/sitemap.xml",
    )

    _TOTAL_KEYS = {"totalCount", "total_count", "totalResults", "nbHits", "total", "count", "hits", "numFound"}

    def _parse(self, html: str) -> dict[str, Any]:
        total = _first_int(html)
        cats: dict[str, int] = {}

        if total is None:
            total = _deep_search_json(html, self._TOTAL_KEYS)

        # Extract categories from embedded JSON or HTML facet links
        for m in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
            blob = m.group(1).strip()
            if not blob or blob[0] not in ("{", "["):
                continue
            try:
                obj = json.loads(blob)
            except (json.JSONDecodeError, RecursionError):
                continue
            cats.update(_extract_facets_from_json(obj, "categor"))

        for match in re.finditer(
            r'<(?:a|li|span)[^>]*>\s*([^<]{2,50}?)\s*\(\s*(\d+)\s*\)\s*</(?:a|li|span)>',
            html, re.I,
        ):
            label, count = match.group(1).strip(), int(match.group(2))
            if count > 0:
                cats[label] = count

        if total is None and cats:
            total = sum(cats.values())

        return {"total_listings": total, "categories": cats}


class ToysRelovedCollector(BaseCollector):
    """toysreloved.de — OUR site. Prefer Shopify `/products.json` if the
    store runs on Shopify (most reliable, no HTML parsing). Falls back to
    `/sitemap_products*.xml` or `/product-sitemap.xml` (Woo) otherwise.

    Overrides `collect()` to paginate through Shopify's 250-per-page
    products.json — otherwise we'd silently cap the total at 250.
    """

    site_key = "toysreloved"
    default_toys_url = "https://toysreloved.de/products.json?limit=250&page=1"
    default_fallback_urls = (
        "https://toysreloved.de/collections/all.json",
        "https://toysreloved.de/collections.json",
        "https://toysreloved.de/sitemap_products_1.xml",
        "https://toysreloved.de/product-sitemap.xml",
        "https://toysreloved.de/wp-json/wc/store/v1/products?per_page=1",
        "https://toysreloved.de/sitemap.xml",
    )
    MAX_SHOPIFY_PAGES = 200  # 50k products ceiling; prevents runaway loops

    _SITEMAP_PRODUCT_RE = re.compile(r"<loc>(https?://[^<]+/products?/[^<]+)</loc>")

    # ---- Custom entry point: paginates Shopify ------------------------

    def collect(self, date: str) -> SnapshotRecord:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Try the primary (Shopify products.json) first, paginating.
        if "products.json" in self.toys_url:
            try:
                return self._collect_shopify_paginated(now, date)
            except urllib.error.HTTPError as e:
                # Shopify endpoint not available — fall through to the
                # standard fallback loop on sitemaps.
                if e.code not in (404, 410, 401, 403):
                    raise
        return super().collect(date)

    def _collect_shopify_paginated(self, now: str, date: str) -> SnapshotRecord:
        base = self.toys_url.split("&page=")[0].split("?page=")[0]
        sep = "&" if "?" in base else "?"
        agg_cats: dict[str, int] = {}
        agg_brands: dict[str, int] = {}
        agg_price: dict[str, int] = {"0-10": 0, "10-25": 0, "25-50": 0, "50-100": 0, "100+": 0}
        agg_cond: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        total = 0
        last_body = ""

        for page in range(1, self.MAX_SHOPIFY_PAGES + 1):
            url = f"{base}{sep}page={page}"
            body = self._fetch(url)
            last_body = body
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                # Not JSON — bail and let the base-class fallback loop
                # (sitemaps, HTML) handle it.
                return super().collect(date)
            products = data.get("products", []) or []
            if not products:
                break
            partial = self._parse_products_json(body, _as_accumulator=True)
            total += partial["total_listings"]
            for k, v in partial["categories"].items():
                agg_cats[k] = agg_cats.get(k, 0) + v
            for b in partial["brands_top10"]:
                agg_brands[b["name"]] = agg_brands.get(b["name"], 0) + b["count"]
            for k, v in partial["price_buckets_eur"].items():
                agg_price[k] = agg_price.get(k, 0) + v
            for k, v in partial["conditions"].items():
                agg_cond[k] = agg_cond.get(k, 0) + v
            if len(samples) < 5:
                samples.extend(partial["sample_listings"][: 5 - len(samples)])
            if len(products) < 250:  # short page = last page
                break
            self._jitter()

        cache_path = self._cache_raw(last_body, date, suffix="toys-lastpage.json")
        canonical_cats = {c: 0 for c in CANONICAL_CATEGORIES}
        for label, count in agg_cats.items():
            canonical_cats[normalize_category(label, self.category_mapping)] += count
        brands_top10 = [
            {"name": n, "count": c}
            for n, c in sorted(agg_brands.items(), key=lambda kv: -kv[1])[:10]
        ]
        return SnapshotRecord(
            site=self.site_key,
            fetched_at=now,
            status="ok",
            total_listings=total,
            categories={k: v for k, v in canonical_cats.items() if v},
            brands_top10=brands_top10,
            price_buckets_eur=agg_price,
            conditions=agg_cond,
            sample_listings=samples,
            raw_cache_path=str(cache_path) if cache_path else None,
        )

    def _parse(self, html: str) -> dict[str, Any]:
        # Reached only via the base-class fallback path (not Shopify JSON).
        if html.lstrip().startswith("{"):
            try:
                return self._parse_products_json(html)
            except (json.JSONDecodeError, KeyError):
                pass
        urls = set(self._SITEMAP_PRODUCT_RE.findall(html))
        if urls:
            return {"total_listings": len(urls), "categories": {}}
        return {"total_listings": _first_int(html), "categories": {}}

    def _parse_products_json(
        self, body: str, *, _as_accumulator: bool = False
    ) -> dict[str, Any]:
        data = json.loads(body)
        products = data.get("products", [])
        cats: dict[str, int] = {}
        brands_counter: dict[str, int] = {}
        conditions: dict[str, int] = {}
        price_buckets = {"0-10": 0, "10-25": 0, "25-50": 0, "50-100": 0, "100+": 0}
        samples: list[dict[str, Any]] = []

        for p in products:
            ptype = (p.get("product_type") or "").strip() or "Other"
            cats[ptype] = cats.get(ptype, 0) + 1
            vendor = (p.get("vendor") or "").strip()
            if vendor:
                brands_counter[vendor] = brands_counter.get(vendor, 0) + 1
            for v in p.get("variants", []) or []:
                try:
                    price = float(v.get("price", 0))
                except (TypeError, ValueError):
                    continue
                price_buckets[_bucket(price)] += 1
            tags = p.get("tags", "")
            cond = _extract_condition(tags if isinstance(tags, str) else " ".join(tags))
            if cond:
                conditions[cond] = conditions.get(cond, 0) + 1
            if len(samples) < 5:
                samples.append(
                    {
                        "id": p.get("id"),
                        "title": p.get("title"),
                        "type": ptype,
                        "vendor": vendor,
                        "handle": p.get("handle"),
                    }
                )

        brands_top10 = [
            {"name": n, "count": c}
            for n, c in sorted(brands_counter.items(), key=lambda kv: -kv[1])[:10]
        ]
        return {
            "total_listings": len(products),  # caller aggregates paginated totals
            "categories": cats,
            "brands_top10": brands_top10,
            "price_buckets_eur": price_buckets,
            "conditions": conditions,
            "sample_listings": samples,
        }


def _bucket(price: float) -> str:
    if price < 10:
        return "0-10"
    if price < 25:
        return "10-25"
    if price < 50:
        return "25-50"
    if price < 100:
        return "50-100"
    return "100+"


_CONDITION_HINTS = {
    "new": ("new", "neu", "neuwertig"),
    "like_new": ("like new", "wie neu", "sehr gut"),
    "good": ("good", "gut"),
    "acceptable": ("acceptable", "akzeptabel", "gebraucht"),
}


def _extract_condition(text: str) -> str | None:
    t = text.lower()
    for canonical, hints in _CONDITION_HINTS.items():
        if any(h in t for h in hints):
            return canonical
    return None


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _extract_facets_from_json(obj: Any, key_hint: str, depth: int = 0) -> dict[str, int]:
    """Walk a JSON tree and find list-of-{name, count} entries whose
    parent key contains `key_hint` (case-insensitive). Returns the first
    match as a {name: count} dict."""
    if depth > 10:
        return {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_hint in k.lower() and isinstance(v, list):
                result: dict[str, int] = {}
                for item in v:
                    if isinstance(item, dict):
                        name = item.get("name") or item.get("label") or item.get("title")
                        count = item.get("count") or item.get("docCount") or item.get("doc_count")
                        if name and isinstance(count, int) and count > 0:
                            result[str(name)] = count
                if result:
                    return result
            found = _extract_facets_from_json(v, key_hint, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj[:30]:
            found = _extract_facets_from_json(item, key_hint, depth + 1)
            if found:
                return found
    return {}


COLLECTORS: dict[str, type[BaseCollector]] = {
    "stuffle": StuffleCollector,
    "sellpy": SellpyCollector,
    "tildi": TildiCollector,
    "toysreloved": ToysRelovedCollector,
}
