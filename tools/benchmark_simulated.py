#!/usr/bin/env python3
"""
tools/benchmark_simulated.py — FOXAI Time Machine v2.2.0

*** SIMULATED ENDURANCE TEST — NOT GENUINE HISTORICAL DISCOVERIES. ***

Every record below is synthetic and produced by a deterministic simulated
archive. Nothing here is a real historical record and none of it comes from
any archive. Its purpose is to prove the ENGINE's long-duration properties
over many batches at scale — sustained unique-record growth, exhaustion
rotation, temporary-failure recovery, restart resumption, storage bounds —
which are independent of which archives a particular network can reach.

For genuine discoveries see tools/benchmark_sustained.py (LIVE).

Usage:
    python3 tools/benchmark_simulated.py [--batches 60] [--archive-pages 12]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.discovery import discover_surprise  # noqa: E402
from sources.discovery_state import DiscoveryState  # noqa: E402

SOURCES = ("loc", "met", "cma", "vam", "nasa", "wellcome")


class SimulatedArchive:
    """Deterministic multi-page archive: `pages` pages of `per_page` records
    per (source, query), then exhausted. Every 7th batch suffers a
    temporary outage (records a temporary outcome; recovers next batch)."""

    def __init__(self, pages=12, per_page=6):
        self.pages = pages
        self.per_page = per_page
        self.fetches = []
        self.batch_no = 0
        self.outage_batches = set()

    def search_fn(self, source_id, query, limit=6, media=None, page=1):
        self.fetches.append((source_id, query, page))
        if self.batch_no in self.outage_batches:
            raise RuntimeError("simulated temporary outage")
        if page > self.pages:
            return []
        start = (page - 1) * self.per_page
        out = []
        for i in range(start, min(start + limit, self.pages * self.per_page)):
            rid = f"{source_id}-{query.replace(' ', '-')}-{i}"
            out.append(TimeMachineItem.build(
                source_id=source_id, source_item_id=rid,
                canonical_url=f"https://{source_id}.example.org/{rid}",
                title=f"{query.title()} simulated archival record {i}",
                media_kind=("photograph", "artwork", "document")[i % 3],
                dateLabel=f"{1620 + (i * 11) % 380}",
                topics=[f"{query} studies", "simulated archival series"],
                places=[["Paris, France", "Kyoto, Japan", "Lagos, Nigeria"][i % 3]],
            ))
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=60)
    ap.add_argument("--archive-pages", type=int, default=12)
    args = ap.parse_args()

    archive = SimulatedArchive(pages=args.archive_pages)
    queries = [{"query": f"simulated historic topic {i}",
                "preferredSources": list(SOURCES)} for i in range(24)]

    def list_sources_fn():
        return [{"id": s, "available": True} for s in SOURCES]

    tmp = tempfile.mkdtemp(prefix="foxai-simbench-")
    db = Path(tmp) / "sim.db"
    state = DiscoveryState(db)
    seen: set = set()
    per_batch = []
    restart_at = args.batches // 2
    t_all = time.monotonic()

    print(f"SIMULATED endurance test (NOT genuine discoveries): "
          f"{args.batches} batches, {args.archive_pages}-page simulated archives\n")

    for b in range(1, args.batches + 1):
        if (b - 1) % 7 == 4:  # periodic simulated outage batch
            archive.outage_batches.add(b)
        else:
            archive.outage_batches.discard(b)
        archive.batch_no = b

        if b == restart_at + 1:
            state.close()
            state = DiscoveryState(db)
            print(f"  -- RESTART (simulated): cursors preserved --")

        stats = {}
        t0 = time.monotonic()
        items, _tried, _used = discover_surprise(
            archive.search_fn, list_sources_fn, exclude_ids=set(seen),
            limit=24, page=b, state=state, stats_out=stats)
        latency = time.monotonic() - t0
        state.record_seen(items)
        new_ids = [i.id for i in items if i.id not in seen]
        seen.update(i.id for i in items)
        per_batch.append({
            "batch": b, "new_unique": len(new_ids), "delivered": len(items),
            "cumulative_unique": len(seen), "latency_seconds": round(latency, 2),
            "outage": b in archive.outage_batches,
            "temporary_errors": stats.get("temporary_errors", 0),
            "exhausted_marks": stats.get("exhausted_marks", 0),
            "skipped_exhausted": stats.get("skipped_exhausted", 0),
            "pages_consumed": stats.get("pages_consumed", 0),
            "db_bytes": db.stat().st_size,
        })
        print(f"  batch {b:2d}: new={len(new_ids):2d} cum={len(seen):4d} "
              f"{latency:4.1f}s outage={'Y' if b in archive.outage_batches else 'n'} "
              f"exh+{stats.get('exhausted_marks', 0):2d} skip={stats.get('skipped_exhausted', 0):2d}")

    state.close()
    lat = [x["latency_seconds"] for x in per_batch]
    outage_batches = [x for x in per_batch if x["outage"]]
    recovered = all(x["new_unique"] > 0 for x in per_batch
                    if not x["outage"] and x["batch"] > 1)
    results = {
        "benchmark": "FOXAI Time Machine v2.2.0 SIMULATED endurance test",
        "warning": "SIMULATED DATA — synthetic records from a deterministic "
                   "simulated archive; NOT genuine historical discoveries.",
        "batches": per_batch[-1]["batch"],
        "totals": {
            "unique_records": len(seen),
            "outage_batches": len(outage_batches),
            "recovery_after_every_outage": recovered,
            "total_seconds": round(time.monotonic() - t_all, 1),
            "db_bytes_end": per_batch[-1]["db_bytes"],
        },
        "latency": {"avg": round(statistics.mean(lat), 2),
                    "median": round(statistics.median(lat), 2),
                    "worst": max(lat)},
        "per_batch": per_batch,
    }
    out = Path("/home/z/my-project/work/benchmark-simulated-results.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nTOTAL: {len(seen)} unique SIMULATED records; every non-outage "
          f"batch recovered with fresh growth: {recovered}")
    print(f"written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
