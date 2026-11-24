"""
sources/vam.py

Victoria and Albert Museum Collections API v2 (https://developers.vam.ac.uk).
No API key required.

Search: GET /v2/objects/search?q=...&images_exist=1&page_size=...&page=...
Response records carry systemNumber, _primaryTitle, _primaryMaker,
_primaryDate, _primaryPlace, objectType and _images with a framed-mark
IIIF thumbnail plus an IIIF presentation manifest URL. Shapes verified
against a live capture in this environment (e.g. q="great wave" →
O73124, "Thirty-Six Views of Mount Fuji", Hokusai).

Pagination note: the older guide documented a page_offset parameter, but
the live API silently ignores it (verified: identical records returned for
any offset). The live response meta echoes a `page` field and honors a
`page` request parameter — that is what this adapter uses, verified live
(page 2 returns genuinely different records).
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://api.vam.ac.uk/v2/objects/search"
CANONICAL_URL = "https://collections.vam.ac.uk/item/{system_number}/"


def _larger_image(thumbnail: Optional[str]) -> Optional[str]:
    """framemark.vam.ac.uk is a IIIF Image API 2 server; the thumbnail is
    served at /full/!100,100/ — request a larger, still bounded size with
    the same URL pattern."""
    if not thumbnail or not isinstance(thumbnail, str):
        return None
    for small, big in (("!100,100", "!600,600"), ("!200,200", "!600,600")):
        if small in thumbnail:
            return thumbnail.replace(small, big, 1)
    return thumbnail


def _guess_media_kind(record: dict) -> str:
    blob = " ".join(
        str(record.get(k) or "")
        for k in ("objectType", "_primaryTitle", "_currentLocation")
    ).lower()
    if "map" in blob or "atlas" in blob:
        return "map"
    if "photograph" in blob:
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "watercolour", "watercolor", "poster", "engraving")):
        return "artwork"
    if any(w in blob for w in ("manuscript", "notebook", "diary", "letter")):
        return "document"
    if any(w in blob for w in ("textile", "costume", "furniture", "ceramic", "sculpture", "silver")):
        return "artifact"
    return "artwork"


def normalize_vam_record(record: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of records[] from the V&A search API. Returns
    None (never raises) on a malformed or imageless record."""
    try:
        system_number = record.get("systemNumber")
        title = clean_text(record.get("_primaryTitle"), max_len=200)
        if not system_number or not title:
            return None

        images = record.get("_images") or {}
        thumb = _larger_image(images.get("_primary_thumbnail"))
        if not thumb:
            return None  # search was already filtered to images_exist=1

        maker = (record.get("_primaryMaker") or {}).get("name")
        canonical_url = CANONICAL_URL.format(system_number=system_number)

        item = TimeMachineItem.build(
            source_id="vam",
            source_item_id=system_number,
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(record),
            description=clean_text(record.get("objectType"), max_len=200),
            dateLabel=clean_text(record.get("_primaryDate"), max_len=80),
            location=clean_text(record.get("_primaryPlace"), max_len=120),
            sourceInstitution="Victoria and Albert Museum",
            thumbnailUrl=thumb,
            imageUrl=thumb,
            people=[clean_text(maker, max_len=80)] if maker else [],
            topics=[str(t) for t in [record.get("objectType"), (record.get("_primaryMaker") or {}).get("association")] if t][:8],
        )
        manifest = images.get("_iiif_presentation_url")
        if isinstance(manifest, str) and manifest.startswith("https://"):
            item.iiifManifestUrl = manifest
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="Victoria and Albert Museum",
            sourceItemId=system_number,
            sourceUrl=canonical_url,
            manifestUrl=manifest if isinstance(manifest, str) else None,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class VamSource(SourceAdapter):
    source_id = "vam"
    display_name = "Victoria and Albert Museum"
    requires_key = False
    deep_pagination = True  # documented page_offset/page_size (verified live)
    api_docs_url = "https://developers.vam.ac.uk/guide/v2/"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # Live-verified pagination: the API echoes/honors `page`
        # (page_offset is ignored by the live service).
        params = {
            "q": query,
            "images_exist": "1",
            "page_size": limit,
            "page": page,
        }
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"vam:search:{urlencode(params)}"
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
            normalized = normalize_vam_record(record)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
