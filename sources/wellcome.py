"""
sources/wellcome.py

Wellcome Collection Catalogue API v2
(https://developers.wellcomecollection.org/catalogue/works).
No API key required.

One of the world's great medical/scientific/social-history archives:
manuscripts, photographs, paintings, engravings, ephemera and film dating
back centuries.

Endpoint shapes verified against live responses captured in this
environment:
  GET /catalogue/v2/works?query=...&pageSize=N&page=P
  -> {"type": "ResultList", "totalResults": N, "totalPages": M,
      "prevPage": ..., "nextPage": ...,
      "results": [{id, title, description?, physicalDescription?,
                   workType{label}, production[]?, thumbnail{url, license},
                   subjects?}]}

Pagination is native (`page` + `pageSize`, with totalPages reported), so
deep exploration walks real result pages. Thumbnails come from Wellcome's
IIIF server at a small bounded size; nothing large is downloaded. The
license attached to each thumbnail (e.g. "In copyright", CC0) is surfaced
verbatim in the item's rights field — never invented.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://api.wellcomecollection.org/catalogue/v2/works"


def _guess_media_kind(work: dict) -> str:
    work_type = ((work.get("workType") or {}).get("label") or "").lower()
    blob = " ".join(
        str(x or "")
        for x in (
            work_type,
            work.get("title"),
            work.get("description"),
            work.get("physicalDescription"),
        )
    ).lower()
    if any(w in blob for w in ("photograph", "photo", "daguerreotype", "lantern slide")):
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "engraving", "illustration")):
        return "artwork"
    if any(w in blob for w in ("manuscript", "archive", "letter", "diary", "notebook")):
        return "manuscript"
    if "map" in blob or "chart" in blob:
        return "map"
    if any(w in blob for w in ("moving image", "film", "video", "cine")):
        return "moving_image"
    if any(w in blob for w in ("sound", "audio", "oral")):
        return "audio"
    return "document"


def _date_label(work: dict) -> Optional[str]:
    rows = work.get("production") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = clean_text(row.get("label"), max_len=80)
        if label:
            return label
    return None


def _license_label(work: dict) -> Optional[str]:
    lic = work.get("license") or {}
    if isinstance(lic, dict) and lic.get("label"):
        return clean_text(lic.get("label"), max_len=80)
    thumb = work.get("thumbnail") or {}
    if isinstance(thumb, dict):
        lic = thumb.get("license") or {}
        if isinstance(lic, dict) and lic.get("label"):
            return clean_text(lic.get("label"), max_len=80)
    return None


def normalize_wellcome_work(work: dict) -> Optional[TimeMachineItem]:
    """Normalize one entry of results[]. Returns None (never raises) for a
    malformed or thumbnail-less record."""
    try:
        work_id = work.get("id")
        title = clean_text(work.get("title"), max_len=200)
        if not work_id or not title:
            return None
        thumbnail = ((work.get("thumbnail") or {}).get("url"))
        if not thumbnail:
            return None

        canonical_url = f"https://wellcomecollection.org/works/{work_id}"
        subjects = []
        for s in work.get("subjects") or []:
            if isinstance(s, dict) and s.get("label"):
                subjects.append(clean_text(s.get("label"), max_len=60))
        description = clean_text(work.get("description")) or clean_text(work.get("physicalDescription"))

        item = TimeMachineItem.build(
            source_id="wellcome",
            source_item_id=str(work_id),
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(work),
            description=description,
            dateLabel=_date_label(work),
            sourceInstitution="Wellcome Collection",
            thumbnailUrl=thumbnail,
            imageUrl=thumbnail,
            topics=subjects[:8],
            rights=_license_label(work),
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="Wellcome Collection",
            sourceItemId=str(work_id),
            sourceUrl=canonical_url,
        )
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class WellcomeSource(SourceAdapter):
    source_id = "wellcome"
    display_name = "Wellcome Collection"
    requires_key = False
    # Native page/pageSize pagination with totalPages reported by the API.
    deep_pagination = True
    api_docs_url = "https://developers.wellcomecollection.org/catalogue/works"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        params = {"query": query, "pageSize": limit, "page": page}
        url = f"{SEARCH_URL}?{urlencode(params)}"

        cache_key = f"wellcome:search:{urlencode(params)}"
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

        works = (raw or {}).get("results") or []
        results = []
        for work in works:
            normalized = normalize_wellcome_work(work)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
