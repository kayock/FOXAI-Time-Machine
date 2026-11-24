"""
sources/internetarchive.py (v2.1)

Internet Archive advancedsearch API (https://archive.org/advancedsearch.php).
No API key required; the shared, documented JSON search endpoint is used
with `output=json`.

v2.1.1 removes the broad tier's arbitrary 1979 year ceiling: modern
history is still history, and reputable institutions preserve news
coverage, scientific milestones and cultural events from every decade.
Quality comes from curation and filtering instead of a date wall:

  1. Curated tier — searches a whitelist of historically-focused
     collections (Prelinger, Old Time Radio, Animation and Cartoons,
     Documentary Films, Movies and Films, News and Public Affairs) with
     no date restriction. These collections are themselves curated by
     the Internet Archive, so anything in them is on-topic. (Collection
     identifiers are well-established archive.org collections; archive.org
     is unreachable from the build sandbox, so the list is docs-based.)
  2. Broad historical tier — when the curated tier yields nothing, the
     query is retried across all suitable media types with quality gates
     rather than date gates:
        mediatype gate            — movies/audio/texts/image only
        sort[] = downloads desc   — well-used, usually well-labeled items
                                    first (engagement as a quality signal)
        original dates kept       — fl year/date carry the item's ORIGINAL
                                    publication/creation date, not the
                                    upload date
     plus client-side filters: a real title is required, metadata-stub
     titles are dropped, and a conservative spam blocklist rejects modern
     entertainment/gaming/streaming re-uploads (let's plays, vlogs, ASMR,
     reaction videos...) regardless of what a keyword match suggested.

Both tiers use the documented advancedsearch contract (q, fl[], sort[],
rows, page, output=json). Response shapes are NOT verified against a live
capture from this environment (archive.org has persistently refused the
shared sandbox IP with 429/connection-refused across sessions — verified
for the v2.0, v2.1 and v2.1.1 builds); the shipped fixture is docs-modeled
and clearly labeled. Treat the first live run on your own machine as the
confirmation step.
"""
from __future__ import annotations

from typing import List, Optional, Tuple
from urllib.parse import urlencode

from .base import normalize_page, SourceAdapter, SourceAvailability, TimeMachineItem, Evidence, clean_text
from .config import http_get_json, FetchError, cache_get, cache_set

SEARCH_URL = "https://archive.org/advancedsearch.php"

# Curated, historically-focused collection identifiers (archive.org/collections/<id>).
CURATED_COLLECTIONS = (
    "prelinger", "oldtimeradio", "animationandcartoons",
    "documentaryfilms", "moviesandfilms", "newsandpublicaffairs",
)

MEDIA_BY_TYPE = {
    "movies": "moving_image",
    "audio": "audio",
    "texts": "document",
    "image": "photograph",
}

# Titles that indicate the item is a modern re-upload or a metadata stub.
_NOISE_TITLE_PREFIXES = ("[untitled]", "untitled", "cover image", "back cover")

# Conservative spam blocklist for the broad tier: modern gaming/streaming/
# social-video re-uploads that keyword matches sometimes drag in. Checked
# against title AND subject text. Historical material that legitimately
# contains one of these words in a different sense is rare; anything in the
# curated tier bypasses this list entirely.
_SPAM_TERMS = (
    "minecraft", "roblox", "fortnite", "lets play", "let's play", "gameplay",
    "speedrun", "unboxing", "vlog", "asmr", "tiktok", "reaction video",
    "playthrough", "walkthrough part", "gacha", "skibidi",
)


def _is_spam(title: str, subjects) -> bool:
    blob = f"{title} {' '.join(str(s) for s in (subjects or []))}".lower()
    return any(term in blob for term in _SPAM_TERMS)


def _broad_query(query: str, media: Optional[str]) -> str:
    """Broad tier: media gate only — no date ceiling (v2.1.1). Quality is
    enforced by engagement sorting plus client-side spam/noise filters."""
    if media in ("video", "moving_image"):
        return f"({query}) AND mediatype:(movies)"
    if media in ("audio",):
        return f"({query}) AND mediatype:(audio)"
    if media in ("document", "text"):
        return f"({query}) AND mediatype:(texts)"
    if media in ("photograph", "image", "photo"):
        return f"({query}) AND mediatype:(image)"
    return f"({query}) AND mediatype:(movies OR audio OR texts OR image)"


def _curated_query(query: str, media: Optional[str]) -> str:
    collections = CURATED_COLLECTIONS
    if media in ("video", "moving_image"):
        collections = ("prelinger", "animationandcartoons")
    elif media in ("audio",):
        collections = ("oldtimeradio",)
    col_filter = " OR ".join(f"collection:{c}" for c in collections)
    return f"({query}) AND ({col_filter})"


def _search_url(q: str, limit: int, page: int) -> str:
    params: List[Tuple[str, str]] = [("q", q)]
    for field in ("identifier", "title", "year", "date", "description", "mediatype",
                  "collection", "licenseurl", "subject", "creator", "downloads"):
        params.append(("fl[]", field))
    params.append(("sort[]", "downloads desc"))
    params.extend([
        ("rows", str(limit)),
        ("page", str(page)),
        ("output", "json"),
    ])
    return f"{SEARCH_URL}?{urlencode(params)}"


def _first(value):
    """IA repeated fields arrive as lists; single-valued fields as strings."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _title_is_noise(title: str) -> bool:
    t = title.strip().lower()
    return any(t.startswith(p) for p in _NOISE_TITLE_PREFIXES) or not t


def normalize_ia_doc(doc: dict) -> Optional[TimeMachineItem]:
    try:
        identifier = doc.get("identifier")
        title = clean_text(_first(doc.get("title")), max_len=200)
        if not identifier or not title or _title_is_noise(title):
            return None
        identifier = str(identifier).strip()
        canonical_url = f"https://archive.org/details/{identifier}"

        mediatype = (_first(doc.get("mediatype")) or "").lower()
        media_kind = MEDIA_BY_TYPE.get(mediatype, "document")

        year = _first(doc.get("year"))
        date = clean_text(_first(doc.get("date")), max_len=80)
        date_label = str(year) if year else (date or None)

        description = clean_text(_first(doc.get("description")))

        creators = doc.get("creator") or []
        if isinstance(creators, str):
            creators = [creators]
        people = [clean_text(c, max_len=80) for c in creators[:5] if clean_text(c, max_len=80)]

        subjects = doc.get("subject") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        if _is_spam(title, subjects):
            return None
        topics = [clean_text(s, max_len=60) for s in subjects[:8] if clean_text(s, max_len=60)]

        collection = _first(doc.get("collection"))
        collection = clean_text(collection, max_len=60) if collection else None

        item = TimeMachineItem.build(
            source_id="internetarchive",
            source_item_id=identifier,
            canonical_url=canonical_url,
            title=title,
            media_kind=media_kind,
            description=description,
            dateLabel=date_label,
            sourceInstitution="Internet Archive",
            thumbnailUrl=f"https://archive.org/services/img/{identifier}",
            people=people,
            topics=topics,
        )
        license_url = _first(doc.get("licenseurl"))
        if isinstance(license_url, str) and license_url:
            item.rights = license_url
        item.sourceUrl = canonical_url
        item.evidence = Evidence(institution="Internet Archive", collection=collection,
                                 sourceItemId=identifier, sourceUrl=canonical_url)
        return item
    except Exception:  # noqa: BLE001 — a malformed record must never crash search
        return None


class InternetArchiveSource(SourceAdapter):
    source_id = "internetarchive"
    display_name = "Internet Archive (historical collections)"
    requires_key = False
    # Native advancedsearch page parameter; each page walks a real window.
    deep_pagination = True
    api_docs_url = "https://archive.org/advancedsearch.php"

    def availability(self) -> SourceAvailability:
        return SourceAvailability(True, None)

    def search(self, query: str, media: Optional[str] = None, limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
        limit = max(1, min(int(limit or 10), 25))
        page = normalize_page(page)

        results = self._search_tier(_curated_query(query, media), media, limit, page)
        if not results:
            # Broad historical tier: same query across all suitable media,
            # hard year ceiling, engagement-sorted. If the curated tier is
            # exhausted for this query, the broad tier keeps discovery alive.
            results = self._search_tier(_broad_query(query, media), media, limit, page)
        return results

    def _search_tier(self, scoped_query: str, media: Optional[str],
                     limit: int, page: int) -> List[TimeMachineItem]:
        url = _search_url(scoped_query, limit, page)
        cache_key = f"ia:search:{urlencode({'q': scoped_query, 'limit': limit, 'page': page, 'media': media or ''})}"
        cached = cache_get(cache_key)
        if cached is not None:
            raw = cached
        else:
            try:
                raw = http_get_json(url, timeout=12.0)
            except FetchError:
                # Propagate: the registry classifies failures so discovery
                # can tell temporary trouble from a genuinely empty page.
                raise
            cache_set(cache_key, raw)

        docs = ((raw or {}).get("response") or {}).get("docs") or []
        results = []
        for doc in docs:
            normalized = normalize_ia_doc(doc)
            if normalized:
                results.append(normalized)
            if len(results) >= limit:
                break
        return results
