#!/usr/bin/env python3
"""
tests/python/test_dynamic_topics.py

Hermetic tests for v2.1 organic topic expansion (sources/dynamic_topics.py)
— how FOXAI Time Machine finds NEW search topics inside authentic archival
metadata without any AI service. Covers:

  - topic mining from subjects/keywords/places metadata
  - frequency ranking (a subject appearing on several records ranks first)
  - generic-label and junk filtering (formats, bare years, overlong noise)
  - parent-query suppression (mined topics differ from the query that
    produced them)
  - cross-record phrase promotion
  - DiscoveryState integration: bounded upsert, LRU rotation, stats

Run with:
    python3 tests/python/test_dynamic_topics.py

No network access is performed or required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.dynamic_topics import mine_topics  # noqa: E402
from sources.discovery_state import DiscoveryState  # noqa: E402
from sources.base import TimeMachineItem  # noqa: E402


def make_item(item_id, topics=None, places=None, title="Record",
              source_id="loc", date_label="1893", collection=None):
    item = TimeMachineItem.build(
        source_id=source_id, source_item_id=item_id,
        canonical_url=f"https://{source_id}.example.org/{item_id}",
        title=title, media_kind="photograph", dateLabel=date_label,
    )
    item.topics = topics or []
    item.places = places or []
    if collection:
        from sources.base import Evidence
        item.evidence = Evidence(institution="Test", sourceItemId=item_id,
                                 sourceUrl=item.sourceUrl, collection=collection)
    return item


class MiningTests(unittest.TestCase):
    def test_mines_subject_metadata(self):
        items = [
            make_item("a", topics=["World's Columbian Exposition", "photographs"]),
            make_item("b", topics=["World's Columbian Exposition", "illinois"]),
        ]
        mined = mine_topics(items, parent_queries=["chicago world fair"])
        queries = [m["query"] for m in mined]
        self.assertIn("World's Columbian Exposition", queries)

    def test_frequency_ranks_first(self):
        items = [
            make_item("a", topics=["railroad construction"]),
            make_item("b", topics=["railroad construction"]),
            make_item("c", topics=["railroad construction"]),
            make_item("d", topics=["one-off topic"]),
        ]
        mined = mine_topics(items, max_topics=2)
        self.assertEqual(mined[0]["query"].lower(), "railroad construction")

    def test_generic_labels_filtered(self):
        items = [
            make_item("a", topics=["photographs", "History", "19th century", "prints"]),
            make_item("b", topics=["photographs", "History", "19th century", "prints"]),
        ]
        mined = mine_topics(items, max_topics=10)
        self.assertEqual(mined, [])

    def test_bare_year_filtered(self):
        items = [
            make_item("a", topics=["1893"]),
            make_item("b", topics=["1893"]),
        ]
        self.assertEqual(mine_topics(items, max_topics=10), [])

    def test_multi_word_phrase_passes_even_without_frequency(self):
        items = [make_item("a", topics=["zywiec highland costume"])]
        mined = mine_topics(items, max_topics=10)
        self.assertEqual(len(mined), 1)
        self.assertEqual(mined[0]["query"].lower(), "zywiec highland costume")

    def test_parent_query_not_remined(self):
        items = [
            make_item("a", topics=["Navajo Code Talkers"]),
            make_item("b", topics=["Navajo Code Talkers"]),
        ]
        mined = mine_topics(items, parent_queries=["Navajo Code Talkers"])
        self.assertEqual(mined, [])

    def test_provenance_and_sources(self):
        items = [
            make_item("a", topics=["Model T Ford"], source_id="loc"),
            make_item("b", topics=["Model T Ford"], source_id="loc"),
        ]
        mined = mine_topics(items, parent_queries=["old cars"])
        self.assertEqual(len(mined), 1)
        self.assertEqual(mined[0]["preferredSources"], ["loc"])
        self.assertIn("archival metadata", mined[0]["provenance"])

    def test_places_metadata_mined(self):
        items = [
            make_item("a", places=["Machu Picchu site"]),
            make_item("b", places=["Machu Picchu site"]),
        ]
        mined = mine_topics(items, max_topics=3)
        self.assertEqual(mined[0]["query"].lower(), "machu picchu site")

    def test_cap_respected(self):
        items = [
            make_item(str(i), topics=[f"distinct topic phrase {i}"])
            for i in range(20)
        ]
        mined = mine_topics(items, max_topics=4)
        self.assertEqual(len(mined), 4)

    def test_no_items_no_topics(self):
        self.assertEqual(mine_topics([]), [])
        self.assertEqual(mine_topics([None]), [])


class StoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = DiscoveryState(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_mined_topics_roundtrip_through_store(self):
        items = [
            make_item("a", topics=["suffrage parade washington"]),
            make_item("b", topics=["suffrage parade washington"]),
        ]
        mined = mine_topics(items, parent_queries=["voting history"])
        added = self.state.upsert_discovered_topics(mined, max_add=6)
        self.assertEqual(added, 1)
        picked = self.state.pick_discovered_topics(3)
        self.assertEqual(picked[0]["query"].lower(), "suffrage parade washington")
        self.assertTrue(picked[0]["discovered"])
        self.assertEqual(picked[0]["preferredSources"], ["loc"])

    def test_curated_style_topics_not_overwritten_by_mining(self):
        # A discovered topic can never clobber a different origin entry with
        # the same normalized key (ON CONFLICT DO NOTHING).
        self.state.upsert_discovered_topics([{"query": "gold rush camps"}], max_add=2)
        added = self.state.upsert_discovered_topics(
            [{"query": "GOLD RUSH CAMPS"}], max_add=2)
        self.assertEqual(added, 0)

    def test_stats_report_topic_growth(self):
        self.assertEqual(self.state.topics_stats()["discovered"], 0)
        items = [make_item("a", topics=["transatlantic cable landing"]),
                 make_item("b", topics=["transatlantic cable landing"])]
        self.state.upsert_discovered_topics(mine_topics(items), max_add=6)
        self.assertEqual(self.state.topics_stats()["discovered"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
