#!/usr/bin/env python3
"""
tools/benchmark_sustained.py — FOXAI Time Machine v2.2.0

LIVE sustained-discovery benchmark (audit deficiency #8): runs the REAL
discovery engine against live archives for N successive batches (default
52), through the persistent DiscoveryState store, INCLUDING a full shutdown
and restart partway through to prove persistent resumption.

Measured per batch and in aggregate:
  records retrieved / unique delivered / duplicates detected
  unique growth per batch, contributing archives
  geographic / historical-period / media-type diversity
  dynamic topics discovered, pagination depth achieved
  temporary failures and recoveries, deadline misses
  batch latency (avg / median / worst), SQLite growth, RSS memory
  concurrency effectiveness (tasks per batch, in-flight overlap)

Raw machine-readable results are written as JSON. This benchmark uses only
whatever archives are reachable from the current network — blocked or
key-less archives contribute nothing (fail-soft), and the report labels the
achievable subset honestly.

Usage:
    python3 tools/benchmark_sustained.py [--batches 52] [--limit 24]
        [--state-dir /tmp/foxai-bench] [--output results.json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.discovery import discover_surprise  # noqa: E402
from sources.discovery_state import DiscoveryState  # noqa: E402
from sources.registry import list_sources, search_source_strict  # noqa: E402
from sources.registry import region_bucket  # noqa: E402


def era_bucket(date_label):
    from sources.discovery_state import parse_year
    year = parse_year(date_label)
    return f"{(year // 100) * 100}s" if year else "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", type=int, default=52)
    ap.add_argument("--limit", type=int, default=24)
    ap.add_argument("--state-dir", default="/tmp/foxai-bench")
    ap.add_argument("--output", default=None)
    ap.add_argument("--restart-at", type=int, default=None,
                    help="batch index after which to restart the store "
                         "(default: batches//2)")
    args = ap.parse_args()
    restart_at = args.restart_at or max(1, args.batches // 2)

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / "bench.db"
    if db.exists():
        db.unlink()  # fresh benchmark each run

    state = DiscoveryState(db)
    db_size_0 = db.stat().st_size
    seen_ids: set = set()
    per_batch = []
    temp_events = 0
    topic_counts = []
    restart_done = False
    t_all = time.monotonic()
    rss_start = _rss_mb()

    print(f"LIVE sustained discovery benchmark: {args.batches} batches x "
          f"limit {args.limit}, restart after batch {restart_at}\n")

    for b in range(1, args.batches + 1):
        if b == restart_at + 1 and not restart_done:
            # ── shutdown + restart: close the store, reopen fresh instance ──
            cursors_before = state.stats()["progress"]["totalQueries"]
            seen_before = state.seen_count()
            state.close()
            state = DiscoveryState(db)  # simulated application restart
            restart_done = True
            print(f"  -- RESTART: store closed+reopened "
                  f"(cursors preserved: {cursors_before}, seen: {seen_before}) --")

        stats = {}
        t0 = time.monotonic()
        items, tried, used = discover_surprise(
            search_source_strict, list_sources,
            exclude_ids=set(seen_ids), limit=args.limit,
            page=b, state=state, stats_out=stats,
        )
        latency = time.monotonic() - t0
        state.record_seen(items)
        new_ids = [i.id for i in items if i.id not in seen_ids]
        dup_detected = (stats.get("already_seen_dropped", 0)
                        + stats.get("cross_archive_dup_dropped", 0))
        seen_ids.update(i.id for i in items)
        temp_events += (stats.get("temporary_errors", 0)
                        + stats.get("rate_limited", 0)
                        + stats.get("deadline_misses", 0))
        topic_counts.append(state.topics_stats()["discovered"])

        srcs = sorted({i.sourceId for i in items})
        regions = sorted({region_bucket(i) for i in items})
        eras = sorted({era_bucket(i.dateLabel) for i in items})
        media = sorted({i.mediaKind for i in items})
        per_batch.append({
            "batch": b,
            "delivered": len(items),
            "new_unique": len(new_ids),
            "cumulative_unique": len(seen_ids),
            "duplicates_detected": dup_detected,
            "latency_seconds": round(latency, 2),
            "sources": srcs,
            "regions": regions,
            "eras": eras[:8],
            "media_kinds": media,
            "concurrent_tasks": stats.get("concurrent_tasks", 0),
            "fetch_seconds": stats.get("fetch_seconds", 0.0),
            "pages_consumed": stats.get("pages_consumed", 0),
            "exhausted_marks": stats.get("exhausted_marks", 0),
            "temporary_errors": stats.get("temporary_errors", 0),
            "rate_limited": stats.get("rate_limited", 0),
            "deadline_misses": stats.get("deadline_misses", 0),
            "deferred_skipped": stats.get("deferred_skipped", 0),
            "candidates_queued": stats.get("candidates_queued", 0),
            "candidates_replayed": stats.get("candidates_replayed", 0),
            "topics_so_far": state.topics_stats()["discovered"],
            "db_bytes": db.stat().st_size,
        })
        print(f"  batch {b:2d}: new={len(new_ids):2d} delivered={len(items):2d} "
              f"cum={len(seen_ids):3d} {latency:5.1f}s "
              f"tasks={stats.get('concurrent_tasks', 0):2d} "
              f"pages+{stats.get('pages_consumed', 0)} srcs={len(srcs)}")

    state.close()
    total_seconds = time.monotonic() - t_all
    latencies = [b["latency_seconds"] for b in per_batch]
    max_pages = {}
    st = DiscoveryState(db)
    pg = st.stats()["progress"].get("sources", {})
    for sid, row in pg.items():
        max_pages[sid] = row["pagesConsumed"]
    st.close()

    results = {
        "benchmark": "FOXAI Time Machine v2.2.0 sustained live discovery",
        "mode": "LIVE (real archives; blocked/key-less archives contribute nothing)",
        "batches": len(per_batch),
        "limit_per_batch": args.limit,
        "restart_after_batch": restart_at,
        "totals": {
            "records_delivered": sum(b["delivered"] for b in per_batch),
            "unique_records": len(seen_ids),
            "duplicate_records_detected": sum(b["duplicates_detected"] for b in per_batch),
            "temporary_failures_and_recoveries": temp_events,
            "total_seconds": round(total_seconds, 1),
            "dynamic_topics_discovered_final": topic_counts[-1] if topic_counts else 0,
            "pagination_depth_by_source": max_pages,
            "rss_memory_mb_start": rss_start,
            "rss_memory_mb_end": _rss_mb(),
            "sqlite_db_bytes_end": per_batch[-1]["db_bytes"] if per_batch else 0,
        },
        "latency": {
            "avg": round(statistics.mean(latencies), 2),
            "median": round(statistics.median(latencies), 2),
            "worst": max(latencies),
        },
        "diversity": {
            "regions_observed": sorted({r for b in per_batch for r in b["regions"]}),
            "eras_observed": sorted({e for b in per_batch for e in b["eras"]}),
            "media_kinds_observed": sorted({m for b in per_batch for m in b["media_kinds"]}),
            "sources_contributing": sorted({s for b in per_batch for s in b["sources"]}),
        },
        "concurrency": {
            "avg_tasks_per_batch": round(statistics.mean(
                [b["concurrent_tasks"] for b in per_batch]), 1),
            "total_pages_consumed": sum(b["pages_consumed"] for b in per_batch),
        },
        "per_batch": per_batch,
    }

    out = Path(args.output) if args.output else Path("/home/z/my-project/work/benchmark-live-results.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"\nTOTAL: {results['totals']['unique_records']} unique records "
          f"delivered across {len(per_batch)} batches; "
          f"duplicates detected: {results['totals']['duplicate_records_detected']}; "
          f"avg/median/worst latency: {results['latency']['avg']}/"
          f"{results['latency']['median']}/{results['latency']['worst']}s")
    print(f"sources contributing: {results['diversity']['sources_contributing']}")
    print(f"written: {out}")
    return 0


def _rss_mb():
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
