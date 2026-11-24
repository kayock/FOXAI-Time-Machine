"""
sources/discovery_state.py

Persistent discovery state for v2.1 deep historical exploration — a small,
bounded, standard-library-only SQLite store (sqlite3, WAL mode) that lets
FOXAI Time Machine remember where exploration left off, across restarts
and browsing sessions, without downloading or mirroring anything.

Three kinds of state live here:

1. Query progress — one row per (source_id, query, media) cursor:
     page          the next API page to fetch for this (source, query)
     exhausted     1 when the archive stopped returning results for this
                   query, so discovery should roam elsewhere
     last_count / last_requested / last_explored_at / last_status
   A page advances only when a real batch was retrieved. Exhausted queries
   are lazily refreshed (page reset, exhausted cleared) after
   EXHAUSTED_REFRESH_DAYS, because archival collections do change —
   previously exhausted searches are periodically re-explored.

2. Seen records — bounded persistent record memory for deduplication:
   every normalized item delivered to the feed is recorded with its
   normalized title, creator, media kind, year and canonical URL, so the
   same record is not re-served after restarts and the same artifact
   published by two different archives is recognized. Matching is
   deliberately conservative (exact normalized URL, or exact normalized
   title + media kind + year + creator) so genuinely distinct artifacts
   that merely share similar titles are never suppressed.

3. Discovered topics — organic search topics mined from authentic
   archival metadata (see sources/dynamic_topics.py), tracked with
   provenance and usage counters so obscure topics get fair play and
   already-explored topics yield to fresh ones.

Storage is bounded: seen-record rows are pruned oldest-first beyond
SEEN_MAX_ROWS, discovered topics beyond TOPIC_MAX_ROWS. The database
defaults to state/discovery_state.db under the project root (override
with the FOXAI_STATE_DIR environment variable) and is created on demand.
All access is serialized through one lock; SQLite WAL allows readers to
proceed without blocking. Nothing here is authoritative data — every
record it stores is metadata about material the archives themselves
returned, never fabricated content.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .base import TimeMachineItem

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = PROJECT_ROOT / "state"

def _env_cap(name: str, default: int) -> int:
    """Read a row cap from the environment; invalid values fall back to the
    default rather than breaking startup."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(100, value)


# Bounds and policies. Defaults scale to long-duration operation (weeks of
# continuous discovery); each cap is overridable via environment variables
# without touching code. Pruning only ever fires when a cap is EXCEEDED —
# historical discovery memory is never trimmed merely because it is old.
SEEN_MAX_ROWS = _env_cap("FOXAI_SEEN_MAX_ROWS", 1_000_000)        # dedup memory
SEEN_PRUNE_MARGIN = 5_000      # prune down to this margin when over cap
TOPIC_MAX_ROWS = _env_cap("FOXAI_TOPIC_MAX_ROWS", 20_000)         # organic topics
EXHAUSTED_REFRESH_DAYS = 30    # re-explore exhausted searches after this
PROGRESS_MAX_ROWS = _env_cap("FOXAI_PROGRESS_MAX_ROWS", 250_000)  # cursors
# Undelivered-but-legitimate records waiting for a later batch. Bounded so
# a long session cannot grow this queue without limit.
CANDIDATE_MAX_ROWS = _env_cap("FOXAI_CANDIDATE_MAX_ROWS", 20_000)

# Temporary-failure backoff (never exhausts a search): exponential, capped.
TEMP_BACKOFF_BASE_S = 60       # first retry after ~1 minute
TEMP_BACKOFF_MAX_S = 3600      # at most one hour between retries
RATE_LIMIT_MIN_COOLDOWN_S = 30 # minimum cooldown on HTTP 429
AUTH_ERROR_COOLDOWN_S = 1800   # missing/invalid key: don't hammer for 30 min
MALFORMED_BACKOFF_S = 120      # truncated/garbled responses: retry soon-ish

_YEAR_RE = re.compile(r"\b(\d{3,4})\b")
_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

_STOPWORDS = {
    "the", "and", "of", "a", "an", "in", "on", "at", "to", "for", "from",
    "by", "with", "is", "are", "this", "that", "de", "la", "der", "des",
    "und", "et", "le", "les", "vol", "volume", "no", "series", "collection",
    "library", "museum", "archive", "archives", "national", "plate", "plates",
}


def normalize_text(value) -> str:
    """Lowercase, strip punctuation and collapse whitespace — the shared
    normalization for titles, creators and topics."""
    if value is None:
        return ""
    text = str(value).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_url(url) -> str:
    """Canonical-URL normalization: lowercase host, drop scheme, query and
    fragment, drop trailing slash — enough to catch the same artifact page
    served with trivially different addresses."""
    if not url:
        return ""
    text = str(url).strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("#", 1)[0].split("?", 1)[0]
    return text.rstrip("/")


def artifact_signature(item) -> Optional[Dict[str, Any]]:
    """Comparable identity for conservative cross-archive matching.
    Returns None when the item has no title — a blank title is never
    enough to call two records the same artifact."""
    if item is None:
        return None
    title = normalize_text(getattr(item, "title", None))
    if not title:
        return None
    people = getattr(item, "people", None) or []
    creator = normalize_text(people[0] if people else "")
    evidence = getattr(item, "evidence", None)
    canonical = getattr(item, "sourceUrl", None) or (
        getattr(evidence, "sourceUrl", None) if evidence else None
    )
    return {
        "id": getattr(item, "id", None) or "",
        "source": getattr(item, "sourceId", None) or "",
        "title": title,
        "media": (getattr(item, "mediaKind", None) or "").strip().lower(),
        "year": parse_year(getattr(item, "dateLabel", None)),
        "creator": creator,
        "url": normalize_url(canonical),
    }


def is_same_artifact(left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> bool:
    """True only for a strong match: same id, the same canonical URL, or
    the same title + media kind + year + a known creator on both sides
    from two different archives. Similar titles alone never match."""
    if not left or not right:
        return False
    if left.get("id") and left.get("id") == right.get("id"):
        return True
    if left.get("url") and left.get("url") == right.get("url"):
        return True
    if (left.get("source") or "") == (right.get("source") or ""):
        return False
    if not left.get("creator") or not right.get("creator"):
        return False
    return (
        left.get("title") == right.get("title")
        and left.get("media") == right.get("media")
        and left.get("year") == right.get("year")
        and left.get("creator") == right.get("creator")
    )


def parse_year(date_label) -> Optional[int]:
    """Best-effort year extraction from a human date label ("1898",
    "c. 1868–73", "1943-06-06"). Returns None when nothing looks like a
    year — never guesses."""
    if not date_label:
        return None
    match = _YEAR_RE.search(str(date_label))
    if not match:
        return None
    year = int(match.group(1))
    if 1 <= year <= 2200:
        return year
    return None


def _significant_terms(text: str) -> List[str]:
    return [t for t in normalize_text(text).split() if t and t not in _STOPWORDS]


class DiscoveryState:
    """Thread-safe persistent discovery state. Instantiate once per process
    (serve.py does); tests may create fresh instances on temp paths."""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            state_dir = Path(os.environ.get("FOXAI_STATE_DIR") or DEFAULT_STATE_DIR)
            state_dir.mkdir(parents=True, exist_ok=True)
            db_path = state_dir / "discovery_state.db"
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False, timeout=10.0
        )
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass  # WAL is an optimization, never a requirement
        try:
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self._migrate()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS query_progress (
                    source_id        TEXT NOT NULL,
                    query_key        TEXT NOT NULL,
                    media            TEXT NOT NULL DEFAULT '',
                    page             INTEGER NOT NULL DEFAULT 1,
                    exhausted        INTEGER NOT NULL DEFAULT 0,
                    last_count       INTEGER NOT NULL DEFAULT 0,
                    last_requested   INTEGER NOT NULL DEFAULT 0,
                    last_explored_at REAL NOT NULL DEFAULT 0,
                    last_status      TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (source_id, query_key, media)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_records (
                    record_id       TEXT PRIMARY KEY,
                    source_id       TEXT NOT NULL,
                    source_item_id  TEXT,
                    title_norm      TEXT NOT NULL DEFAULT '',
                    creator_norm    TEXT NOT NULL DEFAULT '',
                    media_kind      TEXT NOT NULL DEFAULT '',
                    year            INTEGER,
                    url_norm        TEXT NOT NULL DEFAULT '',
                    first_seen_at   REAL NOT NULL,
                    times_seen      INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_url ON seen_records (url_norm)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_title ON seen_records (title_norm, media_kind)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    topic_key       TEXT PRIMARY KEY,
                    query           TEXT NOT NULL,
                    origin          TEXT NOT NULL DEFAULT 'discovered',
                    provenance      TEXT NOT NULL DEFAULT '',
                    preferred_sources TEXT NOT NULL DEFAULT '[]',
                    created_at      REAL NOT NULL,
                    last_used_at    REAL NOT NULL DEFAULT 0,
                    times_used      INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Safe schema migration for databases created by v2.1: add the
            # columns this version needs without touching existing data.
            existing = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(query_progress)").fetchall()}
            if "error_count" not in existing:
                self._conn.execute(
                    "ALTER TABLE query_progress ADD COLUMN error_count INTEGER NOT NULL DEFAULT 0")
            if "next_attempt_at" not in existing:
                self._conn.execute(
                    "ALTER TABLE query_progress ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0")
            seen_existing = {r["name"] for r in self._conn.execute(
                "PRAGMA table_info(seen_records)").fetchall()}
            if "status" not in seen_existing:
                self._conn.execute(
                    "ALTER TABLE seen_records ADD COLUMN status TEXT NOT NULL DEFAULT 'delivered'")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_records (first_seen_at)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_seen_status ON seen_records (status)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_progress_next_attempt ON query_progress (next_attempt_at)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_cooldowns (
                    source_id TEXT PRIMARY KEY,
                    until_at  REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_queue (
                    record_id   TEXT PRIMARY KEY,
                    source_id   TEXT NOT NULL DEFAULT '',
                    payload     TEXT NOT NULL,
                    enqueued_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidate_enqueued ON candidate_queue (enqueued_at)")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.commit()
            cur.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()

    # ------------------------------------------------------------------
    # 1. Query progress (per source/query deep exploration cursors)
    # ------------------------------------------------------------------
    def get_page(self, source_id: str, query: str, media: Optional[str]) -> int:
        """Page to fetch next for this (source, query). Returns:
          >= 1   fetch this page now
          -1     confirmed exhausted (lazily refreshed after
                 EXHAUSTED_REFRESH_DAYS — collections change)
          -2     temporarily deferred (network failure / rate limit cooldown /
                 backoff after malformed responses) — retry later, position
                 preserved; NEVER converted into exhaustion.
        """
        key = normalize_text(query)
        media_key = (media or "").strip().lower()
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT page, exhausted, last_explored_at, next_attempt_at "
                "FROM query_progress WHERE source_id=? AND query_key=? AND media=?",
                (source_id, key, media_key),
            ).fetchone()
            if row is None:
                return 1
            if row["exhausted"]:
                age_days = (now - row["last_explored_at"]) / 86400.0
                if age_days >= EXHAUSTED_REFRESH_DAYS:
                    self._conn.execute(
                        "UPDATE query_progress SET page=1, exhausted=0, last_status='refreshed', "
                        "error_count=0, next_attempt_at=0 "
                        "WHERE source_id=? AND query_key=? AND media=?",
                        (source_id, key, media_key),
                    )
                    self._conn.commit()
                    return 1
                return -1  # still exhausted → skip this query/source pair
            if row["next_attempt_at"] and now < float(row["next_attempt_at"]):
                return -2  # in temporary backoff / cooldown → skip this batch
            return max(1, int(row["page"]))

    def is_exhausted(self, source_id: str, query: str, media: Optional[str]) -> bool:
        return self.get_page(source_id, query, media) == -1

    def record_result(self, source_id: str, query: str, media: Optional[str],
                      requested: int, result_count: int = 0,
                      outcome: str = "ok", retry_after: Optional[float] = None
                      ) -> Tuple[int, bool]:
        """Record one fetch outcome and return (next_page, exhausted).

        Outcomes — the taxonomy that keeps temporary trouble from ever
        exhausting a legitimate discovery path:
          ok            a page arrived with results → advance the cursor
          empty         a well-formed page arrived with zero records → the
                        archive has no more for this query → exhausted
          temporary     network failure / timeout / 5xx → exponential backoff,
                        page preserved, error_count incremented
          rate_limited  HTTP 429 → cooldown (Retry-After honored when sent)
          auth_error    HTTP 401/403 → long cooldown (missing/invalid key)
          malformed     response arrived but was unparseable → short backoff
        """
        key = normalize_text(query)
        media_key = (media or "").strip().lower()
        now = time.time()
        # A caller that reports ok with zero records has observed a
        # well-formed empty page — that IS exhaustion. Only classified
        # failures (temporary/rate_limited/auth_error/malformed) avoid it.
        if outcome == "ok" and int(result_count) <= 0:
            outcome = "empty"
        with self._lock:
            row = self._conn.execute(
                "SELECT page, error_count FROM query_progress "
                "WHERE source_id=? AND query_key=? AND media=?",
                (source_id, key, media_key),
            ).fetchone()
            current_page = max(1, int(row["page"])) if row else 1
            error_count = int(row["error_count"]) if row else 0

            exhausted = outcome == "empty"
            next_page = current_page
            next_error = error_count
            next_attempt = 0.0
            state_status = outcome

            if outcome == "ok":
                next_page = current_page + 1
                next_error = 0
                state_status = "ok"
            elif exhausted:
                next_page = current_page  # stays; exhausted flag marks it
                next_error = 0
                state_status = "exhausted"
            elif outcome == "temporary":
                next_error = error_count + 1
                # A provider-supplied Retry-After is a requested wait, not a
                # hint we may shorten to the generic one-hour ceiling.
                if retry_after is not None and float(retry_after) > 0:
                    backoff = float(retry_after)
                else:
                    backoff = min(TEMP_BACKOFF_BASE_S * (2 ** min(next_error - 1, 6)),
                                  TEMP_BACKOFF_MAX_S)
                next_attempt = now + backoff
            elif outcome == "rate_limited":
                next_error = error_count + 1
                asked_wait = float(retry_after) if retry_after else 0.0
                if asked_wait < 0:
                    asked_wait = 0.0
                # Floor only lengthens a missing or tiny delay. It never
                # truncates a longer provider-requested cooldown.
                cooldown = max(asked_wait, RATE_LIMIT_MIN_COOLDOWN_S)
                next_attempt = now + cooldown
            elif outcome == "auth_error":
                next_error = error_count + 1
                next_attempt = now + AUTH_ERROR_COOLDOWN_S
            elif outcome == "malformed":
                next_error = error_count + 1
                next_attempt = now + MALFORMED_BACKOFF_S
            else:  # unknown outcome — treat as temporary, never as exhaustion
                next_error = error_count + 1
                next_attempt = now + TEMP_BACKOFF_BASE_S

            self._conn.execute(
                """
                INSERT INTO query_progress
                    (source_id, query_key, media, page, exhausted, last_count,
                     last_requested, last_explored_at, last_status,
                     error_count, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, query_key, media) DO UPDATE SET
                    page=excluded.page,
                    exhausted=excluded.exhausted,
                    last_count=excluded.last_count,
                    last_requested=excluded.last_requested,
                    last_explored_at=excluded.last_explored_at,
                    last_status=excluded.last_status,
                    error_count=excluded.error_count,
                    next_attempt_at=excluded.next_attempt_at
                """,
                (source_id, key, media_key, next_page, int(exhausted),
                 int(result_count), int(requested), now, state_status,
                 next_error, next_attempt),
            )
            if next_attempt > now and outcome in ("rate_limited", "temporary") and (
                outcome == "rate_limited" or (retry_after is not None and float(retry_after or 0) > 0)
            ):
                # Provider cooldown is shared by every query to this archive
                # so overlapping requests do not ignore a Retry-After that
                # another in-flight search just observed.
                self._conn.execute(
                    """
                    INSERT INTO provider_cooldowns (source_id, until_at) VALUES (?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        until_at = MAX(provider_cooldowns.until_at, excluded.until_at)
                    """,
                    (source_id, next_attempt),
                )
            self._conn.commit()
            self._prune_table("query_progress", PROGRESS_MAX_ROWS,
                              "ORDER BY last_explored_at ASC")
        return next_page, exhausted

    def reset_all(self) -> None:
        """Clear exploration cursors (not seen-records, not topics)."""
        with self._lock:
            self._conn.execute("DELETE FROM query_progress")
            self._conn.commit()

    def progress_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, COUNT(*) AS queries, "
                "SUM(exhausted) AS exhausted, SUM(page - 1) AS pages_consumed, "
                "MAX(last_explored_at) AS last_explored_at "
                "FROM query_progress GROUP BY source_id"
            ).fetchall()
            return {
                "sources": {
                    r["source_id"]: {
                        "queries": int(r["queries"] or 0),
                        "exhausted": int(r["exhausted"] or 0),
                        "pagesConsumed": int(r["pages_consumed"] or 0),
                        "lastExploredAt": r["last_explored_at"],
                    }
                    for r in rows
                },
                "totalQueries": sum(int(r["queries"] or 0) for r in rows),
                "totalExhausted": sum(int(r["exhausted"] or 0) for r in rows),
                "totalPagesConsumed": sum(int(r["pages_consumed"] or 0) for r in rows),
            }

    # ------------------------------------------------------------------
    # 2. Seen records (persistent dedup memory)
    # ------------------------------------------------------------------
    def _row_for_item(self, item) -> Tuple:
        canonical = item.sourceUrl or (item.evidence.sourceUrl if item.evidence else None)
        return (
            item.id,
            item.sourceId or "",
            item.sourceItemId or "",
            normalize_text(item.title),
            normalize_text((item.people or [""])[0] if item.people else ""),
            item.mediaKind or "",
            parse_year(item.dateLabel),
            normalize_url(canonical),
        )

    def record_seen(self, items, status: str = "delivered") -> int:
        """Persist records into long-term memory. `status` distinguishes
        lifecycle states ('delivered' is written by the feed paths); only
        delivered records suppress future discovery. Retrieved-but-never-
        delivered records are intentionally NOT recorded here, so anything
        fetched but filtered out of a batch remains fully discoverable
        later. Returns the number newly recorded."""
        now = time.time()
        with self._lock:
            newly = 0
            for item in items or []:
                if not item or not item.id:
                    continue
                exists = self._conn.execute(
                    "SELECT 1 FROM seen_records WHERE record_id=?", (item.id,)
                ).fetchone()
                row = self._row_for_item(item)
                if exists:
                    self._conn.execute(
                        "UPDATE seen_records SET times_seen = times_seen + 1 "
                        "WHERE record_id=?",
                        (item.id,),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO seen_records "
                        "(record_id, source_id, source_item_id, title_norm, creator_norm, "
                        " media_kind, year, url_norm, first_seen_at, times_seen, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                        row + (now, status),
                    )
                    newly += 1
            self._conn.commit()
            self._prune_seen()
            return newly

    _SQLITE_VAR_CHUNK = 400

    def known_ids(self, items) -> set:
        """Which of these record ids are already in persistent memory
        (delivered records only). Queries are chunked below SQLite's bound
        on variables so arbitrarily large batches stay safe."""
        ids = list({i.id for i in (items or []) if i and i.id})
        if not ids:
            return set()
        known: set = set()
        with self._lock:
            for start in range(0, len(ids), self._SQLITE_VAR_CHUNK):
                chunk = ids[start:start + self._SQLITE_VAR_CHUNK]
                marks = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT record_id FROM seen_records WHERE status='delivered' "
                    f"AND record_id IN ({marks})",
                    chunk,
                ).fetchall()
                known.update(r["record_id"] for r in rows)
        return known

    def find_cross_archive_duplicate(self, item) -> Optional[str]:
        """Return the record_id of an earlier-seen duplicate of `item`, or
        None. Deliberately conservative — the same artifact is recognized
        only through strong signals:

          - same normalized canonical URL, OR
          - exact normalized title + media kind + matching year (both
            present and equal, or both absent) + a KNOWN creator on BOTH
            sides that normalizes identically.

        Unknown or incomplete creator metadata NEVER establishes a match:
        two differently-photographed objects, two editions, two prints of
        the same subject, or two catalogues that simply omit the artist all
        share titles and years. Suppressing them would hide genuine
        historical material, so similarity without the creator signal
        (or the shared URL) is allowed through."""
        if not item or not item.id:
            return None
        title_norm = normalize_text(item.title)
        if not title_norm:
            return None
        media = (item.mediaKind or "").strip().lower()
        year = parse_year(item.dateLabel)
        creator_norm = normalize_text((item.people or [""])[0] if item.people else "")
        canonical = item.sourceUrl or (item.evidence.sourceUrl if item.evidence else None)
        url_norm = normalize_url(canonical)
        with self._lock:
            if url_norm:
                row = self._conn.execute(
                    "SELECT record_id FROM seen_records WHERE status='delivered' "
                    "AND url_norm=? AND record_id<>? LIMIT 1",
                    (url_norm, item.id),
                ).fetchone()
                if row:
                    return row["record_id"]
            # Strong metadata match requires a known creator on BOTH sides.
            if not creator_norm:
                return None
            year_clause = "year=?" if year is not None else "year IS NULL"
            params = ((title_norm, media, year, item.id) if year is not None
                      else (title_norm, media, item.id))
            rows = self._conn.execute(
                f"SELECT record_id, source_id, creator_norm FROM seen_records "
                f"WHERE status='delivered' AND title_norm=? AND media_kind=? AND {year_clause} "
                f"AND record_id<>? AND creator_norm<>'' LIMIT 3",
                params,
            ).fetchall()
            for cand in rows:
                if creator_norm == cand["creator_norm"] \
                        and cand["source_id"] != (item.sourceId or ""):
                    return cand["record_id"]
        return None

    def _prune_seen(self) -> None:
        self._prune_table("seen_records", SEEN_MAX_ROWS,
                          "ORDER BY first_seen_at ASC", margin=SEEN_PRUNE_MARGIN)

    def seen_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM seen_records").fetchone()
            return int(row["n"])

    # ------------------------------------------------------------------
    # 3. Discovered topics (organic, metadata-derived)
    # ------------------------------------------------------------------
    def upsert_discovered_topics(self, candidates: List[Dict[str, Any]],
                                 max_add: int = 6) -> int:
        """Insert new organic topics (bounded per call). Candidates are dicts
        with {query, preferredSources, provenance}. Curated topics are never
        overwritten; duplicates (by normalized key) are ignored."""
        now = time.time()
        added = 0
        with self._lock:
            for cand in candidates or []:
                if added >= max_add:
                    break
                query = (cand.get("query") or "").strip()
                if not query or len(query) < 5 or len(query) > 120:
                    continue
                key = normalize_text(query)
                if not key:
                    continue
                try:
                    cur = self._conn.execute(
                        "INSERT INTO topics "
                        "(topic_key, query, origin, provenance, preferred_sources, "
                        " created_at, last_used_at, times_used) "
                        "VALUES (?, ?, 'discovered', ?, ?, ?, 0, 0) "
                        "ON CONFLICT(topic_key) DO NOTHING",
                        (key, query, cand.get("provenance") or "",
                         json.dumps(cand.get("preferredSources") or []), now),
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        added += 1
                except sqlite3.Error:
                    continue
            self._conn.commit()
            self._prune_table("topics", TOPIC_MAX_ROWS, "ORDER BY created_at ASC")
            return added

    def pick_discovered_topics(self, count: int) -> List[Dict[str, Any]]:
        """Least-recently-used discovered topics — balanced exploration of
        obscure material rather than a fixed rotation."""
        if count <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT query, preferred_sources, times_used, topic_key FROM topics "
                "WHERE origin='discovered' "
                "ORDER BY times_used ASC, last_used_at ASC, created_at ASC LIMIT ?",
                (int(count),),
            ).fetchall()
            out = []
            for r in rows:
                try:
                    preferred = json.loads(r["preferred_sources"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    preferred = []
                out.append({
                    "query": r["query"],
                    "preferredSources": preferred if isinstance(preferred, list) else [],
                    "discovered": True,
                })
            return out

    def mark_topic_used(self, query: str) -> None:
        key = normalize_text(query)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE topics SET times_used = times_used + 1, last_used_at = ? "
                "WHERE topic_key = ?",
                (now, key),
            )
            self._conn.commit()

    def topics_stats(self) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN origin='discovered' THEN 1 ELSE 0 END) AS d "
                "FROM topics"
            ).fetchone()
            return {"total": int(row["n"] or 0), "discovered": int(row["d"] or 0)}

    # ------------------------------------------------------------------
    # 4. Provider cooldowns (shared across overlapping requests)
    # ------------------------------------------------------------------
    def provider_cooldown_remaining(self, source_id: str) -> float:
        """Seconds until this archive may be contacted again, or 0."""
        if not source_id:
            return 0.0
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT until_at FROM provider_cooldowns WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None:
                return 0.0
            return max(0.0, float(row["until_at"]) - now)

    # ------------------------------------------------------------------
    # 5. Undelivered candidate queue
    # ------------------------------------------------------------------
    def claim_candidates(self, limit: int = 40,
                         exclude_ids: Optional[Iterable[str]] = None
                         ) -> List[TimeMachineItem]:
        """Take up to `limit` queued records off the queue (oldest first).

        Claiming removes them so a second overlapping request cannot be
        handed the same records. The caller must enqueue anything it does
        not present. Delivered records and unreadable payloads are dropped.
        """
        exclude = {i for i in (exclude_ids or []) if i}
        wanted = max(1, int(limit or 1))
        claimed: List[TimeMachineItem] = []
        drop_ids: List[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, payload FROM candidate_queue "
                "ORDER BY enqueued_at ASC LIMIT ?",
                (wanted * 4,),
            ).fetchall()
            for row in rows:
                if len(claimed) >= wanted:
                    break
                record_id = row["record_id"]
                if record_id in exclude:
                    continue
                delivered = self._conn.execute(
                    "SELECT 1 FROM seen_records WHERE record_id=? AND status='delivered'",
                    (record_id,),
                ).fetchone()
                if delivered:
                    drop_ids.append(record_id)
                    continue
                try:
                    item = TimeMachineItem.from_dict(json.loads(row["payload"]))
                except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                    drop_ids.append(record_id)
                    continue
                if not item or not item.id:
                    drop_ids.append(record_id)
                    continue
                claimed.append(item)
                drop_ids.append(record_id)
            if drop_ids:
                for start in range(0, len(drop_ids), self._SQLITE_VAR_CHUNK):
                    chunk = drop_ids[start:start + self._SQLITE_VAR_CHUNK]
                    marks = ",".join("?" for _ in chunk)
                    self._conn.execute(
                        f"DELETE FROM candidate_queue WHERE record_id IN ({marks})",
                        chunk,
                    )
                self._conn.commit()
        return claimed

    def enqueue_candidates(self, items) -> int:
        """Remember legitimate records that were retrieved but not presented.
        Already-delivered ids and duplicates already queued are skipped."""
        now = time.time()
        added = 0
        with self._lock:
            for item in items or []:
                if not item or not getattr(item, "id", None):
                    continue
                delivered = self._conn.execute(
                    "SELECT 1 FROM seen_records WHERE record_id=? AND status='delivered'",
                    (item.id,),
                ).fetchone()
                if delivered:
                    continue
                try:
                    payload = json.dumps(item.to_dict(), separators=(",", ":"), ensure_ascii=False)
                except (TypeError, ValueError):
                    continue
                cur = self._conn.execute(
                    """
                    INSERT INTO candidate_queue (record_id, source_id, payload, enqueued_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(record_id) DO NOTHING
                    """,
                    (item.id, item.sourceId or "", payload, now),
                )
                if cur.rowcount:
                    added += 1
            self._conn.commit()
            self._prune_table("candidate_queue", CANDIDATE_MAX_ROWS,
                              "ORDER BY enqueued_at ASC")
        return added

    def candidate_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM candidate_queue").fetchone()
            return int(row["n"] or 0)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def _prune_table(self, table: str, max_rows: int, order_by: str,
                     margin: int = 0) -> None:
        """Bound table size: delete oldest rows beyond the cap."""
        try:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            count = int(row["n"])
            keep = max(0, max_rows - margin) if margin else max_rows
            if count <= max_rows:
                return
            excess = count - keep
            self._conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} {order_by} LIMIT ?)",
                (excess,),
            )
            self._conn.commit()
        except sqlite3.Error:
            return

    def stats(self) -> Dict[str, Any]:
        """Everything /api/discovery/stats reports."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, SUM(CASE WHEN last_status IN "
                "('temporary','rate_limited','auth_error','malformed') THEN 1 ELSE 0 END) AS errors "
                "FROM query_progress"
            ).fetchone()
            deferred = int(row["errors"] or 0)
        return {
            "progress": self.progress_stats(),
            "seenRecords": self.seen_count(),
            "topics": self.topics_stats(),
            "candidateQueue": self.candidate_count(),
            "policies": {
                "exhaustedRefreshDays": EXHAUSTED_REFRESH_DAYS,
                "seenMaxRows": SEEN_MAX_ROWS,
                "topicMaxRows": TOPIC_MAX_ROWS,
                "progressMaxRows": PROGRESS_MAX_ROWS,
                "candidateMaxRows": CANDIDATE_MAX_ROWS,
                "capsOverridableViaEnv": [
                    "FOXAI_SEEN_MAX_ROWS", "FOXAI_TOPIC_MAX_ROWS",
                    "FOXAI_PROGRESS_MAX_ROWS", "FOXAI_CANDIDATE_MAX_ROWS",
                ],
            },
            "health": {
                "queriesInBackoffOrCooldown": deferred,
            },
        }
