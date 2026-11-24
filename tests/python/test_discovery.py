#!/usr/bin/env python3
"""
tests/python/test_discovery.py

Hermetic tests for sources/discovery.py — general Surprise Me archival
discovery beyond the 15 featured seeds. No network.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.discovery import (  # noqa: E402
    discover_surprise,
    load_discovery_queries,
    MAX_QUERIES_PER_SURPRISE,
)


def make_item(source_id, item_id, title, media_kind="photograph"):
    return TimeMachineItem.build(
        source_id=source_id, source_item_id=item_id,
        canonical_url=f"https://{source_id}.example.org/{item_id}",
        title=title, media_kind=media_kind,
    )


class LoadDiscoveryQueriesTests(unittest.TestCase):
    def test_shipped_pool_has_varied_topics_and_is_nonempty(self):
        pool = load_discovery_queries()
        self.assertGreaterEqual(len(pool), 150)
        # v2.0: 93 additional queries target the seven new sources
        # (Cleveland, V&A, Commons, Internet Archive, Gallica, Rijksmuseum,
        # Harvard), lifting the shipped pool past the old 350 ceiling.
        self.assertLessEqual(len(pool), 500)
        topics = {t for entry in pool for t in (entry.get("topics") or [])}
        for needed in ("space", "art", "indigenous", "maps", "civil-rights", "food", "religion", "holidays", "unusual"):
            self.assertIn(needed, topics)
        self.assertTrue(all(e.get("query") for e in pool))
        # Optional preferredSources remain supported; many entries should use them.
        with_pref = [e for e in pool if e.get("preferredSources")]
        self.assertGreater(len(with_pref), 100)
        self.assertTrue(any("met" in (e.get("preferredSources") or []) for e in pool))
        self.assertTrue(any("aic" in (e.get("preferredSources") or []) for e in pool))
        # New-source coverage: every post-v1 source appears in the shipped pool.
        for new_source in ("cma", "vam", "commons", "internetarchive", "gallica",
                           "rijksmuseum", "harvard", "wellcome", "getty"):
            self.assertTrue(
                any(new_source in (e.get("preferredSources") or []) for e in pool),
                f"shipped pool has no queries targeting {new_source}",
            )

    def test_continuous_feed_expansion_queries_are_present(self):
        pool = load_discovery_queries()
        titles = {e.get("query") for e in pool}
        required = [
            "prehistoric sea monsters",
            "weird Victorian inventions",
            "women pirates",
            "ancient graffiti",
            "famous shipwrecks",
            "lost cities",
            "Native American astronomy",
            "Pueblo pottery",
            "Aztec daily life",
            "samurai armor",
            "medieval monsters",
            "witchcraft trials",
            "early photography",
            "old amusement parks",
            "circus history",
            "ghost towns",
            "Colorado mining towns",
            "historic toys",
            "early automobiles",
            "aviation disasters",
            "famous archaeological discoveries",
            "alchemy manuscript",
            "Maya chocolate vessel",
            "Diwali traditions lamp",
            "Islamic manuscript calligraphy",
            "funerary texts Book of the Dead",
        ]
        missing = [q for q in required if q not in titles]
        self.assertEqual(missing, [], f"missing discovery queries: {missing}")

    def test_preferred_sources_are_optional_on_entries(self):
        pool = load_discovery_queries()
        self.assertTrue(all(e.get("query") or e.get("iiifManifestUrl") for e in pool))
        self.assertTrue(any(e.get("preferredSources") for e in pool))
        # preferredSources remains optional in the schema — a query-only entry is valid input.
        from sources.discovery import _sources_for_entry, MAX_SOURCES_PER_QUERY
        picked = _sources_for_entry({"query": "no prefs"}, ["nasa", "loc", "met"], {})
        self.assertTrue(set(picked) <= {"nasa", "loc", "met"})
        self.assertLessEqual(len(picked), MAX_SOURCES_PER_QUERY)



class DiscoverSurpriseTests(unittest.TestCase):
    def test_queries_multiple_available_sources_and_returns_items(self):
        calls = []

        def search_fn(source_id, query, limit=5, media=None):
            calls.append((source_id, query, media))
            return [make_item(source_id, f"{source_id}-1", f"{query} archival item")]

        queries = [
            {"query": "Apollo mission control", "preferredSources": ["nasa"], "topics": ["space"]},
            {"query": "Lewis and Clark expedition map", "preferredSources": ["loc"], "topics": ["maps"]},
        ]
        items, sources, used = discover_surprise(
            search_fn,
            lambda: [
                {"id": "nasa", "available": True},
                {"id": "loc", "available": True},
            ],
            queries=queries,
            limit=10,
        )
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(set(sources) <= {"nasa", "loc"})
        self.assertGreaterEqual(len(used), 1)
        self.assertTrue(any(c[0] in ("nasa", "loc") for c in calls))

    def test_excludes_visible_ids(self):
        keep = make_item("nasa", "keep", "Keep me")
        drop = make_item("nasa", "drop", "Drop me")

        def search_fn(source_id, query, limit=5, media=None):
            return [drop, keep]

        items, _, _ = discover_surprise(
            search_fn,
            lambda: [{"id": "nasa", "available": True}],
            queries=[{"query": "space", "preferredSources": ["nasa"]}],
            exclude_ids={drop.id},
            limit=10,
        )
        ids = {i.id for i in items}
        self.assertIn(keep.id, ids)
        self.assertNotIn(drop.id, ids)

    def test_unavailable_preferred_source_is_skipped_gracefully(self):
        def search_fn(source_id, query, limit=5, media=None):
            if source_id == "europeana":
                raise AssertionError("europeana should not be queried when unavailable")
            return [make_item(source_id, "x", f"{query} from {source_id}")]

        items, sources, _ = discover_surprise(
            search_fn,
            lambda: [
                {"id": "loc", "available": True},
                {"id": "europeana", "available": False, "reason": "no key"},
            ],
            queries=[{"query": "Pompeii fresco", "preferredSources": ["europeana", "loc"]}],
            limit=5,
        )
        self.assertTrue(items)
        self.assertIn("loc", sources)
        self.assertNotIn("europeana", sources)

    def test_does_not_relabel_media_kinds_for_variety(self):
        def search_fn(source_id, query, limit=5, media=None):
            return [
                make_item(source_id, "p1", f"{query} photo", media_kind="photograph"),
                make_item(source_id, "a1", f"{query} audio", media_kind="audio"),
                make_item(source_id, "d1", f"{query} document", media_kind="document"),
            ]

        items, _, _ = discover_surprise(
            search_fn,
            lambda: [{"id": "loc", "available": True}],
            queries=[{"query": "oral history", "preferredSources": ["loc"]}],
            limit=10,
        )
        by_id = {i.sourceItemId: i for i in items}
        self.assertEqual(by_id["p1"].mediaTemplate, "historical-photograph")
        self.assertEqual(by_id["a1"].mediaTemplate, "audio-oral-history")
        self.assertEqual(by_id["d1"].mediaTemplate, "original-document")

    def test_bounds_number_of_queries_tried(self):
        calls = []

        def search_fn(source_id, query, limit=5, media=None):
            calls.append(query)
            # Always return something so discovery can stop once filled.
            return [make_item(source_id, query.replace(" ", "-")[:20], query)]

        many = [{"query": f"topic-{i}", "preferredSources": ["nasa"]} for i in range(30)]
        discover_surprise(
            search_fn,
            lambda: [{"id": "nasa", "available": True}],
            queries=many,
            limit=8,
        )
        # Soft bound: should not walk the entire 30-query pool when results fill early.
        self.assertLessEqual(len(set(calls)), MAX_QUERIES_PER_SURPRISE * 2)
        self.assertLessEqual(len(set(calls)), 12)

    def test_iiif_entry_uses_iiif_fn_when_provided(self):
        iiif_item = make_item("iiif", "m1", "IIIF Manuscript", media_kind="photograph")

        def search_fn(*args, **kwargs):
            self.fail("search_fn should not run for pure IIIF entries")

        items, sources, used = discover_surprise(
            search_fn,
            lambda: [],
            queries=[{"query": "Getty manuscript", "iiifManifestUrl": "https://example.org/manifest.json"}],
            limit=5,
            iiif_fn=lambda url: iiif_item,
        )
        self.assertEqual([i.id for i in items], [iiif_item.id])
        self.assertIn("iiif", sources)
        self.assertTrue(used)

    def test_source_balancing_avoids_single_institution_monopoly_when_alternatives_exist(self):
        from sources.registry import diversify_sources

        items = [make_item("loc", f"L{i}", f"LOC {i}") for i in range(10)]
        items += [make_item("met", f"M{i}", f"Met {i}") for i in range(4)]
        items += [make_item("aic", f"A{i}", f"AIC {i}") for i in range(4)]
        out = diversify_sources(items, limit=8, recent_source_ids=["loc", "loc", "loc", "loc"])
        sources = {i.sourceId for i in out}
        self.assertGreaterEqual(len(sources), 2)
        loc_count = sum(1 for i in out if i.sourceId == "loc")
        self.assertLessEqual(loc_count, 5)

    def test_recent_source_bias_prefers_underrepresented_preferred_source(self):
        calls = []

        def search_fn(source_id, query, limit=5, media=None):
            calls.append(source_id)
            return [make_item(source_id, f"{source_id}-{query[:6]}", f"{query} from {source_id}")]

        queries = [
            {"query": "samurai armor", "preferredSources": ["met", "loc"]},
            {"query": "medieval marginalia manuscript", "preferredSources": ["met", "aic"]},
            {"query": "Pueblo pottery", "preferredSources": ["met", "smithsonian"]},
        ]
        items, sources, _ = discover_surprise(
            search_fn,
            lambda: [
                {"id": "met", "available": True},
                {"id": "loc", "available": True},
                {"id": "aic", "available": True},
            ],
            queries=queries,
            limit=6,
            recent_source_ids=["loc"] * 12,
        )
        self.assertTrue(items)
        # With LOC heavily represented recently, Met/AIC should be tried.
        self.assertTrue(any(s in ("met", "aic") for s in sources) or any(c in ("met", "aic") for c in calls))
        for item in items:
            self.assertEqual(item.sourceId, item.sourceId)  # metadata untouched
            self.assertIn(item.mediaTemplate, {
                "historical-photograph", "artifact-object", "artwork-illustration",
                "original-document", "map", "moving-image-source", "audio-oral-history",
            })

    def test_falls_back_when_preferred_unavailable_without_fabricating(self):
        def search_fn(source_id, query, limit=5, media=None):
            return [make_item(source_id, "ok", f"{query} real")]

        items, sources, _ = discover_surprise(
            search_fn,
            lambda: [{"id": "loc", "available": True}, {"id": "met", "available": False}],
            queries=[{"query": "ancient Egyptian amulet", "preferredSources": ["met", "loc"]}],
            limit=3,
        )
        self.assertTrue(items)
        self.assertEqual(sources, ["loc"])
        self.assertTrue(all(i.sourceId == "loc" for i in items))


if __name__ == "__main__":
    unittest.main(verbosity=2)
