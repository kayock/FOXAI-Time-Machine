"""
sources/loc.py

Library of Congress loc.gov JSON API (https://www.loc.gov/search/?fo=json).
No API key required. Field shapes verified against LOC's own API
documentation (loc.gov/apis/json-and-yaml).
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://www.loc.gov/search/"

ONLINE_FORMAT_TO_MEDIA_KIND = {
    "image": "photograph",
    "audio": "audio",
    "video": "moving_image",
    "pdf": "document",
    "web page": "document",
    "manuscript": "manuscript",
    "map": "map",
}


def _guess_media_kind(item: dict) -> str:
    for fmt in item.get("online_format") or []:
        kind = ONLINE_FORMAT_TO_MEDIA_KIND.get(fmt)
        if kind:
            return kind
    for fmt in item.get("original_format") or []:
        fmt_l = (fmt or "").lower()
        if "map" in fmt_l:
            return "map"
        if "manuscript" in fmt_l:
            return "manuscript"
        if "sound" in fmt_l or "audio" in fmt_l:
            return "audio"
        if "photo" in fmt_l or "print" in fmt_l:
            return "photograph"
    return "document"


def normalize_loc_item(raw: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of results[] from the LOC search API. Returns
    None (never raises) on a malformed or unusable record."""
    try:
        if raw.get("access_restricted"):
            return None
        title = clean_text(raw.get("title"), max_len=200)
        url = raw.get("url") or raw.get("id")
        if not title or not url:
            return None
        url = url.replace("http://", "https://", 1)

        image_urls = [u for u in (raw.get("image_url") or []) if isinstance(u, str)]
        thumb = None
        full = None
        for u in image_urls:
            full_u = "https:" + u if u.startswith("//") else u
            if thumb is None:
                thumb = full_u
            full = full_u  # LOC usually lists smallest -> largest; last is biggest available
        media_kind = _guess_media_kind(raw)

        date_label = raw.get("date")
        subjects = [s for s in (raw.get("subject") or []) if isinstance(s, str)][:8]
        contributors = [c for c in (raw.get("contributor") or []) if isinstance(c, str)][:5]

        # Prefer a source-item id derived from the item URL slug for a
        # stable identity even if loc.gov doesn't expose a separate id field.
        source_item_id = url.rstrip("/").rsplit("/", 1)[-1] if url else None

        item = TimeMachineItem.build(
            source_id="loc",
            source_item_id=source_item_id,
            canonical_url=url,
            title=title,
            media_kind=media_kind,
            dateLabel=date_label,
            sourceInstitution="Library of Congress",
            thumbnailUrl=thumb,
            imageUrl=full,
            topics=subjects,
            people=contributors,
        )
        item.sourceUrl = url
        item.evidence = Evidence(institution="Library of Congress", sourceItemId=source_item_id, sourceUrl=url)
        return item
    except Exception:  # noqa: BLE001
        return None


class LocSource(SourceAdapter):
    source_id = "loc"
    display_name = "Library of Congress"
    requires_key = False
    deep_pagination = True  # documented sp (start page) parameter
    api_docs_url = "https://www.loc.gov/apis/json-and-yaml/"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10,
               page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # loc.gov JSON API pagination: sp = start page (documented).
        params = {"q": query, "fo": "json", "c": limit, "sp": page, "at": "results"}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"loc:search:{url}"
        cached = cache_get(cache_key)
        if cached is not None:
            raw = cached
        else:
            try:
                raw = http_get_json(url)
            except FetchError:
                # Propagate: the registry classifies failures so discovery
                # can tell temporary trouble from a genuinely empty page.
                raise
            cache_set(cache_key, raw)

        results_raw = (raw or {}).get("results") or []
        results = []
        for r in results_raw:
            normalized = normalize_loc_item(r)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
