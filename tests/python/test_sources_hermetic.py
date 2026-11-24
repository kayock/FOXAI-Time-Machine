#!/usr/bin/env python3
"""
tests/python/test_sources_hermetic.py

Hermetic tests for the Time Machine source layer. "Hermetic" means: no live
network access required or attempted. Fixtures under tests/fixtures/ are
either real captured API responses (NASA) or hand-built responses modeled
directly on official API documentation (LOC, IIIF, institutional page).

Run with:
    python3 tests/python/test_sources_hermetic.py

For live network smoke checks (optional, NOT run here), see
tests/python/test_live_smoke.py.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"

from sources.base import (  # noqa: E402
    make_stable_id, choose_template, template_info, TEMPLATES,
    TimeMachineItem, clean_text,
)
from sources.nasa import normalize_nasa_item  # noqa: E402
from sources.loc import normalize_loc_item  # noqa: E402
from sources.iiif import normalize_iiif_manifest  # noqa: E402
from sources.institutional_page import normalize_institutional_page  # noqa: E402
from sources.registry import diversify, list_sources  # noqa: E402


def load_fixture(name):
    with (FIXTURES / name).open("r", encoding="utf-8") as f:
        return json.load(f)


class StableIdentityTests(unittest.TestCase):
    def test_same_source_item_id_gives_same_id(self):
        a = make_stable_id("nasa", "PIA24439", None)
        b = make_stable_id("nasa", "PIA24439", None)
        self.assertEqual(a, b)

    def test_different_source_item_id_gives_different_id(self):
        a = make_stable_id("nasa", "PIA24439", None)
        b = make_stable_id("nasa", "PIA99999", None)
        self.assertNotEqual(a, b)

    def test_falls_back_to_canonical_url_when_no_item_id(self):
        a = make_stable_id("iiif", None, "https://example.org/manifest.json")
        b = make_stable_id("iiif", None, "https://example.org/manifest.json")
        self.assertEqual(a, b)

    def test_raises_when_neither_id_nor_url_present(self):
        with self.assertRaises(ValueError):
            make_stable_id("iiif", None, None)

    def test_id_is_not_random_across_calls(self):
        # Regression guard for "never generate a new random ID every time
        # the feed refreshes" (brief section 6).
        ids = {make_stable_id("nasa", "PIA24439", None) for _ in range(5)}
        self.assertEqual(len(ids), 1)


class TemplateSelectionTests(unittest.TestCase):
    def test_photograph_maps_to_historical_photograph(self):
        self.assertEqual(choose_template("photograph"), "historical-photograph")

    def test_video_maps_to_moving_image_source(self):
        self.assertEqual(choose_template("video"), "moving-image-source")

    def test_audio_maps_to_audio_oral_history(self):
        self.assertEqual(choose_template("audio"), "audio-oral-history")

    def test_unknown_media_kind_falls_back_to_artifact_object(self):
        # A fossil like Dracorex/SUE should land here rather than needing a
        # parallel "fossil" template system (brief section 7).
        self.assertEqual(choose_template("fossil"), "artifact-object")
        self.assertEqual(choose_template(""), "artifact-object")
        self.assertEqual(choose_template(None), "artifact-object")

    def test_every_template_has_a_primary_action(self):
        for template_id, info in TEMPLATES.items():
            self.assertTrue(info.get("primaryAction"), f"{template_id} missing primaryAction")

    def test_template_action_contract_matches_brief(self):
        cases = {
            "moving-image-source": "Watch at Institution",
            "later-documentary": "Watch at Institution",
            "audio-oral-history": "Listen at Institution",
            "original-document": "View Original at Institution",
            "artifact-object": "View at Institution",
            "reconstructed-story": "Open Gazette",
            "infographic": "Open Gazette",
        }
        for template_id, expected_action in cases.items():
            self.assertEqual(template_info(template_id)["primaryAction"], expected_action)

    def test_gazette_templates_are_internal_not_external(self):
        for template_id in ("reconstructed-story", "photo-story", "infographic", "timeline", "then-and-now"):
            self.assertFalse(template_info(template_id)["external"])

    def test_source_media_templates_are_external(self):
        for template_id in ("historical-photograph", "moving-image-source", "audio-oral-history", "original-document"):
            self.assertTrue(template_info(template_id)["external"])

    def test_no_template_autoplays(self):
        for template_id, info in TEMPLATES.items():
            self.assertFalse(info.get("autoplay"), f"{template_id} must not autoplay")


class NormalizationCoreTests(unittest.TestCase):
    def test_clean_text_collapses_whitespace(self):
        self.assertEqual(clean_text("  a   b\n\nc  "), "a b c")

    def test_clean_text_handles_none(self):
        self.assertIsNone(clean_text(None))

    def test_clean_text_caps_length(self):
        long = "x" * 1000
        self.assertEqual(len(clean_text(long, max_len=50)), 50)

    def test_item_to_dict_is_json_serializable(self):
        item = TimeMachineItem.build(
            source_id="nasa", source_item_id="X1", canonical_url=None, title="Test",
        )
        json.dumps(item.to_dict())  # must not raise


class NasaAdapterTests(unittest.TestCase):
    """Verified against a real captured NASA Images API response."""

    def setUp(self):
        self.raw = load_fixture("nasa_search_response.json")
        self.items = self.raw["collection"]["items"]

    def test_normalizes_real_photo_record(self):
        item = normalize_nasa_item(self.items[0])
        self.assertIsNotNone(item)
        self.assertEqual(item.title, "Apollo Footprint")
        self.assertEqual(item.mediaKind, "photograph")
        self.assertEqual(item.sourceId, "nasa")
        self.assertEqual(item.sourceItemId, "PIA24439")
        self.assertTrue(item.imageUrl.endswith("~orig.jpg"))
        self.assertTrue(item.thumbnailUrl.endswith("~thumb.jpg"))
        self.assertIn("NASA", item.sourceInstitution)
        self.assertEqual(item.mediaTemplate, "historical-photograph")

    def test_normalizes_real_video_record_without_orig_link(self):
        # This fixture item has no 'canonical' image link at all — only a
        # preview thumbnail and a captions file. Must not crash.
        item = normalize_nasa_item(self.items[1])
        self.assertIsNotNone(item)
        self.assertEqual(item.mediaKind, "moving_image")
        self.assertEqual(item.mediaTemplate, "moving-image-source")

    def test_stable_id_is_deterministic_across_normalizations(self):
        a = normalize_nasa_item(self.items[0])
        b = normalize_nasa_item(self.items[0])
        self.assertEqual(a.id, b.id)

    def test_malformed_record_returns_none_not_exception(self):
        self.assertIsNone(normalize_nasa_item({}))
        self.assertIsNone(normalize_nasa_item({"data": []}))
        self.assertIsNone(normalize_nasa_item({"data": [{"nasa_id": "x"}]}))  # no title
        self.assertIsNone(normalize_nasa_item(None if False else "not a dict"))  # extreme garbage — should not raise


class LocAdapterTests(unittest.TestCase):
    """Verified against LOC's own published API documentation shape."""

    def setUp(self):
        self.raw = load_fixture("loc_search_response.json")
        self.results = self.raw["results"]

    def test_normalizes_documented_image_record(self):
        item = normalize_loc_item(self.results[0])
        self.assertIsNotNone(item)
        self.assertEqual(item.sourceId, "loc")
        self.assertEqual(item.mediaKind, "photograph")
        self.assertTrue(item.sourceUrl.startswith("https://"))
        self.assertIn("Navajo", item.topics[0] if item.topics else "")

    def test_normalizes_documented_audio_record(self):
        item = normalize_loc_item(self.results[1])
        self.assertIsNotNone(item)
        self.assertEqual(item.mediaKind, "audio")
        self.assertEqual(item.mediaTemplate, "audio-oral-history")

    def test_access_restricted_record_is_skipped(self):
        item = normalize_loc_item(self.results[2])
        self.assertIsNone(item)

    def test_malformed_record_returns_none_not_exception(self):
        self.assertIsNone(normalize_loc_item({}))
        self.assertIsNone(normalize_loc_item({"title": "no url"}))


class IiifAdapterTests(unittest.TestCase):
    def test_full_v3_manifest_normalizes_all_documented_fields(self):
        manifest = load_fixture("iiif_manifest_v3.json")
        item = normalize_iiif_manifest(manifest, manifest["id"])
        self.assertIsNotNone(item)
        self.assertIn("Saint George", item.title)
        self.assertIsNotNone(item.description)
        self.assertIsNotNone(item.thumbnailUrl)
        self.assertIsNotNone(item.imageUrl)
        self.assertEqual(item.sourceInstitution, "Example Archive")
        self.assertEqual(item.sourceUrl, "https://example-archive.org/items/saint-george-dragon")
        self.assertEqual(item.rights, "http://creativecommons.org/publicdomain/mark/1.0/")
        self.assertEqual(item.dateLabel, "c. 1450")

    def test_minimal_manifest_degrades_gracefully(self):
        manifest = load_fixture("iiif_manifest_minimal.json")
        item = normalize_iiif_manifest(manifest, manifest["id"])
        self.assertIsNotNone(item)  # has a label, so it's still usable
        self.assertEqual(item.title, "Bare Minimum Manifest")
        self.assertIsNone(item.thumbnailUrl)
        self.assertIsNone(item.imageUrl)

    def test_manifest_with_no_label_at_all_is_rejected_not_crashed(self):
        self.assertIsNone(normalize_iiif_manifest({"id": "https://example.org/x"}, "https://example.org/x"))

    def test_completely_malformed_manifest_does_not_raise(self):
        self.assertIsNone(normalize_iiif_manifest({"items": "not a list"}, "https://example.org/x"))
        self.assertIsNone(normalize_iiif_manifest({}, "https://example.org/x"))

    # --- media-kind detection (image vs audio vs video) ---

    def test_image_body_detected_as_photograph(self):
        manifest = load_fixture("iiif_manifest_v3.json")
        item = normalize_iiif_manifest(manifest, manifest["id"])
        self.assertEqual(item.mediaKind, "photograph")
        self.assertEqual(item.mediaTemplate, "historical-photograph")
        self.assertIsNone(item.duration)  # images never carry a duration

    def test_audio_body_detected_as_audio_with_duration(self):
        manifest = load_fixture("iiif_manifest_audio.json")
        item = normalize_iiif_manifest(manifest, manifest["id"])
        self.assertIsNotNone(item)
        self.assertEqual(item.mediaKind, "audio")
        self.assertEqual(item.mediaTemplate, "audio-oral-history")
        self.assertIsNotNone(item.audioUrl)
        self.assertIsNone(item.imageUrl)
        self.assertEqual(item.duration, "2:34")

    def test_video_body_detected_as_moving_image_with_duration(self):
        manifest = load_fixture("iiif_manifest_video.json")
        item = normalize_iiif_manifest(manifest, manifest["id"])
        self.assertIsNotNone(item)
        self.assertEqual(item.mediaKind, "moving_image")
        self.assertEqual(item.mediaTemplate, "moving-image-source")
        self.assertIsNone(item.imageUrl)
        self.assertIsNone(item.audioUrl)
        self.assertEqual(item.duration, "1:00")

    def test_duration_field_alone_does_not_imply_video(self):
        # Regression test for the exact bug described in the brief: a
        # canvas can carry an unrelated 'duration' field while its body is
        # unambiguously an image. Must not be classified as video.
        manifest = load_fixture("iiif_manifest_image_with_stray_duration.json")
        item = normalize_iiif_manifest(manifest, manifest["id"])
        self.assertIsNotNone(item)
        self.assertEqual(item.mediaKind, "photograph")
        self.assertNotEqual(item.mediaKind, "moving_image")
        self.assertIsNone(item.duration)  # duration only kept for audio/video kinds
        self.assertIsNotNone(item.imageUrl)


class InstitutionalPageFallbackTests(unittest.TestCase):
    def test_extracts_opengraph_and_json_ld(self):
        html = (FIXTURES / "institutional_page.html").read_text(encoding="utf-8")
        item = normalize_institutional_page(html, "https://fakemuseum.example.org/collection/great-wave")
        self.assertIsNotNone(item)
        self.assertEqual(item.title, "The Great Wave off Kanagawa")
        self.assertIn("Hokusai", item.description or item.people[0] if item.people else "")
        self.assertEqual(item.sourceInstitution, "Fake Museum")
        self.assertTrue(item.imageUrl.endswith(".jpg"))

    def test_page_with_no_metadata_at_all_returns_none(self):
        item = normalize_institutional_page("<html><body>nothing useful</body></html>", "https://example.org/x")
        self.assertIsNone(item)

    def test_broken_json_ld_does_not_crash_extraction(self):
        html = """<html><head>
          <meta property="og:title" content="Still Has A Title">
          <script type="application/ld+json">{ not valid json </script>
        </head><body></body></html>"""
        item = normalize_institutional_page(html, "https://example.org/x")
        self.assertIsNotNone(item)
        self.assertEqual(item.title, "Still Has A Title")


class RegistryTests(unittest.TestCase):
    def test_list_sources_reports_every_source_without_raising(self):
        sources = list_sources()
        ids = {s["id"] for s in sources}
        # v2.1: seventeen sources — the v2.0 fifteen plus the Wellcome
        # Collection and the J. Paul Getty Museum (documented SPARQL API).
        self.assertEqual(ids, {
            "nasa", "loc", "met", "aic", "smithsonian", "dpla", "europeana", "nara",
            "cma", "vam", "commons", "internetarchive", "gallica", "rijksmuseum", "harvard",
            "wellcome", "getty",
        })

    def test_keyless_sources_report_available_without_config(self):
        sources = {s["id"]: s for s in list_sources()}
        self.assertTrue(sources["nasa"]["available"])
        self.assertTrue(sources["loc"]["available"])
        self.assertTrue(sources["met"]["available"])
        self.assertTrue(sources["aic"]["available"])

    def test_key_required_sources_report_unavailable_without_config(self):
        sources = {s["id"]: s for s in list_sources()}
        for source_id in ("smithsonian", "dpla", "europeana", "nara"):
            self.assertFalse(sources[source_id]["available"])
            self.assertEqual(sources[source_id]["reason"], "API key not configured")

    def test_diversify_interleaves_media_kinds_not_grouped(self):
        items = []
        for i in range(5):
            items.append(TimeMachineItem.build(source_id="x", source_item_id=f"p{i}", canonical_url=None,
                                                 title=f"photo {i}", media_kind="photograph"))
        for i in range(3):
            items.append(TimeMachineItem.build(source_id="x", source_item_id=f"v{i}", canonical_url=None,
                                                 title=f"video {i}", media_kind="video"))
        result = diversify(items, limit=6)
        kinds = [i.mediaKind for i in result]
        # Should not be five photographs in a row before any video appears.
        self.assertNotEqual(kinds[:5], ["photograph"] * 5)
        self.assertIn("moving_image" if False else "video", kinds)  # sanity: video present in first 6

    def test_diversify_respects_limit(self):
        items = [TimeMachineItem.build(source_id="x", source_item_id=str(i), canonical_url=None, title=str(i))
                 for i in range(20)]
        self.assertEqual(len(diversify(items, limit=7)), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
