"""
sources/europeana.py

Europeana Search API (https://api.europeana.eu/record/v2/search.json).
Requires a free wskey (see config.example.json). Field shapes below (title,
dcCreator, dataProvider, edmPreview as thumbnail, edmIsShownAt as canonical
page, guid, year) are taken from Europeana's published REST API
documentation — NOT verified against a live capture, since no key is
configured in this environment.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import (
    SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text, normalize_page,
)
from .config import http_get_json, FetchError, cache_get, cache_set, get_key

SEARCH_URL = "https://api.europeana.eu/record/v2/search.json"
KEY_NAME = "EUROPEANA_API_KEY"
# Official Search API: offset pagination cannot pass the first 1,000 hits
# (`start` is 1-based; start beyond 1,000 is rejected). Deeper results need
# cursor pagination, which this adapter does not invent. Past this window
# the search returns nothing instead of repeating the last in-window page.
# https://europeana.atlassian.net/wiki/spaces/EF/pages/2360508417/API+FAQ
EUROPEANA_OFFSET_WINDOW = 1000


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def normalize_europeana_item(item: dict) -> Optional[TimeMachineItem]:
    try:
        title = clean_text(_first(item.get("title")), max_len=200)
        canonical_url = _first(item.get("edmIsShownAt")) or item.get("guid")
        item_id = item.get("id")
        if not title or not canonical_url or not item_id:
            return None

        provider = _first(item.get("dataProvider"))
        creator = _first(item.get("dcCreator"))
        year_field = item.get("year")
        date_label = str(_first(year_field)) if year_field else None

        media_type = (_first(item.get("type")) or "").upper()
        media_kind = {
            "IMAGE": "photograph", "TEXT": "document", "VIDEO": "moving_image",
            "SOUND": "audio", "3D": "artifact",
        }.get(media_type, "document")

        result = TimeMachineItem.build(
            source_id="europeana",
            source_item_id=item_id,
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            dateLabel=date_label,
            sourceInstitution=provider or "Europeana partner institution",
            thumbnailUrl=item.get("edmPreview") if isinstance(item.get("edmPreview"), str) else _first(item.get("edmPreview")),
            people=[creator] if creator else [],
            rights=_first(item.get("rights")),
        )
        result.sourceUrl = canonical_url
        result.evidence = Evidence(institution=provider or "Europeana", sourceItemId=item_id, sourceUrl=canonical_url)
        return result
    except Exception:  # noqa: BLE001
        return None


class EuropeanaSource(SourceAdapter):
    source_id = "europeana"
    display_name = "Europeana"
    requires_key = True
    deep_pagination = True  # documented start/rows parameters; offset window is 1,000
    api_docs_url = "https://europeana.atlassian.net/wiki/spaces/EF/pages/2385739812/Search+API+Documentation"

    def availability(self) -> SourceAvailability:
        if not get_key(KEY_NAME):
            return SourceAvailability(False, "API key not configured")
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10,
               page: int = 1) -> List[TimeMachineItem]:
        key = get_key(KEY_NAME)
        if not key:
            return []
        limit = max(1, min(int(limit or 10), 100))  # documented rows maximum
        page_number = normalize_page(page)
        # Europeana `start` is the 1-based index of the first hit.
        start = (page_number - 1) * limit + 1
        if start > EUROPEANA_OFFSET_WINDOW:
            return []
        rows = limit
        if start + rows - 1 > EUROPEANA_OFFSET_WINDOW:
            rows = EUROPEANA_OFFSET_WINDOW - start + 1
        params = {"query": query, "rows": rows, "start": start, "wskey": key}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"europeana:search:{urlencode({'query': query, 'rows': rows, 'start': start})}"
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

        items = (raw or {}).get("items") or []
        results = []
        for item in items:
            normalized = normalize_europeana_item(item)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
