"""
sources/discovery.py

General archival discovery for "Surprise Me" / continuous TimeFeed —
distinct from the 15 curated featured seeds.

v2.1 turns this from a first-page sampler into a genuine deep-exploration
engine. On top of the v2.0 query-pool roam it now:

  - walks REAL pages per (source, query): every fetch outcome is recorded
    in the persistent DiscoveryState store, so the next batch resumes each
    search where it left off — across restarts — instead of re-reading
    page one forever;
  - detects exhausted searches (a page that returns nothing) and marks
    them, so dead ends stop consuming fetches; exhausted queries are
    lazily refreshed after EXHAUSTED_REFRESH_DAYS because collections
    change;
  - deduplicates against persistent seen-records, so already-delivered
    material and cross-archive duplicates of the same artifact are
    suppressed between sessions (conservative matching: distinct artifacts
    with similar titles are never suppressed);
  - mixes organically discovered topics (mined from authentic archival
    metadata by sources/dynamic_topics.py) into the curated pool, so
    discovery keeps finding NEW subjects beyond the shipped list;
  - diversifies by institution, historical era and media kind.

Nothing is fabricated: every item comes from an existing source adapter or
the existing IIIF normalizer; every discovered topic string came from the
archives' own metadata.

`state` is dependency-injected (serve.py passes the process-wide store;
tests pass fakes or None). With `state=None` behavior degrades gracefully
to the v2.0 in-memory roam — nothing crashes without persistence.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from concurrent.futures import wait as futures_wait

from .base import TimeMachineItem
from .config import FetchError
from .registry import (diversify, diversify_sources, diversify_regions,
                       SourceTempError, shared_executor, source_semaphore)
from .discovery_state import (DiscoveryState, artifact_signature, is_same_artifact,
                              parse_year)
from .dynamic_topics import mine_topics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES_PATH = PROJECT_ROOT / "seeds" / "discovery_queries.json"

# Hard bounds — discovery is a roam, not an exhaustive crawl.
MAX_QUERIES_PER_SURPRISE = 12
MAX_SOURCES_PER_QUERY = 3
RESULTS_PER_SOURCE_QUERY = 6
MAX_IIIF_PER_SURPRISE = 1

# v2.1 dynamic-discovery mix: at most a quarter of each batch's query
# budget comes from organically discovered topics, and each batch mines at
# most this many new topics from returned metadata.
DISCOVERED_TOPIC_SHARE = 0.25
MAX_NEW_TOPICS_PER_BATCH = 6

# v2.1.1: the planning/fetch phases of THIS pipeline run concurrently —
# every (source, query) page fetch in a batch is submitted to a small
# worker pool with per-source pacing and one batch-wide deadline, so the
# slowest archive no longer stretches the whole batch and the fastest
# cannot hog the pool. TimeStream and Gazette inherit this automatically
# (both go through build_feed → discover_surprise).
DISCOVERY_MAX_WORKERS = 8
DISCOVERY_BATCH_DEADLINE_S = 30.0


def load_discovery_queries(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    target = path or DEFAULT_QUERIES_PATH
    try:
        with target.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [q for q in data if isinstance(q, dict) and (q.get("query") or q.get("iiifManifestUrl"))]


def _available_source_ids(list_sources_fn: Callable[[], List[Dict[str, Any]]]) -> List[str]:
    try:
        rows = list_sources_fn() or []
    except Exception:  # noqa: BLE001
        return []
    return [r["id"] for r in rows if r.get("available") and r.get("id")]


def _recent_counts(recent_source_ids: Optional[List[str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for sid in recent_source_ids or []:
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def _sources_for_entry(
    entry: Dict[str, Any],
    available: List[str],
    recent_counts: Optional[Dict[str, int]] = None,
) -> List[str]:
    """Prefer entry.preferredSources when available; else sample live sources.
    Softly order by underrepresented institutions in the recent session."""
    recent_counts = recent_counts or {}
    preferred = [s for s in (entry.get("preferredSources") or []) if s in available]
    if preferred:
        preferred = sorted(preferred, key=lambda s: (recent_counts.get(s, 0), s))
        return preferred[:MAX_SOURCES_PER_QUERY]
    pool = list(available)
    pool.sort(key=lambda s: (recent_counts.get(s, 0), random.random()))
    return pool[:MAX_SOURCES_PER_QUERY]


def _entry_underuse_score(
    entry: Dict[str, Any],
    available: List[str],
    recent_counts: Dict[str, int],
) -> int:
    prefs = [s for s in (entry.get("preferredSources") or []) if s in available]
    usable = prefs or list(available)
    if not usable:
        return 10**9
    return min(recent_counts.get(s, 0) for s in usable)


def _era_bucket(item: TimeMachineItem) -> str:
    """Coarse historical-period bucket from the record's own date label —
    e.g. "c. 1868–73" -> "1800s". Unparseable labels share one bucket so
    they never dominate ordering."""
    year = parse_year(item.dateLabel)
    if year is None:
        return "unknown"
    return f"{(year // 100) * 100}s"


def diversify_eras(items: List[TimeMachineItem], limit: int) -> List[TimeMachineItem]:
    """Round-robin across historical-period buckets so a batch doesn't come
    back all-one-century. Buckets are ordered largest-first; 'unknown' goes
    last so dated material leads. Pure reordering — nothing is relabeled."""
    buckets: Dict[str, List[TimeMachineItem]] = {}
    for item in items:
        buckets.setdefault(_era_bucket(item), []).append(item)
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


def _drop_persistent_dups(
    collected: List[TimeMachineItem],
    state: Optional[DiscoveryState],
    stats: Dict[str, Any],
) -> List[TimeMachineItem]:
    """Filter already-delivered records and cross-archive duplicates.

    Matches inside this batch even when there is no store, and against the
    store when one is present. With no items, returns them unchanged."""
    if not collected:
        return collected
    known: Set[str] = set()
    if state is not None:
        try:
            known = state.known_ids(collected)
        except Exception:  # noqa: BLE001 — state failures must never kill discovery
            known = set()
    fresh: List[TimeMachineItem] = []
    fresh_ids: Set[str] = set()
    accepted: List[Dict[str, Any]] = []
    for item in collected:
        if not item or not item.id:
            continue
        if item.id in known:
            stats["already_seen_dropped"] += 1
            continue
        if item.id in fresh_ids:
            stats["batch_dup_dropped"] += 1
            continue
        signature = artifact_signature(item)
        if signature is not None and any(is_same_artifact(signature, prev) for prev in accepted):
            # Same batch, two archives, one artifact — suppress before either
            # has been written to the delivered-record store.
            stats["cross_archive_dup_dropped"] += 1
            continue
        dup_of = None
        if state is not None:
            try:
                dup_of = state.find_cross_archive_duplicate(item)
            except Exception:  # noqa: BLE001
                dup_of = None
        if dup_of:
            stats["cross_archive_dup_dropped"] += 1
            continue
        fresh.append(item)
        fresh_ids.add(item.id)
        if signature is not None:
            accepted.append(signature)
    return fresh


def _fetch_task(search_fn, source_id: str, query: str, media: Optional[str],
                page: int, sem: threading.Semaphore,
                state: Optional[DiscoveryState] = None):
    """One (source, query, page) fetch inside the batch worker pool. Returns
    (outcome, items, temp_error). Failures are classified, never swallowed
    into empty results: temporary trouble comes back as outcome='temporary'
    (etc.) and the caller records backoff instead of exhaustion."""
    outcome, results, temp = "ok", [], None
    if state is not None:
        try:
            if state.provider_cooldown_remaining(source_id) > 0:
                return "deferred", [], None
        except Exception:  # noqa: BLE001
            pass
    with sem:  # provider pacing: at most N concurrent per archive, process-wide
        if state is not None:
            try:
                if state.provider_cooldown_remaining(source_id) > 0:
                    return "deferred", [], None
            except Exception:  # noqa: BLE001
                pass
        try:
            try:
                results = search_fn(
                    source_id, query, limit=RESULTS_PER_SOURCE_QUERY,
                    media=media, page=page,
                ) or []
            except TypeError:
                # Older search_fn without a page kwarg (hermetic fakes) —
                # degrade gracefully.
                try:
                    results = search_fn(
                        source_id, query, limit=RESULTS_PER_SOURCE_QUERY,
                        media=media,
                    ) or []
                except SourceTempError as e:
                    outcome, temp = e.kind if e.kind != "server_error" else "temporary", e
                except FetchError as e:
                    outcome, temp = e.kind, e
                except Exception:  # noqa: BLE001
                    outcome = "temporary"
        except SourceTempError as e:
            outcome, temp = (e.kind if e.kind != "server_error" else "temporary"), e
        except FetchError as e:
            outcome, temp = e.kind, e
        except Exception:  # noqa: BLE001 — unclassified trouble is temporary
            outcome = "temporary"
    if outcome not in ("ok",) and results:
        # A failure that somehow carried items: trust the items.
        outcome = "ok"
    return outcome, results, temp


def discover_surprise(
    search_fn: Callable[..., List[TimeMachineItem]],
    list_sources_fn: Callable[[], List[Dict[str, Any]]],
    queries: Optional[List[Dict[str, Any]]] = None,
    exclude_ids: Optional[Set[str]] = None,
    limit: int = 20,
    iiif_fn: Optional[Callable[[str], Optional[TimeMachineItem]]] = None,
    recent_source_ids: Optional[List[str]] = None,
    page: int = 1,
    state: Optional[DiscoveryState] = None,
    stats_out: Optional[Dict[str, Any]] = None,
) -> Tuple[List[TimeMachineItem], List[str], List[str]]:
    """
    Returns (items, sources_tried, queries_used).

    items are exclude-filtered, deduped (in-batch AND against the persistent
    store), soft-balanced by institution, historical era and media kind.
    Media/source metadata are never rewritten.

    `page` (>= 1) still advances the query-pool rotation for freshness; the
    per-(source, query) depth comes from the persistent store when one is
    provided. `stats_out`, when a dict is passed, receives transparency
    counters (pages consumed, exhausted marks, dedup drops, mined topics).
    """
    stats: Dict[str, Any] = {
        "pages_consumed": 0,
        "exhausted_marks": 0,
        "skipped_exhausted": 0,
        "already_seen_dropped": 0,
        "cross_archive_dup_dropped": 0,
        "batch_dup_dropped": 0,
        "topics_mined": 0,
        "discovered_topics_used": 0,
        # v2.1.1 taxonomy + concurrency transparency
        "temporary_errors": 0,
        "rate_limited": 0,
        "auth_errors": 0,
        "malformed_responses": 0,
        "deferred_skipped": 0,
        "deadline_misses": 0,
        "concurrent_tasks": 0,
        "fetch_seconds": 0.0,
        "candidates_replayed": 0,
        "candidates_queued": 0,
        "provider_cooldown_skipped": 0,
    }

    exclude_ids = exclude_ids or set()
    curated_pool = list(queries if queries is not None else load_discovery_queries())

    # v2.1: mix organically discovered topics into the working pool.
    if state is not None and curated_pool:
        try:
            budget = max(1, int(MAX_QUERIES_PER_SURPRISE * DISCOVERED_TOPIC_SHARE))
            discovered = state.pick_discovered_topics(budget + 2)
        except Exception:  # noqa: BLE001
            discovered = []
        if discovered:
            discovered = [d for d in discovered if isinstance(d, dict) and d.get("query")]
            curated_pool = list(discovered) + curated_pool
            stats["discovered_topics_used"] = len(discovered)

    pool = curated_pool
    available = _available_source_ids(list_sources_fn)
    recent_counts = _recent_counts(recent_source_ids)
    if not available and not any(e.get("iiifManifestUrl") for e in pool):
        return [], [], []

    if not pool:
        return [], [], []

    collected: List[TimeMachineItem] = []
    seen_ids: Set[str] = set()
    sources_tried: Set[str] = set()
    queries_used: List[str] = []
    iiif_used = 0
    tasks: List[Tuple[str, str, Optional[str], int]] = []

    # Prefer query entries that can hit underrepresented institutions, then
    # shuffle within score bands so Surprise Me stays surprising.
    scored = sorted(
        pool,
        key=lambda e: (_entry_underuse_score(e, available, recent_counts), random.random()),
    )
    # Take a soft front of the underused half, then shuffle the working set.
    front = max(MAX_QUERIES_PER_SURPRISE * 3, min(len(scored), 40))
    order = scored[:front]
    random.shuffle(order)
    # Append a shuffled remainder so we can keep going if the front is dry.
    remainder = scored[front:]
    random.shuffle(remainder)
    order.extend(remainder)

    # Page-based rotation (v2.0): each successive page rolls the working
    # order forward so continuous scrolling explores further into the query
    # pool. With a persistent store, per-query depth comes from the store;
    # the rotation keeps the topic mix fresh on top of that.
    try:
        page_number = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_number = 1
    if page_number > 1 and order:
        roll = ((page_number - 1) * MAX_QUERIES_PER_SURPRISE) % len(order)
        order = order[roll:] + order[:roll]

    attempts = 0
    target = max(1, min(int(limit or 20), 40))
    max_attempts = MAX_QUERIES_PER_SURPRISE * 2

    # ── Phase A: plan every (source, query, page) fetch for this batch ──
    # The loop walks the (rotated, underuse-scored) query pool, resolving
    # the stored cursor per (source, query) and skipping dead or deferred
    # searches. Planning only — all fetching happens once, concurrently,
    # in Phase B.
    planned_cap = MAX_QUERIES_PER_SURPRISE * MAX_SOURCES_PER_QUERY
    for entry in order:
        if attempts >= MAX_QUERIES_PER_SURPRISE and len(tasks) >= max(3, target // 2):
            break
        if attempts >= max_attempts:
            break
        if len(tasks) >= planned_cap:
            break

        label = entry.get("query") or entry.get("iiifManifestUrl") or "discovery"

        manifest_url = entry.get("iiifManifestUrl")
        if manifest_url and iiif_fn and iiif_used < MAX_IIIF_PER_SURPRISE:
            try:
                item = iiif_fn(manifest_url)
            except Exception:  # noqa: BLE001
                item = None
            iiif_used += 1
            attempts += 1
            queries_used.append(label)
            if item and item.id not in exclude_ids and item.id not in seen_ids:
                collected.append(item)
                seen_ids.add(item.id)
                sources_tried.add(item.sourceId or "iiif")
            continue

        if not available:
            continue

        media = entry.get("media")
        attempts += 1
        queries_used.append(label)
        if entry.get("discovered") and state is not None:
            try:
                state.mark_topic_used(entry.get("query") or "")
            except Exception:  # noqa: BLE001
                pass

        for source_id in _sources_for_entry(entry, available, recent_counts):
            sources_tried.add(source_id)
            query = entry.get("query") or ""

            # Resume this (source, query) where the store says it left off.
            # Skips: -1 confirmed exhausted (and not due for refresh);
            # -2 temporarily deferred (backoff / cooldown / rate limit).
            fetch_page = 1
            if state is not None:
                try:
                    fetch_page = state.get_page(source_id, query, media)
                except Exception:  # noqa: BLE001
                    fetch_page = 1
                if fetch_page == -1:
                    stats["skipped_exhausted"] += 1
                    continue
                if fetch_page == -2:
                    stats["deferred_skipped"] += 1
                    continue
                try:
                    if state.provider_cooldown_remaining(source_id) > 0:
                        # Another query already received this archive's
                        # Retry-After. Leave this cursor where it is.
                        stats["provider_cooldown_skipped"] += 1
                        stats["deferred_skipped"] += 1
                        continue
                except Exception:  # noqa: BLE001
                    pass

            tasks.append((source_id, query, media, fetch_page))

    # ── Phase B: fetch every planned page concurrently ──
    # Workers come from the process-wide pool (APP_MAX_WORKERS). Per-archive
    # semaphores are also process-wide, so overlapping TimeStream / Surprise
    # Me / Search requests cannot each open their own set of workers.
    # One batch-wide deadline still bounds this call. The pool is not shut
    # down — shutting it down per batch is what let threads accumulate.
    if tasks:
        stats["concurrent_tasks"] = len(tasks)
        fetch_t0 = time.monotonic()
        pool = shared_executor()
        futures = []
        for (source_id, query, media, fetch_page) in tasks:
            sem = source_semaphore(source_id)
            futures.append(pool.submit(
                _fetch_task, search_fn, source_id, query, media,
                fetch_page, sem, state))
        done, not_done = futures_wait(
            futures, timeout=DISCOVERY_BATCH_DEADLINE_S)
        stats["fetch_seconds"] = round(time.monotonic() - fetch_t0, 2)

        # Unfinished requests: cancelled and put into temporary
        # backoff — never treated as empty pages, never exhausting,
        # and never waited on (the deadline is real).
        done_ids = {id(f) for f in done}
        if not_done:
            stats["deadline_misses"] = len(not_done)
        for fut in not_done:
            fut.cancel()

        for fut, task in zip(futures, tasks):
            source_id, query, media, fetch_page = task
            if id(fut) in done_ids:
                try:
                    outcome, results, temp = fut.result()
                except Exception:  # noqa: BLE001 — belt and braces
                    outcome, results, temp = "temporary", [], None
            else:
                # Missed the batch deadline: classify as temporary
                # backoff without blocking on the hung request.
                outcome, results, temp = "temporary", [], None

            if outcome == "deferred":
                # Provider cooldown or no free slot. Position stays put
                # and this is not a failure streak.
                stats["deferred_skipped"] += 1
                stats["provider_cooldown_skipped"] += 1
                continue

            if state is not None:
                try:
                    _next_page, exhausted = state.record_result(
                        source_id, query, media, RESULTS_PER_SOURCE_QUERY,
                        result_count=len(results or []),
                        outcome=outcome,
                        retry_after=getattr(temp, "retry_after", None),
                    )
                    if exhausted:
                        stats["exhausted_marks"] += 1
                    elif outcome == "ok":
                        stats["pages_consumed"] += 1
                except Exception:  # noqa: BLE001
                    pass

            # Transparency counters per failure class
            if outcome == "temporary":
                stats["temporary_errors"] += 1
            elif outcome == "rate_limited":
                stats["rate_limited"] += 1
            elif outcome == "auth_error":
                stats["auth_errors"] += 1
            elif outcome == "malformed":
                stats["malformed_responses"] += 1

            for item in results or []:
                if not item or not item.id:
                    continue
                if item.id in exclude_ids or item.id in seen_ids:
                    continue
                collected.append(item)
                seen_ids.add(item.id)

    # Records retrieved earlier but not presented. Claimed (removed) so a
    # second overlapping request cannot be handed the same ones. Anything
    # still legitimate and still unpresented is put back afterwards.
    replay: List[TimeMachineItem] = []
    replay_ids: Set[str] = set()
    if state is not None:
        try:
            replay = state.claim_candidates(
                limit=max(target * 4, target), exclude_ids=exclude_ids) or []
        except Exception:  # noqa: BLE001
            replay = []
        replay_ids = {item.id for item in replay if item and item.id}
        stats["candidates_replayed"] = len(replay)

    combined: List[TimeMachineItem] = []
    combined_ids: Set[str] = set()
    for item in list(replay) + list(collected):
        if not item or not item.id or item.id in combined_ids or item.id in exclude_ids:
            continue
        combined.append(item)
        combined_ids.add(item.id)

    # Persistent dedup plus in-batch cross-archive matches.
    fresh = _drop_persistent_dups(combined, state, stats)
    fresh_ids = {item.id for item in fresh if item and item.id}

    # Mine topics from records retrieved this batch, not from replays.
    newly_fetched = [item for item in fresh if item.id not in replay_ids]
    if state is not None and newly_fetched:
        try:
            mined = mine_topics(newly_fetched, queries_used, max_topics=MAX_NEW_TOPICS_PER_BATCH)
            added = state.upsert_discovered_topics(mined, max_add=MAX_NEW_TOPICS_PER_BATCH)
            stats["topics_mined"] = added
        except Exception:  # noqa: BLE001
            pass

    presented: List[TimeMachineItem] = []
    if fresh:
        preferred = [item for item in fresh if item.id in replay_ids]
        others = [item for item in fresh if item.id not in replay_ids]
        mix_pool = preferred if len(preferred) >= target else preferred + others
        balanced = diversify_sources(mix_pool, max(target * 2, target), recent_source_ids=recent_source_ids)
        era_mixed = diversify_eras(balanced, target)
        region_mixed = diversify_regions(era_mixed, target)
        presented = diversify(region_mixed, target)

    presented_ids = {item.id for item in presented if item and item.id}
    if state is not None:
        try:
            # Newly retrieved records that lost the presentation cut stay
            # available. Claimed replays that were deduped away do not go
            # back. Claimed replays that still weren't shown go back.
            undelivered = [
                item for item in fresh
                if item.id not in presented_ids and item.id not in replay_ids
            ]
            put_back = [
                item for item in replay
                if item.id in fresh_ids and item.id not in presented_ids
            ]
            stats["candidates_queued"] = state.enqueue_candidates(undelivered + put_back)
        except Exception:  # noqa: BLE001
            pass

    if isinstance(stats_out, dict):
        stats_out.clear()
        stats_out.update(stats)

    if not presented:
        return [], sorted(sources_tried), queries_used
    return presented, sorted(sources_tried), queries_used
