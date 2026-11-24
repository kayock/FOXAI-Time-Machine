"""
sources/registry.py

Central list of query-based source adapters (IIIF and the institutional
page fallback are invoked directly by URL, not through this registry, since
they aren't "search a collection" sources — see serve.py).

v2.1: seventeen institutional sources — the eight originals, the seven
v2.0 additions (Cleveland, V&A, Wikimedia Commons, Internet Archive,
Gallica, Rijksmuseum, Harvard) and two v2.1 additions (Wellcome Collection,
J. Paul Getty Museum — via its documented SPARQL service). Eleven of the
seventeen are keyless. `page` parameters flow through to adapters that
support real API pagination so deep exploration goes further into each
archive instead of re-reading first pages; each adapter declares its
pagination capability (`deep_pagination`) and API docs URL for
/api/sources and the capability matrix.

The registry is defensive on purpose: a single source raising, timing out,
or being misconfigured must never take down search or the feed for the
other sources (brief section 22). Multi-source search runs with bounded,
provider-aware concurrency (a small ThreadPoolExecutor with a per-batch
time budget) so one slow archive cannot stall the others.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional

from .base import TimeMachineItem
from .config import FetchError
from .nasa import NasaSource
from .loc import LocSource
from .smithsonian import SmithsonianSource
from .dpla import DplaSource
from .europeana import EuropeanaSource
from .nara import NaraSource
from .met import MetSource
from .aic import AicSource
from .cma import CmaSource
from .vam import VamSource
from .commons import CommonsSource
from .internetarchive import InternetArchiveSource
from .gallica import GallicaSource
from .rijksmuseum import RijksmuseumSource
from .harvard import HarvardSource
from .wellcome import WellcomeSource
from .getty import GettySource

ALL_SOURCES = [
    NasaSource(),
    LocSource(),
    MetSource(),
    AicSource(),
    CmaSource(),
    VamSource(),
    SmithsonianSource(),
    DplaSource(),
    EuropeanaSource(),
    NaraSource(),
    InternetArchiveSource(),
    GallicaSource(),
    CommonsSource(),
    RijksmuseumSource(),
    HarvardSource(),
    WellcomeSource(),
    GettySource(),
]

_BY_ID = {s.source_id: s for s in ALL_SOURCES}

# Bounded concurrency for multi-source search: small pool, short wall-clock
# budget. Individual adapters already carry their own HTTP timeouts; this
# budget is the outer guard so one hung archive delays nobody for long.
SEARCH_MAX_WORKERS = 6
SEARCH_BATCH_TIMEOUT_S = 30.0

# Per-source concurrency cap used by the discovery pipeline: at most two
# in-flight requests per archive regardless of pool size — provider-friendly
# pacing that still lets six archives proceed in parallel.
PER_SOURCE_CONCURRENCY = 2

# One process-wide pool. Overlapping TimeStream, Surprise Me, Search and
# feed requests share these workers so threads cannot pile up per batch.
APP_MAX_WORKERS = 8
_EXECUTOR = ThreadPoolExecutor(max_workers=APP_MAX_WORKERS, thread_name_prefix="foxai-net")
_SOURCE_SEMS: Dict[str, threading.BoundedSemaphore] = {}
_SEM_GUARD = threading.Lock()
_cooldown_checker: Optional[Callable[[str], bool]] = None


def shared_executor() -> ThreadPoolExecutor:
    return _EXECUTOR


def source_semaphore(source_id: str) -> threading.BoundedSemaphore:
    """Process-wide per-archive slot. Shared by every in-flight request."""
    key = source_id or ""
    with _SEM_GUARD:
        sem = _SOURCE_SEMS.get(key)
        if sem is None:
            sem = threading.BoundedSemaphore(PER_SOURCE_CONCURRENCY)
            _SOURCE_SEMS[key] = sem
        return sem


def set_provider_cooldown_checker(checker: Optional[Callable[[str], bool]]) -> None:
    """Installed by serve.py so Search honors a cooldown discovery recorded."""
    global _cooldown_checker
    _cooldown_checker = checker


def provider_is_cooling(source_id: str) -> bool:
    checker = _cooldown_checker
    if checker is None or not source_id:
        return False
    try:
        return bool(checker(source_id))
    except Exception:  # noqa: BLE001
        return False


class SourceTempError(Exception):
    """A failed source query, classified from the underlying FetchError.
    Raised (instead of returning []) so the discovery pipeline can record a
    temporary outcome — backoff, no exhaustion — while public helpers like
    search_source() still fail soft to [] as always."""

    def __init__(self, source_id: str, kind: str,
                 status: Optional[int] = None, retry_after: Optional[float] = None,
                 detail: str = ""):
        super().__init__(f"{source_id}: {kind}" + (f" (HTTP {status})" if status else ""))
        self.source_id = source_id
        self.kind = kind            # temporary | rate_limited | auth_error | malformed | server_error
        self.status = status
        self.retry_after = retry_after
        self.detail = detail


def list_sources() -> List[Dict]:
    out = []
    for s in ALL_SOURCES:
        try:
            avail = s.availability()
        except Exception as e:  # noqa: BLE001 — a broken availability check must not break /api/sources
            avail_dict = {"available": False, "reason": f"internal error: {e}"}
        else:
            avail_dict = avail.to_dict()
        out.append({
            "id": s.source_id,
            "name": s.display_name,
            "requiresKey": s.requires_key,
            "deepPagination": bool(getattr(s, "deep_pagination", False)),
            "apiDocs": getattr(s, "api_docs_url", None),
            **avail_dict,
        })
    return out


def search_source(source_id: str, query: str, media: Optional[str] = None, limit: int = 10,
                  page: int = 1) -> List[TimeMachineItem]:
    source = _BY_ID.get(source_id)
    if not source:
        return []
    try:
        if not source.availability().available:
            return []
        try:
            return source.search(query, media=media, limit=limit, page=page)
        except TypeError:
            # Older adapter without a page parameter — degrade gracefully.
            return source.search(query, media=media, limit=limit)
    except Exception:  # noqa: BLE001 — one broken source must not break search
        return []


def _search_one(source, query: str, media: Optional[str], limit: int, page: int) -> List[TimeMachineItem]:
    """One adapter query, with failure classification. Transport/HTTP
    failures raise SourceTempError (discovery records backoff); everything
    else is normalized. Used by search_all() and the discovery pipeline."""
    try:
        if not source.availability().available:
            return []
        try:
            return source.search(query, media=media, limit=limit, page=page)
        except TypeError:
            return source.search(query, media=media, limit=limit)
    except FetchError as e:
        raise SourceTempError(source.source_id, e.kind, status=e.status,
                              retry_after=e.retry_after, detail=str(e)) from e
    except SourceTempError:
        raise
    except Exception as e:  # noqa: BLE001 — one broken source must not break search
        raise SourceTempError(source.source_id, "temporary", detail=str(e)) from e


def search_all(query: str, media: Optional[str] = None, limit_per_source: int = 6,
               page: int = 1) -> List[TimeMachineItem]:
    """Query every available source and merge results, with bounded
    concurrency (SEARCH_MAX_WORKERS workers, SEARCH_BATCH_TIMEOUT_S budget).
    Sources that are unavailable (missing key, network down, etc.) are
    silently skipped — they simply contribute nothing, per brief section 22.
    `page` flows to adapters that support real API pagination, so
    repeated/continuous discovery can go deeper than the first page of each
    archive."""
    available = []
    for source in ALL_SOURCES:
        try:
            if source.availability().available:
                available.append(source)
        except Exception:  # noqa: BLE001
            continue
    if not available:
        return []

    def _paced(source):
        if provider_is_cooling(source.source_id):
            return []
        sem = source_semaphore(source.source_id)
        with sem:
            if provider_is_cooling(source.source_id):
                return []
            return _search_one(source, query, media, limit_per_source, page)

    results: List[TimeMachineItem] = []
    pool = shared_executor()
    futures = [pool.submit(_paced, source) for source in available]
    # One deadline for the whole batch: a hung archive cannot consume
    # the budget of the others (each also has its own HTTP timeouts).
    # The pool itself is process-wide and is not shut down here.
    done, not_done = wait(futures, timeout=SEARCH_BATCH_TIMEOUT_S)
    for future in not_done:
        future.cancel()
    for future in done:
        try:
            results.extend(future.result())
        except Exception:  # noqa: BLE001 — broken source contributes nothing
            continue
    return results


def search_source_strict(source_id: str, query: str, media: Optional[str] = None,
                         limit: int = 10, page: int = 1) -> List[TimeMachineItem]:
    """Like search_source() but failures raise SourceTempError instead of
    returning [] — the discovery pipeline needs the distinction between a
    genuinely empty page and a failed request. Signature-compatible with
    search_source (including the page kwarg)."""
    return _search_one(_BY_ID.get(source_id), query, media, limit, page)


def diversify(items: List[TimeMachineItem], limit: int) -> List[TimeMachineItem]:
    """
    Simple diversity rule (brief section 19): don't let the feed become
    fifteen photographs. Round-robin across mediaKind buckets rather than
    taking results in raw source order. Not a recommendation engine —
    just a fair interleave.
    """
    buckets: Dict[str, List[TimeMachineItem]] = {}
    for item in items:
        buckets.setdefault(item.mediaKind, []).append(item)

    ordered_kinds = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
    result: List[TimeMachineItem] = []
    while len(result) < limit and any(buckets[k] for k in ordered_kinds):
        for kind in ordered_kinds:
            if buckets[kind]:
                result.append(buckets[kind].pop(0))
            if len(result) >= limit:
                break
    return result


# Coarse region mapping for geographic diversity (v2.1.1). Applied ONLY to
# items carrying places metadata; everything else shares one bucket so it
# never dominates ordering. Compact by design — it exists to interleave
# records from different parts of the world, not to classify the world.
_REGION_BY_COUNTRY = {
    "africa": {"egypt", "morocco", "nigeria", "kenya", "ethiopia", "south africa",
               "ghana", "senegal", "tanzania", "uganda", "algeria", "tunisia",
               "sudan", "mali", "zimbabwe", "congo"},
    "east asia": {"china", "japan", "korea", "taiwan", "mongolia", "hong kong",
                  "tibet", "okinawa"},
    "south southeast asia": {"india", "pakistan", "bangladesh", "sri lanka",
                             "nepal", "indonesia", "philippines", "vietnam",
                             "thailand", "cambodia", "burma", "myanmar", "laos",
                             "malaysia", "singapore"},
    "middle east": {"iran", "iraq", "turkey", "israel", "palestine", "syria",
                    "lebanon", "jordan", "saudi arabia", "yemen", "afghanistan",
                    "mesopotamia", "persia", "ottoman"},
    "europe": {"france", "germany", "italy", "spain", "portugal", "england",
               "scotland", "wales", "ireland", "netherlands", "belgium",
               "austria", "switzerland", "poland", "russia", "sweden", "norway",
               "denmark", "finland", "greece", "hungary", "czech", "romania",
               "ukraine", "croatia", "serbia", "bulgaria", "lithuania",
               "latvia", "estonia", "iceland"},
    "latin america": {"mexico", "brazil", "argentina", "peru", "chile",
                      "colombia", "bolivia", "ecuador", "guatemala", "cuba",
                      "venezuela", "uruguay", "paraguay", "panama",
                      "costa rica"},
    "north america": {"united states", "usa", "canada", "alaska", "hawaii",
                      "new york", "california", "washington dc", "texas",
                      "illinois", "massachusetts", "virginia"},
    "oceania": {"australia", "new zealand", "fiji", "papua new guinea",
                "samoa", "tonga"},
    "central asia": {"kazakhstan", "uzbekistan", "tajikistan", "kyrgyzstan",
                     "turkmenistan", "siberia"},
}


def region_bucket(item) -> str:
    """Coarse region bucket from the record's own places metadata (first
    matching place wins). Items without usable places share 'unknown'."""
    for place in (item.places or []):
        text = normalize_place(place)
        if not text:
            continue
        for region, countries in _REGION_BY_COUNTRY.items():
            for country in countries:
                if country in text:
                    return region
    return "unknown"


def normalize_place(value) -> str:
    if value is None:
        return ""
    text = str(value).lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def diversify_regions(items: List[TimeMachineItem], limit: int) -> List[TimeMachineItem]:
    """Round-robin across coarse geographic buckets so a batch doesn't come
    back all-one-region. Buckets ordered largest-first; 'unknown' last so
    geographically-labeled material leads. Pure reordering — nothing is
    relabeled and items without places metadata are never penalized."""
    buckets: Dict[str, List[TimeMachineItem]] = {}
    for item in items:
        buckets.setdefault(region_bucket(item), []).append(item)
    ordered_buckets = sorted(
        buckets.keys(),
        key=lambda k: (k == "unknown", -len(buckets[k]), k),
    )
    result: List[TimeMachineItem] = []
    while len(result) < limit and any(buckets[k] for k in ordered_buckets):
        for kind in ordered_buckets:
            if buckets[kind]:
                result.append(buckets[kind].pop(0))
            if len(result) >= limit:
                break
    return result


def diversify_sources(
    items: List[TimeMachineItem],
    limit: int,
    recent_source_ids: Optional[List[str]] = None,
) -> List[TimeMachineItem]:
    """
    Soft institution balancing for discovery batches. Prefer multiple
    sources when valid results exist; gently prefer underrepresented
    institutions vs. recent session usage. Never invents items — only
    reorders/selects among real results. Soft caps yield when alternatives
    run out so excellent single-source batches are still allowed.
    """
    if not items:
        return []
    limit = max(1, int(limit or 1))
    recent_counts: Dict[str, int] = {}
    for sid in recent_source_ids or []:
        if sid:
            recent_counts[sid] = recent_counts.get(sid, 0) + 1

    by_source: Dict[str, List[TimeMachineItem]] = {}
    for item in items:
        sid = item.sourceId or "unknown"
        by_source.setdefault(sid, []).append(item)

    soft_cap = max(2, (limit + 1) // 2)
    sources = sorted(
        by_source.keys(),
        key=lambda s: (recent_counts.get(s, 0), -len(by_source[s]), s),
    )
    result: List[TimeMachineItem] = []

    while len(result) < limit and any(by_source[s] for s in sources):
        progressed = False
        for sid in sources:
            if not by_source[sid]:
                continue
            already = sum(1 for i in result if (i.sourceId or "unknown") == sid)
            alternatives = any(by_source[o] for o in sources if o != sid)
            if already >= soft_cap and alternatives:
                continue
            result.append(by_source[sid].pop(0))
            progressed = True
            if len(result) >= limit:
                break
        if not progressed:
            for sid in sources:
                while by_source[sid] and len(result) < limit:
                    result.append(by_source[sid].pop(0))
            break
    return result
