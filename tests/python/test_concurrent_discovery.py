#!/usr/bin/env python3
"""
tests/python/test_concurrent_discovery.py

v2.1.1 integration tests proving that the ACTUAL discovery pipeline
(discover_surprise — the code path behind Surprise Me, TimeStream and the
Gazette) executes its archive requests CONCURRENTLY, with provider-friendly
pacing and deadline handling. The audit's finding was that concurrency
existed only in a helper while the central loop ran sequentially; these
tests pin the fixed behavior.

Proof of concurrency: a fake search_fn blocks every thread on a
threading.Barrier — sequential execution can never reach the barrier count,
so any pass is direct evidence of simultaneous in-flight requests.

Run with:
    python3 tests/python/test_concurrent_discovery.py

No network access is performed or required.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import unittest.mock  # noqa: E402

import sources.discovery as discovery_mod  # noqa: E402
from sources.base import TimeMachineItem  # noqa: E402
from sources.config import FetchError  # noqa: E402
from sources.discovery import (  # noqa: E402
    DISCOVERY_BATCH_DEADLINE_S, discover_surprise,
)
from sources.discovery_state import DiscoveryState  # noqa: E402
from sources.registry import SourceTempError  # noqa: E402


def item(record_id, title, source_id="loc", date_label="1893"):
    return TimeMachineItem.build(
        source_id=source_id, source_item_id=record_id,
        canonical_url=f"https://{source_id}.example.org/{record_id}",
        title=title, media_kind="photograph", dateLabel=date_label,
    )


def list_sources_fn(*ids):
    return [{"id": s, "available": True} for s in (ids or ("loc", "met", "cma"))]


def queries(n):
    return [{"query": f"topic {i}", "preferredSources": ["loc", "met", "cma"]}
            for i in range(n)]


class ConcurrencyProofTests(unittest.TestCase):
    def test_pipeline_executes_requests_concurrently(self):
        """Barrier proof: 6 tasks, barrier of 5. A sequential pipeline would
        deadlock on the barrier (each call waits for 5 simultaneous
        participants that never arrive); the concurrent pipeline sails
        through and delivers items."""
        barrier = threading.Barrier(5, timeout=5)
        in_flight = {"now": 0, "peak": 0}
        lock = threading.Lock()

        def parallel_search(source_id, query, limit=6, media=None, page=1):
            with lock:
                in_flight["now"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                barrier.wait(timeout=5)
                return [item(f"{source_id}-{page}", "concurrent record",
                             source_id=source_id)]
            finally:
                with lock:
                    in_flight["now"] -= 1

        items, _tried, _used = discover_surprise(
            parallel_search, list_sources_fn,
            queries=queries(6), limit=20, state=None,
        )
        self.assertGreaterEqual(in_flight["peak"], 5,
                                "pipeline never had 5 requests in flight")
        self.assertGreater(len(items), 0)

    def test_per_source_pacing_caps_in_flight_requests(self):
        """At most PER_SOURCE_CONCURRENCY requests in flight per archive:
        a fake that records peaks per source, with all tasks hitting the
        same source."""
        from sources.registry import PER_SOURCE_CONCURRENCY
        peak = {"n": 0}
        now = {"n": 0}
        lock = threading.Lock()

        def paced_search(source_id, query, limit=6, media=None, page=1):
            with lock:
                now["n"] += 1
                peak["n"] = max(peak["n"], now["n"])
            time.sleep(0.05)
            with lock:
                now["n"] -= 1
            return [item(f"{source_id}-{page}-{query}", "paced record",
                         source_id=source_id)]

        # 9 tasks all against one source
        qs = [{"query": f"topic {i}", "preferredSources": ["loc"]} for i in range(9)]
        discover_surprise(paced_search, list_sources_fn("loc"),
                          queries=qs, limit=40, state=None)
        self.assertLessEqual(peak["n"], PER_SOURCE_CONCURRENCY,
                             f"per-source cap exceeded: peak={peak['n']}")

    def test_batch_deadline_bounds_slow_archives(self):
        """A hung archive cannot stretch the batch: with a deadline far
        shorter than the hang, discovery returns (with the hung tasks
        cancelled and counted) instead of blocking forever."""
        def hung_search(source_id, query, limit=6, media=None, page=1):
            # 3s hang: the batch must return far sooner (deadline 1s); the
            # worker thread itself joins at process exit within 3s.
            time.sleep(3)
            return [item(f"{source_id}-{page}", "never arrives", source_id=source_id)]

        # Shrink the deadline so the test is fast.
        with unittest.mock.patch.object(discovery_mod,
                                        "DISCOVERY_BATCH_DEADLINE_S", 1.0):
            t0 = time.monotonic()
            stats = {}
            items, _tried, _used = discover_surprise(
                hung_search, list_sources_fn,
                queries=queries(3), limit=12, state=None, stats_out=stats)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 10.0, "batch deadline did not bound the hang")
        self.assertEqual(stats.get("deadline_misses"), 9)
        self.assertEqual(items, [])

    def test_deadline_missed_tasks_back_off_not_exhaust(self):
        """Hung tasks that miss the deadline are put into temporary backoff
        in the persistent store — their discovery position is preserved and
        they are NOT marked exhausted."""
        def hung_search(source_id, query, limit=6, media=None, page=1):
            time.sleep(3)
            return [item(f"{source_id}-{page}", "never", source_id=source_id)]

        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "s.db"
        state = DiscoveryState(db)
        try:
            with unittest.mock.patch.object(discovery_mod,
                                            "DISCOVERY_BATCH_DEADLINE_S", 0.8):
                stats = {}
                discover_surprise(hung_search, list_sources_fn,
                                  queries=queries(2), limit=12,
                                  state=state, stats_out=stats)
            self.assertGreater(stats.get("deadline_misses", 0), 0)
            self.assertEqual(stats.get("exhausted_marks", 0), 0)
            # The cursors exist with a temporary status, not exhausted.
            with state._lock:
                rows = state._conn.execute(
                    "SELECT page, exhausted, last_status, next_attempt_at "
                    "FROM query_progress").fetchall()
            self.assertTrue(rows)
            for r in rows:
                self.assertEqual(r["exhausted"], 0)
                self.assertIn(r["last_status"], ("temporary", "rate_limited",
                                                "auth_error", "malformed"))
                self.assertGreater(r["next_attempt_at"], 0)
        finally:
            state.close()
            tmp.cleanup()


class TaxonomyIntegrationTests(unittest.TestCase):
    """The pipeline turns classified failures into backoff outcomes — never
    into exhausted searches (audit deficiency #3, pipeline level)."""

    def _mk_state(self):
        tmp = tempfile.TemporaryDirectory()
        return DiscoveryState(Path(tmp.name) / "s.db"), tmp

    def test_temporary_failure_does_not_exhaust(self):
        state, tmp = self._mk_state()
        try:
            calls = {"n": 0}

            def flaky_search(source_id, query, limit=6, media=None, page=1):
                calls["n"] += 1
                raise SourceTempError(source_id, "temporary")

            stats = {}
            discover_surprise(flaky_search, list_sources_fn,
                              queries=queries(3), limit=12,
                              state=state, stats_out=stats)
            self.assertEqual(stats.get("exhausted_marks", 0), 0)
            self.assertEqual(stats.get("temporary_errors"), 9)
            self.assertEqual(calls["n"], 9)
            with state._lock:
                row = state._conn.execute(
                    "SELECT exhausted, error_count FROM query_progress LIMIT 1").fetchone()
            self.assertEqual(row["exhausted"], 0)
            self.assertEqual(row["error_count"], 1)
        finally:
            state.close()
            tmp.cleanup()

    def test_rate_limited_uses_retry_after_cooldown(self):
        state, tmp = self._mk_state()
        try:
            def limited(source_id, query, limit=6, media=None, page=1):
                raise SourceTempError(source_id, "rate_limited", status=429,
                                      retry_after=120.0)

            stats = {}
            discover_surprise(limited, list_sources_fn, queries=queries(2),
                              limit=12, state=state, stats_out=stats)
            with state._lock:
                row = state._conn.execute(
                    "SELECT next_attempt_at, last_status FROM query_progress "
                    "LIMIT 1").fetchone()
            self.assertEqual(row["last_status"], "rate_limited")
            # Cooldown >= the provider's Retry-After (120s here).
            self.assertGreaterEqual(row["next_attempt_at"] - time.time(), 100.0)
        finally:
            state.close()
            tmp.cleanup()

    def test_recovery_preserves_position(self):
        """The audit's core scenario: a temporarily unavailable archive
        recovers and RESUMES EXPLORATION at the position it had before the
        failure — page 1 consumed before the outage, page 2 fetched after
        recovery, exhaustion never recorded."""
        state, tmp = self._mk_state()
        try:
            # Batch 1: healthy archive serves page 1.
            def healthy(source_id, query, limit=6, media=None, page=1):
                return [item(f"{source_id}-{query}-{page}-{i}", f"r{i}",
                             source_id=source_id) for i in range(6)]

            discover_surprise(healthy, list_sources_fn, queries=queries(2),
                              limit=24, state=state)

            # Outage: everything fails temporarily.
            discover_surprise(
                lambda *a, **k: (_ for _ in ()).throw(
                    SourceTempError("loc", "temporary")),
                list_sources_fn, queries=queries(2), limit=24, state=state)

            # Expire the backoff so the next batch may retry immediately.
            with state._lock:
                state._conn.execute("UPDATE query_progress SET next_attempt_at=0")
                state._conn.commit()

            # Recovery: the archive is back; served pages must continue
            # from page 2, and the batch must return fresh page-2 records.
            served_pages = []

            def recovered(source_id, query, limit=6, media=None, page=1):
                served_pages.append(page)
                return [item(f"{source_id}-{query}-{page}-{i}", f"r{i}",
                             source_id=source_id) for i in range(6)]

            stats = {}
            items, _t, _u = discover_surprise(
                recovered, list_sources_fn, queries=queries(2), limit=24,
                state=state, stats_out=stats)
            self.assertTrue(served_pages)
            self.assertTrue(all(p == 2 for p in served_pages),
                            f"recovery did not resume: served {served_pages}")
            self.assertGreater(len(items), 0)
            self.assertEqual(stats.get("exhausted_marks", 0), 0)
        finally:
            state.close()
            tmp.cleanup()

    def test_auth_error_long_cooldown(self):
        state, tmp = self._mk_state()
        try:
            def bad_key(source_id, query, limit=6, media=None, page=1):
                raise SourceTempError(source_id, "auth_error", status=403)

            stats = {}
            discover_surprise(bad_key, list_sources_fn, queries=queries(1),
                              limit=12, state=state, stats_out=stats)
            self.assertEqual(stats.get("auth_errors"), 3)
            with state._lock:
                row = state._conn.execute(
                    "SELECT next_attempt_at FROM query_progress LIMIT 1").fetchone()
            self.assertGreater(row["next_attempt_at"] - time.time(),
                               600.0)  # ≥10 min cooldown
        finally:
            state.close()
            tmp.cleanup()

    def test_malformed_response_short_backoff(self):
        state, tmp = self._mk_state()
        try:
            def garbled(source_id, query, limit=6, media=None, page=1):
                raise SourceTempError(source_id, "malformed")

            stats = {}
            discover_surprise(garbled, list_sources_fn, queries=queries(1),
                              limit=12, state=state, stats_out=stats)
            self.assertEqual(stats.get("malformed_responses"), 3)
        finally:
            state.close()
            tmp.cleanup()

    def test_plain_exception_is_temporary_not_empty(self):
        """Any unclassified failure inside the pipeline lands in backoff —
        the old behavior (exception → [] → exhaustion) is gone."""
        state, tmp = self._mk_state()
        try:
            def broken(source_id, query, limit=6, media=None, page=1):
                raise RuntimeError("unexpected adapter crash")

            stats = {}
            discover_surprise(broken, list_sources_fn, queries=queries(1),
                              limit=12, state=state, stats_out=stats)
            self.assertEqual(stats.get("exhausted_marks", 0), 0)
            self.assertEqual(stats.get("temporary_errors"), 3)
        finally:
            state.close()
            tmp.cleanup()


class TimeStreamBenefitTests(unittest.TestCase):
    def test_gazette_path_uses_concurrent_pipeline(self):
        """build_feed(surprise=True) — the TimeStream/Gazette path — passes
        the strict search function, so concurrent classified fetching is
        what actually runs in the app (not just in tests)."""
        import inspect
        import serve
        src = inspect.getsource(serve.build_feed)
        self.assertIn("search_source_strict", src)
        self.assertIn("state=state", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
