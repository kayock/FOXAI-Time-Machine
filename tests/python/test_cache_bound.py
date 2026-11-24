#!/usr/bin/env python3
"""Hermetic tests for the bounded on-disk source cache."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources import config  # noqa: E402


class CacheBoundTests(unittest.TestCase):
    def test_enforce_cache_bound_respects_file_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch.object(config, "CACHE_DIR", cache_dir), \
                 mock.patch.object(config, "CACHE_MAX_FILES", 3), \
                 mock.patch.object(config, "CACHE_MAX_BYTES", 10_000_000), \
                 mock.patch.object(config, "CACHE_MAX_AGE_SECONDS", 3600):
                now = time.time()
                for i in range(6):
                    path = cache_dir / f"{i}.json"
                    path.write_text(json.dumps({"_cached_at": now, "value": i}), encoding="utf-8")
                    time.sleep(0.01)
                config._enforce_cache_bound()
                remaining = list(cache_dir.glob("*.json"))
                self.assertLessEqual(len(remaining), 3)

    def test_enforce_cache_bound_drops_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            with mock.patch.object(config, "CACHE_DIR", cache_dir), \
                 mock.patch.object(config, "CACHE_MAX_FILES", 50), \
                 mock.patch.object(config, "CACHE_MAX_BYTES", 10_000_000), \
                 mock.patch.object(config, "CACHE_MAX_AGE_SECONDS", 10):
                old = cache_dir / "old.json"
                old.write_text(json.dumps({"_cached_at": time.time() - 100, "value": 1}), encoding="utf-8")
                fresh = cache_dir / "fresh.json"
                fresh.write_text(json.dumps({"_cached_at": time.time(), "value": 2}), encoding="utf-8")
                config._enforce_cache_bound()
                self.assertFalse(old.exists())
                self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
