#!/usr/bin/env python3
"""
tests/python/test_new_sources_hermetic.py

Hermetic tests for the v2.0 source layer additions: the seven new adapters
(Cleveland Museum of Art, Victoria & Albert Museum, Wikimedia Commons,
Internet Archive, BnF Gallica, Rijksmuseum, Harvard Art Museums), the
Metropolitan Museum v1.1 search migration, page-based deeper harvesting,
and the registry wiring.

Fixtures under tests/fixtures/ are, per file:
  - LIVE-CAPTURED real API responses captured during development:
      commons_search_response.json, cma_search_response.json,
      vam_search_response.json, met_v11_search_response.json,
      met_object_response.json, vam_object_response.json
  - docs-modeled responses (clearly labeled inside each file), used where
    the development sandbox could not reach the archive (rate limiting /
    edge blocks / key required):
      internetarchive_search_response.json, gallica_sru_response.xml,
      rijksmuseum_search_response.json, harvard_search_response.json

Run with:
    python3 tests/python/test_new_sources_hermetic.py

No network access is performed or required.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"

from sources.base import TimeMachineItem  # noqa: E402
from sources.cma import CmaSource, normalize_cma_artwork  # noqa: E402
from sources.vam import VamSource, normalize_vam_record, _larger_image  # noqa: E402
from sources.commons import CommonsSource, normalize_commons_page  # noqa: E402
from sources.internetarchive import (  # noqa: E402
    InternetArchiveSource, normalize_ia_doc, CURATED_COLLECTIONS,
)
from sources.gallica import GallicaSource, normalize_gallica_record  # noqa: E402
from sources.rijksmuseum import RijksmuseumSource, normalize_rijksmuseum_object  # noqa: E402
from sources.harvard import HarvardSource, normalize_harvard_record  # noqa: E402
from sources.met import MetSource, normalize_met_object  # noqa: E402
from sources.wellcome import WellcomeSource, normalize_wellcome_work  # noqa: E402
from sources.getty import (  # noqa: E402
    GettySource, normalize_getty_record, _sparql_search,
)
from sources import wellcome as wellcome_mod  # noqa: E402
from sources import getty as getty_mod  # noqa: E402
from sources.registry import ALL_SOURCES, search_source, list_sources  # noqa: E402
from sources import discovery as discovery_mod  # noqa: E402
from sources import cma as cma_mod  # noqa: E402
from sources import vam as vam_mod  # noqa: E402
from sources import commons as commons_mod  # noqa: E402
from sources import internetarchive as ia_mod  # noqa: E402
from sources import gallica as gallica_mod  # noqa: E402
from sources import rijksmuseum as rijks_mod  # noqa: E402
from sources import harvard as harvard_mod  # noqa: E402
from sources import met as met_mod  # noqa: E402


def load_fixture(name):
    path = FIXTURES / name
    if path.suffix == ".xml":
        return path.read_text(encoding="utf-8")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def patch_adapter_cache(module):
    """Keep adapter searches hermetic: no cache reads (miss -> fetch) and no
    cache writes (nothing lands in cache_data/ during tests)."""
    return mock.patch.multiple(module, cache_get=mock.Mock(return_value=None), cache_set=mock.Mock())


class ClevelandMuseumTests(unittest.TestCase):
    """Fixture is a real, live-captured Cleveland Open Access API response."""

    def setUp(self):
        self.raw = load_fixture("cma_search_response.json")

    def test_normalizes_all_records(self):
        arts = self.raw["data"]
        items = [i for i in (normalize_cma_artwork(a) for a in arts) if i]
        self.assertEqual(len(items), len(arts))

    def test_stable_identity_and_fields(self):
        art = self.raw["data"][0]
        item = normalize_cma_artwork(art)
        self.assertTrue(item.id.startswith("cma-"))
        self.assertEqual(item.sourceId, "cma")
        self.assertTrue(item.sourceUrl.startswith("https://clevelandart.org/art/"))
        self.assertTrue(item.thumbnailUrl.startswith("https://openaccess-cdn.clevelandart.org/"))
        self.assertIn("CC0", item.rights or "")

    def test_media_kind_mapping(self):
        items = [normalize_cma_artwork(a) for a in self.raw["data"]]
        kinds = {i.mediaKind for i in items if i}
        self.assertTrue(kinds <= {"artwork", "artifact", "photograph", "map", "document"})

    def test_search_uses_skip_pagination(self):
        source = CmaSource()
        with patch_adapter_cache(cma_mod), \
             mock.patch.object(cma_mod, "http_get_json", return_value=self.raw) as fetch:
            results = source.search("great wave", limit=3, page=2)
            called_url = fetch.call_args[0][0]
            self.assertIn("skip=3", called_url)
            self.assertIn("limit=3", called_url)
            self.assertEqual(len(results), 3)

    def test_registered_in_registry(self):
        self.assertIn("cma", [s.source_id for s in ALL_SOURCES])


class VamTests(unittest.TestCase):
    """Fixture is a real, live-captured V&A Collections API response."""

    def setUp(self):
        self.raw = load_fixture("vam_search_response.json")

    def test_normalizes_all_records_with_images(self):
        records = self.raw["records"]
        items = [i for i in (normalize_vam_record(r) for r in records) if i]
        self.assertEqual(len(items), len(records))
        for item in items:
            self.assertTrue(item.thumbnailUrl)
            self.assertTrue(item.sourceUrl.startswith("https://collections.vam.ac.uk/item/"))

    def test_iiif_manifest_kept(self):
        item = normalize_vam_record(self.raw["records"][0])
        self.assertTrue(item.iiifManifestUrl.startswith("https://iiif.vam.ac.uk/"))
        self.assertEqual(item.evidence.manifestUrl, item.iiifManifestUrl)

    def test_thumbnail_upsized_via_iiif(self):
        bigger = _larger_image("https://framemark.vam.ac.uk/collections/X/full/!100,100/0/default.jpg")
        self.assertEqual(bigger, "https://framemark.vam.ac.uk/collections/X/full/!600,600/0/default.jpg")

    def test_search_uses_live_verified_page_param(self):
        source = VamSource()
        with patch_adapter_cache(vam_mod), \
             mock.patch.object(vam_mod, "http_get_json", return_value=self.raw) as fetch:
            results = source.search("great wave", limit=4, page=2)
            url = fetch.call_args[0][0]
            self.assertIn("page=2", url)
            self.assertNotIn("page_offset", url)
            self.assertLessEqual(len(results), 4)

    def test_registered_in_registry(self):
        self.assertIn("vam", [s.source_id for s in ALL_SOURCES])


class WikimediaCommonsTests(unittest.TestCase):
    """Fixture is a real, live-captured Commons MediaWiki API response."""

    def setUp(self):
        self.raw = load_fixture("commons_search_response.json")

    def _pages(self):
        return list(((self.raw.get("query") or {}).get("pages") or {}).values())

    def test_normalizes_real_pages(self):
        items = [i for i in (normalize_commons_page(p) for p in self._pages()) if i]
        self.assertGreater(len(items), 0)
        for item in items:
            self.assertTrue(item.sourceUrl.startswith("https://commons.wikimedia.org/wiki/File:"))
            self.assertTrue(item.thumbnailUrl)

    def test_license_surfaced_as_rights(self):
        item = normalize_commons_page(self._pages()[0])
        self.assertIn("Public domain", item.rights or "")

    def test_non_bitmap_rejected(self):
        page = self._pages()[0]
        page["imageinfo"][0]["mime"] = "image/svg+xml"
        self.assertIsNone(normalize_commons_page(page))

    def test_tiny_images_rejected(self):
        page = self._pages()[0]
        page["imageinfo"][0]["width"] = 120
        self.assertIsNone(normalize_commons_page(page))

    def test_search_targets_file_namespace(self):
        source = CommonsSource()
        with patch_adapter_cache(commons_mod), \
             mock.patch.object(commons_mod, "http_get_json", return_value=self.raw) as fetch:
            source.search("apollo 11", limit=3, page=2)
            url = fetch.call_args[0][0]
            self.assertIn("gsrnamespace=6", url)
            self.assertIn("gsroffset=3", url)
            self.assertIn("filetype%3Abitmap", url)
            self.assertIn("FOXAITimeMachine", fetch.call_args[1]["headers"]["User-Agent"])


class InternetArchiveTests(unittest.TestCase):
    """Fixture is docs-modeled (clearly labeled); archive.org was rate-limited
    from the development sandbox. Normalization + query construction tested."""

    def setUp(self):
        self.raw = load_fixture("internetarchive_search_response.json")

    def test_mediatype_mapping(self):
        docs = self.raw["response"]["docs"]
        self.assertEqual(normalize_ia_doc(docs[0]).mediaKind, "moving_image")
        self.assertEqual(normalize_ia_doc(docs[1]).mediaKind, "audio")

    def test_services_img_thumbnail_and_details_url(self):
        item = normalize_ia_doc(self.raw["response"]["docs"][0])
        self.assertEqual(item.thumbnailUrl, "https://archive.org/services/img/" + item.sourceItemId)
        self.assertEqual(item.sourceUrl, f"https://archive.org/details/{item.sourceItemId}")

    def test_licenseurl_becomes_rights(self):
        item = normalize_ia_doc(self.raw["response"]["docs"][0])
        self.assertIn("creativecommons.org", item.rights or "")

    def test_curated_tier_queries_curated_collections(self):
        source = InternetArchiveSource()
        with patch_adapter_cache(ia_mod), \
             mock.patch.object(ia_mod, "http_get_json", return_value=self.raw) as fetch:
            source.search("cold war", limit=2, page=2)
            url = fetch.call_args[0][0]
            for col in CURATED_COLLECTIONS:
                self.assertIn(f"collection%3A{col}", url)
            self.assertIn("page=2", url)
            self.assertIn("output=json", url)

    def test_broad_tier_quality_gates_without_date_ceiling(self):
        # v2.1.1: the arbitrary 1979 ceiling is gone — modern historical
        # material must stay reachable; quality comes from the media gate,
        # engagement sort and client-side spam filtering instead.
        source = InternetArchiveSource()
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append(url)
            return {"response": {"docs": []}}  # curated tier empty -> broad tier fires

        with patch_adapter_cache(ia_mod), \
             mock.patch.object(ia_mod, "http_get_json", side_effect=fake_fetch):
            results = source.search("potato famine", limit=3, page=1)
            self.assertEqual(results, [])
            self.assertEqual(len(calls), 2)
            broad_url = calls[1]
            self.assertNotIn("year%3A", broad_url)      # no date ceiling
            self.assertIn("downloads+desc", broad_url)  # engagement sort kept
            self.assertIn("mediatype%3A", broad_url)    # media gate kept
            self.assertNotIn("collection%3Aprelinger", broad_url)

    def test_broad_tier_rejects_modern_entertainment_spam(self):
        source = InternetArchiveSource()
        spam_doc = {"identifier": "spam1", "title": "Minecraft gameplay part 12",
                    "mediatype": "movies", "year": 2024, "subject": ["gaming"]}
        with patch_adapter_cache(ia_mod), \
             mock.patch.object(ia_mod, "http_get_json",
                               return_value={"response": {"docs": [spam_doc]}}):
            self.assertEqual(source.search("minecraft", limit=3), [])

    def test_media_filter_narrows_collections(self):
        self.assertEqual(
            ia_mod._curated_query("war", "audio"), "(war) AND (collection:oldtimeradio)"
        )


class GallicaTests(unittest.TestCase):
    """Fixture is docs-modeled SRU XML (clearly labeled); Gallica's edge
    refused the development sandbox IP. Parsing + normalization tested."""

    def setUp(self):
        self.xml_text = load_fixture("gallica_sru_response.xml")

    def _records(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(self.xml_text)
        return [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "record"]

    def test_normalizes_all_records(self):
        records = self._records()
        items = [i for i in (normalize_gallica_record(r) for r in records) if i]
        self.assertEqual(len(items), 3)

    def test_ark_canonical_url(self):
        item = normalize_gallica_record(self._records()[0])
        self.assertEqual(item.sourceUrl, "https://gallica.bnf.fr/ark:/12148/bpt6k6543210")
        self.assertEqual(item.sourceItemId, "ark:/12148/bpt6k6543210")

    def test_dc_type_maps_to_media_kind(self):
        kinds = [normalize_gallica_record(r).mediaKind for r in self._records()]
        self.assertEqual(kinds, ["photograph", "document", "manuscript"])

    def test_iiif_thumbnail_uses_ark_pid(self):
        item = normalize_gallica_record(self._records()[0])
        self.assertIn("iiif/ark:/12148/bpt6k6543210/f1/", item.thumbnailUrl)

    def test_search_uses_sru_pagination(self):
        source = GallicaSource()
        with patch_adapter_cache(gallica_mod), \
             mock.patch.object(gallica_mod, "http_get_text", return_value=self.xml_text) as fetch:
            results = source.search("pompeii", limit=3, page=2)
            url = fetch.call_args[0][0]
            self.assertIn("startRecord=4", url)
            self.assertIn("maximumRecords=3", url)
            self.assertIn("operation=searchRetrieve", url)
            self.assertEqual(len(results), 3)

    def test_malformed_xml_fails_soft(self):
        source = GallicaSource()
        with patch_adapter_cache(gallica_mod), \
             mock.patch.object(gallica_mod, "http_get_text", return_value="<not-xml"):
            self.assertEqual(source.search("pompeii", limit=3), [])


class RijksmuseumTests(unittest.TestCase):
    """Fixture is docs-modeled (clearly labeled); no key was available."""

    def setUp(self):
        self.raw = load_fixture("rijksmuseum_search_response.json")

    def test_requires_key(self):
        with mock.patch.object(rijks_mod, "get_key", return_value=None):
            self.assertFalse(RijksmuseumSource().availability().available)
        with mock.patch.object(rijks_mod, "get_key", return_value="k"):
            self.assertTrue(RijksmuseumSource().availability().available)

    def test_normalizes_night_watch(self):
        obj = self.raw["artObjects"][0]
        item = normalize_rijksmuseum_object(obj)
        self.assertEqual(item.title, "The Night Watch")
        self.assertEqual(item.mediaKind, "artwork")
        self.assertEqual(item.dateLabel, "1642")
        self.assertEqual(item.location, "Amsterdam")
        self.assertEqual(item.people, ["Rembrandt van Rijn"])

    def test_imageless_objects_rejected(self):
        obj = {"objectNumber": "X-1", "title": "no image record"}
        self.assertIsNone(normalize_rijksmuseum_object(obj))

    def test_search_params_documented(self):
        source = RijksmuseumSource()
        with mock.patch.object(rijks_mod, "get_key", return_value="free-key"), \
             patch_adapter_cache(rijks_mod), \
             mock.patch.object(rijks_mod, "http_get_json", return_value=self.raw) as fetch:
            source.search("night watch", limit=2, page=3)
            url = fetch.call_args[0][0]
            self.assertIn("key=free-key", url)
            self.assertIn("p=3", url)
            self.assertIn("ps=2", url)
            self.assertIn("imgonly=true", url)


class HarvardMuseumsTests(unittest.TestCase):
    """Fixture is docs-modeled (clearly labeled); no key was available."""

    def setUp(self):
        self.raw = load_fixture("harvard_search_response.json")

    def test_requires_key(self):
        with mock.patch.object(harvard_mod, "get_key", return_value=None):
            self.assertFalse(HarvardSource().availability().available)
        with mock.patch.object(harvard_mod, "get_key", return_value="k"):
            self.assertTrue(HarvardSource().availability().available)

    def test_permission_restricted_records_skipped(self):
        records = self.raw["records"]
        items = [i for i in (normalize_harvard_record(r) for r in records) if i]
        self.assertEqual([i.sourceItemId for i in items], ["304467", "205500"])

    def test_public_record_normalized(self):
        item = normalize_harvard_record(self.raw["records"][0])
        self.assertEqual(item.title, "Self-Portrait Dedicated to Paul Gauguin")
        self.assertEqual(item.mediaKind, "artwork")
        self.assertEqual(item.people, ["Vincent van Gogh"])
        self.assertTrue(item.sourceUrl.startswith("https://www.harvardartmuseums.org/"))

    def test_search_requests_images_only(self):
        source = HarvardSource()
        with mock.patch.object(harvard_mod, "get_key", return_value="free-key"), \
             patch_adapter_cache(harvard_mod), \
             mock.patch.object(harvard_mod, "http_get_json", return_value=self.raw) as fetch:
            source.search("van gogh", limit=2, page=2)
            url = fetch.call_args[0][0]
            self.assertIn("hasimage=1", url)
            self.assertIn("page=2", url)
            self.assertIn("apikey=free-key", url)


class MetV11MigrationTests(unittest.TestCase):
    """The Met v1 search endpoint was retired 2026-10-01 (HTTP 410). The
    adapter must now use v1.1 search. Both fixtures are live-captured."""

    def setUp(self):
        self.search_raw = load_fixture("met_v11_search_response.json")
        self.object_raw = load_fixture("met_object_response.json")

    def test_search_url_is_v11(self):
        self.assertIn("/v1.1/search", met_mod.SEARCH_URL)

    def test_object_detail_url_still_v1(self):
        self.assertIn("/v1/objects/", met_mod.OBJECT_URL)

    def test_search_returns_normalized_items(self):
        source = MetSource()

        def fake_fetch(url, **kwargs):
            if "/v1.1/search" in url:
                return self.search_raw
            return self.object_raw

        with patch_adapter_cache(met_mod), \
             mock.patch.object(met_mod, "http_get_json", side_effect=fake_fetch):
            results = source.search("monet", limit=4)
            self.assertGreater(len(results), 0)
            for item in results:
                self.assertTrue(item.id.startswith("met-"))
                self.assertTrue(item.sourceUrl.startswith("https://www.metmuseum.org/art/collection/search/"))

    def test_page_becomes_offset(self):
        source = MetSource()
        seen_urls = []

        def fake_fetch(url, **kwargs):
            seen_urls.append(url)
            if "/v1.1/search" in url:
                return {"total": 0, "objectIDs": []}
            return {}

        with patch_adapter_cache(met_mod), \
             mock.patch.object(met_mod, "http_get_json", side_effect=fake_fetch):
            source.search("monet", limit=5, page=3)
            self.assertTrue(any("offset=10" in u for u in seen_urls))

    def test_normalize_real_object(self):
        item = normalize_met_object(self.object_raw)
        self.assertEqual(item.sourceItemId, str(self.object_raw["objectID"]))
        self.assertTrue(item.title)


class RegistryWiringTests(unittest.TestCase):
    def test_seventeen_sources_registered(self):
        self.assertEqual(len(ALL_SOURCES), 17)

    def test_expected_source_ids(self):
        ids = {s.source_id for s in ALL_SOURCES}
        self.assertEqual(ids, {
            "nasa", "loc", "met", "aic", "smithsonian", "dpla", "europeana", "nara",
            "cma", "vam", "commons", "internetarchive", "gallica", "rijksmuseum", "harvard",
            "wellcome", "getty",
        })

    def test_list_sources_reports_all(self):
        rows = list_sources()
        self.assertEqual(len(rows), 17)
        self.assertEqual({r["id"] for r in rows}, {s.source_id for s in ALL_SOURCES})

    def test_pagination_capability_flags(self):
        rows = {r["id"]: r for r in list_sources()}
        self.assertTrue(rows["wellcome"]["deepPagination"])
        self.assertTrue(rows["getty"]["deepPagination"])
        self.assertTrue(rows["nasa"]["deepPagination"])
        # v2.1.1: Smithsonian paginates with the documented rows/start pair.
        self.assertTrue(rows["smithsonian"]["deepPagination"])
        # v2.2.0: NARA OpenAPI documents page (maximum 10000) and limit.
        self.assertTrue(rows["nara"]["deepPagination"])

    def test_search_source_unknown_id_is_soft_empty(self):
        self.assertEqual(search_source("does-not-exist", "query"), [])

    def test_search_source_passes_page_to_adapter(self):
        calls = []

        class FakeSource:
            source_id = "fake"
            display_name = "Fake"
            requires_key = False

            def availability(self):
                from sources.base import SourceAvailability
                return SourceAvailability(True, None)

            def search(self, query, media=None, limit=10, page=1):
                calls.append({"query": query, "media": media, "limit": limit, "page": page})
                return []

        with mock.patch.dict("sources.registry._BY_ID", {"fake": FakeSource()}):
            search_source("fake", "apollo", media="image", limit=5, page=4)
            self.assertEqual(calls, [{"query": "apollo", "media": "image", "limit": 5, "page": 4}])

    def test_search_source_falls_back_when_adapter_has_no_page(self):
        class OldSource:
            source_id = "old"
            display_name = "Old"
            requires_key = False

            def availability(self):
                from sources.base import SourceAvailability
                return SourceAvailability(True, None)

            def search(self, query, media=None, limit=10):
                return ["worked"]

        with mock.patch.dict("sources.registry._BY_ID", {"old": OldSource()}):
            self.assertEqual(search_source("old", "q", page=2), ["worked"])


class WellcomeCollectionTests(unittest.TestCase):
    """Fixture is a real, live-captured Wellcome Catalogue v2 response."""

    def setUp(self):
        self.raw = load_fixture("wellcome_search_response.json")

    def test_normalizes_real_results(self):
        works = self.raw.get("results") or []
        items = [i for i in (normalize_wellcome_work(w) for w in works) if i]
        self.assertGreater(len(items), 0)
        for item in items:
            self.assertTrue(item.sourceUrl.startswith("https://wellcomecollection.org/works/"))
            self.assertTrue(item.thumbnailUrl.startswith("https://iiif.wellcomecollection.org/"))

    def test_license_surfaced_verbatim(self):
        work = self.raw["results"][0]
        item = normalize_wellcome_work(work)
        lic = work.get("license") or {}
        if lic.get("label"):
            self.assertEqual(item.rights, lic["label"])

    def test_imageless_works_rejected(self):
        work = dict(self.raw["results"][0])
        work["thumbnail"] = None
        self.assertIsNone(normalize_wellcome_work(work))

    def test_search_uses_native_page_pagination(self):
        source = WellcomeSource()
        with patch_adapter_cache(wellcome_mod), \
             mock.patch.object(wellcome_mod, "http_get_json", return_value=self.raw) as fetch:
            results = source.search("plague", limit=4, page=2)
            url = fetch.call_args[0][0]
            self.assertIn("page=2", url)
            self.assertIn("pageSize=4", url)
            self.assertLessEqual(len(results), 4)


class GettyMuseumTests(unittest.TestCase):
    """Fixtures are real, live-captured Getty SPARQL + record responses."""

    def setUp(self):
        self.sparql_raw = load_fixture("getty_sparql_search_response.json")
        self.record_raw = load_fixture("getty_object_response.json")

    def test_sparql_page_yields_uuids(self):
        bindings = self.sparql_raw["results"]["bindings"]
        uuids = [b["obj"]["value"].rsplit("/object/", 1)[1].split("/")[0]
                 for b in bindings if "/object/" in b["obj"]["value"]]
        self.assertGreater(len(uuids), 0)

    def test_normalize_real_record(self):
        item = normalize_getty_record(self.record_raw)
        self.assertIsNotNone(item)
        self.assertTrue(item.title)
        self.assertTrue(item.sourceUrl.startswith("https://www.getty.edu/art/collection/object/"))
        self.assertTrue(item.thumbnailUrl.startswith("https://media.getty.edu/iiif/image/"))
        # Bounded IIIF size requested, never the full-size file.
        self.assertIn("/full/600,", item.thumbnailUrl)

    def test_record_without_image_rejected(self):
        record = json.loads(json.dumps(self.record_raw))
        record["representation"] = []
        self.assertIsNone(normalize_getty_record(record))

    def test_search_walks_pages_via_offset(self):
        source = GettySource()
        seen_urls = []

        def fake_text(url, **kwargs):
            seen_urls.append(url)
            import json as _json
            return _json.dumps(self.sparql_raw)

        def fake_json(url, **kwargs):
            return self.record_raw

        with patch_adapter_cache(getty_mod), \
             mock.patch.object(getty_mod, "http_get_text", side_effect=fake_text), \
             mock.patch.object(getty_mod, "http_get_json", side_effect=fake_json):
            source.search("irises", limit=3, page=2)
            self.assertTrue(any("OFFSET+3" in u for u in seen_urls), seen_urls[:1])

    def test_sparql_literal_is_sanitized(self):
        source = GettySource()
        with patch_adapter_cache(getty_mod), \
             mock.patch.object(getty_mod, "http_get_text", return_value='{"results":{"bindings":[]}}') as fetch:
            source.search('quote" ANDevil; DROP', limit=3)
            sql = fetch.call_args[0][0]
            self.assertNotIn('"', sql.split("query=")[1])


class DiscoveryPageTests(unittest.TestCase):
    def _fake_search(self, store):
        def search_fn(source_id, query, limit=5, media=None, page=1):
            store.append((source_id, query, limit, media, page))
            return []
        return search_fn

    def test_discover_surprise_without_state_fetches_page_one(self):
        # v2.1 contract: without a persistent store there is no depth memory,
        # so every (source, query) fetch starts at page 1; the global `page`
        # parameter only rotates the query-pool order.
        store = []
        queries = [{"query": f"topic {i}", "preferredSources": ["nasa"]} for i in range(30)]
        with mock.patch.object(discovery_mod.random, "shuffle", lambda x: None):
            discovery_mod.discover_surprise(
                self._fake_search(store),
                lambda: [{"id": "nasa", "available": True}],
                queries=queries,
                limit=4,
                page=2,
            )
        self.assertTrue(store)
        self.assertTrue(all(call[4] == 1 for call in store))

    def test_discover_surprise_uses_stored_page_from_state(self):
        # With a persistent store, each (source, query) resumes at its
        # stored page instead of page one.
        calls = []

        def search_fn(source_id, query, limit=5, media=None, page=1):
            calls.append((source_id, query, limit, media, page))
            return []

        class FakeStore:
            def get_page(self, source_id, query, media):
                return 7  # stored cursor

            def record_result(self, *a, **k):
                return 8, False

            def mark_topic_used(self, q):
                pass

            def pick_discovered_topics(self, n):
                return []

            def known_ids(self, items):
                return set()

            def find_cross_archive_duplicate(self, item):
                return None

            def upsert_discovered_topics(self, candidates, max_add=6):
                return 0

        queries = [{"query": f"topic {i}", "preferredSources": ["nasa"]} for i in range(20)]
        with mock.patch.object(discovery_mod.random, "shuffle", lambda x: None):
            discovery_mod.discover_surprise(
                search_fn,
                lambda: [{"id": "nasa", "available": True}],
                queries=queries,
                limit=4,
                state=FakeStore(),
            )
        self.assertTrue(calls)
        self.assertTrue(all(call[4] == 7 for call in calls))

    def test_page_rotation_changes_query_order(self):
        queries = [{"query": f"topic {i}", "preferredSources": ["nasa"]} for i in range(40)]
        first_calls, second_calls = [], []
        with mock.patch.object(discovery_mod.random, "shuffle", lambda x: None):
            discovery_mod.discover_surprise(self._fake_search(first_calls),
                                            lambda: [{"id": "nasa", "available": True}],
                                            queries=queries, limit=4, page=1)
            discovery_mod.discover_surprise(self._fake_search(second_calls),
                                            lambda: [{"id": "nasa", "available": True}],
                                            queries=queries, limit=4, page=2)
        first_topics = [c[1] for c in first_calls]
        second_topics = [c[1] for c in second_calls]
        self.assertTrue(first_topics)
        self.assertNotEqual(first_topics[0], second_topics[0])
        self.assertNotEqual(first_topics, second_topics)

    def test_serve_build_feed_passes_page_to_discovery(self):
        import serve
        received = {}

        def fake_discover(*args, **kwargs):
            received.update(kwargs)
            return [], [], []

        with mock.patch.object(serve, "list_sources",
                               lambda: [{"id": "nasa", "available": True}]), \
             mock.patch.object(serve, "discover_surprise", fake_discover):
            serve.build_feed(20, surprise=True, page=3)
            self.assertEqual(received.get("page"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
