#!/usr/bin/env python3
"""
tests/python/test_live_smoke.py

OPTIONAL live network smoke checks. This is NOT part of the normal test
suite and is never run automatically — it makes real HTTP requests to
external institutions.

Run it yourself, with internet access, when you want to confirm the
adapters still work against the real APIs:

    python3 tests/python/test_live_smoke.py

What it checks (brief section 24), skipping anything unavailable:
  - NASA: search "Apollo 11" / "Earthrise"
  - Library of Congress: search "Navajo Code Talkers"
  - The Met (v1.1 search): search "monet"
  - Cleveland Museum of Art: search "great wave"
  - Victoria & Albert Museum: search "great wave"
  - Wikimedia Commons: search "apollo 11"
  - Internet Archive (curated collections): search "civil defense"
  - BnF Gallica: search "pompeii"
  - One public IIIF manifest (IIIF cookbook sample, keyless)
  - Any of Smithsonian/DPLA/Europeana/NARA/Rijksmuseum/Harvard IF a key is
    configured

Honesty note: when this project's v2.0 upgrade was built, the sandbox could
reach some archives (NASA, Met v1.1, Cleveland, V&A, Commons via curl) but
was rate-limited or edge-blocked for others (archive.org, Gallica, LOC,
AIC). The fixtures and the notes in each adapter's docstring record exactly
which were live-captured and which are docs-verified. Run this file on your
own machine to close whatever gap remains for your network.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.nasa import NasaSource  # noqa: E402
from sources.loc import LocSource  # noqa: E402
from sources.met import MetSource  # noqa: E402
from sources.cma import CmaSource  # noqa: E402
from sources.vam import VamSource  # noqa: E402
from sources.commons import CommonsSource  # noqa: E402
from sources.internetarchive import InternetArchiveSource  # noqa: E402
from sources.gallica import GallicaSource  # noqa: E402
from sources.wellcome import WellcomeSource  # noqa: E402
from sources.getty import GettySource  # noqa: E402
from sources.iiif import fetch_and_normalize_iiif_manifest  # noqa: E402
from sources.registry import list_sources, search_source  # noqa: E402

# A real, public, keyless IIIF manifest — this exact URL appeared in IIIF's
# own official documentation search results while building this project.
SAMPLE_IIIF_MANIFEST = "https://iiif.io/api/cookbook/recipe/0003-mvm-video/manifest.json"


def check(label, fn):
    print(f"\n--- {label} ---")
    try:
        result = fn()
        print(result)
        if isinstance(result, str) and (result.startswith("0 result") or "no usable item" in result):
            print("NOTE: this likely means the request never reached the source (network "
                  "blocked, DNS failure, etc.) rather than a genuine empty/invalid result. "
                  "Adapters intentionally swallow network errors and return nothing rather "
                  "than raising, so this script cannot fully tell the two apart from here.")
            return "zero"
        return True
    except Exception as e:  # noqa: BLE001 — this script's whole job is to report failures, not hide them
        print(f"FAILED: {e}")
        return False


def main():
    print("FOXAI Time Machine — LIVE network smoke checks")
    print("These make real requests to external institutions. Results below")
    print("are exactly what was returned just now — nothing is assumed.\n")

    results = {}

    results["nasa_apollo11"] = check("NASA: search 'Apollo 11'", lambda: (
        f"{len(NasaSource().search('Apollo 11', limit=3))} result(s)"
    ))
    results["nasa_earthrise"] = check("NASA: search 'Earthrise'", lambda: (
        f"{len(NasaSource().search('Earthrise', limit=3))} result(s)"
    ))
    results["loc_navajo"] = check("Library of Congress: search 'Navajo Code Talkers'", lambda: (
        f"{len(LocSource().search('Navajo Code Talkers', limit=3))} result(s)"
    ))
    results["met_monet"] = check("The Met (v1.1 search): search 'monet'", lambda: (
        f"{len(MetSource().search('monet', limit=3))} result(s)"
    ))
    results["cma_great_wave"] = check("Cleveland Museum of Art: search 'great wave'", lambda: (
        f"{len(CmaSource().search('great wave', limit=3))} result(s)"
    ))
    results["vam_great_wave"] = check("Victoria & Albert Museum: search 'great wave'", lambda: (
        f"{len(VamSource().search('great wave', limit=3))} result(s)"
    ))
    results["commons_apollo11"] = check("Wikimedia Commons: search 'apollo 11'", lambda: (
        f"{len(CommonsSource().search('apollo 11', limit=3))} result(s)"
    ))
    results["ia_civil_defense"] = check("Internet Archive (curated): search 'civil defense'", lambda: (
        f"{len(InternetArchiveSource().search('civil defense', limit=3))} result(s)"
    ))
    results["gallica_pompeii"] = check("BnF Gallica: search 'pompeii'", lambda: (
        f"{len(GallicaSource().search('pompeii', limit=3))} result(s)"
    ))
    results["wellcome_plague"] = check("Wellcome Collection: search 'plague'", lambda: (
        f"{len(WellcomeSource().search('plague', limit=3))} result(s)"
    ))
    results["getty_irises"] = check("Getty Museum (SPARQL): search 'irises'", lambda: (
        f"{len(GettySource().search('irises', limit=3))} result(s)"
    ))
    results["iiif_sample"] = check("Public IIIF manifest", lambda: (
        (lambda item: f"title={item.title!r}" if item else "manifest returned no usable item")(
            fetch_and_normalize_iiif_manifest(SAMPLE_IIIF_MANIFEST)
        )
    ))

    print("\n--- Key-required sources (only if configured) ---")
    for source_id in ("smithsonian", "dpla", "europeana", "nara", "rijksmuseum", "harvard"):
        info = next(s for s in list_sources() if s["id"] == source_id)
        if not info["available"]:
            print(f"{source_id}: skipped — {info['reason']}")
            results[source_id] = None
            continue
        results[source_id] = check(f"{info['name']}: search 'Apollo 11'", lambda sid=source_id: (
            f"{len(search_source(sid, 'Apollo 11', limit=3))} result(s)"
        ))

    print("\n=== SUMMARY ===")
    for name, ok in results.items():
        if ok is None:
            status = "SKIPPED (no key)"
        elif ok == "zero":
            status = "ZERO RESULTS (see note above — may be network, not 'no matches')"
        elif ok:
            status = "OK"
        else:
            status = "FAILED"
        print(f"{name}: {status}")


if __name__ == "__main__":
    main()
