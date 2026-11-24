#!/usr/bin/env python3
"""
tests/python/test_state_migration.py

v2.1.1 schema-migration tests (audit deficiency #6): a database created by
the v2.1 schema must upgrade in place — new columns added, existing
discovery memory preserved, new functionality immediately usable.

Run with:
    python3 tests/python/test_state_migration.py

No network access is performed or required.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.discovery_state import DiscoveryState  # noqa: E402


V21_SCHEMA = """
CREATE TABLE query_progress (source_id TEXT NOT NULL, query_key TEXT NOT NULL,
  media TEXT NOT NULL DEFAULT '', page INTEGER NOT NULL DEFAULT 1,
  exhausted INTEGER NOT NULL DEFAULT 0, last_count INTEGER NOT NULL DEFAULT 0,
  last_requested INTEGER NOT NULL DEFAULT 0, last_explored_at REAL NOT NULL DEFAULT 0,
  last_status TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source_id, query_key, media));
CREATE TABLE seen_records (record_id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
  source_item_id TEXT, title_norm TEXT NOT NULL DEFAULT '', creator_norm TEXT NOT NULL DEFAULT '',
  media_kind TEXT NOT NULL DEFAULT '', year INTEGER, url_norm TEXT NOT NULL DEFAULT '',
  first_seen_at REAL NOT NULL, times_seen INTEGER NOT NULL DEFAULT 1);
CREATE TABLE topics (topic_key TEXT PRIMARY KEY, query TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT 'discovered', provenance TEXT NOT NULL DEFAULT '',
  preferred_sources TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
  last_used_at REAL NOT NULL DEFAULT 0, times_used INTEGER NOT NULL DEFAULT 0);
"""


def make_item(item_id, title, source_id="loc"):
    from sources.base import TimeMachineItem
    it = TimeMachineItem.build(source_id=source_id, source_item_id=item_id,
                               canonical_url=f"https://{source_id}.example.org/{item_id}",
                               title=title, media_kind="photograph",
                               dateLabel="1893")
    return it


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(self.db))
        conn.executescript(V21_SCHEMA)
        import time as _t
        conn.execute(
            "INSERT INTO query_progress VALUES ('met', 'monet', '', 4, 1, 0, 6, ?, 'exhausted')",
            (_t.time(),))
        # Seen row uses the id the item model actually generates, so the
        # migrated row is findable by the same lookup the feed performs.
        legacy_item = make_item("old-1", "an old record")
        conn.execute(
            "INSERT INTO seen_records VALUES (?, 'loc', 'x', 'an old record', '', "
            "'photograph', 1893, 'loc.gov/x', 1.0, 2)", (legacy_item.id,))

        conn.execute(
            "INSERT INTO topics VALUES ('tk', 'legacy topic', 'discovered', '', '[]', 1.0, 0, 3)")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_preserves_all_existing_state(self):
        state = DiscoveryState(self.db)
        try:
            # Cursors preserved (including exhaustion and page depth).
            self.assertEqual(state.get_page("met", "monet", None), -1)
            self.assertEqual(state.stats()["progress"]["sources"]["met"]["pagesConsumed"], 3)
            # Seen-record memory preserved.
            self.assertEqual(state.seen_count(), 1)
            item = make_item("old-1", "an old record")  # deterministic id
            self.assertIn(item.id, state.known_ids([item]))
            # Discovered topics preserved.
            self.assertEqual(state.topics_stats()["discovered"], 1)
            self.assertEqual(state.topics_stats()["total"], 1)
        finally:
            state.close()

    def test_new_functionality_works_after_migration(self):
        state = DiscoveryState(self.db)
        try:
            # Outcome taxonomy columns exist and work on migrated data.
            state.record_result("met", "van gogh", None, 6, 0, outcome="temporary")
            self.assertEqual(state.get_page("met", "van gogh", None), -2)
            # The legacy exhausted cursor still refreshes lazily after the
            # policy window (simulated by aging the timestamp).
            import time as _t
            with state._lock:
                state._conn.execute(
                    "UPDATE query_progress SET last_explored_at=? "
                    "WHERE query_key='monet'", (_t.time() - 40 * 86400,))
                state._conn.commit()
            self.assertEqual(state.get_page("met", "monet", None), 1)
            # record_seen works with the new status column on old rows.
            state.record_seen([make_item("new-1", "a new record")])
            self.assertEqual(state.seen_count(), 2)
        finally:
            state.close()

    def test_migration_is_idempotent(self):
        state = DiscoveryState(self.db)
        state.close()
        state = DiscoveryState(self.db)  # reopen — columns must not duplicate
        try:
            cols = {r["name"] for r in state._conn.execute(
                "PRAGMA table_info(query_progress)").fetchall()}
            self.assertIn("error_count", cols)
            self.assertIn("next_attempt_at", cols)
            state.record_result("loc", "fresh", None, 6, 6, outcome="ok")
            self.assertEqual(state.get_page("loc", "fresh", None), 2)
        finally:
            state.close()

    def test_fresh_database_initializes_cleanly(self):
        """A brand-new install creates its own state — nothing inherited."""
        fresh = Path(self.tmp.name) / "fresh.db"
        state = DiscoveryState(fresh)
        try:
            self.assertEqual(state.seen_count(), 0)
            self.assertEqual(state.get_page("loc", "anything", None), 1)
            self.assertEqual(state.stats()["progress"]["totalQueries"], 0)
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
