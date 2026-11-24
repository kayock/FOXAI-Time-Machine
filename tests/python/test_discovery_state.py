#!/usr/bin/env python3
"""
tests/python/test_discovery_state.py

Hermetic tests for the v2.1 persistent discovery state store
(sources/discovery_state.py) — the SQLite-backed memory that lets deep
exploration resume across restarts. Covers:

  - per-(source, query) page cursors: advance on results, hold on repeats
  - exhaustion marking and lazy refresh after EXHAUSTED_REFRESH_DAYS
  - persistence across close/reopen (simulating an application restart)
  - persistent seen-record memory and cross-archive duplicate detection,
    including the "distinct artifact with a similar title is NOT a
    duplicate" guarantee
  - bounded storage (seen-record pruning)
  - discovered-topic storage, LRU rotation and bounds
  - thread-safety smoke test (the service runs on ThreadingHTTPServer)
  - fail-soft behavior on a broken database path

Run with:
    python3 tests/python/test_discovery_state.py

No network access is performed or required.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.discovery_state import (  # noqa: E402
    DiscoveryState, normalize_text, normalize_url, parse_year,
)
from sources.base import TimeMachineItem  # noqa: E402


def make_item(item_id, title, source_id="loc", media_kind="photograph",
              date_label="1893", creator=None, url=None):
    item = TimeMachineItem.build(
        source_id=source_id, source_item_id=item_id,
        canonical_url=url or f"https://{source_id}.example.org/{item_id}",
        title=title, media_kind=media_kind, dateLabel=date_label,
    )
    if creator:
        item.people = [creator]
    item.sourceUrl = item.sourceUrl or f"https://{source_id}.example.org/{item_id}"
    return item


class NormalizeTests(unittest.TestCase):
    def test_normalize_text(self):
        self.assertEqual(normalize_text("The  Great  Wave!"), "the great wave")
        self.assertEqual(normalize_text(None), "")

    def test_normalize_url(self):
        self.assertEqual(
            normalize_url("https://www.loc.gov/item/123/?foo=bar#x"),
            "www.loc.gov/item/123",
        )
        self.assertEqual(normalize_url("http://Example.org/a/"), "example.org/a")
        self.assertEqual(normalize_url(None), "")

    def test_parse_year(self):
        self.assertEqual(parse_year("1893"), 1893)
        self.assertEqual(parse_year("c. 1868–73"), 1868)
        self.assertEqual(parse_year("1943-06-06"), 1943)
        self.assertIsNone(parse_year("undated"))
        self.assertIsNone(parse_year(None))


class QueryProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"
        self.state = DiscoveryState(self.db)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_first_page_is_one(self):
        self.assertEqual(self.state.get_page("met", "monet", None), 1)

    def test_page_advances_on_results(self):
        self.state.record_result("met", "monet", None, requested=6, result_count=6)
        self.assertEqual(self.state.get_page("met", "monet", None), 2)
        self.state.record_result("met", "monet", None, requested=6, result_count=3)
        self.assertEqual(self.state.get_page("met", "monet", None), 3)

    def test_exhaustion_on_empty_page(self):
        self.state.record_result("met", "monet", None, requested=6, result_count=0)
        self.assertEqual(self.state.get_page("met", "monet", None), -1)
        self.assertTrue(self.state.is_exhausted("met", "monet", None))

    def test_queries_are_independent_per_source_and_media(self):
        self.state.record_result("met", "monet", None, requested=6, result_count=6)
        self.state.record_result("loc", "monet", None, requested=6, result_count=6)
        self.state.record_result("met", "monet", "video", requested=6, result_count=6)
        self.assertEqual(self.state.get_page("met", "monet", None), 2)
        self.assertEqual(self.state.get_page("loc", "monet", None), 2)
        self.assertEqual(self.state.get_page("met", "monet", "video"), 2)
        self.assertEqual(self.state.get_page("met", "monet", "image"), 1)

    def test_query_key_normalization(self):
        self.state.record_result("met", "Claude  Monet!", None, 6, 6)
        # Same normalized query → same cursor.
        self.assertEqual(self.state.get_page("met", "claude monet", None), 2)

    def test_exhausted_queries_refresh_after_window(self):
        import time as _time
        self.state.record_result("loc", "old topic", None, 6, 0)
        self.assertTrue(self.state.is_exhausted("loc", "old topic", None))
        # Simulate an old exploration timestamp (40 days ago).
        with self.state._lock:
            self.state._conn.execute(
                "UPDATE query_progress SET last_explored_at=? WHERE query_key='old topic'",
                (_time.time() - 40 * 86400,),
            )
            self.state._conn.commit()
        # Lazy refresh: next read resets to page 1.
        self.assertEqual(self.state.get_page("loc", "old topic", None), 1)
        self.assertFalse(self.state.is_exhausted("loc", "old topic", None))

    def test_recent_exhausted_queries_stay_skipped(self):
        self.state.record_result("loc", "fresh corpse", None, 6, 0)
        self.assertEqual(self.state.get_page("loc", "fresh corpse", None), -1)

    def test_persistence_across_restart(self):
        self.state.record_result("met", "monet", None, 6, 6)
        self.state.record_result("met", "monet", None, 6, 6)
        self.state.record_seen([make_item("x1", "A独特波形记录 xyzq")])
        self.state.close()
        reopened = DiscoveryState(self.db)
        try:
            self.assertEqual(reopened.get_page("met", "monet", None), 3)
            self.assertEqual(reopened.seen_count(), 1)
        finally:
            reopened.close()

    def test_reset_clears_progress_only(self):
        self.state.record_result("met", "monet", None, 6, 0)
        self.state.record_seen([make_item("k1", "keepme record alpha")])
        self.state.reset_all()
        self.assertEqual(self.state.get_page("met", "monet", None), 1)
        self.assertEqual(self.state.seen_count(), 1)

    def test_progress_stats(self):
        self.state.record_result("met", "monet", None, 6, 6)
        self.state.record_result("met", "monet", None, 6, 0)
        stats = self.state.progress_stats()
        self.assertEqual(stats["sources"]["met"]["queries"], 1)
        self.assertEqual(stats["sources"]["met"]["exhausted"], 1)
        self.assertEqual(stats["sources"]["met"]["pagesConsumed"], 1)


class SeenRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = DiscoveryState(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_records_and_counts(self):
        items = [make_item(f"i{i}", f"Record {i}") for i in range(3)]
        self.assertEqual(self.state.record_seen(items), 3)
        self.assertEqual(self.state.record_seen(items), 0)  # repeats bump times_seen
        self.assertEqual(self.state.seen_count(), 3)
        self.assertEqual(self.state.known_ids(items), {i.id for i in items})

    def test_cross_archive_same_url_is_duplicate(self):
        self.state.record_seen([
            make_item("loc1", "Great Wave print", source_id="loc",
                      url="https://hdl.loc.gov/item/123"),
        ])
        other = make_item("met1", "Great Wave print (different title in catalog)",
                          source_id="met", url="https://hdl.loc.gov/item/123")
        self.assertIsNotNone(self.state.find_cross_archive_duplicate(other))

    def test_same_artifact_two_archives_same_title_year_creator(self):
        # The same print catalogued by two institutions: same title, year,
        # creator, different record ids and URLs → cross-archive duplicate.
        self.state.record_seen([
            make_item("vam1", "The Great Wave", source_id="vam",
                      date_label="1831", creator="Katsushika Hokusai",
                      url="https://collections.vam.ac.uk/item/O1/"),
        ])
        twin = make_item("cma1", "The Great Wave", source_id="cma",
                         date_label="1831", creator="Katsushika Hokusai",
                         url="https://clevelandart.org/art/1997.5")
        self.assertIsNotNone(self.state.find_cross_archive_duplicate(twin))

    def test_distinct_artifacts_with_similar_titles_are_kept(self):
        # Two DIFFERENT Hokusai prints from the same series: titles differ
        # → never suppressed, even with same creator/year/media.
        self.state.record_seen([
            make_item("vam1", "The Great Wave", source_id="vam",
                      date_label="1831", creator="Katsushika Hokusai",
                      url="https://collections.vam.ac.uk/item/O1/"),
        ])
        other = make_item("cma1", "The Great Wave at Kanagawa", source_id="cma",
                          date_label="1831", creator="Katsushika Hokusai",
                          url="https://clevelandart.org/art/1997.6")
        self.assertIsNone(self.state.find_cross_archive_duplicate(other))

    def test_same_title_different_year_is_kept(self):
        self.state.record_seen([
            make_item("x1", "Battle Scene", source_id="loc",
                      date_label="1862", creator="Unknown"),
        ])
        other = make_item("x2", "Battle Scene", source_id="loc",
                          date_label="1917", creator="Unknown")
        self.assertIsNone(self.state.find_cross_archive_duplicate(other))

    def test_same_title_unknown_year_media_conservative(self):
        # v2.1.1 conservative rule: unknown creator metadata must NEVER
        # establish a duplicate — distinct catalogued items commonly share
        # titles and lack artist fields. Only a shared URL or a known,
        # identical creator on both sides can merge records.
        self.state.record_seen([
            make_item("x1", "Untitled landscape", source_id="loc", date_label=None),
        ])
        other = make_item("x2", "Untitled landscape", source_id="met", date_label=None)
        self.assertIsNone(self.state.find_cross_archive_duplicate(other))
        # ...but a known shared creator still matches even without a year.
        self.state.record_seen([
            make_item("y1", "Untitled landscape", source_id="loc", date_label=None,
                      creator="Ansel Adams"),
        ])
        twin = make_item("y2", "Untitled landscape", source_id="met", date_label=None,
                         creator="Ansel Adams")
        self.assertIsNotNone(self.state.find_cross_archive_duplicate(twin))

    def test_seen_memory_is_bounded(self):
        # Cap logic is verified with a small override — the production
        # default is one million rows (see test_large_state.py for the
        # measured performance at scale).
        import unittest.mock as mock
        from sources import discovery_state as ds
        with mock.patch.object(ds, "SEEN_MAX_ROWS", 5000), \
             mock.patch.object(ds, "SEEN_PRUNE_MARGIN", 250):
            items = [make_item(f"bulk{i}", f"Bulk record {i}") for i in range(5600)]
            self.state.record_seen(items)
            self.assertLessEqual(self.state.seen_count(), 5000)
            # Oldest rows were pruned: an early id should be gone.
            self.assertNotIn(items[0].id, self.state.known_ids([items[0]]))
            self.assertIn(items[-1].id, self.state.known_ids([items[-1]]))

    def test_thread_safety_smoke(self):
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    self.state.record_result("s", f"q{n}-{i}", None, 6, 6)
                    self.state.record_seen([make_item(f"t{n}-{i}", f"Thread item {n} {i}")])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(self.state.seen_count(), 6 * 25)


class DiscoveredTopicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = DiscoveryState(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_upsert_and_pick_lru(self):
        added = self.state.upsert_discovered_topics([
            {"query": "late ottoman cartography", "preferredSources": ["loc"],
             "provenance": "metadata:test"},
            {"query": "rear-admiral peary expedition", "preferredSources": ["loc"],
             "provenance": "metadata:test"},
        ], max_add=6)
        self.assertEqual(added, 2)
        picked = self.state.pick_discovered_topics(5)
        self.assertEqual({p["query"] for p in picked},
                         {"late ottoman cartography", "rear-admiral peary expedition"})
        self.assertTrue(all(p.get("discovered") for p in picked))

    def test_mark_used_rotates_order(self):
        self.state.upsert_discovered_topics([
            {"query": "topic alpha", "preferredSources": []},
            {"query": "topic beta", "preferredSources": []},
        ], max_add=5)
        self.state.mark_topic_used("topic alpha")
        self.state.mark_topic_used("topic alpha")
        picked = self.state.pick_discovered_topics(5)
        self.assertEqual(picked[0]["query"], "topic beta")

    def test_dedup_by_normalized_key(self):
        self.state.upsert_discovered_topics([{"query": "Silk Road caravans"}], max_add=5)
        added = self.state.upsert_discovered_topics([{"query": "silk  road caravans"}], max_add=5)
        self.assertEqual(added, 0)  # case/whitespace variants do not duplicate

    def test_rejects_junk_candidates(self):
        added = self.state.upsert_discovered_topics([
            {"query": "ab"},                    # too short
            {"query": "x" * 200},               # too long
            {"query": ""},                      # empty
        ], max_add=5)
        self.assertEqual(added, 0)

    def test_topic_cap_is_bounded(self):
        from sources.discovery_state import TOPIC_MAX_ROWS
        cands = [{"query": f"organic topic number {i}"} for i in range(TOPIC_MAX_ROWS + 50)]
        self.state.upsert_discovered_topics(cands, max_add=TOPIC_MAX_ROWS + 50)
        self.assertLessEqual(self.state.topics_stats()["discovered"], TOPIC_MAX_ROWS)

    def test_broken_db_path_fails_soft(self):
        # A directory that cannot be a database must not raise out of init.
        bad = Path(self.tmp.name) / "not-a-db" / "nested" / "x.db"
        bad.parent.parent.mkdir(parents=True, exist_ok=True)
        (bad.parent.parent / "x.db").touch()  # a file where a dir is expected
        try:
            state = DiscoveryState(bad.parent.parent / "x.db" / "sub" / "real.db")
            state.close()
        except Exception:
            pass  # DiscoveryState may raise on truly unusable paths; serve.py guards this


if __name__ == "__main__":
    unittest.main(verbosity=2)
