"""
sources/nara.py

U.S. National Archives Catalog API (https://catalog.archives.gov/api/v2).
Requires a free api.data.gov key (see config.example.json). Field shapes
below are taken from NARA's published Catalog API v2 documentation and the
official getting-started guide (archives.gov/research/catalog/help/
api-getting-started) — NOT verified against a live capture, since no key is
configured in this environment.

The v2 guide is explicit that records/search is a GET that must carry BOTH
an `x-api-key` header AND `Content-Type: application/json`, and that
`availableOnline=true` limits results to records with digital objects
viewable online — exactly what a visual feed wants. Each hit's
_source.record carries digitalObjects[] with a direct `objectUrl` and
productionDates[] for dating.

Pagination is taken from the published OpenAPI document
(https://catalog.archives.gov/api/v2/swagger.json, OpenAPI 3.0.0,
Catalog API 0.2.0), component parameters `paramPage` and `paramLimit`:
`page` is an integer, default 1, maximum 10000; `limit` is an integer,
default 20, maximum 1000. The getting-started page lists only a few
examples and points at that Swagger document for the full parameter list.
`searchAfter` is described as the way past 10,000 hits, but its schema
(integer, maximum 100) contradicts the prose (`searchAfter=*` then a sort
array). That conflict is not reliable enough to send `searchAfter`. Pages
past 10000 are not clamped back onto page 10000.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import (
    SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text, page_in_window,
)
from .config import http_get_json, FetchError, cache_get, cache_set, get_key

SEARCH_URL = "https://catalog.archives.gov/api/v2/records/search"
KEY_NAME = "NARA_API_KEY"
# OpenAPI schema maximum on the documented `page` parameter.
NARA_MAX_PAGE = 10000


def _production_date_label(record: dict) -> Optional[str]:
    """productionDates[] rows look like {"creationDateStart": "1940-01-01",
    "creationDateEnd": "1945-12-31", "creationDateText": "circa 1942"} in the
    documented schema; prefer the human-readable text when present."""
    for row in record.get("productionDates") or []:
        if not isinstance(row, dict):
            continue
        text = clean_text(row.get("creationDateText"), max_len=80)
        if text:
            return text
        start = clean_text(row.get("creationDateStart"), max_len=80)
        end = clean_text(row.get("creationDateEnd"), max_len=80)
        if start and end and start != end:
            return f"{start} – {end}"
        if start or end:
            return start or end
    return None


def normalize_nara_item(hit: dict) -> Optional[TimeMachineItem]:
    try:
        record = hit.get("record") or hit
        naId = record.get("naId")
        title = clean_text(record.get("title"), max_len=200)
        if not naId or not title:
            return None
        canonical_url = f"https://catalog.archives.gov/id/{naId}"

        description = clean_text(record.get("scopeAndContentNote") or record.get("generalNotes"))
        digital_objects = record.get("digitalObjects") or []
        thumbnail = None
        for do in digital_objects:
            # Documented v2 field is objectUrl (direct media URL); older
            # thumbnailUrl values are accepted as a defensive fallback.
            thumb_candidate = do.get("thumbnailUrl") or do.get("objectUrl")
            if thumb_candidate and str(thumb_candidate).lower().endswith((".jpg", ".jpeg", ".png", ".gif")):
                thumbnail = thumbnail or thumb_candidate

        level = (record.get("levelOfDescription") or "").lower()
        media_kind = "document"
        if "photograph" in level or "photo" in level:
            media_kind = "photograph"
        elif "map" in level or "cartograph" in level:
            media_kind = "map"
        elif "moving" in level or "film" in level or "video" in level:
            media_kind = "moving_image"
        elif "sound" in level or "audio" in level:
            media_kind = "audio"

        item = TimeMachineItem.build(
            source_id="nara",
            source_item_id=str(naId),
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            description=description,
            dateLabel=_production_date_label(record),
            sourceInstitution="U.S. National Archives",
            thumbnailUrl=thumbnail,
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(institution="U.S. National Archives", sourceItemId=str(naId), sourceUrl=canonical_url)
        return item
    except Exception:  # noqa: BLE001
        return None


class NaraSource(SourceAdapter):
    source_id = "nara"
    display_name = "U.S. National Archives"
    requires_key = True
    deep_pagination = True  # documented page/limit; page maximum 10000
    api_docs_url = "https://catalog.archives.gov/api/v2/swagger.json"

    def availability(self) -> SourceAvailability:
        if not get_key(KEY_NAME):
            return SourceAvailability(False, "API key not configured")
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        key = get_key(KEY_NAME)
        if not key:
            return []
        limit = max(1, min(int(limit or 10), 1000))  # documented limit maximum
        page_number = page_in_window(page, NARA_MAX_PAGE)
        if page_number is None:
            return []
        params = {
            "q": query,
            "limit": limit,
            "page": page_number,
            "availableOnline": "true",
        }
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"nara:search:{urlencode({'q': query, 'limit': limit, 'page': page_number})}"
        cached = cache_get(cache_key)
        if cached is not None:
            raw = cached
        else:
            try:
                # v2 requires both headers on every call (see module docstring).
                raw = http_get_json(url, headers={
                    "x-api-key": key,
                    "Content-Type": "application/json",
                    "User-Agent": "FOXAITimeMachine/2.2.0",
                })
            except FetchError:
                # Propagate: the registry classifies failures so discovery
                # can tell temporary trouble from a genuinely empty page.
                raise
            cache_set(cache_key, raw)

        hits = ((raw or {}).get("body") or {}).get("hits", {}).get("hits") or []
        results = []
        for hit in hits:
            normalized = normalize_nara_item(hit.get("_source", hit))
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
