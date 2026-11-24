#!/usr/bin/env python3
"""Hermetic normalization tests for Met and AIC keyless adapters."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.met import normalize_met_object  # noqa: E402
from sources.aic import (  # noqa: E402
    normalize_aic_artwork,
    build_aic_iiif_url,
    is_aic_iiif_url,
    aic_browser_image_url,
    usable_image_id,
    AicSource,
)


class MetNormalizeTests(unittest.TestCase):
    def test_normalizes_public_object_without_relabeling(self):
        obj = {
            "objectID": 544215,
            "title": "Cat Statuette",
            "objectURL": "https://www.metmuseum.org/art/collection/search/544215",
            "primaryImage": "https://images.metmuseum.org/CRDImages/eg/original/x.jpg",
            "primaryImageSmall": "https://images.metmuseum.org/CRDImages/eg/web-large/x.jpg",
            "artistDisplayName": "",
            "objectDate": "332–30 B.C.",
            "medium": "Cupreous metal",
            "culture": "Egyptian",
            "period": "Ptolemaic Period",
            "department": "Egyptian Art",
            "classification": "Metalwork-Sculpture",
            "objectName": "Statuette, cat",
            "isPublicDomain": True,
        }
        item = normalize_met_object(obj)
        self.assertIsNotNone(item)
        self.assertEqual(item.sourceId, "met")
        self.assertEqual(item.title, "Cat Statuette")
        self.assertEqual(item.mediaKind, "artifact")
        self.assertEqual(item.mediaTemplate, "artifact-object")
        self.assertTrue(item.sourceUrl.endswith("544215"))
        self.assertEqual(item.sourceInstitution, "The Metropolitan Museum of Art")

    def test_painting_classifies_as_artwork(self):
        item = normalize_met_object({
            "objectID": 1,
            "title": "A Painting",
            "objectURL": "https://www.metmuseum.org/art/collection/search/1",
            "classification": "Paintings",
            "objectName": "Painting",
            "primaryImageSmall": "https://example.org/a.jpg",
        })
        self.assertEqual(item.mediaKind, "artwork")
        self.assertEqual(item.mediaTemplate, "artwork-illustration")


class AicNormalizeTests(unittest.TestCase):
    def test_normalizes_artwork_with_iiif_image(self):
        obj = {
            "id": 116363,
            "title": "Field Armor for Man",
            "date_display": "c. 1520",
            "artist_display": "South German, Nuremberg",
            "medium_display": "Steel, iron, brass, leather, and textile",
            "department_title": "Applied Arts of Europe",
            "place_of_origin": "Nuremberg",
            "artwork_type_title": "Armor",
            "is_public_domain": True,
            "image_id": "0b992dcc-d94b-d47d-f1e3-88c1ceb024bd",
        }
        item = normalize_aic_artwork(obj, iiif_url="https://www.artic.edu/iiif/2")
        self.assertIsNotNone(item)
        self.assertEqual(item.sourceId, "aic")
        self.assertEqual(item.title, "Field Armor for Man")
        self.assertEqual(item.mediaKind, "artifact")
        self.assertIn("0b992dcc", item.imageUrl or "")
        self.assertEqual(item.sourceInstitution, "Art Institute of Chicago")
        self.assertEqual(item.mediaTemplate, "artifact-object")
        self.assertTrue(item.imageUrl.startswith("https://www.artic.edu/iiif/2/"))
        self.assertTrue(item.imageUrl.endswith("/full/843,/0/default.jpg"))
        self.assertIn("/full/400,/0/default.jpg", item.thumbnailUrl or "")

    def test_uses_api_config_iiif_url_not_a_hardcoded_host(self):
        url = build_aic_iiif_url(
            "2d484387-2509-5e8e-2c43-22f9981972eb",
            "https://www-test.artic.edu/iiif/2",
        )
        self.assertEqual(
            url,
            "https://www-test.artic.edu/iiif/2/2d484387-2509-5e8e-2c43-22f9981972eb/full/843,/0/default.jpg",
        )

    def test_rejects_records_without_usable_image_media(self):
        self.assertIsNone(usable_image_id(None))
        self.assertIsNone(usable_image_id(""))
        self.assertIsNone(usable_image_id("abc/../evil"))
        self.assertIsNone(normalize_aic_artwork({
            "id": 1, "title": "No picture", "image_id": None,
        }, iiif_url="https://www.artic.edu/iiif/2"))
        self.assertIsNone(normalize_aic_artwork({
            "id": 2, "title": "Blank id", "image_id": "  ",
        }))

    def test_browser_display_url_is_same_origin_proxy(self):
        official = "https://www.artic.edu/iiif/2/0b992dcc-d94b-d47d-f1e3-88c1ceb024bd/full/843,/0/default.jpg"
        self.assertTrue(is_aic_iiif_url(official))
        self.assertFalse(is_aic_iiif_url("https://evil.example/iiif/2/x/full/843,/0/default.jpg"))
        self.assertFalse(is_aic_iiif_url("https://www.artic.edu/secret"))
        proxied = aic_browser_image_url(official)
        self.assertTrue(proxied.startswith("/api/aic-image?u="))
        item = normalize_aic_artwork({
            "id": 116363,
            "title": "Field Armor for Man",
            "image_id": "0b992dcc-d94b-d47d-f1e3-88c1ceb024bd",
            "artwork_type_title": "Armor",
        }, iiif_url="https://www.artic.edu/iiif/2", for_browser=True)
        self.assertTrue(item.imageUrl.startswith("/api/aic-image?u="))
        self.assertIn("0b992dcc", item.imageUrl)

    def test_search_skips_imageless_hits_and_uses_response_config(self):
        import json
        fixture = json.loads((PROJECT_ROOT / "tests" / "fixtures" / "aic_search_response.json").read_text(encoding="utf-8"))
        from unittest.mock import patch
        with patch("sources.aic.cache_get", return_value=fixture), \
             patch("sources.aic.http_get_json") as mocked_get:
            items = AicSource().search("monet water lilies", limit=5)
            mocked_get.assert_not_called()
        self.assertEqual(len(items), 2)
        titles = [i.title for i in items]
        self.assertEqual(titles, ["Water Lilies", "American Gothic"])
        self.assertTrue(all(i.imageUrl.startswith("/api/aic-image?u=") for i in items))
        from urllib.parse import unquote
        self.assertIn("www.artic.edu/iiif/2", unquote(items[0].imageUrl))
        self.assertIn("3c27b499", unquote(items[0].imageUrl))


if __name__ == "__main__":
    unittest.main(verbosity=2)
