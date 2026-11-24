#!/usr/bin/env python3
"""
tests/python/test_seed_resolution.py

Hermetic tests for sources/seeds.py — proves the resolution order actually
works (preferred-source search, templateHint-aware selection, the
optionalSeedUrl fallback, and honest "unresolved" reporting) without any
live network access. search_fn/institutional_fn are fake, injected
functions so this never touches the real adapters or the internet.

Run with:
    python3 tests/python/test_seed_resolution.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.base import TimeMachineItem  # noqa: E402
from sources.seeds import resolve_seed, resolve_all_seeds  # noqa: E402


def make_item(source_id, item_id, title, media_kind="photograph"):
    item = TimeMachineItem.build(
        source_id=source_id, source_item_id=item_id,
        canonical_url=f"https://{source_id}.example.org/{item_id}",
        title=title, media_kind=media_kind,
    )
    item.sourceUrl = item.evidence and item.evidence.sourceUrl or f"https://{source_id}.example.org/{item_id}"
    return item


class FakeSearchRouter:
    """A tiny fake search_fn: source_id -> query -> [items]. Records every
    call so tests can assert exactly what was (and wasn't) tried."""

    def __init__(self, table):
        self.table = table  # {(source_id, query): [items]}
        self.calls = []

    def __call__(self, source_id, query, limit=5, media=None):
        self.calls.append((source_id, query))
        return self.table.get((source_id, query), [])


class PreferredSourceSearchTests(unittest.TestCase):
    def test_resolves_from_first_preferred_source_that_has_results(self):
        item = make_item("nasa", "PIA1", "Earthrise")
        router = FakeSearchRouter({("nasa", "Earthrise"): [item]})
        seed = {"title": "Earthrise", "searchQueries": ["Earthrise"], "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertTrue(result.resolved)
        self.assertEqual(result.method, "source-search")
        self.assertEqual(result.sourceId, "nasa")
        self.assertEqual(result.item.title, "Earthrise")
        self.assertIn(("nasa", "Earthrise"), router.calls)

    def test_tries_multiple_search_queries_for_one_source(self):
        item = make_item("loc", "L1", "Navajo Code Talker Oral History Interview", media_kind="audio")
        router = FakeSearchRouter({("loc", "Navajo Code Talkers oral history"): [item]})
        seed = {
            "title": "Navajo Code Talkers",
            "searchQueries": ["Navajo Code Talkers", "Navajo Code Talkers oral history"],
            "preferredSources": ["loc"],
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertTrue(result.resolved)
        self.assertEqual(result.item.title, "Navajo Code Talker Oral History Interview")
        # both queries should have been tried since the first came back empty
        self.assertIn(("loc", "Navajo Code Talkers"), router.calls)
        self.assertIn(("loc", "Navajo Code Talkers oral history"), router.calls)

    def test_falls_through_to_second_preferred_source(self):
        item = make_item("nara", "N1", "Removal Act Record", media_kind="document")
        router = FakeSearchRouter({("nara", "q"): [item]})  # loc returns nothing
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": ["loc", "nara"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertTrue(result.resolved)
        self.assertEqual(result.sourceId, "nara")


class TemplateHintTests(unittest.TestCase):
    def test_prefers_result_matching_template_hint_over_first_candidate(self):
        # Two different sources return different-shaped real results; the
        # audio one truthfully matches the hint, so it should be chosen
        # even though it wasn't first.
        doc_item = make_item("loc", "D1", "A related document", media_kind="document")
        audio_item = make_item("loc", "A1", "An oral history clip", media_kind="audio")
        router = FakeSearchRouter({("loc", "q"): [doc_item, audio_item]})
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": ["loc"], "templateHint": "audio-oral-history"}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertTrue(result.resolved)
        self.assertEqual(result.item.title, "An oral history clip")
        self.assertTrue(result.matchedTemplateHint)

    def test_does_not_relabel_a_result_to_match_the_hint(self):
        # Only a photograph is available; the hint asks for video, which is
        # a strict media requirement (brief: "when templateHint represents
        # an important media requirement, the result is compatible with it").
        # Under the tightened rules this candidate is too weak (media-
        # incompatible) to accept outright — resolution must fall through
        # rather than accept it, and it must never be dishonestly coerced
        # into a moving-image template just because that's what was hoped for.
        photo = make_item("nasa", "P1", "Apollo photo", media_kind="photograph")
        router = FakeSearchRouter({("nasa", "Apollo 11"): [photo]})
        seed = {"title": "Apollo 11", "searchQueries": ["Apollo 11"], "preferredSources": ["nasa"],
                "templateHint": "moving-image-source"}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        # No fallback configured and the only candidate is media-incompatible
        # with a strict hint -> honestly unresolved, never coerced into video.
        self.assertEqual(result.method, "unresolved")
        self.assertIsNone(result.item)

    def test_no_hint_given_reports_none_not_false(self):
        item = make_item("nasa", "P1", "Photo")
        router = FakeSearchRouter({("nasa", "q"): [item]})
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertIsNone(result.matchedTemplateHint)


class InstitutionalFallbackTests(unittest.TestCase):
    def test_fallback_invoked_when_source_search_returns_nothing(self):
        router = FakeSearchRouter({})  # nothing from any source
        fallback_item = make_item("institutional_page", None, "Rosetta Stone")
        calls = []

        def institutional_fn(url):
            calls.append(url)
            return fallback_item

        seed = {
            "title": "Rosetta Stone", "searchQueries": ["Rosetta Stone"], "preferredSources": ["europeana"],
            "optionalSeedUrl": "https://www.britishmuseum.org/collection/egypt/explore-rosetta-stone",
        }
        result = resolve_seed(seed, router, institutional_fn)
        self.assertTrue(result.resolved)
        self.assertEqual(result.method, "institutional-fallback")
        self.assertEqual(result.sourceId, "institutional_page")
        self.assertEqual(calls, ["https://www.britishmuseum.org/collection/egypt/explore-rosetta-stone"])

    def test_fallback_not_invoked_when_source_search_already_succeeded(self):
        item = make_item("nasa", "P1", "Found it")
        router = FakeSearchRouter({("nasa", "q"): [item]})
        calls = []

        def institutional_fn(url):
            calls.append(url)
            return None

        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": ["nasa"], "optionalSeedUrl": "https://example.org"}
        result = resolve_seed(seed, router, institutional_fn)
        self.assertEqual(result.method, "source-search")
        self.assertEqual(calls, [])  # optionalSeedUrl must be dead weight when search already worked

    def test_fallback_receives_exactly_the_supplied_url_no_more_no_less(self):
        seen = []

        def institutional_fn(url):
            seen.append(url)
            return None

        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": [], "optionalSeedUrl": "https://exact.example.org/page"}
        resolve_seed(seed, FakeSearchRouter({}), institutional_fn)
        self.assertEqual(seen, ["https://exact.example.org/page"])


class FallbackTemplateHintTests(unittest.TestCase):
    """
    The institutional-page fallback always normalizes a page as the generic
    'document' media kind (it can't reliably detect real media type from
    arbitrary page metadata). When the fallback is generic and the seed's
    templateHint is a safe, non-media-claiming presentation, the hint should
    guide presentation. It must never be used to claim playback capability
    the page hasn't demonstrated, and must never override a fallback that
    already carries a confident, non-generic media kind.
    """

    def _generic_fallback_item(self):
        # Mirrors exactly what sources/institutional_page.py normalizes a
        # page to: mediaKind="document" (its hardcoded default).
        return TimeMachineItem.build(
            source_id="institutional_page", source_item_id=None,
            canonical_url="https://example.org/page", title="A Museum Page",
            media_kind="document",
        )

    def test_safe_hint_is_applied_to_a_generic_fallback(self):
        item = self._generic_fallback_item()
        seed = {
            "title": "Dracorex hogwartsia", "searchQueries": ["Dracorex hogwartsia"],
            "preferredSources": ["dpla"], "templateHint": "artifact-object",
            "optionalSeedUrl": "https://www.childrensmuseum.org/stories/meet-dracorex-hogwartsia",
        }
        result = resolve_seed(seed, FakeSearchRouter({}), institutional_fn=lambda url: item)
        self.assertTrue(result.resolved)
        self.assertEqual(result.method, "institutional-fallback")
        self.assertEqual(result.item.mediaTemplate, "artifact-object")
        self.assertTrue(result.matchedTemplateHint)

    def test_media_claiming_hint_is_not_applied_to_a_generic_fallback(self):
        # A generic bio/profile page must not be presented as if it offers
        # playable audio just because the seed hoped for oral history.
        item = self._generic_fallback_item()
        seed = {
            "title": "Navajo Code Talkers", "searchQueries": ["Navajo Code Talkers"],
            "preferredSources": ["loc"], "templateHint": "audio-oral-history",
            "optionalSeedUrl": "https://guides.loc.gov/navajo-code-talkers/profiles/alfred-newman",
        }
        result = resolve_seed(seed, FakeSearchRouter({}), institutional_fn=lambda url: item)
        self.assertTrue(result.resolved)
        self.assertNotEqual(result.item.mediaTemplate, "audio-oral-history")
        self.assertEqual(result.item.mediaTemplate, "original-document")  # untouched default for a document
        self.assertFalse(result.matchedTemplateHint)  # honestly reported as not matching

    def test_hint_does_not_override_a_fallback_with_a_confident_non_generic_media_kind(self):
        # If the fallback somehow already resolved to a specific, real media
        # kind, a template hint must not overwrite that truth.
        item = TimeMachineItem.build(
            source_id="institutional_page", source_item_id=None,
            canonical_url="https://example.org/photo-page", title="A Photograph Page",
            media_kind="photograph",
        )
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": [], "templateHint": "artifact-object",
                "optionalSeedUrl": "https://example.org/photo-page"}
        result = resolve_seed(seed, FakeSearchRouter({}), institutional_fn=lambda url: item)
        self.assertEqual(result.item.mediaKind, "photograph")
        self.assertEqual(result.item.mediaTemplate, "historical-photograph")

    def test_no_hint_leaves_generic_fallback_untouched(self):
        item = self._generic_fallback_item()
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": [], "optionalSeedUrl": "https://example.org/page"}
        result = resolve_seed(seed, FakeSearchRouter({}), institutional_fn=lambda url: item)
        self.assertEqual(result.item.mediaTemplate, "original-document")
        self.assertIsNone(result.matchedTemplateHint)


class UnresolvedSeedTests(unittest.TestCase):
    def test_seed_with_no_results_and_no_fallback_url_is_unresolved(self):
        seed = {"title": "Titanic survivor voices", "searchQueries": ["Titanic survivor"], "preferredSources": ["loc"]}
        result = resolve_seed(seed, FakeSearchRouter({}), institutional_fn=lambda url: None)
        self.assertFalse(result.resolved)
        self.assertEqual(result.method, "unresolved")
        self.assertIsNone(result.item)
        self.assertIsNone(result.sourceId)

    def test_seed_stays_unresolved_when_fallback_url_also_fails(self):
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": ["loc"], "optionalSeedUrl": "https://example.org/dead-page"}
        result = resolve_seed(seed, FakeSearchRouter({}), institutional_fn=lambda url: None)
        self.assertFalse(result.resolved)
        self.assertEqual(result.method, "unresolved")

    def test_resolve_all_seeds_never_raises_on_a_broken_search_fn(self):
        def broken_search(source_id, query, limit=5, media=None):
            raise RuntimeError("simulated network failure")

        seeds = [{"title": "T", "searchQueries": ["q"], "preferredSources": ["nasa"]}]
        results = resolve_all_seeds(seeds, broken_search, institutional_fn=lambda url: None)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].resolved)


class WeakResultRejectionTests(unittest.TestCase):
    """
    Mirrors the exact failure modes described in the brief: a search result
    that's only loosely related, or is retrospective commentary rather than
    primary archival material, must not count as resolved — the seed should
    fall through to its institutional fallback (or stay honestly unresolved)
    instead of settling for it.
    """

    def test_apollo11_anniversary_message_is_rejected_falls_to_fallback(self):
        weak = make_item("nasa", "A1", "NASA Administrator's 2024 Message on the Apollo 11 Anniversary")
        router = FakeSearchRouter({("nasa", "Apollo 11 moon landing"): [weak]})
        strong_fallback = make_item("institutional_page", None, "Apollo 11 Mission Overview")
        seed = {
            "title": "Apollo 11 Moon Landing", "searchQueries": ["Apollo 11 moon landing"],
            "preferredSources": ["nasa"], "templateHint": "moving-image-source",
            "optionalSeedUrl": "https://www.nasa.gov/history/apollo-11-mission-overview/",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: strong_fallback)
        self.assertEqual(result.method, "institutional-fallback")
        self.assertIs(result.item, strong_fallback)

    def test_apollo11_genuine_archival_result_is_accepted(self):
        strong = make_item("nasa", "A2", "Apollo 11 Lunar Module Eagle on the Moon", media_kind="photograph")
        router = FakeSearchRouter({("nasa", "Apollo 11 moon landing"): [strong]})
        seed = {"title": "Apollo 11 Moon Landing", "searchQueries": ["Apollo 11 moon landing"],
                "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertIs(result.item, strong)

    def test_earthrise_visualization_is_rejected_not_the_photograph(self):
        weak = make_item("nasa", "E1", "NASA Visualization Shows Earth's Ozone Layer, 2017")
        router = FakeSearchRouter({("nasa", "Earthrise Apollo 8"): [weak]})
        seed = {"title": "Earthrise", "searchQueries": ["Earthrise Apollo 8"], "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "unresolved")  # weak, and no fallback configured in this test

    def test_earthrise_actual_photograph_is_accepted(self):
        strong = make_item("nasa", "E2", "Earthrise Over the Lunar Horizon", media_kind="photograph")
        router = FakeSearchRouter({("nasa", "Earthrise Apollo 8"): [strong]})
        seed = {"title": "Earthrise", "searchQueries": ["Earthrise Apollo 8"], "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertIs(result.item, strong)

    def test_navajo_generic_blog_post_rejected_for_audio_hint(self):
        weak = make_item("loc", "N1", "Carrying the Mail: A Blog Post About WWII Communications", media_kind="document")
        router = FakeSearchRouter({("loc", "Navajo Code Talkers"): [weak]})
        strong_fallback = make_item("institutional_page", None, "Alfred K. Newman, Sr. — Code Talker Profile", media_kind="audio")
        seed = {
            "title": "Navajo Code Talkers", "searchQueries": ["Navajo Code Talkers"], "preferredSources": ["loc"],
            "templateHint": "audio-oral-history",
            "optionalSeedUrl": "https://guides.loc.gov/navajo-code-talkers/profiles/alfred-newman",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: strong_fallback)
        self.assertEqual(result.method, "institutional-fallback")

    def test_navajo_actual_oral_history_result_is_accepted(self):
        strong = make_item("loc", "N2", "Navajo Code Talker Oral History Interview", media_kind="audio")
        router = FakeSearchRouter({("loc", "Navajo Code Talkers"): [strong]})
        seed = {"title": "Navajo Code Talkers", "searchQueries": ["Navajo Code Talkers"],
                "preferredSources": ["loc"], "templateHint": "audio-oral-history"}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertEqual(result.item.mediaTemplate, "audio-oral-history")

    def test_women_of_nasa_unrelated_program_page_rejected(self):
        weak = make_item("nasa", "W1", "Women@NASA Program Overview and Mission Statement")
        router = FakeSearchRouter({
            ("nasa", "Women of NASA"): [weak],
            ("nasa", "Hidden Figures NASA mathematicians"): [],
        })
        seed = {"title": "Women of NASA / Hidden Figures",
                "searchQueries": ["Women of NASA", "Hidden Figures NASA mathematicians", "Katherine Johnson NASA"],
                "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "unresolved")

    def test_women_at_nasa_portrait_rejected_in_favor_of_katherine_johnson_query(self):
        weak = make_item("nasa", "W2", "Judy Bruner for Women@NASA")
        strong = make_item("nasa", "W3", "Katherine Johnson At Her Desk at NASA Langley Research Center")
        router = FakeSearchRouter({
            ("nasa", "Women of NASA"): [weak],
            ("nasa", "Katherine Johnson At Her Desk"): [strong],
        })
        seed = {
            "title": "Women of NASA / Hidden Figures",
            "searchQueries": ["Women of NASA", "Katherine Johnson At Her Desk"],
            "preferredSources": ["nasa"],
            "templateHint": "historical-photograph",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertIs(result.item, strong)

    def test_earthrise_conversation_video_rejected_for_photograph_hint(self):
        weak = make_item(
            "nasa", "E3",
            "Earthrise: A Conversation with Apollo 8 Astronaut Bill Anders (Official NASA Video)",
            media_kind="moving_image",
        )
        strong = make_item(
            "nasa", "E4",
            "Apollo 8 Mission image, Earth over the horizon of the moon",
            media_kind="photograph",
        )
        router = FakeSearchRouter({
            ("nasa", "Earthrise Apollo 8"): [weak],
            ("nasa", "AS08-14-2383"): [strong],
        })
        seed = {
            "title": "Earthrise",
            "searchQueries": ["Earthrise Apollo 8", "AS08-14-2383"],
            "preferredSources": ["nasa"],
            "templateHint": "historical-photograph",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertIs(result.item, strong)
        self.assertTrue(result.matchedTemplateHint)

    def test_cherokee_commemorative_congress_bill_rejected(self):
        weak = make_item(
            "loc", "C1",
            "S.578 - 100th Congress (1987-1988): A bill to amend the National Trails System Act to designate the Trail of Tears",
            media_kind="document",
        )
        strong = make_item(
            "loc", "C2",
            "Indian Removal Act: Primary Documents in American History",
            media_kind="document",
        )
        router = FakeSearchRouter({
            ("loc", "Cherokee Nation Trail of Tears"): [weak],
            ("loc", "Indian Removal Act 1830"): [strong],
        })
        seed = {
            "title": "Cherokee Nation / Trail of Tears",
            "searchQueries": ["Cherokee Nation Trail of Tears", "Indian Removal Act 1830"],
            "preferredSources": ["loc"],
            "templateHint": "original-document",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertIs(result.item, strong)

    def test_continues_to_next_query_when_first_results_are_all_weak(self):
        weak = make_item("nasa", "A1", "NASA Administrator's 2024 Message on the Apollo 11 Anniversary", media_kind="moving_image")
        strong = make_item("nasa", "A2", "Apollo 11 Activities", media_kind="moving_image")
        router = FakeSearchRouter({
            ("nasa", "Apollo 11 moon landing"): [weak],
            ("nasa", "Apollo 11 Activities"): [strong],
        })
        seed = {
            "title": "Apollo 11 Moon Landing",
            "searchQueries": ["Apollo 11 moon landing", "Apollo 11 Activities"],
            "preferredSources": ["nasa"],
            "templateHint": "moving-image-source",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "source-search")
        self.assertIs(result.item, strong)
        self.assertIn(("nasa", "Apollo 11 Activities"), router.calls)

    def test_titanic_unrelated_result_rejected_for_survivor_hint(self):
        weak = make_item("loc", "T1", "Titanic Wreck Site Sonar Mapping Expedition 2023", media_kind="document")
        router = FakeSearchRouter({("loc", "Titanic survivor interview"): [weak]})
        strong_fallback = make_item("institutional_page", None, "Letter from Mrs Jacques Futrelle, TITANIC survivor")
        seed = {
            "title": "Titanic Survivor Voices / Artifacts", "searchQueries": ["Titanic survivor interview", "Titanic artifact recovered"],
            "preferredSources": ["loc"], "templateHint": "audio-oral-history",
            "optionalSeedUrl": "https://www.rmg.co.uk/collections/objects/rmgc-object-485436",
        }
        result = resolve_seed(seed, router, institutional_fn=lambda url: strong_fallback)
        self.assertEqual(result.method, "institutional-fallback")

    def test_pueblo_mismatched_result_rejected(self):
        weak = make_item("smithsonian", "P1", "General Southwest Region Visitor Guide")
        router = FakeSearchRouter({("smithsonian", "Ancestral Pueblo pottery"): [weak]})
        seed = {"title": "Pueblo / Ancestral Pueblo History",
                "searchQueries": ["Ancestral Pueblo pottery", "Pueblo peoples Southwest history"],
                "preferredSources": ["smithsonian"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertEqual(result.method, "unresolved")

    def test_matching_result_is_preferred_over_a_weak_one_from_the_same_search(self):
        weak = make_item("nasa", "M1", "NASA 2024 Message on the Apollo Program Anniversary")
        strong = make_item("nasa", "M2", "Apollo 11 Command Module Columbia in Lunar Orbit")
        router = FakeSearchRouter({("nasa", "Apollo 11"): [weak, strong]})
        seed = {"title": "Apollo 11", "searchQueries": ["Apollo 11"], "preferredSources": ["nasa"]}
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertIs(result.item, strong)

    def test_weak_result_with_no_fallback_configured_stays_honestly_unresolved(self):
        weak = make_item("nasa", "U1", "NASA Anniversary Retrospective")
        router = FakeSearchRouter({("nasa", "q"): [weak]})
        seed = {"title": "T", "searchQueries": ["q"], "preferredSources": ["nasa"]}  # no optionalSeedUrl
        result = resolve_seed(seed, router, institutional_fn=lambda url: None)
        self.assertFalse(result.resolved)
        self.assertEqual(result.method, "unresolved")


class SurpriseMeRandomizationTests(unittest.TestCase):
    def test_randomize_selects_among_multiple_strong_candidates(self):
        items = [make_item("nasa", f"R{i}", f"Apollo 11 Archival Photo {i}") for i in range(6)]
        router = FakeSearchRouter({("nasa", "Apollo 11"): items})
        seed = {"title": "Apollo 11", "searchQueries": ["Apollo 11"], "preferredSources": ["nasa"]}
        seen_ids = set()
        for _ in range(25):
            result = resolve_seed(seed, router, institutional_fn=lambda url: None, randomize=True)
            seen_ids.add(result.item.sourceItemId)
        self.assertGreater(len(seen_ids), 1, "randomize=True should not always return the same candidate")

    def test_non_randomized_calls_are_deterministic(self):
        items = [make_item("nasa", f"D{i}", f"Apollo 11 Archival Photo {i}") for i in range(6)]
        router = FakeSearchRouter({("nasa", "Apollo 11"): items})
        seed = {"title": "Apollo 11", "searchQueries": ["Apollo 11"], "preferredSources": ["nasa"]}
        results = {resolve_seed(seed, router, institutional_fn=lambda url: None).item.sourceItemId for _ in range(10)}
        self.assertEqual(len(results), 1, "without randomize, the same strong candidate should be picked every time")

    def test_resolve_all_seeds_randomize_varies_order_across_calls(self):
        import json as _json
        with (PROJECT_ROOT / "seeds" / "discovery_seeds.json").open() as f:
            seeds = _json.load(f)

        def always_finds_something(source_id, query, limit=5, media=None):
            return [make_item(source_id, "X", f"{query} archival photograph")]

        orders = set()
        for _ in range(10):
            results = resolve_all_seeds(seeds, always_finds_something, institutional_fn=lambda url: None, randomize=True)
            orders.add(tuple(r.title for r in results))
        self.assertGreater(len(orders), 1, "randomize=True should vary seed order across repeated calls")

    def test_resolve_all_seeds_still_returns_every_seed_when_randomized(self):
        import json as _json
        with (PROJECT_ROOT / "seeds" / "discovery_seeds.json").open() as f:
            seeds = _json.load(f)

        def always_finds_something(source_id, query, limit=5, media=None):
            return [make_item(source_id, "X", f"{query} archival photograph")]

        results = resolve_all_seeds(seeds, always_finds_something, institutional_fn=lambda url: None, randomize=True)
        self.assertEqual({r.title for r in results}, {s["title"] for s in seeds})


class RealDiscoverySeedsFileTests(unittest.TestCase):
    """Sanity checks against the actual seeds/discovery_seeds.json shipped
    with the project (still hermetic — uses a fake search_fn)."""

    def setUp(self):
        import json
        with (PROJECT_ROOT / "seeds" / "discovery_seeds.json").open() as f:
            self.seeds = json.load(f)

    def test_all_15_seeds_present_with_required_fields(self):
        self.assertEqual(len(self.seeds), 15)
        for seed in self.seeds:
            self.assertIn("title", seed)
            self.assertIn("searchQueries", seed)
            self.assertIn("preferredSources", seed)

    def test_dracorex_and_sue_have_artifact_object_hint(self):
        by_title = {s["title"]: s for s in self.seeds}
        self.assertEqual(by_title["Dracorex hogwartsia"]["templateHint"], "artifact-object")
        self.assertEqual(by_title["SUE the T. rex"]["templateHint"], "artifact-object")

    def test_specific_institutional_fallback_urls_are_no_longer_dead_or_generic(self):
        # Regression guard for the featured-seed cleanup: these four now
        # point at the exact object/profile page, not a generic landing page.
        by_title = {s["title"]: s for s in self.seeds}
        self.assertEqual(
            by_title["Dracorex hogwartsia"]["optionalSeedUrl"],
            "https://www.childrensmuseum.org/stories/meet-dracorex-hogwartsia",
        )
        self.assertEqual(
            by_title["Titanic Survivor Voices / Artifacts"]["optionalSeedUrl"],
            "https://www.rmg.co.uk/collections/objects/rmgc-object-485436",
        )
        self.assertEqual(
            by_title["Saint George and the Dragon Medieval Illuminated Manuscript"]["optionalSeedUrl"],
            "https://www.getty.edu/art/collection/object/105VJZ",
        )
        self.assertIn(
            "guides.loc.gov/navajo-code-talkers/profiles/",
            by_title["Navajo Code Talkers"]["optionalSeedUrl"],
        )
        self.assertNotEqual(
            by_title["Navajo Code Talkers"]["optionalSeedUrl"],
            "https://www.loc.gov/collections/veterans-history-project/",
        )
        self.assertIn("fieldmuseum.org", by_title["SUE the T. rex"]["optionalSeedUrl"])
        self.assertIn("metmuseum.org", by_title["Hokusai — The Great Wave"]["optionalSeedUrl"])
        self.assertIn("243770", by_title["Cats / Household Life in Ancient Egypt"]["optionalSeedUrl"])
        self.assertEqual(
            by_title["Women of NASA / Hidden Figures"]["searchQueries"][0],
            "Katherine Johnson At Her Desk",
        )
        self.assertEqual(
            by_title["Earthrise"]["searchQueries"][0],
            "AS08-14-2383",
        )

    def test_every_seed_resolves_to_something_with_a_fully_faked_search(self):
        # Not a claim about real institutions — just proves the resolution
        # *mechanism* runs cleanly end-to-end over the real seed file, when
        # given results that are both on-topic (title contains the actual
        # query) and, for seeds with a strict media hint, the right kind of
        # media — a fair test of the plumbing, not of weak-result rejection
        # (which has its own dedicated tests above).
        hint_to_media_kind = {
            "audio-oral-history": "audio",
            "moving-image-source": "video",
            "later-documentary": "video",
            "historical-photograph": "photograph",
            "artwork-illustration": "artwork",
        }
        query_to_hint = {}
        for s in self.seeds:
            for q in s.get("searchQueries", []):
                query_to_hint[q] = s.get("templateHint")

        def always_finds_something(source_id, query, limit=5, media=None):
            media_kind = hint_to_media_kind.get(query_to_hint.get(query), "photograph")
            return [make_item(source_id, "X", f"{query} — archival record", media_kind=media_kind)]

        results = resolve_all_seeds(self.seeds, always_finds_something, institutional_fn=lambda url: None)
        self.assertTrue(all(r.resolved for r in results), [r.title for r in results if not r.resolved])


if __name__ == "__main__":
    unittest.main(verbosity=2)
