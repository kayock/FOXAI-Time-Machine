"""
sources/config.py

Reads local, optional API credentials, and provides a tiny bounded cache and
an HTTP GET helper (stdlib urllib only — no requests dependency).

Credentials live in config.local.json (gitignored) at the project root.
config.example.json documents the shape. Missing keys are never fatal —
adapters that need a missing key report `available: false`.
"""
from __future__ import annotations

import json
import os
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.local.json"
CACHE_DIR = PROJECT_ROOT / "cache_data"
CACHE_MAX_AGE_SECONDS = 60 * 60 * 6     # 6 hours — bounded, not indefinite
CACHE_MAX_FILES = 200                   # small personal app — not a mirror
CACHE_MAX_BYTES = 40 * 1024 * 1024      # ~40 MB hard ceiling on cache_data/

_config_cache: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    """Read config.local.json if present. Never raises — missing/broken
    config just means every key-required source reports itself unavailable."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not CONFIG_PATH.exists():
        _config_cache = {}
        return _config_cache
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        _config_cache = {}
    return _config_cache


def get_key(name: str) -> Optional[str]:
    """API keys can come from config.local.json OR an environment variable
    of the same name (useful for not committing secrets at all)."""
    env_val = os.environ.get(name)
    if env_val:
        return env_val
    cfg = load_config()
    val = cfg.get(name)
    return val if isinstance(val, str) and val.strip() else None


# ---------------------------------------------------------------------------
# Bounded on-disk cache — normalized results and thumbnails only, never a
# full archival mirror. Old entries expire; the cache directory is capped.
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def cache_get(key: str) -> Optional[Any]:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            envelope = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - envelope.get("_cached_at", 0) > CACHE_MAX_AGE_SECONDS:
        return None
    return envelope.get("value")


def cache_set(key: str, value: Any) -> None:
    path = _cache_path(key)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump({"_cached_at": time.time(), "value": value}, f)
    except OSError:
        return
    _enforce_cache_bound()


def _enforce_cache_bound() -> None:
    """Keep the cache bounded by age, file count, and total bytes.
    Never touches authored content, config, or user localStorage data."""
    if not CACHE_DIR.exists():
        return
    now = time.time()
    files = []
    for path in CACHE_DIR.glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        # Drop expired envelopes first (best-effort; unreadable files go too).
        try:
            with path.open("r", encoding="utf-8") as f:
                envelope = json.load(f)
            age = now - float(envelope.get("_cached_at", 0) or 0)
            if age > CACHE_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
                continue
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        files.append((path, stat.st_mtime, stat.st_size))

    files.sort(key=lambda row: row[1])  # oldest first

    # Count bound
    while len(files) > CACHE_MAX_FILES:
        path, _mtime, _size = files.pop(0)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    # Size bound
    total = sum(size for _p, _m, size in files)
    while files and total > CACHE_MAX_BYTES:
        path, _mtime, size = files.pop(0)
        try:
            path.unlink(missing_ok=True)
            total -= size
        except OSError:
            pass


# ---------------------------------------------------------------------------
# HTTP GET helper
# ---------------------------------------------------------------------------

class FetchError(Exception):
    """A failed fetch, classified so discovery can distinguish temporary
    trouble (which must never exhaust a search) from a genuinely empty
    result. Kinds:
      rate_limited  HTTP 429 (Retry-After honored when the provider sends it)
      auth_error    HTTP 401/403 (missing/invalid key — long cooldown)
      server_error  HTTP 5xx (provider outage — temporary)
      http_error    other HTTP status (temporary by default)
      network       could not connect / DNS / timeout (temporary)
      malformed     response arrived but could not be parsed (temporary)
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 kind: str = "network", retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.retry_after = retry_after


def parse_retry_after(header: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Seconds to wait, from either RFC 9110 Retry-After form.

    Valid forms:
      - delta-seconds (`120`, `120.5`)
      - HTTP-date (`Wed, 21 Oct 2015 07:28:00 GMT`)

    Returns None when the header is missing or not a valid form. Never
    shortens a parsed value. A date in the past yields 0.
    """
    if header is None:
        return None
    text = str(header).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        return 0.0 if seconds < 0 else seconds
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (when - current).total_seconds())


def _classify_http_error(status: int, retry_after_header: Optional[str]) -> FetchError:
    kind = "http_error"
    retry_after = parse_retry_after(retry_after_header)
    if status == 429:
        kind = "rate_limited"
    elif status in (401, 403):
        kind = "auth_error"
    elif 500 <= status <= 599:
        kind = "server_error"
    return FetchError(f"HTTP {status}", status=status, kind=kind,
                      retry_after=retry_after)


def _http_open(req: urllib.request.Request, timeout: float, context=None):
    """Open a URL once. A 429 that names Retry-After is not retried here —
    sleeping a shortened slice of that delay would ignore the provider.
    A 429 with no Retry-After gets one short retry, then surfaces."""
    attempts = 0
    while True:
        attempts += 1
        try:
            if context is not None:
                return urllib.request.urlopen(req, timeout=timeout, context=context)
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempts < 2:
                ra = None
                try:
                    ra = e.headers.get("Retry-After") if e.headers else None
                except Exception:  # noqa: BLE001
                    ra = None
                if ra:
                    raise
                time.sleep(1.5)
                continue
            raise


def http_get_json(url: str, timeout: float = 8.0, headers: Optional[Dict[str, str]] = None) -> Any:
    """GET a URL and parse JSON. Raises FetchError on any failure — callers
    (adapters) are responsible for catching this and failing soft so one
    broken source never takes down the rest of the feed."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "FOXAITimeMachine/2.2.0"})
    try:
        with _http_open(req, timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        raise _classify_http_error(e.code, ra) from e
    except urllib.error.URLError as e:
        raise FetchError(f"Could not reach {url}: {e.reason}", kind="network") from e
    except Exception as e:  # noqa: BLE001 — deliberately broad; network calls fail in many ways
        raise FetchError(f"Unexpected error fetching {url}: {e}", kind="network") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FetchError(f"Invalid JSON from {url}: {e}", kind="malformed") from e


def http_get_text(url: str, timeout: float = 8.0, headers: Optional[Dict[str, str]] = None) -> str:
    req_headers = headers or {"User-Agent": "FOXAITimeMachine/2.2.0"}
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with _http_open(req, timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        raise _classify_http_error(e.code, ra) from e
    except urllib.error.URLError as e:
        # Some institutional hosts present incomplete certificate chains that
        # browsers/Schannel accept but Python's default verify rejects. Retry
        # once without verification so exact optionalSeedUrl fallbacks can
        # still resolve on Windows; never used as the first attempt.
        reason = str(getattr(e, "reason", e))
        if "CERTIFICATE_VERIFY_FAILED" in reason or "SSL" in reason:
            try:
                import ssl
                ctx = ssl._create_unverified_context()
                with _http_open(req, timeout, context=ctx) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
            except Exception as e2:  # noqa: BLE001
                raise FetchError(f"Could not reach {url}: {e.reason}") from e2
        else:
            raise FetchError(f"Could not reach {url}: {e.reason}") from e
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"Unexpected error fetching {url}: {e}") from e
    return raw.decode(charset, errors="replace")


def http_get_bytes(
    url: str,
    timeout: float = 10.0,
    headers: Optional[Dict[str, str]] = None,
    max_bytes: int = 2_000_000,
) -> tuple:
    """GET raw bytes (for a small same-origin media proxy). Retries once
    without TLS verification when the remote certificate is expired or
    otherwise untrusted — same classroom-host pattern as http_get_text.
    Returns (bytes, content_type)."""
    req_headers = headers or {"User-Agent": "FOXAITimeMachine/2.0"}
    req = urllib.request.Request(url, headers=req_headers)

    def _read(resp):
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise FetchError(f"Response from {url} exceeded {max_bytes} bytes")
        ctype = resp.headers.get("Content-Type") or "application/octet-stream"
        return raw, ctype.split(";")[0].strip()

    try:
        with _http_open(req, timeout) as resp:
            return _read(resp)
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} from {url}", status=e.code) from e
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "CERTIFICATE_VERIFY_FAILED" in reason or "SSL" in reason:
            try:
                import ssl
                ctx = ssl._create_unverified_context()
                with _http_open(req, timeout, context=ctx) as resp:
                    return _read(resp)
            except Exception as e2:  # noqa: BLE001
                raise FetchError(f"Could not reach {url}: {e.reason}") from e2
        raise FetchError(f"Could not reach {url}: {e.reason}") from e
    except FetchError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"Unexpected error fetching {url}: {e}") from e
