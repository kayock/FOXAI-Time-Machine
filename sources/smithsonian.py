"""
sources/smithsonian.py

Smithsonian Open Access API (https://api.si.edu/openaccess/api/v1.0/search).
Requires a free api.data.gov key (see config.example.json). Response field
shapes below (content.descriptiveNonRepeating / content.freetext /
content.indexedStructured) are taken from Smithsonian's own API docs and
community client libraries — NOT verified against a live capture, since no
key is configured in this environment. Treat the normalize function as
best-effort until exercised against a real response.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set, get_key

SEARCH_URL = "https://api.si.edu/openaccess/api/v1.0/search"
KEY_NAME = "SMITHSONIAN_API_KEY"


def normalize_smithsonian_item(row: dict) -> Optional[TimeMachineItem]:
    try:
        content = row.get("content") or {}
        descriptive = content.get("descriptiveNonRepeating") or {}
        freetext = content.get("freetext") or {}
        indexed = content.get("indexedStructured") or {}

        record_id = descriptive.get("record_ID") or row.get("id")
        title_field = descriptive.get("title")
        title = clean_text(title_field.get("content") if isinstance(title_field, dict) else title_field, max_len=200)
        canonical_url = descriptive.get("record_link") or descriptive.get("guid")
        if not record_id or not title or not canonical_url:
            return None

        online_media = ((descriptive.get("online_media") or {}).get("media")) or []
        thumbnail = None
        image = None
        for m in online_media:
            if m.get("thumbnail"):
                thumbnail = thumbnail or m.get("thumbnail")
            if m.get("content"):
                image = image or m.get("content")

        unit_code = descriptive.get("unit_code")
        object_type = indexed.get("object_type") or []
        media_kind = "artifact"
        if object_type:
            ot = " ".join(object_type).lower()
            if "photograph" in ot:
                media_kind = "photograph"
            elif "painting" in ot or "artwork" in ot:
                media_kind = "artwork"
            elif "document" in ot or "letter" in ot:
                media_kind = "document"

        notes = freetext.get("notes") or []
        description = clean_text(" ".join(n.get("content", "") for n in notes if isinstance(n, dict))[:600])

        item = TimeMachineItem.build(
            source_id="smithsonian",
            source_item_id=record_id,
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            description=description,
            sourceInstitution=f"Smithsonian Institution ({unit_code})" if unit_code else "Smithsonian Institution",
            thumbnailUrl=thumbnail,
            imageUrl=image,
            topics=indexed.get("topic") or [],
            places=indexed.get("place") or [],
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(institution="Smithsonian Institution", collection=unit_code,
                                  sourceItemId=record_id, sourceUrl=canonical_url)
        return item
    except Exception:  # noqa: BLE001
        return None


class SmithsonianSource(SourceAdapter):
    source_id = "smithsonian"
    display_name = "Smithsonian Open Access"
    requires_key = True
    # Zero-based `start` index pagination (see search docstring).
    deep_pagination = True
    api_docs_url = "https://www.si.edu/openaccess/api"

    def availability(self) -> SourceAvailability:
        if not get_key(KEY_NAME):
            return SourceAvailability(False, "API key not configured")
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10,
               page: int = 1) -> List[TimeMachineItem]:
        key = get_key(KEY_NAME)
        if not key:
            return []
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # Smithsonian Open Access search paginates with the zero-based
        # `start` index alongside `rows` (the pair used consistently in the
        # provider's own examples and client libraries; the provider's docs
        # site itself was unreachable from the build sandbox, so this is
        # docs/community-corroborated and marked accordingly in the matrix).
        params = {"q": query, "rows": limit, "start": (page - 1) * limit,
                  "api_key": key}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"smithsonian:search:{urlencode({'q': query, 'rows': limit, 'page': page})}"
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

        rows = ((raw or {}).get("response") or {}).get("rows") or []
        results = []
        for row in rows:
            normalized = normalize_smithsonian_item(row)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
