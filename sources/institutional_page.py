"""
sources/institutional_page.py

A deliberately limited fallback for when a specific institution doesn't
expose an API Time Machine can use. Given ONE explicitly supplied page URL,
this reads structured metadata already embedded in that page (JSON-LD,
OpenGraph, or plain HTML <title>/<meta name="description">) and normalizes
it into a TimeMachineItem.

This is NOT a general web scraper: it never follows links, never crawls,
never extracts arbitrary article prose, and only looks at metadata the
page itself published for exactly this purpose. Standard library only
(html.parser + a small regex-based JSON-LD extractor) — no BeautifulSoup.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Optional, Dict, Any, List

from .base import TimeMachineItem, Evidence, clean_text
from .config import http_get_text, FetchError, cache_get, cache_set

JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)

# Institutional museum/library pages often soft-block non-browser user agents
# (403/429). Use a plain browser UA for the one-URL fallback only — never for
# the keyed API adapters, which get their own headers.
_INSTITUTIONAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class _MetaTagParser(HTMLParser):
    """Collects <meta property="og:*"/name="..."> tags and the <title>."""

    def __init__(self):
        super().__init__()
        self.meta: Dict[str, str] = {}
        self.title: Optional[str] = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "meta":
            key = attrs_d.get("property") or attrs_d.get("name")
            content = attrs_d.get("content")
            if key and content:
                self.meta[key] = content
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data


def _extract_json_ld(html: str) -> List[Dict[str, Any]]:
    """Extract every JSON-LD block. Skips (does not crash on) any block
    that isn't valid JSON."""
    blocks = []
    for match in JSON_LD_RE.findall(html):
        try:
            parsed = json.loads(match.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            blocks.extend([b for b in parsed if isinstance(b, dict)])
        elif isinstance(parsed, dict):
            blocks.append(parsed)
    return blocks


def _json_ld_field(block: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        val = block.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, dict) and "name" in val:
            return val.get("name")
        if isinstance(val, list) and val:
            first = val[0]
            if isinstance(first, str):
                return first
            if isinstance(first, dict):
                return first.get("name") or first.get("url")
    return None


def normalize_institutional_page(html: str, page_url: str) -> Optional[TimeMachineItem]:
    """Normalize a fetched institutional page's structured metadata into a
    TimeMachineItem. Returns None if there isn't enough usable metadata to
    build one — never raises on malformed HTML/JSON-LD."""
    try:
        parser = _MetaTagParser()
        parser.feed(html)
    except Exception:  # noqa: BLE001 — tolerate genuinely broken HTML
        parser = _MetaTagParser()

    json_ld_blocks = _extract_json_ld(html)
    primary_ld = json_ld_blocks[0] if json_ld_blocks else {}

    title = clean_text(
        parser.meta.get("og:title")
        or _json_ld_field(primary_ld, "name", "headline")
        or parser.title,
        max_len=200,
    )
    if not title:
        return None

    description = clean_text(
        parser.meta.get("og:description")
        or _json_ld_field(primary_ld, "description")
    )
    image = parser.meta.get("og:image") or _json_ld_field(primary_ld, "image")
    canonical_url = parser.meta.get("og:url") or primary_ld.get("url") or page_url
    institution = parser.meta.get("og:site_name") or _json_ld_field(primary_ld, "publisher", "provider")
    date_label = primary_ld.get("dateCreated") or primary_ld.get("datePublished")
    creator = _json_ld_field(primary_ld, "creator", "author")

    item = TimeMachineItem.build(
        source_id="institutional_page",
        source_item_id=None,
        canonical_url=canonical_url,
        title=title,
        media_kind="document",
        description=description,
        dateLabel=date_label,
        sourceInstitution=institution,
        thumbnailUrl=image,
        imageUrl=image,
        people=[creator] if creator else [],
    )
    item.sourceUrl = canonical_url
    item.evidence = Evidence(institution=institution, sourceUrl=canonical_url)
    return item


def fetch_and_normalize_institutional_page(page_url: str) -> Optional[TimeMachineItem]:
    """Fetch exactly the one supplied URL (no crawling) and normalize its
    metadata. Returns None on any network or parse failure."""
    cache_key = f"institutional_page:{page_url}"
    cached = cache_get(cache_key)
    if cached is not None:
        html = cached
    else:
        try:
            html = http_get_text(page_url, headers=_INSTITUTIONAL_HEADERS)
        except FetchError:
            return None
        cache_set(cache_key, html)
    return normalize_institutional_page(html, page_url)
