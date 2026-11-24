#!/usr/bin/env python3
"""
tests/python/test_error_taxonomy.py

v2.1.1 store-level tests for the failure/exhaustion taxonomy
(audit deficiency #3). The store must treat these differently:

  successful page with records   → cursor advances
  successful EMPTY page          → confirmed exhausted (skipped; lazily
                                   refreshed after 30 days)
  temporary network failure      → exponential backoff, position preserved,
                                   NEVER exhausted
  rate limiting (429)            → cooldown honoring Retry-After
  invalid credentials (401/403)  → long cooldown (no hammering)
  malformed responses            → short backoff

Run with:
    python3 tests/python/test_error_taxonomy.py

No network access is performed or required.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.config import FetchError  # noqa: E402
from sources.discovery_state import (  # noqa: E402
    AUTH_ERROR_COOLDOWN_S, MALFORMED_BACKOFF_S, TEMP_BACKOFF_BASE_S,
    TEMP_BACKOFF_MAX_S, DiscoveryState,
)
from sources.registry import SourceTempError, search_source  # noqa: E402


class StoreOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = DiscoveryState(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def _row(self, source="loc", query="q"):
        with self.state._lock:
            return self.state._conn.execute(
                "SELECT page, exhausted, error_count, next_attempt_at, last_status "
                "FROM query_progress WHERE source_id=? AND query_key=?",
                (source, query),
            ).fetchone()

    def test_ok_advances_and_resets_backoff(self):
        self.state.record_result("loc", "q", None, 6, 0, outcome="temporary")
        self.state.record_result("loc", "q", None, 6, 6, outcome="ok")
        row = self._row()
        self.assertEqual(row["page"], 2)          # advanced from page 1
        self.assertEqual(row["error_count"], 0)   # backoff reset by success
        self.assertEqual(row["next_attempt_at"], 0)
        self.assertEqual(row["exhausted"], 0)

    def test_empty_page_confirms_exhaustion(self):
        self.state.record_result("loc", "q", None, 6, 0, outcome="empty")
        row = self._row()
        self.assertEqual(row["exhausted"], 1)
        self.assertEqual(self.state.get_page("loc", "q", None), -1)
        # The position is preserved for the eventual refresh.
        self.assertEqual(row["page"], 1)

    def test_temporary_backoff_is_exponential_and_capped(self):
        pages = []
        for i in range(6):
            self.state.record_result("loc", "q", None, 6, 0, outcome="temporary")
            row = self._row()
            pages.append(row["page"])
            wait = row["next_attempt_at"] - time.time()
            expected = min(TEMP_BACKOFF_BASE_S * (2 ** i), TEMP_BACKOFF_MAX_S)
            self.assertAlmostEqual(wait, expected, delta=3.0)
            self.assertEqual(row["exhausted"], 0)
        # Every attempt stays on the SAME page — position preserved.
        self.assertEqual(set(pages), {1})

    def test_deferred_queries_skip_then_retry(self):
        self.state.record_result("loc", "q", None, 6, 0, outcome="temporary")
        self.assertEqual(self.state.get_page("loc", "q", None), -2)
        # Expire the backoff: the search becomes fetchable again, at the
        # same page, with no exhaustion.
        with self.state._lock:
            self.state._conn.execute(
                "UPDATE query_progress SET next_attempt_at=0 WHERE query_key='q'")
            self.state._conn.commit()
        self.assertEqual(self.state.get_page("loc", "q", None), 1)

    def test_rate_limit_cooldown_honors_retry_after(self):
        self.state.record_result("loc", "q", None, 6, 0,
                                 outcome="rate_limited", retry_after=300.0)
        row = self._row()
        self.assertEqual(row["last_status"], "rate_limited")
        self.assertGreaterEqual(row["next_attempt_at"] - time.time(), 280.0)

    def test_rate_limit_cooldown_has_floor(self):
        self.state.record_result("loc", "q", None, 6, 0,
                                 outcome="rate_limited", retry_after=0.5)
        row = self._row()
        self.assertGreaterEqual(row["next_attempt_at"] - time.time(), 20.0)

    def test_auth_error_long_cooldown(self):
        self.state.record_result("loc", "q", None, 6, 0, outcome="auth_error")
        row = self._row()
        self.assertGreaterEqual(row["next_attempt_at"] - time.time(),
                                AUTH_ERROR_COOLDOWN_S - 5.0)
        self.assertEqual(row["exhausted"], 0)

    def test_malformed_short_backoff(self):
        self.state.record_result("loc", "q", None, 6, 0, outcome="malformed")
        row = self._row()
        self.assertAlmostEqual(row["next_attempt_at"] - time.time(),
                               MALFORMED_BACKOFF_S, delta=3.0)
        self.assertEqual(row["exhausted"], 0)

    def test_unknown_outcome_is_temporary(self):
        self.state.record_result("loc", "q", None, 6, 0, outcome="mystery")
        row = self._row()
        self.assertEqual(row["exhausted"], 0)
        self.assertGreater(row["error_count"], 0)

    def test_temporary_never_exhausts_across_many_failures(self):
        for _ in range(10):
            self.state.record_result("loc", "q", None, 6, 0, outcome="temporary")
        self.assertEqual(self.state.get_page("loc", "q", None), -2)  # deferred
        # After backoff expires the search is fetchable again — a failure
        # streak can never permanently exhaust a legitimate path.
        with self.state._lock:
            self.state._conn.execute(
                "UPDATE query_progress SET next_attempt_at=0 WHERE query_key='q'")
            self.state._conn.commit()
        self.assertEqual(self.state.get_page("loc", "q", None), 1)


class RegistryClassificationTests(unittest.TestCase):
    """search_source_strict raises SourceTempError with the FetchError's
    classification; search_source still fails soft to [] (public contract)."""

    class _FakeSource:
        source_id = "fake"

        def __init__(self, behavior):
            self._behavior = behavior

        def availability(self):
            from sources.base import SourceAvailability
            return SourceAvailability(True, None)

        def search(self, query, media=None, limit=10, page=1):
            return self._behavior()

    def _strict(self, behavior):
        import sources.registry as reg
        with unittest.mock.patch.object(reg, "_BY_ID", {"fake": self._FakeSource(behavior)}):
            return reg.search_source_strict("fake", "query", limit=3, page=1)

    def test_rate_limited_classification(self):
        def boom():
            raise FetchError("HTTP 429", status=429, kind="rate_limited",
                             retry_after=45.0)
        import unittest.mock
        with unittest.mock.patch.object(
                __import__("sources.registry", fromlist=["_BY_ID"]), "_BY_ID",
                {"fake": self._FakeSource(boom)}):
            import sources.registry as reg
            with self.assertRaises(SourceTempError) as ctx:
                reg.search_source_strict("fake", "q", limit=3, page=1)
        self.assertEqual(ctx.exception.kind, "rate_limited")
        self.assertEqual(ctx.exception.retry_after, 45.0)
        self.assertEqual(ctx.exception.status, 429)

    def test_server_error_maps_to_temporary(self):
        import unittest.mock
        import sources.registry as reg
        def boom():
            raise FetchError("HTTP 503", status=503, kind="server_error")
        with unittest.mock.patch.object(reg, "_BY_ID",
                                        {"fake": self._FakeSource(boom)}):
            with self.assertRaises(SourceTempError) as ctx:
                reg.search_source_strict("fake", "q", limit=3, page=1)
        # Registry keeps the granular kind; the discovery pipeline buckets
        # server_error into temporary backoff (proven in
        # test_concurrent_discovery's pipeline tests).
        self.assertEqual(ctx.exception.kind, "server_error")

    def test_public_search_source_still_fails_soft(self):
        import unittest.mock
        import sources.registry as reg
        def boom():
            raise FetchError("HTTP 429", status=429, kind="rate_limited")
        with unittest.mock.patch.object(reg, "_BY_ID",
                                        {"fake": self._FakeSource(boom)}):
            self.assertEqual(reg.search_source("fake", "q", limit=3), [])


import unittest.mock  # noqa: E402  (used above)

if __name__ == "__main__":
    unittest.main(verbosity=2)
