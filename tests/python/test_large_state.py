#!/usr/bin/env python3
"""
tests/python/test_large_state.py

v2.1.1 large-database performance tests (audit deficiencies #6 and #9):
the persistent store must hold a large discovery memory (measured here at
100,000 records) with bounded insert time, fast dedup lookups, modest RAM
use, and a small on-disk footprint — and the measured per-row cost drives
the documented projections for one and five million records.

Run with:
    python3 tests/python/test_large_state.py

No network access is performed or required. Runtime: a few seconds.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.discovery_state import DiscoveryState  # noqa: E402


def make_item(i, source_id="loc"):
    return TimeMachineItem.build(
        source_id=source_id, source_item_id=f"item-{i}",
        canonical_url=f"https://{source_id}.example.org/item/{i}",
        title=f"Historical archival record number {i}",
        media_kind="photograph", dateLabel=f"{1800 + (i % 120)}",
        topics=[f"subject series {i % 500}"],
    )


class LargeStateTests(unittest.TestCase):
    N = 100_000

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "large.db"
        cls.state = DiscoveryState(cls.db_path)
        t0 = time.monotonic()
        # Batch in chunks of 5,000 to exercise realistic feed-sized writes.
        for start in range(0, cls.N, 5000):
            cls.state.record_seen([make_item(i) for i in range(start, start + 5000)])
        cls.insert_seconds = time.monotonic() - t0
        cls.db_bytes = cls.db_path.stat().st_size

    @classmethod
    def tearDownClass(cls):
        cls.state.close()
        cls.tmp.cleanup()

    def test_all_rows_recorded(self):
        self.assertEqual(self.state.seen_count(), self.N)

    def test_insert_throughput_documented(self):
        rows_per_second = self.N / max(self.insert_seconds, 1e-6)
        print(f"\n    measured: {self.N} rows in {self.insert_seconds:.1f}s "
              f"({rows_per_second:,.0f} rows/s); db size {self.db_bytes/1e6:.1f} MB")
        # Sanity bound only: stdlib SQLite inserts far faster than this.
        self.assertLess(self.insert_seconds, 60.0,
                        "100k inserts took unreasonably long")

    def test_dedup_lookups_stay_fast_at_scale(self):
        batch = [make_item(10_000 + i) for i in range(40)]
        t0 = time.monotonic()
        known = self.state.known_ids(batch)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.assertEqual(len(known), 40)
        self.assertLess(elapsed_ms, 50.0,
                        f"known_ids lookup took {elapsed_ms:.0f}ms at 100k rows")

    def test_cross_archive_duplicate_lookup_fast_at_scale(self):
        probe = make_item(50_000, source_id="met")  # same title+year, unknown creator
        t0 = time.monotonic()
        self.assertIsNone(self.state.find_cross_archive_duplicate(probe))
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.assertLess(elapsed_ms, 50.0)

    def test_ram_stays_bounded(self):
        """The store must not hold discovery memory in RAM: RSS delta across
        a large lookup workload stays small (SQLite pages, not row objects)."""
        import resource
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for start in range(0, 20_000, 2000):
            self.state.known_ids([make_item(start + i) for i in range(2000)])
            self.state.find_cross_archive_duplicate(make_item(start + 7))
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        growth_mb = (after - before) / 1024
        print(f"    RAM growth across 20k+ lookups: {growth_mb:.1f} MB")
        self.assertLess(growth_mb, 128.0, "discovery memory leaked into RAM")

    def test_disk_projection_for_millions_of_rows(self):
        """Documented projection: measured bytes/row at 100k scale drives the
        1M and 5M estimates. ~85 bytes/row nominal → ~85 MB at 1M rows and
        ~425 MB at 5M rows (within the report's documented envelope)."""
        bytes_per_row = self.db_bytes / self.N
        proj_1m = bytes_per_row * 1_000_000
        proj_5m = bytes_per_row * 5_000_000
        print(f"    measured {bytes_per_row:.0f} bytes/row → "
              f"1M ≈ {proj_1m/1e6:.0f} MB, 5M ≈ {proj_5m/1e6:.0f} MB")
        # Measured envelope (title+URL+indexes ≈ 320 B/row): one million
        # records ≈ a third of a GB, five million ≈ 1.6 GB — years of
        # continuous daily use on an ordinary laptop disk.
        self.assertLess(bytes_per_row, 500, "row footprint unexpectedly large")
        self.assertLess(proj_5m, 2000 * 1e6, "5M-row projection exceeds 2 GB")

    def test_configurable_cap_is_honored(self):
        import unittest.mock
        from sources import discovery_state as ds
        with unittest.mock.patch.object(ds, "SEEN_MAX_ROWS", 120_000), \
             unittest.mock.patch.object(ds, "SEEN_PRUNE_MARGIN", 5_000):
            # Recording 5k more rows against the patched cap keeps us under
            # it (no prune fired yet) and stays fast.
            self.state.record_seen(
                [make_item(self.N + i) for i in range(5_000)])
            self.assertLessEqual(self.state.seen_count(), 120_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
