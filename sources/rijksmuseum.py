"""
sources/rijksmuseum.py

Rijksmuseum Collection API (https://data.rijksmuseum.nl — the classic
collection endpoint https://www.rijksmuseum.nl/api/en/collection).
Requires a free Rijksmuseum API key (RIJKSMUSEUM_API_KEY, see
config.example.json).

Documented parameters used here: q (search), p (page number), ps (page
size), imgonly=true (only objects with a web image). Response shape
(artObjects[].objectNumber/title/longTitle/webImage.url/dating.
presentingDate/productionPlaces/links.web) is taken from the published
API documentation — NOT verified against a live capture from this
environment, since no key is configured here.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set, get_key

SEARCH_URL = "https://www.rijksmuseum.nl/api/en/collection"
KEY_NAME = "RIJKSMUSEUM_API_KEY"


def _guess_media_kind(obj: dict) -> str:
    blob = " ".join(
        str(obj.get(k) or "")
        for k in ("title", "longTitle", "classification")
    ).lower()
    if "map" in blob or "atlas" in blob:
        return "map"
    if "photograph" in blob:
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "watercolor", "watercolour")):
        return "artwork"
    if any(w in blob for w in ("manuscript", "book", "letter")):
        return "document"
    if any(w in blob for w in ("delftware", "porcelain", "furniture", "costume", "model", "ship")):
        return "artifact"
    return "artwork"


def normalize_rijksmuseum_object(obj: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of artObjects[]. Returns None (never raises) on a
    malformed or imageless record."""
    try:
        object_number = obj.get("objectNumber")
        title = clean_text(obj.get("title"), max_len=200)
        if not object_number or not title:
            return None

        web_image = (obj.get("webImage") or {}).get("url")
        if not web_image:
            return None

        canonical_url = ((obj.get("links") or {}).get("web")
                         or f"https://www.rijksmuseum.nl/en/collection/{object_number}")
        dating = (obj.get("dating") or {}).get("presentingDate")
        places = [clean_text(p, max_len=80) for p in (obj.get("productionPlaces") or []) if clean_text(p, max_len=80)]
        maker = clean_text(obj.get("principalOrFirstMaker"), max_len=80)
        description = clean_text(obj.get("plaqueDescriptionEnglish") or obj.get("longTitle"))

        item = TimeMachineItem.build(
            source_id="rijksmuseum",
            source_item_id=object_number,
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(obj),
            description=description,
            dateLabel=clean_text(dating, max_len=80),
            location=places[0] if places else None,
            sourceInstitution="Rijksmuseum, Amsterdam",
            thumbnailUrl=web_image,
            imageUrl=web_image,
            people=[maker] if maker else [],
            topics=places[:8],
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="Rijksmuseum, Amsterdam",
            sourceItemId=object_number,
            sourceUrl=canonical_url,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class RijksmuseumSource(SourceAdapter):
    source_id = "rijksmuseum"
    display_name = "Rijksmuseum"
    requires_key = True
    deep_pagination = True  # documented p (page) / ps (page size)
    api_docs_url = "https://data.rijksmuseum.nl/collection/"

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
        # Documented params: q, p (page), ps (page size), imgonly.
        params = {"key": key, "q": query, "p": page, "ps": limit, "imgonly": "true"}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"rijksmuseum:search:{urlencode({'q': query, 'p': page, 'ps': limit})}"
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

        objects = (raw or {}).get("artObjects") or []
        results = []
        for obj in objects:
            normalized = normalize_rijksmuseum_object(obj)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
