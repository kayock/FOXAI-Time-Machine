#!/usr/bin/env python3
"""
tests/python/test_feed_endpoint.py

Hermetic tests for serve.build_feed — the function behind GET /api/feed,
including featured-seed resolution and Surprise Me general discovery.
No network access: list_sources / resolve_all_seeds / discover_surprise
are monkeypatched on the imported serve module.

Run with:
    python3 tests/python/test_feed_endpoint.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import serve  # noqa: E402
from sources.base import TimeMachineItem  # noqa: E402
from sources.seeds import SeedResolution  # noqa: E402


def make_item(item_id, title, media_kind="photograph"):
    return TimeMachineItem.build(
        source_id="nasa", source_item_id=item_id, canonical_url=f"https://example.org/{item_id}",
        title=title, media_kind=media_kind,
    )


class BuildFeedTests(unittest.TestCase):
    def setUp(self):
        # Always report at least one source available, so build_feed
        # doesn't short-circuit to the "no sources at all" empty case.
        self._orig_list_sources = serve.list_sources
        self._orig_resolve_all_seeds = serve.resolve_all_seeds
        self._orig_discover_surprise = serve.discover_surprise
        serve.list_sources = lambda: [{"id": "nasa", "name": "NASA", "requiresKey": False, "available": True, "reason": None}]

    def tearDown(self):
        serve.list_sources = self._orig_list_sources
        serve.resolve_all_seeds = self._orig_resolve_all_seeds
        serve.discover_surprise = self._orig_discover_surprise

    def _fake_resolutions(self, items):
        return [
            SeedResolution(title=f"seed-{i}", resolved=True, method="source-search", item=item,
                            sourceId="nasa", templateHint=None, matchedTemplateHint=None)
            for i, item in enumerate(items)
        ]

    def test_no_available_sources_returns_empty_and_not_live(self):
        serve.list_sources = lambda: [{"id": "nasa", "name": "NASA", "requiresKey": False, "available": False, "reason": "no key"}]
        items, live, tried = serve.build_feed(20)
        self.assertEqual(items, [])
        self.assertFalse(live)

    def test_exclude_ids_are_filtered_out_of_the_result(self):
        a, b, c = make_item("a", "Photo A"), make_item("b", "Photo B"), make_item("c", "Photo C")
        serve.resolve_all_seeds = lambda *args, **kwargs: self._fake_resolutions([a, b, c])
        items, live, tried = serve.build_feed(20, exclude_ids={a.id, b.id})
        self.assertTrue(live)
        self.assertEqual([i.id for i in items], [c.id])

    def test_no_exclude_ids_returns_everything_resolved(self):
        a, b = make_item("a", "Photo A"), make_item("b", "Photo B")
        serve.resolve_all_seeds = lambda *args, **kwargs: self._fake_resolutions([a, b])
        items, live, tried = serve.build_feed(20)
        self.assertEqual({i.id for i in items}, {a.id, b.id})

    def test_limit_is_bounded_even_if_more_items_resolve(self):
        many = [make_item(str(i), f"Photo {i}") for i in range(60)]
        serve.resolve_all_seeds = lambda *args, **kwargs: self._fake_resolutions(many)
        items, live, tried = serve.build_feed(500)  # way over MAX_FEED_LIMIT
        self.assertLessEqual(len(items), serve.MAX_FEED_LIMIT)

    def test_randomize_flag_is_passed_through_to_resolve_all_seeds_on_featured_path(self):
        seen_kwargs = {}

        def fake_resolve_all_seeds(seeds, search_fn, institutional_fn, limit=None, randomize=False):
            seen_kwargs["randomize"] = randomize
            return self._fake_resolutions([make_item("a", "Photo A")])

        serve.resolve_all_seeds = fake_resolve_all_seeds
        serve.build_feed(20, randomize=True, surprise=False)
        self.assertTrue(seen_kwargs["randomize"])

        serve.build_feed(20, randomize=False, surprise=False)
        self.assertFalse(seen_kwargs["randomize"])

    def test_excluding_everything_resolved_yields_empty_but_still_live(self):
        a = make_item("a", "Photo A")
        serve.resolve_all_seeds = lambda *args, **kwargs: self._fake_resolutions([a])
        items, live, tried = serve.build_feed(20, exclude_ids={a.id})
        self.assertEqual(items, [])
        self.assertTrue(live)  # sources were reachable, just nothing new right now

    def test_surprise_uses_general_discovery_not_featured_seeds(self):
        called = {"discovery": False, "seeds": False}
        surprise_item = make_item("s1", "Surprise find")

        def fake_discover(*args, **kwargs):
            called["discovery"] = True
            return [surprise_item], ["nasa", "loc"], ["Apollo mission control"]

        def fake_resolve(*args, **kwargs):
            called["seeds"] = True
            return self._fake_resolutions([make_item("featured", "Featured only")])

        serve.discover_surprise = fake_discover
        serve.resolve_all_seeds = fake_resolve
        items, live, tried = serve.build_feed(20, surprise=True)
        self.assertTrue(called["discovery"])
        self.assertFalse(called["seeds"])
        self.assertEqual([i.id for i in items], [surprise_item.id])
        self.assertEqual(tried, ["nasa", "loc"])
        self.assertTrue(live)

    def test_featured_path_does_not_call_general_discovery(self):
        called = {"discovery": False}

        def fake_discover(*args, **kwargs):
            called["discovery"] = True
            return [], [], []

        serve.discover_surprise = fake_discover
        serve.resolve_all_seeds = lambda *args, **kwargs: self._fake_resolutions([make_item("f", "Featured")])
        items, live, tried = serve.build_feed(20, surprise=False)
        self.assertFalse(called["discovery"])
        self.assertEqual(len(items), 1)


class ShutdownEndpointTests(unittest.TestCase):
    def test_shutdown_allowed_from_localhost(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.server = mock.Mock()
        sent = {}

        def send_json(payload, status=200):
            sent["payload"] = payload
            sent["status"] = status

        handler._send_json = send_json
        handler._send_error_json = lambda msg, status=400: sent.update({"error": msg, "status": status})
        handler._request_shutdown = mock.Mock()
        handler._read_json_body = lambda: {}
        handler.path = "/api/shutdown"
        handler.headers = {}
        handler.do_POST()
        self.assertEqual(sent.get("status"), 200)
        self.assertTrue(sent["payload"].get("ok"))
        handler._request_shutdown.assert_called_once()

    def test_shutdown_rejected_from_non_localhost(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.client_address = ("8.8.8.8", 12345)
        sent = {}
        handler._send_json = lambda *a, **k: sent.update({"ok": True})
        handler._send_error_json = lambda msg, status=400: sent.update({"error": msg, "status": status})
        handler._request_shutdown = mock.Mock()
        handler._read_json_body = lambda: {}
        handler.path = "/api/shutdown"
        handler.headers = {}
        handler.do_POST()
        self.assertEqual(sent.get("status"), 403)
        handler._request_shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
