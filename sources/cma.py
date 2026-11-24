"""
sources/cma.py

Cleveland Museum of Art Open Access API (https://openaccess-api.clevelandart.org).
No API key required — GET works directly; responses are CC0-licensed metadata
with direct image URLs on openaccess-cdn.clevelandart.org.

Field shapes (id, title, creation_date, type, description, culture,
department, images.web.url, creators[].description, share_license_status,
url) verified against a live API capture in this environment (e.g. q=monet
→ "The Red Kerchief", 1958.39).
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://openaccess-api.clevelandart.org/api/artworks/"


def _guess_media_kind(art: dict) -> str:
    blob = " ".join(
        str(art.get(k) or "")
        for k in ("type", "technique", "department", "collection")
    ).lower()
    if "map" in blob or "cartograph" in blob:
        return "map"
    if "photograph" in blob:
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "watercolor", "pastel", "engraving")):
        return "artwork"
    if any(w in blob for w in ("manuscript", "book", "calligraphy")):
        return "document"
    return "artifact"


def normalize_cma_artwork(art: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of data[] from the CMA artworks API. Returns None
    (never raises) for a malformed entry so one bad record can't take down
    a search."""
    try:
        artwork_id = art.get("id")
        title = clean_text(art.get("title"), max_len=200)
        if not artwork_id or not title:
            return None

        canonical_url = art.get("url") or f"https://clevelandart.org/art/{art.get('accession_number', '')}"
        images = art.get("images") or {}
        web_image = (images.get("web") or {}).get("url")
        print_image = (images.get("print") or {}).get("url")
        if not web_image and not print_image:
            return None  # never emit a broken, imageless CMA card

        creators = []
        for c in art.get("creators") or []:
            if isinstance(c, dict) and c.get("description"):
                creators.append(clean_text(c.get("description"), max_len=80))

        culture = art.get("culture") or []
        culture_label = culture[0] if isinstance(culture, list) and culture and isinstance(culture[0], str) else None

        license_status = art.get("share_license_status")
        rights = f"CC0 — no rights reserved ({license_status})" if license_status == "CC0" else (
            f"License: {license_status}" if license_status else None
        )

        item = TimeMachineItem.build(
            source_id="cma",
            source_item_id=str(artwork_id),
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(art),
            description=clean_text(art.get("description") or art.get("tombstone"), max_len=500),
            dateLabel=clean_text(art.get("creation_date"), max_len=80),
            location=culture_label,
            sourceInstitution="The Cleveland Museum of Art",
            collection=clean_text(art.get("department"), max_len=120),
            thumbnailUrl=web_image or print_image,
            imageUrl=print_image or web_image,
            people=creators[:5],
            topics=[str(t) for t in [art.get("type"), art.get("technique"), culture_label] if t][:8],
            rights=rights,
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="The Cleveland Museum of Art",
            collection=clean_text(art.get("collection"), max_len=120),
            sourceItemId=str(artwork_id),
            sourceUrl=canonical_url,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class CmaSource(SourceAdapter):
    source_id = "cma"
    display_name = "The Cleveland Museum of Art"
    requires_key = False
    deep_pagination = True  # documented skip/limit parameters (verified live)
    api_docs_url = "http://openaccess-api.clevelandart.org/"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # Documented pagination: skip/limit.
        params = {"q": query, "limit": limit, "skip": (page - 1) * limit}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"cma:search:{urlencode(params)}"
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

        arts = (raw or {}).get("data") or []
        results = []
        for art in arts:
            normalized = normalize_cma_artwork(art)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
