"""
sources/broadcast.py

Classroom Broadcast helpers: localhost vs LAN bind, a practical join URL,
and ephemeral in-memory sessions. No accounts, no cloud, no permanent IDs.
"""
from __future__ import annotations

import json
import socket
import time
import uuid
from typing import Any, Dict, List, Optional


SESSION_TTL_SECONDS = 10 * 60
RECENT_CARD_CAP = 8
ONLINE_WITHIN_SECONDS = 45


def bind_host(broadcast_enabled: bool) -> str:
    """Disabled = existing localhost-only bind. Enabled = all interfaces."""
    return "0.0.0.0" if broadcast_enabled else "127.0.0.1"


def _octets(ip: str) -> Optional[List[int]]:
    parts = (ip or "").split(".")
    if len(parts) != 4:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 or n > 255 for n in nums):
        return None
    return nums


def rfc1918_priority(ip: str) -> Optional[int]:
    """
    Classroom LAN preference: 10/8, then 192.168/16, then 172.16/12.
    Returns 0/1/2 for those ranges, or None for anything else (incl. 100.x).
    """
    nums = _octets(ip)
    if not nums:
        return None
    a, b = nums[0], nums[1]
    if a == 10:
        return 0
    if a == 192 and b == 168:
        return 1
    if a == 172 and 16 <= b <= 31:
        return 2
    return None


def classroom_address_rank(ip: str) -> int:
    pri = rfc1918_priority(ip)
    return pri if pri is not None else 50


def sort_classroom_addresses(addresses: Optional[List[str]]) -> List[str]:
    """Prefer RFC1918 classroom LAN addresses; keep 100.x/other as extras."""
    seen: List[str] = []
    for ip in addresses or []:
        if not ip or ip.startswith("127.") or ip in seen:
            continue
        seen.append(ip)
    return sorted(seen, key=lambda ip: (classroom_address_rank(ip), ip))


def lan_ipv4_addresses() -> List[str]:
    """Best-effort LAN IPv4 list. Never includes loopback. Never raises."""
    found: List[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            if ip and not ip.startswith("127."):
                found.append(ip)
        finally:
            probe.close()
    except OSError:
        pass
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return sort_classroom_addresses(found)


def join_urls(port: int, addresses: Optional[List[str]] = None) -> List[str]:
    addrs = sort_classroom_addresses(
        addresses if addresses is not None else lan_ipv4_addresses()
    )
    return [f"http://{ip}:{int(port)}" for ip in addrs]


def device_label(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "cros" in ua:
        return "Chromebook"
    if "ipad" in ua:
        return "iPad"
    if "iphone" in ua:
        return "iPhone"
    if "android" in ua:
        return "Android tablet" if "mobile" not in ua else "Android phone"
    if "macintosh" in ua or "mac os" in ua:
        return "Mac"
    if "windows" in ua:
        return "Windows PC"
    if "linux" in ua:
        return "Linux"
    return "Browser"


def _clip_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:200]


def normalize_recent_cards(
    existing: Optional[List[Any]] = None,
    current_card_id: Optional[str] = None,
    current_card_title: Optional[str] = None,
    incoming: Optional[List[Any]] = None,
    cap: int = RECENT_CARD_CAP,
) -> List[Dict[str, str]]:
    """Newest-first, unique ids, hard-capped. Memory only."""
    out: List[Dict[str, str]] = []
    seen = set()

    def add(card_id: Any, title: Any = "") -> None:
        if not card_id:
            return
        cid = str(card_id)
        if cid in seen:
            return
        seen.add(cid)
        out.append({"id": cid, "title": _clip_title(title)})

    add(current_card_id, current_card_title)
    if isinstance(incoming, list):
        for item in incoming:
            if isinstance(item, dict):
                add(item.get("id") or item.get("cardId"), item.get("title") or "")
            else:
                add(item, "")
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict):
                add(item.get("id"), item.get("title") or "")
            else:
                add(item, "")
    return out[: max(1, int(cap))]


class SessionBook:
    """Ephemeral local sessions. Forgotten after TTL with no heartbeat."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._rows: Dict[str, Dict[str, Any]] = {}

    def _purge(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        expired = [sid for sid, row in self._rows.items() if now - row["lastSeen"] > self.ttl_seconds]
        for sid in expired:
            self._rows.pop(sid, None)

    def register(
        self,
        session_id: Optional[str] = None,
        user_agent: str = "",
        current_card_id: Optional[str] = None,
        saved_count: int = 0,
        is_host: bool = False,
        current_card_title: Optional[str] = None,
        recent_cards: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        self._purge()
        sid = session_id if session_id and session_id in self._rows else uuid.uuid4().hex[:12]
        now = time.time()
        existing = self._rows.get(sid) or {}
        title = current_card_title if current_card_title is not None else existing.get("currentCardTitle")
        card_id = current_card_id if current_card_id is not None else existing.get("currentCardId")
        recent = normalize_recent_cards(
            existing.get("recentCards") or [],
            current_card_id=card_id,
            current_card_title=title,
            incoming=recent_cards,
        )
        row = {
            "id": sid,
            "role": "host" if is_host else "classroom",
            "device": device_label(user_agent or existing.get("userAgent") or ""),
            "userAgent": user_agent or existing.get("userAgent") or "",
            "createdAt": existing.get("createdAt", now),
            "lastSeen": now,
            "currentCardId": card_id,
            "currentCardTitle": _clip_title(title) if title else (recent[0]["title"] if recent else ""),
            "savedCount": int(saved_count or 0),
            "recentCards": recent,
        }
        self._rows[sid] = row
        return dict(row)

    def _public_row(self, row: Dict[str, Any], now: float) -> Dict[str, Any]:
        age = now - float(row.get("lastSeen") or 0)
        return {
            "id": row["id"],
            "device": row["device"],
            "lastSeen": row["lastSeen"],
            "presence": "online" if age <= ONLINE_WITHIN_SECONDS else "recent",
            "currentCardId": row.get("currentCardId"),
            "currentCardTitle": row.get("currentCardTitle") or "",
            "savedCount": row.get("savedCount", 0),
            "recentCards": list(row.get("recentCards") or [])[:RECENT_CARD_CAP],
        }

    def list_classroom(self) -> List[Dict[str, Any]]:
        """Remote classroom browsers only — never the host/teacher session."""
        self._purge()
        now = time.time()
        rows = [r for r in self._rows.values() if r.get("role") != "host"]
        rows.sort(key=lambda r: r["lastSeen"], reverse=True)
        return [self._public_row(r, now) for r in rows]

    def list_connected(self) -> List[Dict[str, Any]]:
        return self.list_classroom()

    def classroom_count(self) -> int:
        """All retained classroom sessions (online + recently seen)."""
        self._purge()
        return sum(1 for r in self._rows.values() if r.get("role") != "host")

    def classroom_online_count(self, now: Optional[float] = None) -> int:
        """Devices currently online — not recently-seen leftovers, not the host."""
        self._purge()
        now = time.time() if now is None else now
        return sum(
            1
            for r in self._rows.values()
            if r.get("role") != "host"
            and (now - float(r.get("lastSeen") or 0)) <= ONLINE_WITHIN_SECONDS
        )

    def count(self) -> int:
        self._purge()
        return len(self._rows)


SHARED_CARD_STR_FIELDS = (
    "id",
    "title",
    "hook",
    "longerContext",
    "date",
    "era",
    "location",
    "type",
    "mediaTemplate",
    "classificationLabel",
    "image",
    "thumbnailUrl",
    "iiifImageUrl",
    "localImagePath",
    "audioUrl",
    "sourceUrl",
    "sourceName",
    "institution",
    "actionLabel",
    "creator",
    "duration",
    "contextStatus",
)
SHARED_CARD_STR_LISTS = ("tags", "relatedPeople", "relatedPlaces", "relatedCardIds")
SHARED_CARD_BOOLS = ("reconstruction", "indigenous")
EVIDENCE_KEYS = ("institution", "sourceItemId", "sourceUrl", "collection", "provider")
MAX_SHARED_STR = 2000
MAX_SHARED_ID = 200
MAX_SHARED_TITLE = 200
MAX_SHARED_LIST = 24
MAX_SHARED_JSON_BYTES = 80_000


def _clip_shared_str(value: Any, limit: int = MAX_SHARED_STR) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


def sanitize_shared_card(raw: Any) -> Optional[Dict[str, Any]]:
    """Keep a normalized Time Machine card in ephemeral Broadcast memory.

    Accepts `{card: {...}}` or a flat card / `{cardId, title}` wrapper.
    Unknown keys are dropped. No database — caller stores this in RAM.
    """
    if not isinstance(raw, dict):
        return None
    src = raw["card"] if isinstance(raw.get("card"), dict) else raw
    card_id = src.get("id") or raw.get("cardId") or raw.get("id")
    title = src.get("title") or raw.get("title")
    if not card_id or not title:
        return None
    card: Dict[str, Any] = {}
    for key in SHARED_CARD_STR_FIELDS:
        if key in src and src[key] is not None:
            limit = MAX_SHARED_ID if key == "id" else MAX_SHARED_TITLE if key == "title" else MAX_SHARED_STR
            clipped = _clip_shared_str(src[key], limit)
            if clipped is not None:
                card[key] = clipped
    card["id"] = _clip_shared_str(card_id, MAX_SHARED_ID) or ""
    card["title"] = _clip_shared_str(title, MAX_SHARED_TITLE) or ""
    if not card["id"] or not card["title"]:
        return None
    for key in SHARED_CARD_STR_LISTS:
        val = src.get(key)
        if isinstance(val, list):
            card[key] = [
                item[:200]
                for item in val
                if isinstance(item, str) and item
            ][:MAX_SHARED_LIST]
    for key in SHARED_CARD_BOOLS:
        if key in src:
            card[key] = bool(src[key])
    ev = src.get("evidence")
    if isinstance(ev, dict):
        evidence = {
            k: _clip_shared_str(ev.get(k), 500)
            for k in EVIDENCE_KEYS
            if ev.get(k) is not None
        }
        if evidence:
            card["evidence"] = evidence
    encoded = json.dumps(card, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_SHARED_JSON_BYTES:
        keep = (
            "id",
            "title",
            "hook",
            "longerContext",
            "date",
            "location",
            "type",
            "image",
            "thumbnailUrl",
            "sourceUrl",
            "sourceName",
            "institution",
        )
        card = {k: card[k] for k in keep if k in card}
    return {"id": card["id"], "title": card["title"], "card": card}


def public_shared_card(shared: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(shared, dict):
        return None
    out: Dict[str, Any] = {
        "id": shared.get("id"),
        "title": shared.get("title"),
    }
    nested = shared.get("card")
    if isinstance(nested, dict):
        out["card"] = dict(nested)
    return out
