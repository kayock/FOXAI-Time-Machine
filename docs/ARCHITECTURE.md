# FOXAI Time Machine — Architecture

v2.2.0. Standard library only. No web framework, no account system, no
GPU, no language model, and no paid AI service.

## Process

`start-timemachine.sh`, `Start Time Machine.cmd`, and
`Start Time Machine.command` all run `launch.py`. That script starts
`serve.py` on `127.0.0.1:8420` (or reuses a server that is already this
app) and opens a browser. One process serves the static frontend and the
JSON API. Favorites, saves, and follows stay in the browser's
`localStorage` for that origin, so the host and port are part of the
user's data.

If the service is not running, the frontend falls back to the bundled
cards in `content/cards.json`. Those cards are a demo set. Live discovery
does not invent records.

## Request path

`serve.py` is a `ThreadingHTTPServer`. Feed, Surprise Me, TimeStream, and
the Gazette call `discover_surprise`. Search calls `search_all` or
`search_source`. Both go through the same process-wide worker pool
(`APP_MAX_WORKERS = 8`) and the same per-archive semaphore
(`PER_SOURCE_CONCURRENCY = 2`). Each discovery batch still stops waiting
at its own deadline (30 seconds). The pool is not shut down between
batches, so overlapping requests cannot accumulate threads.

## Archives

`sources/registry.py` lists the seventeen adapters. Each one implements
`availability()` and `search()` and returns `TimeMachineItem` values.
Optional keys live in `config.local.json` (Smithsonian, DPLA, Europeana,
National Archives, Rijksmuseum, Harvard). A missing key makes that source
unavailable. It does not fail the others.

Pagination is provider-specific (`page`, `offset`, `start`, `skip`,
`gsroffset`, SPARQL `OFFSET`, SRU `startRecord`). There is no local page
cap. Where a provider documents a result window, a request past that
window returns no rows and the cursor is exhausted instead of repeating
the last page:

- Europeana Search API: `start` is 1-based and offset paging stops at the
  first 1,000 hits.
- National Archives Catalog API v2: OpenAPI `page` maximum 10000 and
  `limit` maximum 1000. `searchAfter` is not used.

## Memory

`sources/discovery_state.py` is one SQLite file, `state/discovery_state.db`
(override the directory with `FOXAI_STATE_DIR`):

- Query cursors, including backoff and exhaustion.
- Seen records, default cap 1,000,000 (`FOXAI_SEEN_MAX_ROWS`).
- Organic topics mined from archive metadata.
- A candidate queue of retrieved-but-not-presented records, default cap
  20,000 (`FOXAI_CANDIDATE_MAX_ROWS`).
- Per-archive cooldowns taken from Retry-After, shared by overlapping
  queries.

Duplicate matching is conservative: the same id, the same normalized URL,
or the same normalized title, media kind, year, and a known creator on
both sides from two different archives. Similar titles alone are kept.

Retry-After accepts delta-seconds and an HTTP-date. The stored wait is
never shorter than the provider asked for.

## What is not generated

Topic strings come from metadata the archives returned. Cards in the live
feed come from adapter results or the offline demo file. The app does not
fill gaps with synthesized history.
