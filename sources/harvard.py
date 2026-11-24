"""
sources/harvard.py

Harvard Art Museums API (https://harvardartmuseums.org/collections/api).
Requires a free API key (HARVARD_API_KEY, see config.example.json).

The /object endpoint is documented with q, size (max 100), page, hasimage
and imagepermissionlevel parameters; records carry objectid, title, dated,
classification, culture, department, people[], primaryimageurl and url.
Shapes below are taken from the published API documentation — NOT verified
against a live capture from this environment, since no key is configured
here. Only records with imagepermissionlevel 0 (no permission restrictions)
are surfaced.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set, get_key

SEARCH_URL = "https://api.harvardartmuseums.org/object"
KEY_NAME = "HARVARD_API_KEY"


def _guess_media_kind(record: dict) -> str:
    blob = " ".join(
        str(record.get(k) or "")
        for k in ("classification", "medium", "department", "title")
    ).lower()
    if "map" in blob or "cartograph" in blob:
        return "map"
    if "photograph" in blob or "daguerreotype" in blob:
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "watercolor", "pastel")):
        return "artwork"
    if any(w in blob for w in ("manuscript", "book", "letter", "document")):
        return "document"
    return "artifact"


def normalize_harvard_record(record: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of records[]. Returns None (never raises) on a
    malformed, imageless, or permission-restricted record."""
    try:
        object_id = record.get("objectid")
        title = clean_text(record.get("title"), max_len=200)
        if not object_id or not title:
            return None
        # imagepermissionlevel 0 = no restrictions; anything else is withheld.
        if record.get("imagepermissionlevel") not in (0, None):
            return None
        image = record.get("primaryimageurl")
        if not image:
            return None

        canonical_url = record.get("url") or f"https://www.harvardartmuseums.org/collections/object/{object_id}"
        people = []
        for p in record.get("people") or []:
            if isinstance(p, dict) and p.get("name"):
                people.append(clean_text(p.get("name"), max_len=80))

        item = TimeMachineItem.build(
            source_id="harvard",
            source_item_id=str(object_id),
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(record),
            description=clean_text(record.get("medium") or record.get("caption")),
            dateLabel=clean_text(record.get("dated") or record.get("datebegin"), max_len=80),
            location=clean_text(record.get("culture") or record.get("placeoforigin"), max_len=120),
            sourceInstitution="Harvard Art Museums",
            collection=clean_text(record.get("department"), max_len=120),
            thumbnailUrl=image,
            imageUrl=image,
            people=people[:5],
            topics=[str(t) for t in [record.get("classification"), record.get("culture")] if t][:8],
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="Harvard Art Museums",
            collection=clean_text(record.get("department"), max_len=120),
            sourceItemId=str(object_id),
            sourceUrl=canonical_url,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class HarvardSource(SourceAdapter):
    source_id = "harvard"
    display_name = "Harvard Art Museums"
    requires_key = True
    deep_pagination = True  # documented size/page parameters
    api_docs_url = "https://harvardartmuseums.org/collections/api"

    def availability(self) -> SourceAvailability:
        if not get_key(KEY_NAME):
            return SourceAvailability(False, "API key not configured")
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        key = get_key(KEY_NAME)
        if not key:
            return []
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        params = {
            "q": query,
            "apikey": key,
            "size": limit * 2,  # ask extra; permission/imageless rows are filtered out
            "page": page,
            "hasimage": "1",
        }
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"harvard:search:{urlencode({'q': query, 'page': page, 'size': limit * 2})}"
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

        records = (raw or {}).get("records") or []
        results = []
        for record in records:
            normalized = normalize_harvard_record(record)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
