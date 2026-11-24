# Archive-by-Archive Capability and Verification Matrix (v2.2.0)

17 registered sources. Eleven keyless. All seventeen declare native deep
pagination. National Archives `page` and `limit` come from the published
OpenAPI document at `https://catalog.archives.gov/api/v2/swagger.json`
(`page` maximum 10000, `limit` maximum 1000). `searchAfter` is not sent:
its schema (integer, maximum 100) contradicts the prose (`searchAfter=*`).
Europeana uses documented `start`/`rows` and stops at the first 1,000 hits.
Arbitrary page clamps were removed; a page past a documented window is not
re-requested as the last in-window page.

The "Station 2026-11-24" column is what this machine observed while
preparing v2.2.0. Earlier sandbox notes stay in the fixture column.
Keyed archives were not called: no optional API keys were configured.

| # | Source | Institution | Key | Media emphasis | Pagination mechanism | Station 2026-11-24 |
|---|--------|-------------|-----|----------------|----------------------|----------------------|
| 1 | `nasa` | NASA Image & Video Library | None | Photos, film, audio | page/page_size (documented) | Smoke returned 3 records for Apollo 11 and for Earthrise. Page 2 ids were fully distinct from page 1 (3 and 3). |
| 2 | `loc` | Library of Congress | None | Photos, documents, maps, audio | sp start-page (documented) | Search timed out (read timeout) on "Navajo Code Talkers" and on page 2 of "Navajo". No records this run. |
| 3 | `met` | The Metropolitan Museum of Art | None | Artwork, artifacts | v1.1 limit/offset | Smoke returned 3 records for monet. Page 2 ids fully distinct (3 and 3). |
| 4 | `aic` | Art Institute of Chicago | None | Artwork | page/limit (documented) | Not in the smoke script. Direct page probe returned 3 and 3 with no shared ids. Surprise feed included AIC records. |
| 5 | `cma` | Cleveland Museum of Art | None | Artwork, artifacts | skip/limit | Smoke returned 3 records for great wave. Page probe: 2 then 3, no shared ids. |
| 6 | `vam` | Victoria & Albert Museum | None | Design, photography | page/page_size | Smoke returned 3 records for great wave. Page probe: 2 then 3, no shared ids. |
| 7 | `wellcome` | Wellcome Collection | None | Manuscripts, photos, art, film | page/pageSize | Smoke returned 3 records for plague. Page probe: 3 then 2, no shared ids. |
| 8 | `getty` | J. Paul Getty Museum | None | Artwork (Linked.Art) | SPARQL ORDER BY + OFFSET | Smoke SPARQL for irises timed out. A later page probe returned 3 then 2 with no shared ids. |
| 9 | `commons` | Wikimedia Commons | None | Broad historical imagery | gsroffset continuation | Smoke returned 3 records for apollo 11. Page probe: 3 then 3 with 1 shared id, so the window moved and one record repeated. |
| 10 | `internetarchive` | Internet Archive | None | Film, radio audio, texts | page | Smoke returned 3 records for civil defense. Page 2 ids fully distinct (3 and 3). |
| 11 | `gallica` | BnF Gallica | None | Manuscripts, newspapers | SRU startRecord/maximumRecords | HTTP 403 on pompeii, page 1 and page 2. Surprise listed Gallica among sources tried; no Gallica record was in the 8 delivered items. |
| 12 | `smithsonian` | Smithsonian Open Access | Free key | Artifacts, photos | page/rows, no artificial page clamp | Skipped. No API key configured. |
| 13 | `dpla` | Digital Public Library of America | Free key | Aggregated US collections | page/page_size | Skipped. No API key configured. |
| 14 | `europeana` | Europeana | Free key | Aggregated EU collections | start/rows, offset window of 1,000 hits | Skipped. No API key configured. Cursor past 1,000 is not implemented. |
| 15 | `nara` | U.S. National Archives | Free key | Documents, photos | page/limit (OpenAPI 3.0, page max 10000) | Skipped. No API key configured. searchAfter is not sent. |
| 16 | `rijksmuseum` | Rijksmuseum | Free key | Dutch Golden Age | p/ps page/size | Skipped. No API key configured. |
| 17 | `harvard` | Harvard Art Museums | Free key | Artwork, artifacts | size/page | Skipped. No API key configured. |

## Discovery-state capability (v2.1)

| Capability | Status | Verified by |
|------------|--------|-------------|
| Per-(source, query) cursors | Implemented | 27 hermetic tests + live server run |
| Restart resume | Implemented | live restart test (cursors/seen/topics preserved) |
| Exhaustion detection + skip | Implemented | hermetic 12-test suite; live sustained run |
| Lazy refresh of exhausted searches (30 days) | Implemented | hermetic test |
| Persistent seen-record memory (bounded 100k) | Implemented | hermetic tests incl. pruning |
| Cross-archive duplicate detection | Implemented | hermetic tests (same URL / title+year+creator) |
| Distinct-artifact preservation | Implemented | hermetic tests (similar titles kept) |
| Organic topic mining (no AI) | Implemented | 13 hermetic tests; live batches mined 6→11 topics |
| Era / institution / media diversity | Implemented | hermetic tests |
| Geographic (region) diversity | Implemented (v2.1.1) | hermetic tests |
| Bounded concurrency + batch deadline | Implemented | registry implementation + fail-soft tests |
| `/api/discovery/stats` transparency | Implemented | live server run |
| `/api/discovery/reset` (localhost) | Implemented | live server run |
| Temporary-failure taxonomy (backoff/Retry-After/cooldowns) | Implemented (v2.1.1) | 13 store tests + 11 pipeline tests; live sustained run |
| Concurrency inside the discovery pipeline | Implemented (v2.1.1) | Barrier-proof tests (peak ≥5 in flight), pacing + deadline tests |
| Conservative cross-archive dedup (no unknown-creator merges) | Implemented (v2.1.1) | false-positive/negative + restart tests |
| Schema migration (v2.1 DB → v2.1.1 in place) | Implemented (v2.1.1) | migration suite (data preserved, idempotent) |
| Configurable 1M-scale storage caps | Implemented (v2.1.1) | measured: 321 B/row, 1M ≈ 320 MB, 5M ≈ 1.6 GB, flat RAM |

A public IIIF manifest fetch during the same smoke run returned
title "Simplest Video Example (IIIF Presentation 3)".

## Sustained live discovery on Station (v2.2.0, bounded)

`tools/benchmark_sustained.py --batches 4 --limit 24 --restart-at 2`
on 2026-11-24. Raw aggregates are in
`docs/verification/benchmark-v220-bounded.json`. This is not the
historical 52-batch file.

- Unique records grew 24, 48, 72, 96. Every delivered record was new.
  Duplicate detections: 0.
- Dynamic topics grew 6, 11, 17, 22.
- Concurrent tasks per batch: 23, 21, 23, 21. Pages consumed: 50.
- Temporary errors: 3 in batch 1 and 1 in batch 2, then 0 in batches
  3 and 4. One later fetch was skipped because the provider was still
  cooling down. No rate-limit responses in this run.
- After batch 2 the store was closed and reopened. Cursors (44) and
  seen records (48) were still there, and batches 3 and 4 continued
  to new records. Candidate queue replayed 33, then 83, then 96 rows.
- Archives that contributed records: AIC, Cleveland, Commons,
  Internet Archive, Library of Congress, the Met, NASA, V&A, Wellcome.
  Getty and Gallica contributed no rows in these four batches.
  Keyed archives were not called.

## Sustained live discovery (v2.1.1 sandbox notes, not this Station run)

12 batches × 24 requested: **288 unique authentic records, zero
repeats**, ~16.5 s per batch, deep-page fetches in every batch.
Sources contributing live: met, vam, cma, wellcome, nasa (and getty
in the pagination matrix). Blocked-in-sandbox sources (loc, commons,
ia, gallica, all key-required) fail soft and simply contribute
nothing; on a normal home connection all 17 contribute.

