# FOXAI Time Machine — Cursor Handoff

Status snapshot for continuing this project locally. No architecture or
feature changes were made to produce this document — it's documentation
only.

---

## 1. Architecture & launch

Static frontend (HTML/CSS/vanilla JS) + a small stdlib-only Python local
service. No frameworks, no database server, no build step beyond one script
for the offline demo content.

```bash
cd foxai-timemachine
python3 serve.py
# open http://localhost:8420
```

Optional API keys (Smithsonian, DPLA, Europeana, NARA — NASA and LOC need
none) go in `config.local.json`, copied from `config.example.json`.
Missing keys just mean that source reports itself unavailable; nothing
crashes.

Layout:
```
serve.py                    static file server + JSON API (stdlib http.server)
sources/                    adapters, normalized item model, template registry, seed resolution
seeds/discovery_seeds.json  the 15 featured discovery targets (search queries, not records)
content/                    offline/demo fallback cards, worldlines, infographics
js/app.js, js/lib.js        frontend logic + shared pure helpers
tests/                      hermetic + one optional live-network suite
```

Personalization (favorites/saves/follows/recently-viewed) lives entirely in
browser `localStorage` — no server-side accounts.

---

## 2. Source adapters & IIIF

`sources/` — one file per institution, all implementing the same
`SourceAdapter` contract (`availability()`, `search()`):

- `nasa.py`, `loc.py` — keyless, work immediately
- `smithsonian.py`, `dpla.py`, `europeana.py`, `nara.py` — require a free
  API key each; report `available: false, reason: "API key not configured"`
  without one
- `iiif.py` — generic IIIF Presentation 2.x/3.0 manifest parser, not a
  per-institution adapter. Classifies image/audio/video from the canvas
  body's actual `type`/`format`/MIME — **not** from the presence of a
  `duration` field (that was a fixed bug; a canvas can carry an unrelated
  duration without being time-based media)
- `institutional_page.py` — deliberately limited JSON-LD/OpenGraph fallback
  for one explicitly supplied URL; no crawling

All normalize into one shared `TimeMachineItem` (`sources/base.py`) with a
stable id (`sourceId + sourceItemId`, or a hashed canonical URL) so
favorites/saves survive feed refreshes.

---

## 3. Featured-seed resolution flow

`sources/seeds.py` — `resolve_seed()` per seed in `seeds/discovery_seeds.json`:

1. Search the seed's `preferredSources` with its `searchQueries` (bounded:
   ≤2 sources, ≤2 queries per source).
2. **Reject weak candidates** — a result only counts if its title has
   decent keyword overlap with the search queries AND it isn't a
   retrospective/anniversary-style hit (phrase-filtered: "anniversary",
   "visualization", "program overview", etc.) AND, when the hint names a
   real media requirement (`audio-oral-history`, `moving-image-source`,
   `later-documentary`), the result actually is that media type.
3. Among the remaining strong candidates, prefer one whose own template
   matches `templateHint` — **never relabels** a result to force a match.
4. If nothing strong was found, try the exact `optionalSeedUrl` via the
   institutional-page fallback.
5. Otherwise: honestly `unresolved`. Nothing is fabricated.

`GET /api/seeds` exposes this directly (resolved/method/sourceId/
matchedTemplateHint per seed) — the fastest way to see what's happening.

---

## 4. Mixed-media template system

`sources/base.py` (Python) + `js/lib.js` (JS mirror) — one shared registry
mapping a template id to a classification badge, a primary action, and
whether that action is external (links to the institution) or internal
(opens the Gazette/Learn More). E.g. `historical-photograph` → "View
Original at Institution" (external); `reconstructed-story` → "Open
Gazette" (internal). Cards show this as a real primary button, with
Favorite/Save/Share/Learn More/Evidence as secondary actions. Full contract
in `README.md` § "Mixed-media template registry."

---

## 5. Surprise Me

Fetches fresh material — it does not just reshuffle loaded cards.

- Frontend: `GET /api/feed?limit=20&surprise=true&exclude=<visible ids>`
- Backend: `build_feed(randomize=True, exclude_ids=...)` shuffles seed
  order, picks randomly among a seed's equally-valid strong candidates, and
  filters out anything already visible
- Falls back to a local reshuffle only in offline/demo mode or if the
  request fails
- Favorites/saves are untouched (tracked by id in `localStorage`,
  independent of the feed's current contents)

---

## 6. Tests — commands & last-run results

Run once during this handoff, all passing:

```bash
python3 tests/python/test_sources_hermetic.py    # 42 passed
python3 tests/python/test_seed_resolution.py     # 35 passed
python3 tests/python/test_feed_endpoint.py       # 6 passed
```

Frontend — open `tests/test-runner.html` in a browser, or serve the project
and run it via the DOM (29 tests, all passing last run).

**Total: 112/112 passing.**

Optional, not run automatically (makes real network requests):
```bash
python3 tests/python/test_live_smoke.py
```

---

## 7. Live testing is now working

Previous sessions could only verify seed resolution **hermetically** — the
build sandbox's network was locked to package registries and couldn't
reach any institution, so every live run reported all 15 seeds
`unresolved` regardless of logic correctness.

**That's no longer the limitation.** Live testing is now working on the
user's Windows machine, with real network access to NASA, LOC, and the
other sources. The results below are from that live run and are the first
real signal on how the resolution logic performs against actual API
responses, not fixtures.

---

## 8. Current live seed issues to tune next

Live network access surfaced real tuning problems the hermetic test suite
can't catch — it only proves the mechanism handles the cases its own
fixtures describe. Real APIs return real messy results.

**Unresolved (search rejected everything, and either no fallback fired or
also failed) — needs investigation source-by-source:**
- Dracorex hogwartsia
- SUE the T. rex
- Apollo 11 Moon Landing
- Machu Picchu
- Rosetta Stone
- Cats / Household Life in Ancient Egypt
- Hokusai — The Great Wave

**Resolved, but not quite right:**
- **Earthrise** — resolves, but currently selects a *video* result instead
  of the intended photograph. Since `historical-photograph` isn't in
  `STRICT_MEDIA_HINTS`, a video result isn't being rejected the way an
  audio/video mismatch would be for a strict-hint seed. Worth deciding
  whether `historical-photograph`/`artwork-illustration` should also gate
  on media kind, or whether `_choose_best_candidate` needs a media-kind
  tiebreak even for non-strict hints.
- **Pueblo / Ancestral Pueblo History** — the result is topically relevant
  but doesn't match the `artifact-object` template hint. Since this hint
  isn't in `STRICT_MEDIA_HINTS`, mismatches are currently accepted with
  `matchedTemplateHint: false` rather than rejected. Worth revisiting
  whether `artifact-object` should be stricter, or whether the real issue
  is `preferredSources`/`searchQueries` returning the wrong kind of item.
- **Cherokee Nation / Trail of Tears** — relevant to the topic, but the
  result is a 1987 document rather than period material. Relevance/weak-
  signal filtering currently has no sense of "how old should this be" —
  only topical relevance and retrospective-phrase detection. May need a
  date-plausibility check or a phrase/format signal specific to secondary
  vs. primary sources.
- **Women of NASA / Hidden Figures** — still selects overly generic
  Women@NASA program content over Hidden Figures/Katherine Johnson-specific
  material. The weak-signal phrase list and relevance threshold catch some
  of this class of mismatch (there's a dedicated hermetic test for exactly
  this scenario) but evidently not this specific real result — worth
  checking the actual title/description NASA's API returns for this query
  and tightening the phrase list or query specificity accordingly.

**Working correctly via fallback** (search didn't find a strong match, and
the `optionalSeedUrl` fallback resolved them properly):
- Navajo Code Talkers
- Pompeii
- Titanic Survivor Voices / Artifacts
- Saint George and the Dragon Medieval Illuminated Manuscript

---

## Suggested next step

For each unresolved/mismatched seed, capture what the real API actually
returned (title, description, media type) and decide per-case whether the
fix belongs in: `searchQueries` (better query), `preferredSources` (wrong
source for this topic), the relevance/weak-signal heuristic in
`sources/seeds.py`, or `STRICT_MEDIA_HINTS` (whether more templates should
gate on media kind). The hermetic tests in `test_seed_resolution.py` are
the right place to lock in each fix once identified.

---

## v2.0 upgrade (what changed, where)

**New sources (7)** — all in `sources/`, all registered in `sources/registry.py`:
`cma.py` (Cleveland, keyless), `vam.py` (V&A, keyless), `commons.py`
(Wikimedia Commons, keyless, File-namespace search + compliant UA),
`internetarchive.py` (keyless, restricted to Prelinger/Old Time Radio/
Animation curated collections), `gallica.py` (BnF Gallica SRU, keyless,
XML), `rijksmuseum.py` (key), `harvard.py` (key). Registry now has 15
adapters; 9 are keyless.

**Two real fixes to existing sources**:
- `met.py` — v1 search was retired 2026-10-01 (HTTP 410); migrated to the
  v1.1 search endpoint (limit/offset), object detail stays on v1 (verified).
- `nara.py` — aligned with the official v2 getting-started guide: GET +
  `x-api-key` + `Content-Type: application/json`, `availableOnline=true`,
  `digitalObjects[].objectUrl` thumbnails, productionDates for dating.

**Deeper harvesting**: `page` parameter flows frontend → serve.py →
registry/discovery → adapters with real API pagination (nasa page, loc sp,
met offset, cma skip, vam page_offset, commons gsroffset, ia page, aic
page, gallica startRecord, europeana/dpla/rijksmuseum/harvard page).
Discovery bounds widened 8→12 queries, 2→3 sources, 4→6 results/query.
`seeds/discovery_queries.json` grew 283→376 entries; all 15 featured seeds'
`preferredSources` extended with fitting new sources. Frontend keeps a
`discoveryPage` counter (resets on fresh featured load).

**Tests**: `tests/python/test_new_sources_hermetic.py` (48 tests) added;
total hermetic backend tests 189, all green. Live smoke suite covers all
fifteen sources. Live-captured fixtures: commons/cma/vam/met v1.1;
docs-modeled fixtures (labeled): internetarchive/gallica/rijksmuseum/harvard.

---

## v2.1 deep discovery upgrade (what changed, where)

**Foundation note**: v2.1 was assembled by auditing both v2.0 trees. The
"page-aware discovery engine" attributed to the other tree does not exist
in its code (no page parameter anywhere in its discovery/serve/registry,
Met still on the retired v1 endpoint, Getty adapter targeting a 404 URL);
this tree's page-aware architecture was therefore kept as the foundation
and the other tree's worthwhile pieces were ported in (Wellcome adapter
hardened, Getty rebuilt on the real API, IA breadth ideas). Full audit
findings are in the v2.1 upgrade report.

**New modules**:
- `sources/discovery_state.py` — persistent SQLite (WAL, bounded) store:
  per-(source, query) cursors with exhaustion + 30-day lazy refresh,
  seen-record dedup memory (100k cap, oldest-pruned), discovered topics.
  Thread-safe; degrades to in-memory roam if the store cannot open.
- `sources/dynamic_topics.py` — organic topic mining from archival
  metadata (no AI). Up to 25% of each batch's topics come from the
  mined pool; provenance recorded.

**Adapters**: 17 sources now (added wellcome, getty). 15 have native deep
pagination and report `deep_pagination=True` via /api/sources; vam moved
to the live-verified `page` parameter (the documented page_offset is
ignored by the live API); nasa/loc/aic/dpla/europeana gained pagination.

**Discovery**: `discover_surprise(..., state=None, stats_out=None)` —
per-search cursors, exhaustion skip, persistent dedup, discovered-topic
mix, era diversity. serve.py passes the process-wide store (AppState);
`GET /api/discovery/stats` and `POST /api/discovery/reset` added.

**Tests**: 253 hermetic backend tests across 12 suites, all green;
`docs/ARCHIVE-MATRIX.md` = capability + verification matrix. Live runs:
8/11 keyless smoke checks pass from the build sandbox; per-adapter
pagination verified (page 2 fully distinct) for nasa/met/cma/vam/
wellcome/getty; sustained 12-batch live run delivered 288 unique records
with zero repeats.

---

## v2.1.1 remediation (what changed, where)

**Pagination (audit #1)**: Smithsonian `start`/`rows` pagination added
(`sources/smithsonian.py`, docs/community-corroborated; live check needs a
key). NARA stays honestly false — the official v2 getting-started page
documents no pagination params; their Swagger is a JS SPA that could not
be rendered from the build sandbox. 16/17 adapters now paginate; every
page param + cache-key distinction is pinned by
`tests/python/test_pagination_wiring.py`.

**Concurrency (audit #2)**: `discover_surprise` restructured into plan →
concurrent fetch → collect (`sources/discovery.py`). Pool ≤ 8 workers,
`PER_SOURCE_CONCURRENCY = 2` per archive, `DISCOVERY_BATCH_DEADLINE_S = 30`
with `pool.shutdown(wait=False, cancel_futures=True)` — deadline-missed
requests are cancelled and put into temporary backoff, never waited on.
serve.py passes `search_source_strict` so the operational path is the
classified one. Proof tests: Barrier fake with ≥5 simultaneous in-flight
requests through the real pipeline.

**Error taxonomy (audit #3)**: `FetchError` carries kind + retry_after
(`sources/config.py`); adapters re-raise search-level failures (per-record
fetches stay fail-soft); `registry.SourceTempError` classifies;
`discovery_state.record_result(outcome=...)` implements ok/empty/
temporary/rate_limited/auth_error/malformed with exponential backoff
(60 s → 1 h cap), Retry-After cooldowns, and page −2 deferral in
`get_page`. Recovery preserves position (proven by tests).

**IA policy (audit #4)**: broad tier has NO date ceiling
(`sources/internetarchive.py`); expanded curated list + `_SPAM_TERMS`
blocklist + engagement sort. `year`/`date` fields are original publication
dates (documented).

**Dedup (audit #5)**: `find_cross_archive_duplicate` requires a known
creator on both sides for metadata matching; URL match unchanged; status
column filters delivered rows only.

**Storage (audit #6)**: caps via env (`FOXAI_SEEN_MAX_ROWS` = 1,000,000
default), guarded ALTER TABLE migrations (`_migrate`), status column,
chunked `known_ids`, index on next_attempt_at. Measured: 321 B/row,
≈320 MB per million rows, flat RAM, 69 k rows/s inserts.

**Geographic diversity (audit #7)**: `registry.diversify_regions` +
`region_bucket` wired into the discovery balancing chain.

**Benchmarks (audit #8)**: `tools/benchmark_sustained.py` (LIVE, restart
mid-way) and `tools/benchmark_simulated.py` (clearly-labeled synthetic
endurance). Raw JSON results under `docs/verification/`.

**Tests**: 306 hermetic tests across 17 suites — all green; no test
weakened or removed.
