#!/usr/bin/env python3
"""
tests/python/test_pagination_wiring.py

v2.1.1 complete pagination request wiring (audit deficiency #1): for every
adapter with native pagination, the page parameter must actually reach the
outgoing HTTP request with the right provider-specific name and value, and
the cache key must distinguish pages. Also pins the Smithsonian rows/start
implementation and the honest deepPagination flags for the two sources
whose providers document no pagination.

Run with:
    python3 tests/python/test_pagination_wiring.py

No network access is performed or required.
"""
from __future__ import annotations

import sys
import unittest
import unittest.mock
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources import (  # noqa: E402
    aic as aic_mod, cma as cma_mod, commons as commons_mod,
    dpla as dpla_mod, europeana as europeana_mod, gallica as gallica_mod,
    getty as getty_mod, harvard as harvard_mod, internetarchive as ia_mod,
    loc as loc_mod, met as met_mod, nasa as nasa_mod, nara as nara_mod,
    rijksmuseum as rijks_mod, smithsonian as si_mod, vam as vam_mod,
    wellcome as wellcome_mod,
)
from sources.base import SourceAvailability  # noqa: E402
from sources.registry import ALL_SOURCES  # noqa: E402

EMPTY = {"response": {"docs": []}, "results": [], "records": [], "rows": []}


def _fetch_name(module):
    """Gallica answers SRU XML via http_get_text; most others are JSON."""
    return "http_get_text" if module.__name__.endswith("gallica") else "http_get_json"


def capture_url(module, adapter, page, limit=6):
    """Run adapter.search(page=N) with the HTTP layer mocked; return the
    request URL that would have gone out."""
    captured = {}
    fetch_name = _fetch_name(module)

    def fake_fetch(url, **kwargs):
        captured["url"] = url
        return {"response": {"docs": []}, "results": []} if fetch_name.endswith("json") else ""

    patches = [
        unittest.mock.patch.object(module, "cache_get",
                                   unittest.mock.Mock(return_value=None)),
        unittest.mock.patch.object(module, "cache_set",
                                   unittest.mock.Mock(return_value=None)),
        unittest.mock.patch.object(module, fetch_name, side_effect=fake_fetch),
    ]
    if getattr(adapter, "requires_key", False) and hasattr(module, "get_key"):
        patches.append(unittest.mock.patch.object(module, "get_key",
                                                  unittest.mock.Mock(return_value="k")))
    with ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        adapter.search("history", limit=limit, page=page)
        return captured.get("url", "")


def qs(url):
    return {k: v[-1] for k, v in parse_qs(urlparse(url).query).items()}


class PaginationWiringTests(unittest.TestCase):
    """Each entry: (module, adapter, param-name, expected value for page 3)."""

    def test_every_paginating_adapter_sends_its_page_param(self):
        cases = [
            (nasa_mod, "nasa", "page", "3"),
            (loc_mod, "loc", "sp", "3"),
            (met_mod, "met", "offset", "12"),           # (3-1)*6
            (aic_mod, "aic", "page", "3"),
            (cma_mod, "cma", "skip", "12"),             # (3-1)*6
            (vam_mod, "vam", "page", "3"),
            (wellcome_mod, "wellcome", "page", "3"),
            (dpla_mod, "dpla", "page", "3"),
            # Europeana's documented offset parameter is `start` (1-based).
            # Page 3 at limit 6 begins at hit 13.
            (europeana_mod, "europeana", "start", "13"),
            (rijks_mod, "rijksmuseum", "p", "3"),
            (nara_mod, "nara", "page", "3"),
            (harvard_mod, "harvard", "page", "3"),
            (ia_mod, "internetarchive", "page", "3"),
        ]
        for module, sid, param, expected in cases:
            adapter = next(s for s in ALL_SOURCES if s.source_id == sid)
            url = capture_url(module, adapter, page=3)
            params = qs(url)
            self.assertIn(param, params,
                          f"{sid}: page param '{param}' missing in {url}")
            self.assertEqual(
                params[param], expected,
                f"{sid}: {param}={params.get(param)} for page 3, expected {expected}")

    def test_smithsonian_uses_start_index(self):
        adapter = next(s for s in ALL_SOURCES if s.source_id == "smithsonian")
        url = capture_url(si_mod, adapter, page=3)
        params = qs(url)
        self.assertIn("start", params)
        self.assertEqual(params["start"], "12")  # (3-1)*6, zero-based

    def test_cache_keys_distinguish_pages(self):
        """A page-2 request must never be served from a page-1 cache entry:
        the cache key includes the page for every paginating adapter."""
        import hashlib
        checks = [
            (nasa_mod, "nasa"), (loc_mod, "loc"), (met_mod, "met"),
            (aic_mod, "aic"), (cma_mod, "cma"), (vam_mod, "vam"),
            (wellcome_mod, "wellcome"), (si_mod, "smithsonian"),
            (dpla_mod, "dpla"), (europeana_mod, "europeana"),
            (rijks_mod, "rijksmuseum"), (harvard_mod, "harvard"),
            (ia_mod, "internetarchive"), (nara_mod, "nara"),
        ]
        for module, sid in checks:
            adapter = next(s for s in ALL_SOURCES if s.source_id == sid)
            fetch_name = _fetch_name(module)
            with unittest.mock.patch.object(module, fetch_name,
                                            return_value={} if fetch_name.endswith("json") else ""), \
                 unittest.mock.patch.object(
                     module, "get_key", unittest.mock.Mock(return_value="k"),
                     create=not hasattr(module, "get_key")):
                set_calls = []
                module.cache_set = unittest.mock.Mock(
                    side_effect=lambda k, v: set_calls.append(k))
                module.cache_get = unittest.mock.Mock(return_value=None)
                adapter.search("history", limit=6, page=1)
                key1 = set_calls[-1]
                adapter.search("history", limit=6, page=2)
                key2 = set_calls[-1]
                self.assertNotEqual(
                    key1, key2,
                    f"{sid}: cache key does not distinguish pages ({key1!r})")

    def test_commons_continuation_offset_reaches_url(self):
        adapter = next(s for s in ALL_SOURCES if s.source_id == "commons")
        url = capture_url(commons_mod, adapter, page=3)
        self.assertIn("gsroffset", url)

    def test_gallica_sru_start_record_reaches_url(self):
        adapter = next(s for s in ALL_SOURCES if s.source_id == "gallica")
        url = capture_url(gallica_mod, adapter, page=3)
        self.assertIn("startRecord", url)

    def test_getty_sparql_offset_reaches_request(self):
        adapter = next(s for s in ALL_SOURCES if s.source_id == "getty")
        captured = {}

        def fake_text(url, **kwargs):
            captured["url"] = url
            return '{"results":{"bindings":[]}}'

        with unittest.mock.patch.object(getty_mod, "cache_get",
                                        unittest.mock.Mock(return_value=None)), \
             unittest.mock.patch.object(getty_mod, "cache_set",
                                        unittest.mock.Mock(return_value=None)), \
             unittest.mock.patch.object(getty_mod, "http_get_text",
                                        side_effect=fake_text):
            adapter.search("irises", limit=6, page=3)
        self.assertIn("OFFSET+12", captured["url"])  # (3-1)*6

    def test_nara_uses_documented_page_window(self):
        """OpenAPI (catalog.archives.gov/api/v2/swagger.json) defines `page`
        with maximum 10000. A later page must not be clamped onto 10000,
        which would refetch that page forever."""
        adapter = next(s for s in ALL_SOURCES if s.source_id == "nara")
        self.assertTrue(adapter.deep_pagination)
        url = capture_url(nara_mod, adapter, page=3)
        params = qs(url)
        self.assertEqual(params.get("page"), "3")
        self.assertIn("q", params)
        self.assertNotIn("searchAfter", params)
        beyond = capture_url(nara_mod, adapter, page=10001)
        self.assertEqual(beyond, "")

    def test_capability_flags_match_implementations(self):
        from sources.registry import list_sources
        rows = {r["id"]: r for r in list_sources()}
        deep = sorted(k for k, v in rows.items() if v["deepPagination"])
        self.assertEqual(deep, [
            "aic", "cma", "commons", "dpla", "europeana", "gallica", "getty",
            "harvard", "internetarchive", "loc", "met", "nara", "nasa",
            "rijksmuseum", "smithsonian", "vam", "wellcome",
        ])
        self.assertEqual([k for k, v in rows.items() if not v["deepPagination"]], [])

    def test_no_artificial_depth_caps_and_no_repeated_window(self):
        """High pages are sent as themselves. Europeana's documented offset
        window stops at the first 1,000 hits and must not repeat an earlier
        start. The Wellcome call is mocked — this test does not touch the
        network."""
        adapter = next(s for s in ALL_SOURCES if s.source_id == "wellcome")
        url = capture_url(wellcome_mod, adapter, page=10_000)
        self.assertEqual(qs(url).get("page"), "10000")
        europeana = next(s for s in ALL_SOURCES if s.source_id == "europeana")
        # Page 99 at limit 6 starts at hit 589, still inside the 1,000 window.
        inside = capture_url(europeana_mod, europeana, page=99)
        self.assertEqual(qs(inside).get("start"), "589")
        self.assertNotIn("page", qs(inside))
        # Page 200 at limit 6 starts at hit 1195, past the window: no request.
        beyond = capture_url(europeana_mod, europeana, page=200)
        self.assertEqual(beyond, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
