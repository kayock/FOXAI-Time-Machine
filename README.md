# FOXAI Time Machine

A visual, exploratory history feed for authentic historical discovery and
classroom use. What if history had a really good social feed?

FOXAI Time Machine v2.2.0 browses real institutional archives. It does not
generate history. Continuous TimeStream, Surprise Me, and the Gazette keep
walking those archives; discovery progress, organic topics, and duplicate
memory persist on disk; and the same app launches on Windows, Linux, and
macOS. No GPU, no LLM, and no paid AI service is required.

Time Machine is normally driven by **real online institutional archives** —
seventeen of them as of v2.2.0: NASA, the Library of Congress, The Metropolitan
Museum of Art, the Art Institute of Chicago, the Cleveland Museum of Art,
the Victoria & Albert Museum, the Rijksmuseum, Harvard Art Museums, the
Wellcome Collection, the J. Paul Getty Museum (via its documented SPARQL
service), Smithsonian Open Access, DPLA, Europeana, the U.S. National
Archives, Wikimedia Commons, the Internet Archive (historical collections,
two-tier gated), BnF Gallica (the French national library's digital
library), and any IIIF-publishing museum or library — through
a small local Python service that queries those sources, normalizes the
results, and hands them to the same frontend as before. The bundled
`content/cards.json` cards are an **offline/demo fallback**, used only when
the local service isn't running or a query returns nothing live.

No build step, no database server, no login, no cloud dependency. Just:
- a small stdlib-only Python service (`serve.py` + `sources/`) that talks to
  the outside internet so the browser doesn't have to (avoids CORS and keeps
  API credentials out of the browser), and
- the same static HTML/CSS/vanilla JS frontend as before, now able to load
  live results from that service.

Personal favorites/saves/follows still live in your own browser's
localStorage — nothing about that changed.

## Running it

**Easiest — one-click starters** (same app on every OS):

- **Windows:** double-click `Start Time Machine.cmd`
- **macOS:** double-click `Start Time Machine.command`  
  First time, macOS Gatekeeper may block it — use **Right-click → Open**, then confirm.
- **Linux:** run / double-click `start-timemachine.sh`

Each starter runs shared `launch.py`, which starts `serve.py` on
`http://127.0.0.1:8420` (or reuses it if already running) and opens your
browser. In the app, use **Stop Time Machine** to shut down cleanly.

**Troubleshooting fallback** (terminal):

```bash
cd foxai-timemachine
py serve.py          # Windows
python3 serve.py     # macOS / Linux
# then open http://127.0.0.1:8420
```

That's it — one process serves both the frontend and the API. If you just
open `index.html` directly instead (no service running), Time Machine still
works, using the offline demo cards, and says so in the feed.

**Portable ZIP** (encodes Mac/Linux launcher execute bits even on Windows):

```bash
py package_release.py          # Windows
python3 package_release.py     # macOS / Linux
# → ../FOXAI-Time-Machine.zip
```

### Adding API keys for the sources that need them

Eleven sources need no key and work immediately: NASA, the Library of
Congress, The Metropolitan Museum of Art, the Art Institute of Chicago, the
Cleveland Museum of Art, the Victoria & Albert Museum, Wikimedia Commons,
the Internet Archive (two-tier historical gate), BnF Gallica, the Wellcome
Collection and the J. Paul Getty Museum. The others (Smithsonian, DPLA,
Europeana, NARA, Rijksmuseum, Harvard Art Museums) need a free key. With a
key, the Smithsonian paginates with the documented rows/start pair.
All seventeen adapters declare native deep pagination. Europeana stops
at the documented first 1,000 hits. National Archives uses the OpenAPI
`page` window (maximum 10000) and does not send `searchAfter`:

```bash
cp config.example.json config.local.json
# edit config.local.json and fill in whichever keys you have
```

`config.local.json` is gitignored. Keys can also be set as environment
variables of the same name instead, if you'd rather not put them in a file.
A source with no key configured simply reports itself unavailable — it
doesn't crash anything, and the rest of Time Machine works normally.

## How a request flows

```
Browser (TimeFeed / Explore / Learn More)
   │  fetch("/api/feed"), fetch("/api/search?q=...")
   ▼
serve.py (local, stdlib http.server)
   │  calls the right adapter(s) in sources/
   ▼
sources/nasa.py, loc.py, met.py (v1.1 search), aic.py, cma.py, vam.py,
sources/commons.py, internetarchive.py, gallica.py, smithsonian.py,
sources/dpla.py, europeana.py, nara.py, rijksmuseum.py, harvard.py,
sources/iiif.py (any IIIF manifest URL), sources/institutional_page.py
   │  normalize messy real API responses into one small shape
   ▼
sources/base.py — TimeMachineItem (stable id, mediaKind, mediaTemplate,
evidence, etc.) + the mixed-media template registry
   │
   ▼
Browser renders it with the existing card/Gazette/Learn More UI
```

If the service can't reach a source (no key, network down, bad response),
that source is silently skipped — the rest of the feed still works, and if
literally nothing is available, Time Machine falls back to the offline demo
cards and says so.

### The 15 featured discovery seeds

`seeds/discovery_seeds.json` lists 15 topics as search targets, not
hand-authored records. `sources/seeds.py` resolves each one, in order:

1. Search its `preferredSources` with its `searchQueries`.
2. If more than one real result comes back, prefer whichever one's own,
   truthfully-computed template happens to match `templateHint` — this
   never relabels a result to fake a match (a photograph stays a
   photograph even if the hint asked for video).
3. If nothing came back, and the seed has an `optionalSeedUrl`, fetch
   exactly that URL through the institutional-page fallback.
4. Otherwise the seed is honestly reported as unresolved.

`GET /api/seeds` exposes this directly — for each of the 15 you can see
whether it resolved, how (`source-search` vs `institutional-fallback` vs
`unresolved`), which source, and whether the `templateHint` was actually
matched.

## Project layout

```
serve.py                    local service: static files + JSON API (stdlib only)
sources/
  base.py                   normalized item model, stable IDs, template registry
  config.py                 API key loading, bounded cache, HTTP GET helper
  discovery_state.py        v2.1 persistent discovery state: per-(source,query)
                            cursors, exhaustion + lazy refresh, seen-record
                            dedup memory, discovered topics (SQLite, WAL,
                            bounded; state/discovery_state.db)
  dynamic_topics.py         v2.1 organic topic mining from archival metadata
  nasa.py, loc.py, met.py,  keyless adapters (met.py uses the live-verified
  aic.py, cma.py, vam.py,   v1.1 search API; cma = Cleveland; vam = V&A)
  commons.py,               keyless: Wikimedia Commons (File-namespace
  internetarchive.py,       search), Internet Archive (two-tier historical
  gallica.py                gate), BnF Gallica via SRU/Dublin-Core XML
  wellcome.py, getty.py     v2.1 keyless: Wellcome Collection (native page
                            pagination), J. Paul Getty Museum (documented
                            SPARQL service + record REST)
  smithsonian.py, dpla.py,
  europeana.py, nara.py,    key-required adapters (rijks = Rijksmuseum,
  rijksmuseum.py, harvard.py  harvard = Harvard Art Museums)
  iiif.py                   generic IIIF Presentation manifest adapter
  institutional_page.py     JSON-LD/OpenGraph fallback for one specific supplied page
  registry.py                17 sources, capability flags, bounded-concurrency
                            multi-source search
  discovery.py               deep-exploration roam: per-search cursors,
                            exhaustion, dedup, era/institution/media balance
seeds/discovery_seeds.json  the 15 featured discovery targets (search queries, not cards)
seeds/discovery_queries.json  400-topic curated discovery pool + organic growth
state/discovery_state.db    created at runtime; persistent exploration progress
config.example.json         copy to config.local.json and fill in keys you have

index.html                  the app shell (TimeFeed, Gazette, Explore, Worldlines, Saved)
css/styles.css               all styling
js/lib.js                    pure helper functions — unit tested, shared with tests
js/data.js                   generated — offline demo cards, embedded for the browser
js/app.js                    app logic: rendering, live-feed loading, search, favorites/saves/follows

content/cards.json           offline/demo fallback cards — edit this for demo content
content/worldlines.json      demo worldlines
content/infographics.json    the reusable infographic authoring template + one example

tools/rebuild_data.py        regenerates js/data.js from content/cards.json (demo mode only)
tests/                       hermetic browser + Python test suites (see Testing below)
```

## Adding a new offline/demo card

This still works exactly as before, for the offline fallback content only —
live content comes from the sources above and needs no hand-authoring.

1. Open `content/cards.json`.
2. Copy an existing card object and edit it. The minimum you need:

```json
{
  "id": "unique-id-no-spaces",
  "title": "Earthrise",
  "date": "December 24, 1968",
  "location": "Lunar orbit",
  "type": "photograph",
  "hook": "Apollo 8 astronauts became the first humans to see Earth rising above another world.",
  "image": "https://example.org/earthrise.jpg",
  "sourceUrl": "https://example.org/source",
  "sourceName": "NASA",
  "actionLabel": "View Original"
}
```

   Everything else — `creator`, `institution`, `longerContext`, `transcript`,
   `tags`, `relatedPeople`, `relatedPlaces`, `relatedCardIds`, `coordinates`,
   `perspectives`, `indigenous`, `reconstruction` — is optional.

3. **Images** — use whichever field matches what you have; the app tries
   them in order and falls back automatically if one 404s:
   `"image"`, `"thumbnailUrl"`, `"iiifImageUrl"`, `"localImagePath"`.

4. **Audio** — add `"audioUrl": "https://..."` for a real native browser
   `<audio>` player. Without it, the card shows a plain "no playable
   recording linked yet" placeholder.

5. For a Time Machine Gazette entry, use `"type": "gazette"`. If the event
   didn't have a real period newspaper, set `"reconstruction": true`.

6. Regenerate the embedded demo data file:

```bash
python3 tools/rebuild_data.py
```

7. Reload the page (with `serve.py` not running, or with no live results,
   you'll see these demo cards).

## Adding a discovery seed

Live content comes from `seeds/discovery_seeds.json` — each entry is a
title, a few search queries, and which sources to prefer, NOT a
hand-authored catalog record. Add an entry there to make Time Machine go
looking for a new topic across real archives.

## Adding an infographic

Use the reusable schema in `content/infographics.json` (see brief section 9)
rather than inventing a new shape per infographic — title, question, hook,
infographicType, facts, visualSpecification, sources, accessibilityDescription,
optionalContext, status.

## Design notes

- **No login, no server-side accounts.** The local service has no concept of
  a user; personalization (favorites/saves/follows/recently-viewed) is still
  entirely client-side in `localStorage`.
- **A saved remote item survives its source going offline.** Favoriting or
  saving a live item keeps a small copy of its normalized data
  (`state.itemCache` in `js/app.js`) so it still displays later even if that
  institution or the local service is temporarily unreachable — Time Machine
  never re-downloads or mirrors the original archival media for this.
- **Error handling is graceful on purpose**, at every layer: a missing/broken
  image shows a placeholder; an audio card with no real URL shows a plain
  non-clickable-looking placeholder instead of a fake play button; a
  malformed remote record or IIIF manifest is skipped, not crashed on; one
  source being down or missing a key never affects the others.
- **The mixed-media template registry** (`sources/base.py` on the backend,
  mirrored in `js/lib.js` on the frontend) decides how an item is classified
  and what its primary action button says/does — e.g. `audio-oral-history`
  → "Listen at Institution" (external), `reconstructed-story` → "Open
  Gazette" (internal). See brief section 8 for the full contract.
- **The 15 discovery seeds are search targets, not a database.** Time
  Machine goes and finds them live; it doesn't ship a pre-built catalog.
  Resolution is deliberately picky: a search result only counts if it's
  genuinely on-topic and, where a `templateHint` names a real media
  requirement (audio/video), actually matches it — a loosely-related result
  (a modern anniversary post, a generic program page, an unrelated hit) is
  treated the same as "nothing found" and resolution falls through to the
  seed's exact `optionalSeedUrl` instead of settling for it.
- **"Surprise Me" fetches fresh material, it doesn't just reshuffle.** When
  live sources are available, it calls `/api/feed?surprise=true&exclude=...`
  and roams a curated **discovery-query pool** (376 topics in
  `seeds/discovery_queries.json`) across available archival adapters —
  topics beyond the 15 featured seeds, drawn from all fifteen institutions.
  The featured seeds still power the normal TimeFeed load. Surprise Me
  skips whatever's already on screen, diversifies by media kind without
  relabeling media, and only falls back to a local reshuffle in offline/
  demo mode or if that request fails. Favorites/saves are untouched either
  way (tracked by id in `localStorage`).
- **Deep exploration with persistent progress (v2.1).** Discovery no
  longer re-reads first pages: every (source, query) pair has its own
  cursor in a small SQLite store (`state/discovery_state.db`), advanced
  when a batch retrieves results, marked exhausted when an archive runs
  dry (skipped thereafter, lazily re-explored after 30 days because
  collections change), and resumed across restarts. All seventeen
  adapters use native pagination (pages, offsets, SPARQL OFFSET, SRU
  startRecord, MediaWiki continuation, Europeana `start`, National
  Archives `page`). A documented provider window, such as Europeana's
  first 1,000 hits or the National Archives page maximum of 10,000, ends
  the search instead of repeating the last in-window page. Records that
  were retrieved but not shown stay in a bounded candidate queue for a
  later batch. GET
  `/api/discovery/stats` shows exactly where exploration stands, and POST
  `/api/discovery/reset` (localhost) starts the cursors over.
- **Organic discovery beyond the curated pool (v2.1).** Each batch mines
  NEW search topics from the archives' own subject/keyword/place metadata
  (no AI service, nothing fabricated), stores them with provenance, and
  mixes up to a quarter of the next batch's topics from that pool — the
  app keeps finding subjects nobody had to pre-write.
- **Persistent deduplication.** Delivered records are remembered across
  restarts (bounded, oldest-pruned). The same conservative match also
  runs inside a single batch: same normalized URL, or exact title + media
  kind + year + a known creator on both sides, across different
  institutions. Distinct artifacts with similar titles, unknown creators,
  or different years are kept. Batches are additionally balanced by
  institution, historical era (century buckets from the records' own date
  labels), geography and media kind.
- **The discovery pipeline itself runs concurrently (v2.2.0).** Surprise
  Me, continuous TimeFeed, TimeStream and the Gazette share one
  process-wide worker pool — at most eight fetches in flight for the
  whole application, and at most two per archive — with the existing
  per-batch deadline. Overlapping requests do not each spawn another pool.
- **Failures never masquerade as exhausted searches (v2.2.0).** Network
  errors, timeouts, 5xx outages, rate limits and malformed responses are
  classified and put into backoff or cooldown with the discovery position
  preserved. HTTP 429 honors Retry-After as delta-seconds or an HTTP-date,
  and that wait is not shortened to a local ceiling. The cooldown applies
  to other in-flight queries for the same archive. Only a well-formed
  empty page, or a page past a documented result window, marks a search
  exhausted. `GET
  /api/discovery/stats` reports backoff/cooldown counts.
- **Modern history is included (v2.1.1).** The Internet Archive's broad
  tier no longer carries a 1979 date ceiling: quality comes from curated
  collections, an expanded historical whitelist, engagement-sorted
  ranking and a conservative spam blocklist (gaming/streaming
  re-uploads) — not from excluding everything made after 1979.
- **Conservative cross-archive deduplication (v2.1.1).** Unknown creator
  metadata can never merge two records: suppression requires the same
  normalized URL, or exact title + media kind + year + a KNOWN identical
  creator on both sides. Distinct artifacts with similar metadata always
  survive.
- **Sustainable storage (v2.1.1).** Caps are configurable
  (FOXAI_SEEN_MAX_ROWS / FOXAI_TOPIC_MAX_ROWS / FOXAI_PROGRESS_MAX_ROWS;
  defaults 1,000,000 / 20,000 / 250,000), pruning fires only when a cap is
  exceeded, schema upgrades migrate old databases in place without data
  loss, and measured cost is ≈320 bytes/row (≈320 MB per million records,
  flat RAM).
- **Continuous TimeFeed** uses the same discovery path: scrolling near the
  bottom appends another bounded batch (about 8 cards), excludes session-
  seen ids, shows a small “Loading more history…” note, backs off on empty
  or HTTP 429 responses, and trims oldest ordinary rendered cards around
  ~180 so the DOM stays bounded. Favorites/saves/recent state are preserved.

### Stopping the local server (Windows-friendly)

With the service running on this machine:

- Click **Stop Time Machine** in the feed toolbar, or
- `POST http://127.0.0.1:8420/api/shutdown` (localhost only; empty JSON body)

Example:

```bash
curl -X POST http://127.0.0.1:8420/api/shutdown -H "Content-Type: application/json" -d "{}"
```

Ctrl+C in the terminal still works. Shutdown is refused from non-loopback
clients. Browser favorites/saves in `localStorage` are not deleted.

Runtime cache (`cache_data/`), `__pycache__/`, and similar artifacts are
gitignored and not part of normal packaging.

## Testing

Two independent suites, kept deliberately hermetic (no live network) by default:

**Frontend** — open `tests/test-runner.html` in a browser (or serve the
project and visit `/tests/test-runner.html`). 29 tests covering content
validation, malformed-card handling, favorites/save logic, search, the
mixed-media template contract (primary-action wording, internal vs.
external templates), infographic loading, and real boot/render checks of
`index.html` via hidden iframes — including that a photograph's primary
action really is a link to the institution, a Gazette card's primary action
really opens Learn More instead of navigating, infographics render
generically and show up in the Gazette, and the Evidence button actually
reveals evidence data. These genuinely fail if the app doesn't behave this
way — they're not placeholders that always pass.

**Backend** — hermetic, no network required:

```bash
python3 tests/python/test_sources_hermetic.py     # 42 tests: adapters, IIIF media detection, templates
python3 tests/python/test_new_sources_hermetic.py # 60 tests: the v2.0/v2.1 adapters (incl. Wellcome, Getty),
                                                  #   Met v1.1 migration, pagination params, registry wiring
python3 tests/python/test_discovery_state.py      # 27 tests: persistent cursors, exhaustion + refresh,
                                                  #   restart-resume, cross-archive dedup, bounds, thread safety
python3 tests/python/test_deep_discovery.py       # 12 tests: sustained unique-record growth, page walking,
                                                  #   exhausted-query skipping, dynamic-topic usage, era mix
python3 tests/python/test_dynamic_topics.py       # 13 tests: organic topic mining + store integration
python3 tests/python/test_seed_resolution.py      # seed resolution, weak-result rejection, templateHint, fallback
python3 tests/python/test_discovery.py            # general Surprise Me discovery pool
python3 tests/python/test_feed_endpoint.py        # /api/feed featured + surprise paths, localhost shutdown
python3 tests/python/test_broadcast.py            # Broadcast / classroom sessions
python3 tests/python/test_met_aic.py              # Met + AIC normalization
python3 tests/python/test_gazette.py              # Gazette edition selection
python3 tests/python/test_cache_bound.py          # bounded cache behavior

Total: 253 hermetic backend tests, all passing. The optional live suite
(test_live_smoke.py) covers all seventeen sources; docs/ARCHIVE-MATRIX.md
is the archive-by-archive capability and verification matrix.
```

Fixtures for the v2.0 adapters are, per file, either **live-captured real
API responses** (Cleveland, V&A, Commons, Met v1.1 — captured during the
upgrade) or **docs-modeled responses** clearly labeled inside the file
(Internet Archive, Gallica, Rijksmuseum, Harvard), used where the
development sandbox was rate-limited or edge-blocked. Each adapter's
docstring records exactly which case applies.

Covers stable identity, template selection, normalization for NASA (verified
against a real captured API response) and LOC (verified against LOC's own
documented schema), IIIF manifest parsing — including telling image, audio,
and video apart from the canvas body's actual type/format rather than
guessing from unrelated fields like the presence of a `duration` — the
institutional-page JSON-LD/OpenGraph fallback, and the seed-resolution
engine: preferred-source search, rejecting loosely-related or retrospective/
anniversary-style results in favor of the exact `optionalSeedUrl` fallback,
templateHint-aware selection that never relabels a real result, and honest
"unresolved" reporting when nothing good is available.

**Optional live smoke test** — makes real requests to external institutions;
never run automatically:

```bash
python3 tests/python/test_live_smoke.py
```


