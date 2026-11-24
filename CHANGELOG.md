# FOXAI Time Machine — Changelog

## v2.2.0 — Audit corrections on the v2.1.1 architecture

Keeps the v2.1.1 discovery engine (17 adapters, persistent cursors,
organic topics, Getty SPARQL, concurrent fetch, million-row seen-record
memory) and corrects the remaining audit findings.

- **Pagination.** Arbitrary page clamps (25, 200, 400, and Europeana's
  misread 12-page cap) are gone. A later page is never folded back onto
  the last allowed page. Europeana uses the documented `start`/`rows`
  pair and stops at the first 1,000 hits instead of repeating them.
  National Archives pagination is implemented from the published OpenAPI
  document (`page` maximum 10000, `limit` maximum 1000). `searchAfter` is
  not sent: its schema and its prose disagree.
- **Application-wide concurrency.** One shared pool of eight workers and
  process-wide per-archive semaphores cover overlapping TimeStream,
  Surprise Me, feed, and search requests. Each batch still has its own
  deadline. The pool is not created and abandoned per batch.
- **Retry-After.** Delta-seconds and HTTP-date are both accepted. A
  provider-requested wait is not capped at one hour or at five seconds
  inside the HTTP helper. The resulting cooldown is stored per archive so
  a second query does not ignore it. The failed page number is kept.
- **Deduplication.** Cross-archive matches are applied inside a batch as
  well as across sessions. Matching stays conservative: URL, or exact
  title + media kind + year + a known creator on both sides from two
  archives.
- **Undelivered records.** Retrieved records that lose the presentation
  cut go into a bounded SQLite candidate queue (default 20,000 rows) and
  are offered again, including after a restart.
- **Test isolation.** The hermetic pagination check that called the live
  Wellcome API now uses a mocked response. The regression suite does not
  drop tests.

## v2.1.1 — Independent-Audit Remediation

Responds, deficiency by deficiency, to the independent source-code audit
and regression testing of v2.1, while preserving every architectural
advantage that scored 89/100.

### 1. Complete archive pagination

- **Smithsonian Open Access now paginates** (16 of 17 sources with native
  pagination): the documented zero-based `start` index alongside `rows`,
  as used consistently in the provider's examples and client libraries.
  The provider's docs site was unreachable from the build sandbox, so the
  implementation is docs/community-corroborated and labeled accordingly;
  hermetic tests prove the parameter reaches the real request URL and
  that cache keys distinguish pages. Live confirmation requires an API key.
- **National Archives remains honestly `deepPagination: false`.** The
  official getting-started documentation lists only `q`,
  `typeOfMaterials` and `availableOnline` for `/api/v2/records/search`
  (verified November 2026); the full Swagger spec renders only inside a
  JavaScript SPA that could not be executed from the build sandbox. No
  speculative parameters are sent. Documented in the matrix.
- **All 17 adapters audited**: parameter names/values for every page
  parameter verified against request URLs (new
  `tests/python/test_pagination_wiring.py`, 9 tests); every paginating
  adapter's cache key distinguishes pages; Europeana's 12-page bound is
  the provider's documented cursor limit; the generic page caps elsewhere
  are input-sanity guards that never bind during discovery.

### 2. Concurrency connected to actual discovery

- The central Surprise Me / continuous-discovery pipeline was restructured
  into **plan → concurrent fetch → collect**: every (source, query, page)
  fetch in a batch executes on a bounded worker pool. TimeStream, TimeFeed
  and the Gazette inherit this automatically (they all route through
  build_feed → discover_surprise, now wired to the strict classified
  search).
- **Provider-aware pacing**: at most two in-flight requests per archive
  (`PER_SOURCE_CONCURRENCY`), pool ≤ 8 workers, one batch-wide deadline
  (30 s) with non-blocking shutdown and cancellation — a hung archive
  cannot stretch the batch or accumulate threads.
- Proof-level integration tests: a Barrier-based fake demonstrates ≥5
  genuinely simultaneous in-flight requests inside the real pipeline;
  per-source pacing caps are asserted; deadline bounding and cancellation
  are asserted with hung fakes.

### 3. Network failures distinguished from exhaustion

- Full outcome taxonomy in the store: `ok` (advance), `empty` (confirmed
  exhausted), `temporary` (exponential backoff 60 s → 1 h, position
  preserved), `rate_limited` (cooldown honoring Retry-After, 30 s floor),
  `auth_error` (30 min cooldown), `malformed` (short backoff). Deferred
  searches report page −2 and skip a batch instead of re-hammering.
- Registry classifies adapter failures (`SourceTempError` with kind,
  status, retry-after) while public helpers still fail soft to [].
- 13 store-level tests + pipeline-level tests prove the audit's core
  scenario: an unavailable archive recovers and resumes at its exact
  discovery position, exhaustion never recorded.

### 4. Arbitrary date restriction removed

- The Internet Archive broad tier's hard `year:[* TO 1979]` ceiling is
  gone. Quality now comes from: an expanded curated collection whitelist
  (added Documentary Films, Movies and Films, News and Public Affairs —
  docs-based, archive.org unreachable from sandbox), engagement-sorted
  ranking, the media-type gate, original creation dates (the `year`/`date`
  fields are publication dates, not upload dates), and a conservative
  spam blocklist rejecting modern gaming/streaming/social-video
  re-uploads. Modern historical material (e.g. 1995 news coverage)
  passes; spam fixtures are rejected. Verified by tests.

### 5. Stronger cross-archive deduplication

- Unknown creator metadata can never establish a match: suppression now
  requires the same normalized URL, or exact normalized title + media
  kind + matching year (both present-and-equal or both absent) + a KNOWN
  creator on both sides that normalizes identically. Different creators,
  unknown creators, and missing years are all preserved as distinct.
- Tests cover false positives (unknown/different creators, similar
  titles) and false negatives (shared URL, shared known creator), and
  enforcement across restarts and successive batches.

### 6. Long-term persistence scaled

- Caps raised and made configurable via environment:
  `FOXAI_SEEN_MAX_ROWS` (default **1,000,000**), `FOXAI_PROGRESS_MAX_ROWS`
  (250,000), `FOXAI_TOPIC_MAX_ROWS` (20,000). Pruning fires only when a
  cap is exceeded — historical memory is never trimmed for age alone.
- Safe schema migrations: v2.1 databases upgrade in place (new columns
  error_count / next_attempt_at / status added via guarded ALTER TABLE);
  migration suite proves data preservation, immediate usability and
  idempotency. Retrieved-but-never-delivered records are never recorded,
  so nothing is permanently suppressed for being filtered out of a batch.
- Measured performance (test_large_state.py): 100,000 rows inserted in
  1.4 s (≈69 k rows/s), ≈321 bytes/row on disk → **1 M records ≈ 320 MB,
  5 M records ≈ 1.6 GB**, dedup lookups < 50 ms at 100 k rows, zero RSS
  growth across tens of thousands of lookups (flat memory — SQLite, not
  row objects).

### 7. Architectural advantages preserved and extended

- All v2.1 advantages retained (per-search cursors, organic topics with
  provenance and quality filtering, underexplored-topic rotation, era
  balancing, /api/discovery/stats + reset, Getty SPARQL, the live-verified
  V&A page fix, exhausted-query refresh, graceful fallbacks).
- New: **geographic diversity** — batches are round-robined across coarse
  world regions derived only from the records' own places metadata
  (unlabeled items never lead and are never dropped).

### 8. Comprehensive sustained benchmark

- `tools/benchmark_sustained.py` — LIVE benchmark, 52 batches × 24 items
  through the persistent store with a **shutdown + restart mid-way**,
  measuring unique growth, duplicates, latency (avg/median/worst),
  archives/regions/eras/media contributing, dynamic topics, pagination
  depth, temporary failures and recoveries, SQLite growth and RSS.
  Raw results: `docs/verification/benchmark-live-results.json`.
- `tools/benchmark_simulated.py` — clearly-labeled **SIMULATED** endurance
  test (synthetic records; NOT genuine discoveries) proving 60-batch
  engine endurance: uninterrupted fresh growth, outage recovery every
  time, restart resumption. Raw results:
  `docs/verification/benchmark-simulated-results.json`.

### 9. Regression and integration testing

- Backend suite: **306 hermetic tests across 17 suites, all passing**
  (was 253 across 12). New: test_concurrent_discovery (11),
  test_error_taxonomy (13), test_pagination_wiring (9),
  test_state_migration (4), test_large_state (7), plus region/dedup
  additions to test_deep_discovery (20 total there). No test was deleted,
  weakened or bypassed; three assertions were updated because the
  remediation intentionally changed the guarded behavior (IA date gate →
  quality gate; Smithsonian pagination flag; unknown-creator dedup rule).
- The real application was started and exercised over HTTP end-to-end
  (sources capability flags, featured feed, surprise batches, discovery
  stats, reset, restart resume).

### 10. Everything else preserved

- All 17 adapters, TimeFeed, Gazette, Explore, Worldlines, Broadcast,
  Teacher View, TimeStream, Surprise Me, Search, Saved items, favorites,
  personalization, IIIF support, institutional-page fallback, offline
  demo, launchers, visual identity — unchanged. No auth, subscriptions,
  paid services, AI dependencies or new frameworks.

### Packaging

- The release ships clean: no populated discovery databases, no benchmark
  or test state, no caches, logs, bytecode or private configuration. The
  application initializes its own clean `state/discovery_state.db` on
  first launch and migrates existing installations in place.

## v2.1 — Deep Historical Discovery

Goal: make Time Machine a continuously expanding window into authentic
history — massive expansion of accessible authentic material in breadth
and depth — without fabricating anything, without external AI services,
and without breaking a single existing feature.

### Added

- **Persistent deep exploration** (`sources/discovery_state.py`): every
  (source, query) pair has a SQLite-backed cursor (WAL, bounded, thread-
  safe) that advances as batches retrieve results, is marked exhausted
  when an archive runs dry, is skipped while exhausted, and is lazily
  re-explored after 30 days (collections change). Progress survives
  restarts and browsing sessions; `/api/discovery/stats` reports exactly
  where exploration stands; `POST /api/discovery/reset` (localhost) clears
  cursors without touching dedup memory.
- **Organic dynamic discovery** (`sources/dynamic_topics.py`): batches
  mine NEW search topics from authentic archival metadata (subjects,
  keywords, places, institutional collection labels) with provenance and
  frequency ranking; up to a quarter of each batch's topics come from this
  organically grown pool. No AI service, no fabricated strings.
- **Wellcome Collection** (`sources/wellcome.py`): keyless; native page
  pagination; medical/scientific/social-history manuscripts, photographs
  and art; licenses surfaced verbatim. Verified live; fixture captured.
- **J. Paul Getty Museum** (`sources/getty.py`): keyless; built on the
  documented data.getty.edu SPARQL service (ORDER BY/OFFSET stable pages)
  plus the REST record endpoint; bounded 600px IIIF images; CC0/Open
  Content detection. Verified live; fixtures captured. (Replaces a
  non-functional attempt against a retired endpoint found in the other
  v2.0 tree.)
- **Cross-archive deduplication**: persistent seen-record memory
  (bounded at 100k rows, oldest-pruned); conservative matching — same
  normalized URL, or exact normalized title + media kind + year with
  compatible creator across archives. Distinct artifacts with similar
  titles are never suppressed.
- **Historical-era diversity**: batches are re-ordered across century
  buckets derived from the records' own date labels; undated material
  never leads.
- **Bounded concurrency**: multi-source search runs on a small worker
  pool with one batch-wide deadline, so a slow archive cannot stall the
  others; every adapter keeps its own HTTP timeouts and fail-soft behavior.
- **Internet Archive two-tier historical gate** (`sources/internetarchive.py`):
  curated collections (Prelinger, Old Time Radio, Animation & Cartoons)
  unrestricted, plus a broad historical tier hard-gated to
  `year:[* TO 1979]`, suitable media types, and engagement-sorted
  (downloads desc) — much broader coverage without modern junk.
- **Capability reporting**: `/api/sources` now includes
  `deepPagination` and `apiDocs` per source; `docs/ARCHIVE-MATRIX.md` is
  the full archive-by-archive capability and verification matrix.
- **New test suites**: `test_discovery_state.py` (27), `test_deep_discovery.py`
  (12), `test_dynamic_topics.py` (13), plus Wellcome/Getty adapters in
  `test_new_sources_hermetic.py` (60 total). Backend suite: 189 → 253
  hermetic tests, all passing.
- **Live verification**: per-adapter pagination matrix (page 1 vs page 2
  must be fully distinct) and a sustained 12-batch live run — 288 unique
  authentic records with zero repeats across met/vam/cma/wellcome/nasa
  (sandbox-reachable sources only; full network sees more).

### Changed

- Discovery is now page-aware per search rather than via a single global
  counter; the global `page` parameter still rotates the topic pool.
- 15 of 17 adapters implement native pagination (was 12 of 15 in v2.0);
  V&A migrated from the (live-ignored) `page_offset` parameter to the
  live-verified `page` parameter; NASA (`page`), LOC (`sp` start page),
  AIC (`page`), DPLA (`page`), Europeana (`page`) added.
- NARA keeps honest `deepPagination: false` and Smithsonian likewise:
  their verified parameter lists do not include pagination.
- Discovery pool grew 376 → 400 curated topics (Wellcome and Getty
  coverage added); exhausted-query handling added on top.
- serve.py: `/api/feed` and `/api/gazette` run through the persistent
  store; featured-seed deliveries are recorded too.

### Fixed

- Getty adapter against the real API (the other tree's adapter targeted a
  retired URL and parsed defensive empty containers).
- Getty record normalization: title from the record's own `Name` row
  content (was picking up row labels like "Alternate Title"), display
  date from timespan identifiers with a digit guard, CC0 detection across
  all rights rows (the CC0 URI can sit behind generic rows).
- Two latent bugs found during the audit of the other v2.0 tree are
  avoided here by design (Met v1.1 migration and NARA v2 protocol were
  already fixed in this lineage's v2.0).

### Preserved

- TimeFeed, Gazette, Explore, Worldlines, Broadcast, Teacher View,
  TimeStream, Surprise Me, Search, Saved, favorites, follows,
  personalization, seed resolution, IIIF resolver, institutional-page
  fallback, offline demo mode, all three OS launchers, and the existing
  visual identity — untouched. All 253 backend tests pass; the frontend
  suite is unchanged and still valid.

## v2.0 — Broader Sources, Deeper Harvesting

(Recap of this lineage's v2.0, included in the same tree.)

- 8 → 15 archive sources; 9 keyless (added Cleveland, V&A, Wikimedia
  Commons, Internet Archive curated collections, BnF Gallica, Rijksmuseum,
  Harvard Art Museums).
- Fixed The Metropolitan Museum adapter (v1 search retired 2026-10-01;
  migrated to live-verified v1.1) and the U.S. National Archives adapter
  (official v2 protocol: required headers, `availableOnline=true`,
  `objectUrl` thumbnails, production dates).
- Page parameter flowed through frontend → serve → discovery → adapters.
- Discovery pool 283 → 376 topics; featured seeds widened; per-batch
  bounds widened (12 queries × 3 sources × 6 results, feed cap 48).
- 48 new hermetic tests; live-captured fixtures for four sources.
