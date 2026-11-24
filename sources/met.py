"""
sources/met.py

The Metropolitan Museum of Art Collection API (Open Access).
No API key required — https://metmuseum.github.io/

Search uses the v1.1 endpoint: the former v1 search (Solr-backed) was
retired on 2026-10-01 and now answers HTTP 410 Gone, directing callers to
/public/collection/v1.1/search (Elastic-based), which accepts q, hasImages,
limit and offset and returns {"total": ..., "objectIDs": [...]}. Object
details are still served from /public/collection/v1/objects/{id} (verified
working). Search returns a bounded page of IDs; object details are fetched
individually (bounded).
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
OBJECT_URL = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{object_id}"
MAX_OBJECT_FETCHES = 8


def _guess_media_kind(obj: dict) -> str:
    blob = " ".join(
        str(obj.get(k) or "")
        for k in ("classification", "objectName", "medium", "department")
    ).lower()
    if "photograph" in blob:
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "watercolor", "ink", "calligraphy")):
        return "artwork"
    if any(w in blob for w in ("manuscript", "codex", "book", "papyrus", "scroll")):
        return "document"
    if "map" in blob or "cartograph" in blob:
        return "map"
    return "artifact"


def normalize_met_object(obj: dict) -> Optional[TimeMachineItem]:
    try:
        object_id = obj.get("objectID")
        title = clean_text(obj.get("title"), max_len=200)
        canonical_url = obj.get("objectURL") or (
            f"https://www.metmuseum.org/art/collection/search/{object_id}" if object_id else None
        )
        if not object_id or not title or not canonical_url:
            return None

        artist = clean_text(obj.get("artistDisplayName"))
        culture = clean_text(obj.get("culture"))
        period = clean_text(obj.get("period"))
        medium = clean_text(obj.get("medium"))
        date_label = clean_text(obj.get("objectDate"), max_len=80)
        department = clean_text(obj.get("department"))

        description_parts = [p for p in (culture, period, medium, department) if p]
        description = clean_text(" · ".join(description_parts)) if description_parts else None

        image = obj.get("primaryImageSmall") or obj.get("primaryImage") or None
        # Prefer public-domain images when available; still link the object either way.
        topics = [t for t in [obj.get("classification"), obj.get("objectName"), culture] if t]

        item = TimeMachineItem.build(
            source_id="met",
            source_item_id=str(object_id),
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(obj),
            description=description,
            dateLabel=date_label,
            location=culture,
            sourceInstitution="The Metropolitan Museum of Art",
            thumbnailUrl=image,
            imageUrl=obj.get("primaryImage") or image,
            people=[artist] if artist else [],
            topics=[str(t) for t in topics if isinstance(t, str)][:8],
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="The Metropolitan Museum of Art",
            sourceItemId=str(object_id),
            sourceUrl=canonical_url,
            collection=department,
        )
        return item
    except Exception:  # noqa: BLE001
        return None


class MetSource(SourceAdapter):
    source_id = "met"
    display_name = "The Metropolitan Museum of Art"
    requires_key = False
    deep_pagination = True  # v1.1 search limit/offset (verified live)
    api_docs_url = "https://metmuseum.github.io/"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # v1.1 search supports limit + offset (verified against the live API).
        offset = (page - 1) * limit
        params = {"q": query, "hasImages": "true", "limit": limit, "offset": offset}
        url = f"{SEARCH_URL}?{urlencode(params)}"
        cache_key = f"met:search:{urlencode(params)}"
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

        object_ids = (raw or {}).get("objectIDs") or []
        if not object_ids:
            return []

        results: List[TimeMachineItem] = []
        for object_id in object_ids[: max(limit, MAX_OBJECT_FETCHES)]:
            if len(results) >= limit:
                break
            obj_cache = f"met:object:{object_id}"
            obj = cache_get(obj_cache)
            if obj is None:
                try:
                    obj = http_get_json(OBJECT_URL.format(object_id=object_id))
                except FetchError:
                    continue
                cache_set(obj_cache, obj)
            item = normalize_met_object(obj or {})
            if item:
                results.append(item)
        return results
