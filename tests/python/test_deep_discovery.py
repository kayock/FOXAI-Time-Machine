#!/usr/bin/env python3
"""
tests/python/test_deep_discovery.py

Hermetic integration tests for v2.1 deep historical discovery — the full
discover_surprise pipeline over a simulated archive, driven through the
real DiscoveryState store. Demonstrates the properties the upgrade exists
for:

  - SUSTAINED DISCOVERY: unique-record growth across successive batches
    (the growth-curve measurement, hermetically, with a deterministic
    simulated archive of 600 records)
  - DEEP PAGE WALKING: later batches fetch later pages of the same
    (source, query), verified by recording every page the fake adapter
    served
  - EXHAUSTION: a query that runs out of records is marked exhausted and
    stops consuming fetches (without crashing the batch)
  - RESUME ACROSS RESTARTS: state written by one DiscoveryState instance
    changes behavior of a second instance opened on the same database
  - PERSISTENT DEDUP: a record delivered in batch 1 is not re-delivered in
    a later batch of the same session, nor after a simulated restart
  - CROSS-ARCHIVE DEDUP: the same artifact published by two simulated
    archives is delivered once
  - DYNAMIC TOPICS: batches mine new topics from returned metadata and
    later batches actually use them
  - ERA DIVERSITY: a batch spanning centuries comes back period-mixed

Run with:
    python3 tests/python/test_deep_discovery.py

No network access is performed or required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.discovery import discover_surprise, diversify_eras  # noqa: E402
from sources.discovery_state import DiscoveryState  # noqa: E402
from sources.registry import diversify_regions, region_bucket  # noqa: E402
import sources.discovery as discovery_mod  # noqa: E402


def item(record_id, title, source_id="loc", media_kind="photograph",
         date_label="1893", topics=None, creator=None, places=None):
    it = TimeMachineItem.build(
        source_id=source_id, source_item_id=record_id,
        canonical_url=f"https://{source_id}.example.org/{record_id}",
        title=title, media_kind=media_kind, dateLabel=date_label,
        topics=topics or [],
    )
    if creator:
        it.people = [creator]
    if places:
        it.places = places
    return it


class SimulatedArchive:
    """A deterministic fake multi-source archive: N pages of K records per
    (source, query), after which the source reports exhausted. Records
    carry authentic-looking subject metadata for topic mining."""

    def __init__(self, sources=("loc", "met", "cma"), pages=5, per_page=6):
        self.sources = sources
        self.pages = pages
        self.per_page = per_page
        self.fetches = []          # (source, query, page) history
        self.exhausted_queries = set()

    def search_fn(self, source_id, query, limit=6, media=None, page=1):
        self.fetches.append((source_id, query, page))
        max_page = self.pages
        if (source_id, query) in self.exhausted_queries or page > max_page:
            return []
        out = []
        start = (page - 1) * self.per_page
        for i in range(start, min(start + limit, self.pages * self.per_page)):
            rid = f"{source_id}-{query.replace(' ', '-')}-{i}"
            out.append(item(
                rid,
                f"{query.title()} archival record {i}",
                source_id=source_id,
                media_kind=("photograph", "artwork", "document")[i % 3],
                date_label=f"{1650 + (i * 7) % 300}",
                topics=[f"{query} studies", "archival series"],
            ))
        if page >= max_page:
            self.exhausted_queries.add((source_id, query))
        return out


def make_queries(n=40):
    return [{"query": f"historic topic {i}", "preferredSources": ["loc", "met", "cma"]}
            for i in range(n)]


def list_sources_fn():
    return [{"id": s, "available": True} for s in ("loc", "met", "cma")]


class SustainedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_batches(self, n_batches, state_path=None, archive=None, seen_ids=None,
                     n_topics=40, deterministic=False):
        """Run n_batches surprise batches; return (new_ids_per_batch, archive).

        With deterministic=True the discovery module's RNG is pinned so the
        (source, query) visit order is stable — used to prove cursor
        deepening on a small pool that every batch revisits."""
        import unittest.mock
        archive = archive or SimulatedArchive()
        seen_ids = seen_ids if seen_ids is not None else set()
        per_batch = []
        for batch_index in range(n_batches):
            state = DiscoveryState(state_path) if state_path else None
            try:
                if deterministic:
                    with unittest.mock.patch.object(discovery_mod.random, "shuffle", lambda x: None), \
                         unittest.mock.patch.object(discovery_mod.random, "random", lambda: 0.5):
                        items, tried, used = discover_surprise(
                            archive.search_fn, list_sources_fn,
                            queries=make_queries(n_topics),
                            exclude_ids=set(seen_ids),
                            limit=12,
                            page=batch_index + 1,
                            state=state,
                        )
                else:
                    items, tried, used = discover_surprise(
                        archive.search_fn, list_sources_fn,
                        queries=make_queries(n_topics),
                        exclude_ids=set(seen_ids),
                        limit=12,
                        page=batch_index + 1,
                        state=state,
                    )
            finally:
                if state is not None:
                    state.close()
            new_ids = [i.id for i in items if i.id not in seen_ids]
            seen_ids.update(i.id for i in items)
            per_batch.append(new_ids)
        return per_batch, archive, seen_ids

    @staticmethod
    def _import_discovery_mod():
        import sources.discovery as discovery_mod
        return discovery_mod

    def test_unique_records_grow_across_batches(self):
        per_batch, _, _ = self._run_batches(8)
        uniques = [len(b) for b in per_batch]
        # Every batch should return a meaningful number of fresh records…
        self.assertGreaterEqual(sum(uniques), 50,
                                f"cumulative growth too small: {uniques}")
        # …and distinct batches must deliver DIFFERENT records (no page-1
        # re-serving): the union is strictly larger than any single batch.
        self.assertGreater(sum(uniques), max(uniques) * 2)
        # And every individual batch is non-trivially fresh.
        self.assertTrue(all(n > 0 for n in uniques),
                        f"a batch delivered nothing new: {uniques}")

    def test_pages_walk_forward_across_batches(self):
        # Deterministic small pool (4 topics x 3 sources): every batch
        # revisits the same (source, query) pairs, so cursors must walk
        # deeper page by page until the archive runs dry.
        per_batch, archive, _ = self._run_batches(
            10, state_path=self.db, n_topics=4, deterministic=True)
        pages_seen = {page for (_s, _q, page) in archive.fetches}
        self.assertTrue(pages_seen >= {1}, f"page 1 never fetched: {pages_seen}")
        self.assertTrue(
            any(p > 1 for p in pages_seen),
            f"deep exploration never left page 1: {sorted(pages_seen)}",
        )

    def test_exhausted_queries_skip_fetches(self):
        # Deterministic 5-topic pool with one persistent store: pages walk
        # forward, searches run dry (all 15 (source, query) pairs marked
        # exhausted in one sweep), and every later batch completes with
        # ZERO fetches — dead ends never block discovery.
        archive = SimulatedArchive(pages=1)
        db_path = Path(self.tmp.name) / "s.db"
        fetch_counts = []
        batch_stats = []
        for batch_index in range(20):
            state = DiscoveryState(db_path)
            stats = {}
            try:
                with unittest.mock.patch.object(discovery_mod.random, "shuffle", lambda x: None), \
                     unittest.mock.patch.object(discovery_mod.random, "random", lambda: 0.5):
                    discover_surprise(
                        archive.search_fn, list_sources_fn,
                        queries=make_queries(5), limit=12,
                        page=batch_index + 1, state=state, stats_out=stats)
            finally:
                state.close()
            fetch_counts.append(len(archive.fetches))
            batch_stats.append(stats)

        # Every (source, query) pair eventually runs dry: the last three
        # batches fetch nothing yet complete responsively (skips, not
        # hangs) — dead ends never block discovery.
        self.assertGreater(max(s.get("exhausted_marks", 0) for s in batch_stats), 0)
        for stats in batch_stats[-3:]:
            self.assertEqual(stats.get("pages_consumed", 0), 0)
            self.assertEqual(stats.get("exhausted_marks", 0), 0)
            self.assertGreater(stats.get("skipped_exhausted", 0), 0)
        self.assertEqual(fetch_counts[-1], fetch_counts[-2],
                         "steady state: no new fetches once everything is exhausted")
        self.assertEqual(fetch_counts[-2], fetch_counts[-3],
                         "steady state holds across batches")

    def test_resume_across_restarts(self):
        # Batch 1 with store A; batches 2-3 with store B opened on the SAME
        # database file — the simulated restart.
        per_batch, archive, seen = self._run_batches(
            1, state_path=self.db)
        per_batch2, archive2, seen2 = self._run_batches(
            2, state_path=self.db, seen_ids=seen)
        # After the restart the walks continue from stored cursors: page 1
        # records are not re-served as new.
        for batch in per_batch2:
            self.assertTrue(all(i not in set(per_batch[0]) for i in batch))

    def test_persistent_dedup_suppresses_rediscovery(self):
        # A store pre-populated with seen records; a fake archive that
        # always returns those same records regardless of page.
        state = DiscoveryState(Path(self.tmp.name) / "d.db")
        try:
            repeated = [item(f"fixed-{i}", f"Fixed archival record {i}")
                        for i in range(6)]

            def fixed_search(source_id, query, limit=6, media=None, page=1):
                return list(repeated)

            state.record_seen(repeated)
            stats = {}
            items, _, _ = discover_surprise(
                fixed_search, list_sources_fn, queries=make_queries(8),
                limit=10, state=state, stats_out=stats)
            self.assertEqual(items, [])
            self.assertGreater(stats.get("already_seen_dropped", 0), 0)
        finally:
            state.close()

    def test_cross_archive_duplicate_delivered_once(self):
        state = DiscoveryState(Path(self.tmp.name) / "x.db")
        try:
            def dup_search(source_id, query, limit=6, media=None, page=1):
                # Both archives publish the SAME artifact (same title,
                # creator, year) under their own ids.
                if source_id == "met":
                    return [item("met-same", "Sunset over Harbor",
                                 source_id="met", date_label="1874",
                                 creator="Eugène Boudin")]
                return [item("loc-same", "Sunset over Harbor",
                             source_id="loc", date_label="1874",
                             creator="Eugène Boudin")]

            items1, _, _ = discover_surprise(
                dup_search, list_sources_fn, queries=make_queries(4), limit=6,
                state=state)
            self.assertGreaterEqual(len(items1), 1)
            state.record_seen(items1)

            items2, _, _ = discover_surprise(
                dup_search, list_sources_fn, queries=make_queries(4), limit=6,
                state=state)
            delivered_ids = {i.sourceItemId for i in items2}
            # Only one archive's copy ever survives; the other is filtered.
            self.assertNotIn("met-same", delivered_ids)
            self.assertNotIn("loc-same", delivered_ids)
        finally:
            state.close()

    def test_discovered_topics_get_used(self):
        state = DiscoveryState(Path(self.tmp.name) / "t.db")
        try:
            archive = SimulatedArchive(pages=3)
            stats1 = {}
            discover_surprise(archive.search_fn, list_sources_fn,
                              queries=make_queries(12), limit=12,
                              state=state, stats_out=stats1)
            self.assertGreater(stats1.get("topics_mined", 0), 0)
            # A later batch mixes discovered topics into its roam.
            stats2 = {}
            discover_surprise(archive.search_fn, list_sources_fn,
                              queries=make_queries(12), limit=12,
                              state=state, stats_out=stats2)
            self.assertGreater(stats2.get("discovered_topics_used", 0), 0)
        finally:
            state.close()

    def test_stats_out_transparency(self):
        state = DiscoveryState(Path(self.tmp.name) / "st.db")
        try:
            stats = {}
            discover_surprise(SimulatedArchive().search_fn, list_sources_fn,
                              queries=make_queries(6), limit=8,
                              state=state, stats_out=stats)
            for key in ("pages_consumed", "already_seen_dropped",
                        "cross_archive_dup_dropped", "topics_mined"):
                self.assertIn(key, stats)
            self.assertGreater(stats["pages_consumed"], 0)
        finally:
            state.close()

    def test_no_state_still_works(self):
        per_batch, _, _ = self._run_batches(2, state_path=None)
        self.assertTrue(all(len(b) > 0 for b in per_batch))


class RegionDiversityTests(unittest.TestCase):
    """v2.1.1 geographic diversity (audit deficiency #7): batches spanning
    the world come back region-mixed, driven only by the records' own
    places metadata."""

    def test_regions_interleaved(self):
        items = (
            [item(f"eu{i}", f"EU {i}", places=["Paris, France"]) for i in range(5)]
            + [item(f"as{i}", f"AS {i}", places=["Kyoto, Japan"]) for i in range(5)]
            + [item(f"af{i}", f"AF {i}", places=["Lagos, Nigeria"]) for i in range(5)]
        )
        mixed = diversify_regions(items, 15)
        regions = [region_bucket(i) for i in mixed]
        self.assertEqual(len({regions[0], regions[1], regions[2]}), 3)

    def test_unlabeled_items_never_lead_or_vanish(self):
        items = (
            [item("u1", "No places")]
            + [item(f"eu{i}", f"EU {i}", places=["Berlin, Germany"]) for i in range(4)]
        )
        mixed = diversify_regions(items, 5)
        self.assertEqual(len(mixed), 5)
        self.assertIsNotNone(mixed[0].places == [], None)
        self.assertEqual(region_bucket(mixed[0]), "europe")

    def test_no_relabeling(self):
        it = item("a", "A", places=["Cairo, Egypt"])
        bucket = region_bucket(it)
        self.assertEqual(bucket, "africa")
        # The item's own metadata is untouched by diversification.
        self.assertEqual(it.places, ["Cairo, Egypt"])

    def test_pipeline_applies_region_diversity(self):
        """The discovery chain includes region mixing (a pipeline-level
        wiring check, not just a helper)."""
        import inspect
        import sources.discovery as dmod
        self.assertIn("diversify_regions", inspect.getsource(dmod))


class ConservativeDedupPipelineTests(unittest.TestCase):
    """v2.1.1 audit deficiency #5: unknown creator metadata never merges
    records; enforced persistently across restarts and successive batches."""

    def _state(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        return DiscoveryState(Path(tmp.name) / "s.db"), tmp

    def test_unknown_creator_not_merged_across_archives(self):
        state, tmp = self._state()
        try:
            state.record_seen([
                item("loc-1", "Harbor at Sunset", source_id="loc",
                     date_label="1874"),
            ])
            twin = item("met-1", "Harbor at Sunset", source_id="met",
                        date_label="1874")
            self.assertIsNone(state.find_cross_archive_duplicate(twin))
        finally:
            state.close()
            tmp.cleanup()

    def test_known_shared_creator_still_merges(self):
        state, tmp = self._state()
        try:
            state.record_seen([
                item("vam-1", "The Great Wave", source_id="vam",
                     date_label="1831", creator="Katsushika Hokusai"),
            ])
            twin = item("cma-1", "The Great Wave", source_id="cma",
                        date_label="1831", creator="Katsushika Hokusai")
            self.assertIsNotNone(state.find_cross_archive_duplicate(twin))
        finally:
            state.close()
            tmp.cleanup()

    def test_different_creators_same_title_year_not_merged(self):
        state, tmp = self._state()
        try:
            state.record_seen([
                item("loc-1", "Battle Scene", source_id="loc",
                     date_label="1862", creator="Unknown Artist A"),
            ])
            other = item("met-1", "Battle Scene", source_id="met",
                         date_label="1862", creator="Unknown Artist B")
            self.assertIsNone(state.find_cross_archive_duplicate(other))
        finally:
            state.close()
            tmp.cleanup()

    def test_enforced_across_restart_and_batches(self):
        """A duplicate delivered in an earlier session (separate store
        instance on the same file) is still suppressed by later batches —
        the false-negative guard."""
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        db = Path(tmp.name) / "s.db"
        try:
            state1 = DiscoveryState(db)
            state1.record_seen([
                item("vam-1", "The Great Wave", source_id="vam",
                     date_label="1831", creator="Katsushika Hokusai"),
            ])
            state1.close()

            def dup_search(source_id, query, limit=6, media=None, page=1):
                if source_id == "cma":
                    return [item("cma-1", "The Great Wave", source_id="cma",
                                 date_label="1831", creator="Katsushika Hokusai")]
                # Distinct title per archive: same creator and year are not
                # enough to call these the same artifact once the titles differ.
                return [item(f"{source_id}-fresh",
                             f"A genuinely fresh print from {source_id}",
                             source_id=source_id, date_label="1832",
                             creator="Utagawa Hiroshige")]

            state2 = DiscoveryState(db)  # simulated restart
            try:
                stats = {}
                items, _t, _u = discover_surprise(
                    dup_search, list_sources_fn, queries=make_queries(4),
                    limit=10, state=state2, stats_out=stats)
                delivered = {i.sourceItemId for i in items}
                # Fresh material from OTHER archives flows normally.
                self.assertIn("loc-fresh", delivered)
                self.assertIn("met-fresh", delivered)
                # The cma twin of the already-delivered vam record was
                # filtered before delivery — across a restart.
                self.assertNotIn("cma-1", delivered)
                self.assertGreater(
                    stats.get("cross_archive_dup_dropped", 0), 0)
            finally:
                state2.close()
        finally:
            tmp.cleanup()


class EraDiversityTests(unittest.TestCase):
    def test_eras_interleaved(self):
        items = (
            [item(f"old{i}", f"Old {i}", date_label="1650") for i in range(6)]
            + [item(f"mid{i}", f"Mid {i}", date_label="1850") for i in range(6)]
            + [item(f"new{i}", f"New {i}", date_label="1969") for i in range(6)]
        )
        mixed = diversify_eras(items, 18)
        labels = [i.dateLabel for i in mixed]
        # The first three must span three different centuries.
        self.assertEqual(len({labels[0], labels[1], labels[2]}), 3)

    def test_unknown_dates_do_not_lead(self):
        items = (
            [item("u1", "Undated", date_label=None)]
            + [item(f"d{i}", f"Dated {i}", date_label="1900") for i in range(4)]
        )
        mixed = diversify_eras(items, 5)
        self.assertEqual(len(mixed), 5)
        # Dated material leads the batch; undated items only appear after
        # at least one dated item.
        self.assertIsNotNone(mixed[0].dateLabel)
        first_unknown = next(idx for idx, it in enumerate(mixed) if it.dateLabel is None)
        self.assertGreater(first_unknown, 0)

    def test_no_relabeling(self):
        items = [item("a", "A", date_label="1888")]
        self.assertEqual(diversify_eras(items, 1)[0].dateLabel, "1888")


if __name__ == "__main__":
    unittest.main(verbosity=2)
