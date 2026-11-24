#!/usr/bin/env python3
"""
tests/python/test_audit_v220.py

Targeted integration tests for the v2.2.0 audit corrections:

  - provider result windows are not re-fetched by clamping
  - concurrency is bounded across overlapping discovery calls
  - Retry-After delta-seconds and HTTP-date are honored in full
  - provider cooldowns apply to other queries, and position is kept
  - cross-archive duplicates inside one batch are suppressed
  - distinct artifacts are not merged
  - undelivered records survive in a bounded queue and resume after restart
  - the Wellcome high-page check does not open a socket

Run with:
    python3 tests/python/test_audit_v220.py

No network access is performed or required.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.config import _classify_http_error, parse_retry_after  # noqa: E402
from sources.discovery import discover_surprise  # noqa: E402
from sources.discovery_state import (  # noqa: E402
    CANDIDATE_MAX_ROWS, DiscoveryState, is_same_artifact, artifact_signature,
)
from sources.registry import APP_MAX_WORKERS, PER_SOURCE_CONCURRENCY  # noqa: E402


def item(record_id, title, source_id="loc", date_label="1893", url=None, people=None,
         media_kind="photograph"):
    return TimeMachineItem.build(
        source_id=source_id,
        source_item_id=record_id,
        canonical_url=url or f"https://{source_id}.example.org/item/{record_id}",
        title=title,
        media_kind=media_kind,
        dateLabel=date_label,
        people=people or [],
    )


def sources(*ids):
    return [{"id": sid, "available": True} for sid in ids]


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds_and_http_date(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after("0"), 0.0)
        self.assertIsNone(parse_retry_after("not-a-date"))
        when = datetime.now(timezone.utc) + timedelta(hours=2)
        header = format_datetime(when, usegmt=True)
        seconds = parse_retry_after(header)
        self.assertIsNotNone(seconds)
        self.assertGreater(seconds, 7000)
        self.assertLess(seconds, 7300)
        classified = _classify_http_error(429, header)
        self.assertEqual(classified.kind, "rate_limited")
        self.assertGreater(classified.retry_after, 7000)

    def test_long_retry_after_is_not_shortened(self):
        tmp = tempfile.TemporaryDirectory()
        state = DiscoveryState(Path(tmp.name) / "s.db")
        try:
            state.record_result("loc", "plates", None, 6, 0,
                                outcome="rate_limited", retry_after=7200.0)
            row = state._conn.execute(
                "SELECT next_attempt_at FROM query_progress"
            ).fetchone()
            wait = row["next_attempt_at"] - time.time()
            self.assertGreaterEqual(wait, 7000)
            self.assertLess(wait, 7300)
            # The same cooldown covers every query to that archive.
            self.assertGreater(state.provider_cooldown_remaining("loc"), 7000)
            self.assertEqual(state.provider_cooldown_remaining("met"), 0.0)
            # Position stays on the page that failed.
            self.assertEqual(state._conn.execute(
                "SELECT page, exhausted FROM query_progress").fetchone()["page"], 1)
        finally:
            state.close()
            tmp.cleanup()


class ProviderCooldownTests(unittest.TestCase):
    def test_overlapping_query_skips_without_moving_cursor(self):
        tmp = tempfile.TemporaryDirectory()
        state = DiscoveryState(Path(tmp.name) / "s.db")
        try:
            state.record_result("loc", "topic 0", None, 6, 0,
                                outcome="rate_limited", retry_after=300.0)
            calls = []

            def search(source_id, query, limit=6, media=None, page=1):
                calls.append((source_id, query, page))
                return [item(f"{source_id}-{query}", f"Record {query}", source_id=source_id)]

            stats = {}
            discover_surprise(
                search, lambda: sources("loc"),
                queries=[
                    {"query": "topic 0", "preferredSources": ["loc"]},
                    {"query": "topic 1", "preferredSources": ["loc"]},
                ],
                limit=10, state=state, stats_out=stats,
            )
            self.assertEqual(calls, [])
            self.assertGreater(stats.get("provider_cooldown_skipped", 0), 0)
            # The query that was not itself rate-limited is still on page 1.
            self.assertEqual(state.get_page("loc", "topic 1", None), 1)
            self.assertEqual(state.get_page("loc", "topic 0", None), -2)
        finally:
            state.close()
            tmp.cleanup()


class OverlappingConcurrencyTests(unittest.TestCase):
    def test_two_discovery_calls_share_the_worker_budget(self):
        now = {"n": 0}
        peak = {"n": 0}
        per = {}
        per_peak = {}
        lock = threading.Lock()

        def search(source_id, query, limit=6, media=None, page=1):
            with lock:
                now["n"] += 1
                peak["n"] = max(peak["n"], now["n"])
                per[source_id] = per.get(source_id, 0) + 1
                per_peak[source_id] = max(per_peak.get(source_id, 0), per[source_id])
            time.sleep(0.15)
            with lock:
                now["n"] -= 1
                per[source_id] -= 1
            return [item(f"{source_id}-{query}-{page}", f"{query} plate", source_id=source_id)]

        def run():
            discover_surprise(
                search, lambda: sources("loc"),
                queries=[{"query": f"topic {i}", "preferredSources": ["loc"]} for i in range(6)],
                limit=12, state=None,
            )

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertLessEqual(peak["n"], APP_MAX_WORKERS)
        self.assertLessEqual(per_peak.get("loc", 0), PER_SOURCE_CONCURRENCY)
        self.assertGreaterEqual(peak["n"], 1)


class DedupTests(unittest.TestCase):
    def test_same_batch_cross_archive_duplicate_dropped(self):
        shared = "https://example.org/artifacts/shared-plate"

        def search(source_id, query, limit=6, media=None, page=1):
            return [item(
                f"{source_id}-same", "Copper plate of 1843",
                source_id=source_id, url=shared, date_label="1843",
                people=["Ada Lovelace"],
            )]

        items, _tried, _used = discover_surprise(
            search, lambda: sources("loc", "met"),
            queries=[{"query": "plate", "preferredSources": ["loc", "met"]}],
            limit=10, state=None,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].sourceUrl, shared)

    def test_distinct_artifacts_with_similar_titles_both_kept(self):
        def search(source_id, query, limit=6, media=None, page=1):
            return [item(
                f"{source_id}-own", "Harbor view",
                source_id=source_id, date_label="1901", people=[],
            )]

        items, _tried, _used = discover_surprise(
            search, lambda: sources("loc", "met"),
            queries=[{"query": "harbor", "preferredSources": ["loc", "met"]}],
            limit=10, state=None,
        )
        self.assertEqual(len(items), 2)
        left = artifact_signature(items[0])
        right = artifact_signature(items[1])
        self.assertFalse(is_same_artifact(left, right))


class CandidateQueueTests(unittest.TestCase):
    def test_undelivered_records_return_after_restart(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "s.db"
        state = DiscoveryState(path)
        fetched = []
        try:
            def search(source_id, query, limit=6, media=None, page=1):
                batch = [
                    item(f"{source_id}-{query}-p{page}-{i}",
                         f"Distinct plate {page}-{i}-{source_id}",
                         source_id=source_id, date_label=str(1800 + i))
                    for i in range(6)
                ]
                fetched.extend(batch)
                return batch

            first, _t, _u = discover_surprise(
                search, lambda: sources("loc"),
                queries=[{"query": "plates", "preferredSources": ["loc"]}],
                limit=1, state=state,
            )
            self.assertEqual(len(first), 1)
            presented = {first[0].id}
            missed = [row.id for row in fetched if row.id not in presented]
            self.assertGreaterEqual(len(missed), 1)
            # Cursor moved past the page that held the unpresented records.
            self.assertGreaterEqual(state.get_page("loc", "plates", None), 2)
            queued_before = state.candidate_count()
            self.assertGreaterEqual(queued_before, 1)
            state.close()

            state = DiscoveryState(path)
            self.assertGreaterEqual(state.candidate_count(), 1)

            def no_more(source_id, query, limit=6, media=None, page=1):
                return []

            second, _t, _u = discover_surprise(
                no_more, lambda: sources("loc"),
                queries=[{"query": "plates", "preferredSources": ["loc"]}],
                limit=6, state=state,
            )
            second_ids = {row.id for row in second}
            self.assertTrue(second_ids & set(missed),
                            "undelivered records were not available after restart")
        finally:
            state.close()
            tmp.cleanup()

    def test_queue_is_bounded(self):
        tmp = tempfile.TemporaryDirectory()
        state = DiscoveryState(Path(tmp.name) / "s.db")
        try:
            batch = [
                item(f"id-{i}", f"Bounded record {i}", date_label=str(1800 + (i % 50)))
                for i in range(CANDIDATE_MAX_ROWS + 50)
            ]
            state.enqueue_candidates(batch)
            self.assertLessEqual(state.candidate_count(), CANDIDATE_MAX_ROWS)
        finally:
            state.close()
            tmp.cleanup()


class WellcomeIsolationTests(unittest.TestCase):
    def test_high_page_does_not_open_a_socket(self):
        """The old hermetic pagination test called WellcomeSource.search
        for real. This replacement fails if any TCP connect is attempted."""
        from sources.wellcome import WellcomeSource

        def refuse(*_args, **_kwargs):
            raise AssertionError("hermetic test attempted a live socket")

        with unittest.mock.patch.object(socket, "create_connection", side_effect=refuse), \
             unittest.mock.patch.object(socket, "socket", side_effect=refuse):
            # Past any former artificial cap, with the HTTP layer blocked,
            # a mocked empty body still proves the page number is forwarded
            # rather than clamped — and that no socket is required to see it.
            captured = {}

            def fake_json(url, **_kwargs):
                captured["url"] = url
                return {"results": []}

            import sources.wellcome as wellcome_mod
            with unittest.mock.patch.object(wellcome_mod, "cache_get", return_value=None), \
                 unittest.mock.patch.object(wellcome_mod, "cache_set", return_value=None), \
                 unittest.mock.patch.object(wellcome_mod, "http_get_json", side_effect=fake_json):
                WellcomeSource().search("plague", limit=1, page=10_000)
        self.assertIn("page=10000", captured.get("url", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
