#!/usr/bin/env python3
"""Hermetic tests for Gazette edition assembly. No network."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.gazette import has_visual_media, item_to_story, select_edition  # noqa: E402


def make_item(item_id, title, *, image=True, description="A real archival hook.",
              date="1906", location="Chicago", source="aic"):
    return TimeMachineItem.build(
        source_id=source,
        source_item_id=item_id,
        canonical_url=f"https://example.org/{item_id}",
        title=title,
        media_kind="artwork",
        description=description,
        dateLabel=date,
        location=location,
        sourceInstitution="Art Institute of Chicago",
        imageUrl=f"https://example.org/{item_id}.jpg" if image else None,
        thumbnailUrl=f"https://example.org/{item_id}.t.jpg" if image else None,
    )


class GazetteEditionTests(unittest.TestCase):
    def test_lead_secondary_and_archives_come_from_visual_items(self):
        items = [make_item(str(i), f"Story {i}") for i in range(8)]
        edition = select_edition(items)
        self.assertTrue(edition["available"])
        self.assertEqual(edition["lead"]["title"], "Story 0")
        self.assertEqual([s["title"] for s in edition["secondary"]], ["Story 1", "Story 2", "Story 3", "Story 4"])
        self.assertEqual([s["title"] for s in edition["archives"]], ["Story 5", "Story 6", "Story 7"])
        self.assertEqual(edition["storyCount"], 8)

    def test_imageless_items_are_not_used_as_stories(self):
        visual = make_item("v", "Has picture")
        blank = make_item("b", "No picture", image=False)
        edition = select_edition([blank, visual])
        self.assertEqual(edition["lead"]["title"], "Has picture")
        self.assertEqual(edition["secondary"], [])
        self.assertFalse(has_visual_media(blank))

    def test_empty_pool_degrades_without_inventing_copy(self):
        edition = select_edition([])
        self.assertFalse(edition["available"])
        self.assertIsNone(edition["lead"])
        self.assertEqual(edition["secondary"], [])
        self.assertEqual(edition["archives"], [])
        self.assertEqual(edition["storyCount"], 0)

    def test_story_fields_are_item_fields_not_fabricated_newspaper_text(self):
        item = make_item("16568", "Water Lilies", description="France · Oil on canvas")
        story = item_to_story(item)
        self.assertEqual(story["title"], "Water Lilies")
        self.assertEqual(story["hook"], "France · Oil on canvas")
        self.assertEqual(story["dateLabel"], "1906")
        self.assertEqual(story["location"], "Chicago")
        self.assertEqual(story["sourceInstitution"], "Art Institute of Chicago")
        self.assertTrue(story["sourceUrl"].startswith("https://"))
        self.assertNotIn("said", story["hook"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
