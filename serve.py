#!/usr/bin/env python3
"""
serve.py — FOXAI Time Machine local service.

Serves the existing static frontend (index.html, css/, js/, content/) AND a
small JSON API that queries real online institutional sources, normalizes
results, and returns them to the browser.

Why a local service at all: browser CORS policies and (for some sources)
API credentials make direct browser-to-archive requests impractical. This
process is the only thing that talks to the outside internet; the browser
only ever talks to this local service.

Standard library only — http.server, no Flask/FastAPI/etc.

Usage:
    python3 serve.py [--port 8420]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from sources.registry import (  # noqa: E402
    list_sources, search_source, search_source_strict, search_all, diversify,
    set_provider_cooldown_checker,
)
from sources.iiif import fetch_and_normalize_iiif_manifest  # noqa: E402
from sources.institutional_page import fetch_and_normalize_institutional_page  # noqa: E402
from sources.base import TimeMachineItem  # noqa: E402
from sources.seeds import resolve_all_seeds  # noqa: E402
from sources.discovery import discover_surprise, load_discovery_queries  # noqa: E402
from sources.gazette import select_edition  # noqa: E402
from sources.aic import is_aic_iiif_url  # noqa: E402
from sources.broadcast import (  # noqa: E402
    SessionBook,
    bind_host,
    join_urls,
    public_shared_card,
    sanitize_shared_card,
)
from sources.config import FetchError, http_get_bytes  # noqa: E402

try:
    with (PROJECT_ROOT / "seeds" / "discovery_seeds.json").open("r", encoding="utf-8") as f:
        DISCOVERY_SEEDS = json.load(f)
except (OSError, json.JSONDecodeError):
    DISCOVERY_SEEDS = []

DISCOVERY_QUERIES = load_discovery_queries()

STATIC_EXTENSIONS = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
STATIC_ALLOWED_DIRS = {"css", "js", "content", "tests"}

# Bounded — never request or return huge result sets. v2.0 widened the
# ceilings modestly (15 sources now contribute; pagination keeps batches
# fresh) without turning discovery into an unbounded crawl.
MAX_FEED_LIMIT = 48
MAX_SEARCH_LIMIT = 30
APP_VERSION = "2.2.0"
MAX_AIC_IMAGE_BYTES = 2_000_000


class AppState:
    """Process-wide Broadcast / session + persistent discovery state."""

    def __init__(self, bind: str = "127.0.0.1", port: int = 8420):
        self.bind = bind
        self.port = port
        self.broadcast_enabled = False
        self.stop_requested = False
        self.rebind_to = None
        self.sessions = SessionBook()
        self.httpd = None
        self.shared_card = None  # ephemeral {id, title, card}; cleared when Broadcast ends
        # v2.1 persistent discovery state (per-source/query cursors, seen
        # records, discovered topics). Created lazily so a broken/absent
        # state directory can never prevent startup; discovery degrades to
        # in-memory behavior if the store cannot be opened.
        self._discovery_state = None
        self._discovery_state_failed = False

    @property
    def discovery_state(self):
        if self._discovery_state is None and not self._discovery_state_failed:
            try:
                from sources.discovery_state import DiscoveryState
                self._discovery_state = DiscoveryState()
                state_ref = self._discovery_state
                set_provider_cooldown_checker(
                    lambda sid, _state=state_ref: _state.provider_cooldown_remaining(sid) > 0
                )
            except Exception:  # noqa: BLE001 — no persistence is better than no app
                self._discovery_state_failed = True
                self._discovery_state = None
        return self._discovery_state

    def close_discovery_state(self):
        if self._discovery_state is not None:
            try:
                self._discovery_state.close()
            except Exception:  # noqa: BLE001
                pass
            self._discovery_state = None


def _items_to_dicts(items):
    return [i.to_dict() for i in items]


def broadcast_payload(state: AppState, is_host: bool) -> dict:
    urls = join_urls(state.port)
    payload = {
        "enabled": bool(state.broadcast_enabled),
        "bind": state.bind,
        "port": state.port,
        "isHost": is_host,
        "joinUrls": urls,
        "joinUrl": urls[0] if urls else None,
        "sharedCard": public_shared_card(state.shared_card),
    }
    if is_host:
        payload["connected"] = state.sessions.classroom_online_count()
    return payload


def build_feed(limit: int, exclude_ids=None, randomize: bool = False, surprise: bool = False,
               recent_source_ids=None, page: int = 1, state=None, stats_out=None):
    """
    Featured feed (default): resolve the 15 curated discovery seeds, merge
    whatever actually resolved, diversify by media kind, and cap at `limit`.

    Surprise Me (`surprise=True`): roam the general discovery-query pool
    across available archival adapters — not limited to the 15 featured
    topics. With a persistent DiscoveryState (v2.1), each (source, query)
    resumes at its stored page, exhausted searches are skipped, delivered
    records are remembered across restarts, and organically discovered
    topics are mixed into the roam.

    `page` (>= 1) advances the query-pool rotation for Surprise Me batches;
    per-search depth comes from the persistent store when available.

    Returns (items, live: bool, sourcesTried: list[str]).
    """
    limit = max(1, min(int(limit or 20), MAX_FEED_LIMIT))
    exclude_ids = exclude_ids or set()

    available_ids = {s["id"] for s in list_sources() if s["available"]}
    if not available_ids:
        return [], False, []

    if surprise:
        # v2.1.1: the strict variant lets discovery distinguish empty pages
        # from failures (temporary outcomes get backoff, never exhaustion),
        # and the pipeline fetches all planned pages concurrently.
        items, sources_tried, _queries = discover_surprise(
            search_source_strict,
            list_sources,
            queries=DISCOVERY_QUERIES,
            exclude_ids=exclude_ids,
            limit=limit,
            iiif_fn=fetch_and_normalize_iiif_manifest,
            recent_source_ids=recent_source_ids or [],
            page=page,
            state=state,
            stats_out=stats_out,
        )
        if state is not None and items:
            try:
                state.record_seen(items)
            except Exception:  # noqa: BLE001 — persistence must never break the feed
                pass
        return items, True, sources_tried

    resolutions = resolve_all_seeds(
        DISCOVERY_SEEDS, search_source, fetch_and_normalize_institutional_page,
        limit=15, randomize=randomize,
    )
    sources_tried = sorted({r.sourceId for r in resolutions if r.sourceId})
    collected = [r.item for r in resolutions if r.resolved and r.item and r.item.id not in exclude_ids]

    if not collected:
        return [], True, sources_tried

    diversified = diversify(collected, limit)
    if state is not None and diversified:
        try:
            # Featured seeds count as delivered material too, so continuous
            # discovery won't re-serve them later in the session.
            state.record_seen(diversified)
        except Exception:  # noqa: BLE001
            pass
    return diversified, True, sources_tried


class Handler(BaseHTTPRequestHandler):
    server_version = "FOXAITimeMachine/2.2.0"

    def log_message(self, format, *args):  # noqa: A002 — quiet, structured-ish logging
        sys.stderr.write(f"[serve.py] {self.address_string()} {format % args}\n")

    # ---- helpers -----------------------------------------------------
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _app_state(self):
        return getattr(self.server, "app_state", None)

    def _send_error_json(self, message, status=400):
        self._send_json({"error": message}, status=status)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _serve_static(self, path: str):
        # Only ever serve index.html at root, or files under an explicitly
        # allowed subdirectory — never arbitrary filesystem paths.
        clean = path.lstrip("/").split("?")[0]
        if clean == "" or clean == "index.html":
            file_path = PROJECT_ROOT / "index.html"
        else:
            parts = clean.split("/")
            if parts[0] not in STATIC_ALLOWED_DIRS:
                self._send_error_json("Not found", status=404)
                return
            file_path = (PROJECT_ROOT / clean).resolve()
            if PROJECT_ROOT not in file_path.parents and file_path != PROJECT_ROOT:
                self._send_error_json("Not found", status=404)
                return

        if not file_path.exists() or not file_path.is_file():
            self._send_error_json("Not found", status=404)
            return

        content_type = STATIC_EXTENSIONS.get(file_path.suffix, "application/octet-stream")
        try:
            data = file_path.read_bytes()
        except OSError:
            self._send_error_json("Could not read file", status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- routing -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        try:
            if parsed.path == "/api/sources":
                self._send_json({"version": APP_VERSION, "sources": list_sources()})
                return

            if parsed.path == "/api/search":
                q = (query.get("q") or [""])[0].strip()
                source_id = (query.get("source") or [None])[0]
                media = (query.get("media") or [None])[0]
                limit = int((query.get("limit") or [10])[0])
                limit = max(1, min(limit, MAX_SEARCH_LIMIT))
                page = max(1, int((query.get("page") or [1])[0] or 1))
                if not q:
                    self._send_error_json("Missing required query parameter 'q'", status=400)
                    return
                if source_id:
                    items = search_source(source_id, q, media=media, limit=limit, page=page)
                else:
                    items = diversify(search_all(q, media=media, limit_per_source=max(3, limit // 2), page=page), limit)
                self._send_json({"query": q, "items": _items_to_dicts(items)})
                return

            if parsed.path == "/api/feed":
                limit = int((query.get("limit") or [20])[0])
                # "Surprise Me" passes surprise=true (general archival discovery
                # beyond the 15 featured seeds) and exclude=<comma-separated
                # ids> (what's already on screen). page=<n> advances the
                # query-pool rotation; per-search depth comes from the
                # persistent discovery store.
                surprise = (query.get("surprise") or ["false"])[0].lower() == "true"
                exclude_raw = (query.get("exclude") or [""])[0]
                exclude_ids = {x for x in exclude_raw.split(",") if x} if exclude_raw else set()
                recent_raw = (query.get("recentSources") or [""])[0]
                recent_source_ids = [x for x in recent_raw.split(",") if x] if recent_raw else []
                try:
                    page = max(1, int((query.get("page") or [1])[0] or 1))
                except ValueError:
                    page = 1
                app = self._app_state()
                discovery_state = app.discovery_state if app is not None else None
                items, live, tried = build_feed(
                    limit,
                    exclude_ids=exclude_ids,
                    randomize=surprise,
                    surprise=surprise,
                    recent_source_ids=recent_source_ids,
                    page=page,
                    state=discovery_state,
                )
                self._send_json({"live": live, "sourcesTried": tried, "items": _items_to_dicts(items)})
                return

            if parsed.path == "/api/discovery/stats":
                # Transparency for deep exploration: cursors per source,
                # exhausted searches, seen-record memory, discovered topics.
                app = self._app_state()
                store = app.discovery_state if app is not None else None
                if store is None:
                    self._send_json({"available": False, "reason": "persistent state unavailable"})
                else:
                    payload = {"available": True}
                    payload.update(store.stats())
                self._send_json(payload)
                return

            if parsed.path == "/api/gazette":
                app = self._app_state()
                items, live, tried = build_feed(
                    24, surprise=True,
                    state=app.discovery_state if app is not None else None,
                )
                edition = select_edition(items)
                self._send_json({
                    "live": live,
                    "sourcesTried": tried,
                    "available": edition["available"],
                    "lead": edition["lead"],
                    "secondary": edition["secondary"],
                    "archives": edition["archives"],
                    "storyCount": edition["storyCount"],
                    "note": None if edition["available"] else (
                        "No illustrated archival stories were available just now."
                    ),
                })
                return

            if parsed.path == "/api/broadcast":
                state = self._app_state() or AppState()
                self._send_json(broadcast_payload(state, self._client_is_localhost()))
                return

            if parsed.path == "/api/sessions":
                if not self._client_is_localhost():
                    self._send_error_json("Session list only available on the host machine", status=403)
                    return
                state = self._app_state() or AppState()
                payload = broadcast_payload(state, True)
                payload["sessions"] = state.sessions.list_classroom()
                self._send_json(payload)
                return

            if parsed.path == "/api/aic-image":
                remote = (query.get("u") or [""])[0]
                if not is_aic_iiif_url(remote):
                    self._send_error_json(
                        "Only Art Institute of Chicago IIIF image URLs can be proxied",
                        status=400,
                    )
                    return
                try:
                    data, ctype = http_get_bytes(remote, max_bytes=MAX_AIC_IMAGE_BYTES)
                except FetchError as e:
                    self._send_error_json(str(e), status=502)
                    return
                if not (ctype or "").startswith("image/"):
                    self._send_error_json("Remote URL was not an image", status=502)
                    return
                self._send_bytes(data, ctype)
                return

            if parsed.path == "/api/seeds":
                # Makes seed resolution (brief section 1) directly inspectable:
                # which of the 15 featured targets resolved, how, and whether
                # the templateHint was actually matched — not dead metadata.
                resolutions = resolve_all_seeds(DISCOVERY_SEEDS, search_source, fetch_and_normalize_institutional_page)
                self._send_json({"seeds": [r.to_dict() for r in resolutions]})
                return

            if parsed.path == "/api/item":
                # Single-item re-fetch by source + sourceItemId (best effort;
                # saved items should carry their own full data too — see
                # brief section 14 — so this is a refresh convenience, not
                # the only way to redisplay a saved card).
                source_id = (query.get("source") or [None])[0]
                source_item_id = (query.get("sourceItemId") or [None])[0]
                q = (query.get("q") or [source_item_id or ""])[0]
                if not source_id or not q:
                    self._send_error_json("Requires 'source' and either 'sourceItemId' or 'q'", status=400)
                    return
                items = search_source(source_id, q, limit=5)
                match = next((i for i in items if i.sourceItemId == source_item_id), items[0] if items else None)
                if not match:
                    self._send_error_json("Item not found or source unavailable", status=404)
                    return
                self._send_json({"item": match.to_dict()})
                return

            # static files (index.html, css/js/content/tests)
            self._serve_static(parsed.path)
        except Exception as e:  # noqa: BLE001 — the service must survive a bad request
            self._send_error_json(f"Internal error: {e}", status=500)

    def _client_is_localhost(self) -> bool:
        host = (self.client_address[0] if self.client_address else "") or ""
        return host in ("127.0.0.1", "::1", "localhost")

    def _request_shutdown(self):
        """Flush the JSON response, then stop the HTTP server from a daemon
        thread so this request handler can finish cleanly."""
        state = self._app_state()
        if state is not None:
            state.stop_requested = True
        def _stop():
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_stop, daemon=True).start()

    def _request_rebind(self, bind: str):
        """Restart the HTTP server on a new bind address (Broadcast on/off)."""
        state = self._app_state()
        if state is not None:
            state.rebind_to = bind
            state.stop_requested = False
        def _stop():
            try:
                self.server.shutdown()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_stop, daemon=True).start()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
            if body is None:
                self._send_error_json("Request body must be valid JSON", status=400)
                return

            if parsed.path == "/api/shutdown":
                # Localhost-only graceful stop — no auth system; the bind
                # address is already 127.0.0.1 by default, and this rejects
                # any non-loopback client as a second belt.
                if not self._client_is_localhost():
                    self._send_error_json("Shutdown only allowed from localhost", status=403)
                    return
                self._send_json({"ok": True, "message": "Time Machine server shutting down"})
                self._request_shutdown()
                return

            if parsed.path == "/api/discovery/reset":
                # Localhost-only: clear exploration cursors (per-source/query
                # pages and exhaustion marks) so discovery starts roaming
                # from first pages again. Seen-records and discovered topics
                # are preserved — use the FOXAI_STATE_DIR database file for a
                # full wipe.
                if not self._client_is_localhost():
                    self._send_error_json("Discovery reset only allowed from localhost", status=403)
                    return
                state = self._app_state()
                store = state.discovery_state if state is not None else None
                if store is None:
                    self._send_error_json("Persistent discovery state unavailable", status=500)
                    return
                store.reset_all()
                self._send_json({"ok": True, "reset": "query_progress"})
                return

            if parsed.path == "/api/broadcast":
                if not self._client_is_localhost():
                    self._send_error_json("Broadcast can only be changed from the host machine", status=403)
                    return
                state = self._app_state()
                if state is None:
                    self._send_error_json("Broadcast state unavailable", status=500)
                    return
                enabled = bool((body or {}).get("enabled"))
                state.broadcast_enabled = enabled
                if not enabled:
                    state.shared_card = None
                target = bind_host(enabled)
                payload = broadcast_payload(state, True)
                payload["ok"] = True
                payload["bind"] = target
                self._send_json(payload)
                if target != state.bind:
                    self._request_rebind(target)
                return

            if parsed.path == "/api/class-card":
                if not self._client_is_localhost():
                    self._send_error_json("Class card can only be set from the host machine", status=403)
                    return
                state = self._app_state()
                if state is None:
                    self._send_error_json("Broadcast state unavailable", status=500)
                    return
                card_id = (body or {}).get("cardId") or (body or {}).get("id")
                nested = (body or {}).get("card") if isinstance((body or {}).get("card"), dict) else None
                if not card_id and not nested:
                    state.shared_card = None
                else:
                    state.shared_card = sanitize_shared_card(body)
                self._send_json({"ok": True, "sharedCard": public_shared_card(state.shared_card)})
                return

            if parsed.path == "/api/session":
                state = self._app_state()
                if state is None:
                    self._send_error_json("Session state unavailable", status=500)
                    return
                ua = self.headers.get("User-Agent") or ""
                is_host = self._client_is_localhost()
                row = state.sessions.register(
                    session_id=(body or {}).get("sessionId"),
                    user_agent=ua,
                    current_card_id=(body or {}).get("currentCardId"),
                    saved_count=int((body or {}).get("savedCount") or 0),
                    is_host=is_host,
                    current_card_title=(body or {}).get("currentCardTitle"),
                    recent_cards=(body or {}).get("recentCards"),
                )
                self._send_json({
                    "sessionId": row["id"],
                    "device": row["device"],
                    "role": row.get("role"),
                    "currentCardId": row.get("currentCardId"),
                    "savedCount": row.get("savedCount", 0),
                    "sharedCard": public_shared_card(state.shared_card),
                })
                return

            if parsed.path == "/api/iiif":
                manifest_url = (body or {}).get("manifestUrl")
                if not manifest_url:
                    self._send_error_json("Missing 'manifestUrl'", status=400)
                    return
                item = fetch_and_normalize_iiif_manifest(manifest_url)
                if not item:
                    self._send_json({"available": False, "reason": "Manifest unavailable or unusable"}, status=200)
                    return
                self._send_json({"available": True, "item": item.to_dict()})
                return

            if parsed.path == "/api/institutional-item":
                url = (body or {}).get("url")
                if not url:
                    self._send_error_json("Missing 'url'", status=400)
                    return
                item = fetch_and_normalize_institutional_page(url)
                if not item:
                    self._send_json({"available": False, "reason": "Page unavailable or had no usable metadata"}, status=200)
                    return
                self._send_json({"available": True, "item": item.to_dict()})
                return

            self._send_error_json("Not found", status=404)
        except Exception as e:  # noqa: BLE001
            self._send_error_json(f"Internal error: {e}", status=500)


def main():
    parser = argparse.ArgumentParser(description="FOXAI Time Machine local service")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--bind", type=str, default="127.0.0.1")
    args = parser.parse_args()

    state = AppState(bind=args.bind, port=args.port)
    state.broadcast_enabled = (args.bind not in ("127.0.0.1", "::1", "localhost"))

    while True:
        server = ThreadingHTTPServer((state.bind, state.port), Handler)
        server.app_state = state
        state.httpd = server
        print(f"FOXAI Time Machine running at http://{state.bind}:{state.port}")
        if state.broadcast_enabled:
            urls = join_urls(state.port)
            if urls:
                print("Broadcasting. Students: open " + "  or  ".join(urls))
            else:
                print("Broadcasting on all interfaces, but no LAN address was detected.")
        else:
            print("Press Ctrl+C to stop, or POST http://127.0.0.1:{}/api/shutdown from this machine.".format(state.port))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            state.stop_requested = True
            print("\nStopping.")
        finally:
            server.server_close()
            state.httpd = None
            state.close_discovery_state()

        if state.stop_requested:
            print("Stopped.")
            break
        if state.rebind_to is not None:
            state.bind = state.rebind_to
            state.rebind_to = None
            print(f"Rebinding to {state.bind}:{state.port}...")
            continue
        print("Stopped.")
        break


if __name__ == "__main__":
    main()