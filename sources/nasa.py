"""
sources/nasa.py

NASA Image and Video Library (https://images-api.nasa.gov). No API key
required — a plain GET works. Response shape verified against a real
captured sample (see tests/fixtures/nasa_search_response.json).
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://images-api.nasa.gov/search"
ITEM_PAGE_URL = "https://images.nasa.gov/details/{nasa_id}"


def normalize_nasa_item(raw_item: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of collection.items[] from the NASA Images API
    into a TimeMachineItem. Returns None (never raises) for a malformed
    entry so one bad record can't take down a search."""
    try:
        data_list = raw_item.get("data") or []
        if not data_list:
            return None
        data = data_list[0]
        nasa_id = data.get("nasa_id")
        title = clean_text(data.get("title"), max_len=200)
        if not nasa_id or not title:
            return None

        media_type = data.get("media_type", "image")
        media_kind = {"image": "photograph", "video": "moving_image", "audio": "audio"}.get(media_type, "photograph")

        links = raw_item.get("links") or []
        thumbnail_url = None
        image_url = None
        for link in links:
            if link.get("rel") == "preview":
                thumbnail_url = link.get("href")
            if link.get("rel") == "canonical" and link.get("render") == "image":
                image_url = link.get("href")
        if not image_url:
            # fall back to any alternate image link, largest first isn't
            # guaranteed by the API order, so just take what's there.
            for link in links:
                if link.get("render") == "image":
                    image_url = image_url or link.get("href")

        canonical_url = ITEM_PAGE_URL.format(nasa_id=nasa_id)
        description = clean_text(data.get("description") or data.get("description_508"))
        date_created = data.get("date_created")
        date_label = date_created.split("T")[0] if isinstance(date_created, str) else None

        people = []
        for field_name in ("photographer", "secondary_creator"):
            val = data.get(field_name)
            if val:
                people.append(val)

        topics = [k for k in (data.get("keywords") or []) if isinstance(k, str)][:8]

        item = TimeMachineItem.build(
            source_id="nasa",
            source_item_id=nasa_id,
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            description=description,
            dateLabel=date_label,
            location=data.get("location") or data.get("center"),
            sourceInstitution="NASA",
            thumbnailUrl=thumbnail_url,
            imageUrl=image_url,
            people=people,
            topics=topics,
        )
        item.sourceUrl = canonical_url
        item.evidence = _evidence(nasa_id, canonical_url)
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


def _evidence(nasa_id: str, canonical_url: str):
    from .base import Evidence
    return Evidence(institution="NASA", sourceItemId=nasa_id, sourceUrl=canonical_url)


class NasaSource(SourceAdapter):
    source_id = "nasa"
    display_name = "NASA Image and Video Library"
    requires_key = False
    deep_pagination = True  # documented page/page_size parameters
    api_docs_url = "https://images.nasa.gov/docs"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10,
               page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))  # bounded — never request huge result sets
        page = normalize_page(page)
        params = {"q": query, "page_size": limit, "page": page}  # documented page param
        if media in ("image", "video", "audio"):
            params["media_type"] = media
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"nasa:search:{url}"
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

        items_raw = ((raw or {}).get("collection") or {}).get("items") or []
        results = []
        for raw_item in items_raw:
            normalized = normalize_nasa_item(raw_item)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
