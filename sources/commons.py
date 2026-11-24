"""
sources/commons.py

Wikimedia Commons MediaWiki API (https://commons.wikimedia.org/w/api.php).
No API key required.

Search is restricted to the File namespace (gsrnamespace=6) and to bitmap
images, and uses prop=imageinfo with iiurlwidth so the API hands back a
ready-made thumbnail plus extmetadata (license, date, description).
Shapes verified against a live capture in this environment.

Commons is a broad aggregator: every file is user-uploaded and
license-tagged, so the adapter surfaces the license (rights) and the
canonical Commons description page rather than implying any institutional
provenance it cannot verify.
"""
from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

API_URL = "https://commons.wikimedia.org/w/api.php"

# Wikimedia's User-Agent policy asks for a descriptive client with a way to
# identify it — keep this compliant and specific rather than a bare version.
COMMONS_UA = "FOXAITimeMachine/2.0 (local personal history-feed app; runs on the user's own machine)"

MIN_WIDTH = 400  # reject icons/sprites; historical scans are rarely tiny


def _strip_html(value) -> Optional[str]:
    """ImageDescription often carries inline HTML — strip tags, keep text."""
    if not value or not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    return clean_text(text, max_len=500)


def _ext_metadata(image_info: dict, field: str) -> Optional[str]:
    row = ((image_info.get("extmetadata") or {}).get(field) or {})
    return row.get("value") if isinstance(row, dict) else None


def _guess_media_kind(title: str, object_name: Optional[str]) -> str:
    blob = f"{title or ''} {object_name or ''}".lower()
    if any(w in blob for w in ("map", "atlas", "plan of", "chart")):
        return "map"
    if any(w in blob for w in ("painting", "drawing", "print", "lithograph", "engraving", "illustration", "poster")):
        return "artwork"
    if any(w in blob for w in ("photograph", "photo of", "portrait of", "daguerreotype")):
        return "photograph"
    if any(w in blob for w in ("manuscript", "letter", "document", "page of")):
        return "document"
    return "photograph"


def normalize_commons_page(page: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of query.pages{} from the Commons search API.
    Returns None (never raises) on a malformed or unusable record."""
    try:
        pageid = page.get("pageid")
        title = clean_text((page.get("title") or "").replace("File:", ""), max_len=200)
        image_info = (page.get("imageinfo") or [{}])[0]
        if not pageid or not title or not isinstance(image_info, dict):
            return None

        mime = (image_info.get("mime") or "").lower()
        if mime not in ("image/jpeg", "image/png", "image/tiff"):
            return None
        width = image_info.get("width") or 0
        if not isinstance(width, int) or width < MIN_WIDTH:
            return None

        thumb = image_info.get("thumburl") or image_info.get("url")
        canonical_url = image_info.get("descriptionurl")
        if not thumb or not canonical_url:
            return None

        object_name = _ext_metadata(image_info, "ObjectName")
        description = _strip_html(_ext_metadata(image_info, "ImageDescription")) or None
        date_raw = _ext_metadata(image_info, "DateTimeOriginal")
        date_label = clean_text(date_raw, max_len=80) if date_raw else None
        license_name = _ext_metadata(image_info, "LicenseShortName")
        artist = _strip_html(_ext_metadata(image_info, "Artist")) or None

        item = TimeMachineItem.build(
            source_id="commons",
            source_item_id=str(pageid),
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(title, object_name),
            description=description,
            dateLabel=date_label,
            sourceInstitution="Wikimedia Commons",
            thumbnailUrl=thumb,
            imageUrl=image_info.get("url") or thumb,
            people=[artist] if artist else [],
            rights=f"{license_name} (see file page)" if license_name else "License on file page",
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="Wikimedia Commons",
            sourceItemId=str(pageid),
            sourceUrl=canonical_url,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class CommonsSource(SourceAdapter):
    source_id = "commons"
    display_name = "Wikimedia Commons"
    requires_key = False
    deep_pagination = True  # MediaWiki gsroffset continuation
    api_docs_url = "https://commons.wikimedia.org/w/api.php"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # MediaWiki generator pagination: gsroffset skips N results.
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": 6,  # File: namespace only
            "gsrlimit": limit * 2,  # ask extra; some rows fail quality filters
            "gsroffset": (page - 1) * limit,
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": 640,
        }
        url = f"{API_URL}?{urlencode(params)}"

        cache_key = f"commons:search:{urlencode({'q': query, 'limit': limit, 'page': page})}"
        cached = cache_get(cache_key)
        if cached is not None:
            raw = cached
        else:
            try:
                raw = http_get_json(url, headers={"User-Agent": COMMONS_UA})
            except FetchError:
                # Propagate: the registry classifies failures so discovery
                # can tell temporary trouble from a genuinely empty page.
                raise
            cache_set(cache_key, raw)

        pages = ((raw or {}).get("query") or {}).get("pages") or {}
        results = []
        for page_obj in pages.values():
            normalized = normalize_commons_page(page_obj)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
