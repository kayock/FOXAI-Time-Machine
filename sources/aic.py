"""
sources/aic.py

Art Institute of Chicago public API (https://api.artic.edu/docs/).
No API key required for search / artwork reads.

Images are served from the API's config.iiif_url + artwork image_id, using
the documented IIIF Image API 2.0 pattern:
    {iiif_url}/{image_id}/full/843,/0/default.jpg

The IIIF host (www.artic.edu) currently presents an expired TLS certificate
that browsers reject, so display URLs are rewritten to a same-origin proxy
(/api/aic-image) after normalization. The official IIIF URL is still what
gets fetched. Records without a usable image_id are skipped.
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote, urlencode, urlparse

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://api.artic.edu/api/v1/artworks/search"
# Documented fallback only — prefer config.iiif_url from each API response.
IIIF_DEFAULT = "https://www.artic.edu/iiif/2"
IIIF_SIZE_FULL = "843,"
IIIF_SIZE_THUMB = "400,"
FIELDS = ",".join([
    "id", "title", "image_id", "artist_display", "date_display", "medium_display",
    "department_title", "place_of_origin", "artwork_type_title", "is_public_domain",
    "api_link",
])


def usable_image_id(value) -> Optional[str]:
    """AIC image_id is a IIIF identifier string. Reject missing/unsafe values."""
    if not isinstance(value, str):
        return None
    image_id = value.strip()
    if not image_id or len(image_id) > 80:
        return None
    if any(ch in image_id for ch in "/\\?&# "):
        return None
    return image_id


def build_aic_iiif_url(image_id: str, iiif_url: Optional[str], size: str = IIIF_SIZE_FULL) -> Optional[str]:
    """Documented IIIF construction: config.iiif_url + image_id + size suffix."""
    ident = usable_image_id(image_id)
    if not ident:
        return None
    base = (iiif_url or "").strip().rstrip("/")
    if not base:
        base = IIIF_DEFAULT
    parsed = urlparse(base)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    return f"{base}/{ident}/full/{size}/0/default.jpg"


def is_aic_iiif_url(url: str) -> bool:
    """Allowlist for the same-origin AIC image proxy — not a general proxy."""
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host != "artic.edu" and not host.endswith(".artic.edu"):
        return False
    path = parsed.path or ""
    if "/iiif/" not in path:
        return False
    if not path.endswith("/default.jpg"):
        return False
    return True


def aic_browser_image_url(remote_iiif_url: Optional[str]) -> Optional[str]:
    """Same-origin URL so the browser does not have to TLS-verify artic.edu."""
    if not remote_iiif_url or not is_aic_iiif_url(remote_iiif_url):
        return None
    return "/api/aic-image?u=" + quote(remote_iiif_url, safe="")


def _guess_media_kind(obj: dict) -> str:
    blob = " ".join(
        str(obj.get(k) or "")
        for k in ("artwork_type_title", "medium_display", "department_title")
    ).lower()
    if "photograph" in blob:
        return "photograph"
    if any(w in blob for w in ("painting", "drawing", "print", "watercolor", "pastel")):
        return "artwork"
    if "manuscript" in blob or "book" in blob:
        return "document"
    if "textile" in blob or "armor" in blob or "sculpture" in blob or "vessel" in blob:
        return "artifact"
    return "artwork"


def normalize_aic_artwork(
    obj: dict,
    iiif_url: str = IIIF_DEFAULT,
    *,
    for_browser: bool = False,
) -> Optional[TimeMachineItem]:
    """Normalize one AIC artwork. Returns None when there is no usable image."""
    try:
        artwork_id = obj.get("id")
        title = clean_text(obj.get("title"), max_len=200)
        if not artwork_id or not title:
            return None

        image_id = usable_image_id(obj.get("image_id"))
        image_url = build_aic_iiif_url(image_id, iiif_url, IIIF_SIZE_FULL) if image_id else None
        thumb = build_aic_iiif_url(image_id, iiif_url, IIIF_SIZE_THUMB) if image_id else None
        if not image_url:
            # Do not emit a broken / imageless AIC card.
            return None
        if for_browser:
            image_url = aic_browser_image_url(image_url) or image_url
            thumb = aic_browser_image_url(thumb) or thumb

        canonical_url = f"https://www.artic.edu/artworks/{artwork_id}"
        artist = clean_text(obj.get("artist_display"), max_len=120)
        date_label = clean_text(obj.get("date_display"), max_len=80)
        medium = clean_text(obj.get("medium_display"))
        department = clean_text(obj.get("department_title"))
        origin = clean_text(obj.get("place_of_origin"))
        description = clean_text(" · ".join(p for p in (origin, medium, department) if p))

        item = TimeMachineItem.build(
            source_id="aic",
            source_item_id=str(artwork_id),
            canonical_url=canonical_url,
            title=title,
            media_kind=_guess_media_kind(obj),
            description=description,
            dateLabel=date_label,
            location=origin,
            sourceInstitution="Art Institute of Chicago",
            thumbnailUrl=thumb,
            imageUrl=image_url,
            people=[artist] if artist else [],
            topics=[t for t in [obj.get("artwork_type_title"), department] if isinstance(t, str)][:8],
        )
        item.sourceUrl = canonical_url
        item.evidence = Evidence(
            institution="Art Institute of Chicago",
            sourceItemId=str(artwork_id),
            sourceUrl=canonical_url,
            collection=department,
        )
        return item
    except Exception:  # noqa: BLE001
        return None


class AicSource(SourceAdapter):
    source_id = "aic"
    display_name = "Art Institute of Chicago"
    requires_key = False
    deep_pagination = True  # documented page/limit parameters
    api_docs_url = "https://api.artic.edu/docs/"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10,
               page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)
        # Ask for extra hits so skipping imageless records can still fill `limit`.
        fetch_limit = max(1, min(25, limit * 2))
        params = {"q": query, "limit": fetch_limit, "page": page, "fields": FIELDS}
        url = f"{SEARCH_URL}?{urlencode(params)}"
        cache_key = f"aic:search:{urlencode({'q': query, 'limit': fetch_limit, 'page': page})}"
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

        iiif_url = ((raw or {}).get("config") or {}).get("iiif_url") or IIIF_DEFAULT
        results: List[TimeMachineItem] = []
        for doc in (raw or {}).get("data") or []:
            item = normalize_aic_artwork(doc, iiif_url=iiif_url, for_browser=True)
            if item:
                results.append(item)
            if len(results) >= limit:
                break
        return results
