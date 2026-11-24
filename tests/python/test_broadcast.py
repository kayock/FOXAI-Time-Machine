#!/usr/bin/env python3
"""Hermetic tests for Broadcast bind/config and ephemeral sessions."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import serve  # noqa: E402
from sources.broadcast import (  # noqa: E402
    ONLINE_WITHIN_SECONDS,
    RECENT_CARD_CAP,
    SessionBook,
    bind_host,
    device_label,
    join_urls,
    sanitize_shared_card,
    sort_classroom_addresses,
)


class BindConfigTests(unittest.TestCase):
    def test_disabled_is_localhost_only(self):
        self.assertEqual(bind_host(False), "127.0.0.1")

    def test_enabled_is_lan_capable(self):
        self.assertEqual(bind_host(True), "0.0.0.0")

    def test_join_urls_use_port_8420_and_lan_ip(self):
        urls = join_urls(8420, ["192.168.1.40"])
        self.assertEqual(urls, ["http://192.168.1.40:8420"])

    def test_rfc1918_lan_addresses_beat_100x(self):
        ordered = sort_classroom_addresses([
            "100.113.89.37",
            "192.168.1.9",
            "10.0.0.104",
            "172.16.4.2",
        ])
        self.assertEqual(ordered[0], "10.0.0.104")
        self.assertEqual(ordered[1], "192.168.1.9")
        self.assertEqual(ordered[2], "172.16.4.2")
        self.assertEqual(ordered[-1], "100.113.89.37")
        urls = join_urls(8420, ["100.113.89.37", "10.0.0.104"])
        self.assertEqual(urls[0], "http://10.0.0.104:8420")
        self.assertIn("http://100.113.89.37:8420", urls)


class SessionTests(unittest.TestCase):
    def test_register_creates_ephemeral_id_without_accounts(self):
        book = SessionBook(ttl_seconds=60)
        row = book.register(user_agent="Mozilla/5.0 (Windows NT 10.0)")
        self.assertTrue(row["id"])
        self.assertEqual(len(row["id"]), 12)
        self.assertEqual(row["device"], "Windows PC")
        self.assertNotIn("email", row)
        self.assertNotIn("username", row)
        self.assertNotIn("password", row)
        self.assertEqual(book.count(), 1)
        again = book.register(session_id=row["id"], current_card_id="earthrise-1968", saved_count=2)
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(book.count(), 1)
        self.assertEqual(again["currentCardId"], "earthrise-1968")
        self.assertEqual(again["savedCount"], 2)

    def test_unknown_session_id_issues_a_fresh_ephemeral_id(self):
        book = SessionBook()
        row = book.register(session_id="not-a-real-session")
        self.assertNotEqual(row["id"], "not-a-real-session")
        self.assertEqual(book.count(), 1)

    def test_device_labels_are_friendly(self):
        self.assertEqual(device_label("Mozilla/5.0 (X11; CrOS x86_64)"), "Chromebook")
        self.assertEqual(device_label("Mozilla/5.0 (iPad; CPU OS 17_0)"), "iPad")
        self.assertEqual(device_label("Mozilla/5.0 (Macintosh; Intel Mac OS X)"), "Mac")

    def test_host_session_is_not_counted_as_classroom(self):
        book = SessionBook()
        host = book.register(is_host=True, user_agent="Mozilla/5.0 (Windows NT 10.0)")
        student = book.register(is_host=False, user_agent="Mozilla/5.0 (iPad; CPU OS 17_0)")
        self.assertEqual(book.count(), 2)
        self.assertEqual(book.classroom_count(), 1)
        classroom = book.list_classroom()
        self.assertEqual(len(classroom), 1)
        self.assertEqual(classroom[0]["id"], student["id"])
        self.assertNotEqual(classroom[0]["id"], host["id"])
        self.assertEqual(classroom[0]["device"], "iPad")

    def test_recent_cards_are_bounded(self):
        book = SessionBook()
        incoming = [{"id": f"card-{i}", "title": f"Title {i}"} for i in range(20)]
        row = book.register(
            is_host=False,
            current_card_id="card-now",
            current_card_title="Now showing",
            recent_cards=incoming,
        )
        self.assertLessEqual(len(row["recentCards"]), RECENT_CARD_CAP)
        self.assertEqual(row["recentCards"][0]["id"], "card-now")
        self.assertEqual(len(book.list_classroom()[0]["recentCards"]), len(row["recentCards"]))

    def test_recently_seen_sessions_are_listed_but_not_online(self):
        book = SessionBook()
        online = book.register(is_host=False, user_agent="Mozilla/5.0 (X11; CrOS x86_64)")
        recent = book.register(is_host=False, user_agent="Mozilla/5.0 (iPad; CPU OS 17_0)")
        book.register(is_host=True, user_agent="Mozilla/5.0 (Windows NT 10.0)")
        book._rows[recent["id"]]["lastSeen"] = time.time() - (ONLINE_WITHIN_SECONDS + 15)
        self.assertEqual(book.classroom_count(), 2)
        self.assertEqual(book.classroom_online_count(), 1)
        listed = {row["id"]: row["presence"] for row in book.list_classroom()}
        self.assertEqual(listed[online["id"]], "online")
        self.assertEqual(listed[recent["id"]], "recent")
        self.assertEqual(len(listed), 2)


class SharedCardSanitizeTests(unittest.TestCase):
    def test_sanitize_keeps_normalized_card_fields(self):
        raw = {
            "cardId": "apollo-11",
            "title": "Apollo 11 Moon Landing",
            "card": {
                "id": "apollo-11",
                "title": "Apollo 11 Moon Landing",
                "hook": "One small step.",
                "longerContext": "Armstrong and Aldrin landed in the Sea of Tranquility.",
                "date": "July 20, 1969",
                "location": "Moon",
                "type": "photograph",
                "image": "https://images-assets.nasa.gov/image/as11-40-5874/as11-40-5874~medium.jpg",
                "sourceUrl": "https://www.nasa.gov/mission/apollo-11/",
                "sourceName": "NASA",
                "institution": "NASA",
                "secretAdminToken": "should-not-survive",
            },
        }
        out = sanitize_shared_card(raw)
        self.assertEqual(out["id"], "apollo-11")
        self.assertEqual(out["title"], "Apollo 11 Moon Landing")
        card = out["card"]
        self.assertEqual(card["hook"], "One small step.")
        self.assertEqual(card["image"], raw["card"]["image"])
        self.assertEqual(card["sourceUrl"], raw["card"]["sourceUrl"])
        self.assertEqual(card["institution"], "NASA")
        self.assertNotIn("secretAdminToken", card)

    def test_sanitize_accepts_flat_id_and_title(self):
        out = sanitize_shared_card({"cardId": "earthrise-1968", "title": "Earthrise"})
        self.assertEqual(out["id"], "earthrise-1968")
        self.assertEqual(out["card"]["title"], "Earthrise")


class BroadcastEndpointTests(unittest.TestCase):
    def setUp(self):
        self.state = serve.AppState(bind="127.0.0.1", port=8420)
        self.handler = serve.Handler.__new__(serve.Handler)
        self.handler.server = mock.Mock(app_state=self.state)
        self.handler.headers = {"User-Agent": "test-agent"}
        self.sent = {}
        self.handler._send_json = lambda payload, status=200: self.sent.update({"payload": payload, "status": status})
        self.handler._send_error_json = lambda msg, status=400: self.sent.update({"error": msg, "status": status})
        self.handler._request_rebind = mock.Mock()

    def test_get_broadcast_disabled_reports_localhost_bind(self):
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/broadcast"
        self.handler.do_GET()
        self.assertEqual(self.sent["status"], 200)
        payload = self.sent["payload"]
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["bind"], "127.0.0.1")
        self.assertEqual(payload["port"], 8420)
        self.assertTrue(payload["isHost"])

    def test_enable_broadcast_from_localhost_requests_lan_bind(self):
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/broadcast"
        self.handler._read_json_body = lambda: {"enabled": True}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 200)
        self.assertTrue(self.state.broadcast_enabled)
        self.handler._request_rebind.assert_called_once_with("0.0.0.0")

    def test_enable_broadcast_from_lan_client_is_forbidden(self):
        self.handler.client_address = ("192.168.1.50", 9)
        self.handler.path = "/api/broadcast"
        self.handler._read_json_body = lambda: {"enabled": True}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 403)
        self.assertFalse(self.state.broadcast_enabled)
        self.handler._request_rebind.assert_not_called()

    def test_session_post_does_not_require_identity(self):
        self.handler.client_address = ("192.168.1.50", 9)
        self.handler.path = "/api/session"
        self.handler._read_json_body = lambda: {}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 200)
        self.assertTrue(self.sent["payload"]["sessionId"])
        self.assertNotIn("account", self.sent["payload"])
        self.assertNotIn("login", self.sent["payload"])
        self.assertEqual(self.state.sessions.classroom_count(), 1)

    def test_host_session_post_is_not_a_classroom_client(self):
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/session"
        self.handler._read_json_body = lambda: {"currentCardId": "earthrise-1968", "currentCardTitle": "Earthrise"}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 200)
        self.assertEqual(self.sent["payload"].get("role"), "host")
        self.assertEqual(self.state.sessions.classroom_count(), 0)
        self.assertEqual(self.state.sessions.list_classroom(), [])

    def test_teacher_sessions_api_lists_remote_only(self):
        self.state.sessions.register(is_host=True, user_agent="Windows")
        self.state.sessions.register(
            is_host=False,
            user_agent="Mozilla/5.0 (X11; CrOS x86_64)",
            current_card_id="apollo-11",
            current_card_title="Apollo 11 Moon Landing",
            saved_count=3,
            recent_cards=[{"id": "apollo-11", "title": "Apollo 11 Moon Landing"}],
        )
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/sessions"
        self.handler.do_GET()
        self.assertEqual(self.sent["status"], 200)
        sessions = self.sent["payload"]["sessions"]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["device"], "Chromebook")
        self.assertEqual(sessions[0]["currentCardTitle"], "Apollo 11 Moon Landing")
        self.assertEqual(self.sent["payload"]["connected"], 1)

    def test_lan_client_cannot_read_teacher_session_list(self):
        self.handler.client_address = ("192.168.1.50", 9)
        self.handler.path = "/api/sessions"
        self.handler.do_GET()
        self.assertEqual(self.sent["status"], 403)
        self.assertNotIn("sessions", self.sent)

    def test_shared_class_card_is_ephemeral_and_clears_when_broadcast_ends(self):
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/class-card"
        self.handler._read_json_body = lambda: {"cardId": "apollo-11", "title": "Apollo 11 Moon Landing"}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 200)
        self.assertEqual(self.state.shared_card["id"], "apollo-11")
        self.handler.path = "/api/broadcast"
        self.handler._read_json_body = lambda: {"enabled": False}
        self.handler.do_POST()
        self.assertIsNone(self.state.shared_card)
        self.assertFalse(self.state.broadcast_enabled)
        self.handler._request_rebind.assert_not_called()  # already on 127.0.0.1

    def test_lan_client_cannot_set_class_card(self):
        self.handler.client_address = ("10.0.0.55", 9)
        self.handler.path = "/api/class-card"
        self.handler._read_json_body = lambda: {"cardId": "apollo-11", "title": "Apollo 11"}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 403)
        self.assertIsNone(self.state.shared_card)

    def test_shared_class_card_carries_normalized_card_to_students(self):
        card = {
            "id": "apollo-11",
            "title": "Apollo 11 Moon Landing",
            "hook": "One small step.",
            "longerContext": "The lunar module Eagle landed in the Sea of Tranquility.",
            "date": "July 20, 1969",
            "location": "Moon",
            "type": "photograph",
            "image": "https://images-assets.nasa.gov/image/as11-40-5874/as11-40-5874~medium.jpg",
            "sourceUrl": "https://www.nasa.gov/mission/apollo-11/",
            "sourceName": "NASA",
            "institution": "NASA",
        }
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/class-card"
        self.handler._read_json_body = lambda: {"cardId": card["id"], "title": card["title"], "card": card}
        self.handler.do_POST()
        self.assertEqual(self.sent["status"], 200)
        stored = self.state.shared_card["card"]
        self.assertEqual(stored["image"], card["image"])
        self.assertEqual(stored["hook"], card["hook"])
        self.assertEqual(stored["sourceUrl"], card["sourceUrl"])
        self.handler.client_address = ("10.0.0.55", 9)
        self.handler.path = "/api/broadcast"
        self.handler.do_GET()
        self.assertEqual(self.sent["status"], 200)
        payload = self.sent["payload"]
        self.assertFalse(payload["isHost"])
        self.assertNotIn("sessions", payload)
        shared = payload["sharedCard"]
        self.assertEqual(shared["title"], "Apollo 11 Moon Landing")
        self.assertEqual(shared["card"]["image"], card["image"])
        self.assertEqual(shared["card"]["hook"], "One small step.")
        self.assertEqual(shared["card"]["sourceUrl"], card["sourceUrl"])
        self.handler.path = "/api/session"
        self.handler._read_json_body = lambda: {}
        self.handler.do_POST()
        student_shared = self.sent["payload"]["sharedCard"]
        self.assertEqual(student_shared["card"]["institution"], "NASA")
        self.assertNotIn("sessions", self.sent["payload"])

    def test_connected_count_ignores_recently_seen_and_host(self):
        self.state.sessions.register(is_host=True, user_agent="Mozilla/5.0 (Windows NT 10.0)")
        self.state.sessions.register(
            is_host=False,
            user_agent="Mozilla/5.0 (X11; CrOS x86_64)",
            current_card_title="Earthrise",
        )
        stale = self.state.sessions.register(
            is_host=False,
            user_agent="Mozilla/5.0 (iPad; CPU OS 17_0)",
            current_card_title="Apollo 11",
        )
        self.state.sessions._rows[stale["id"]]["lastSeen"] = time.time() - (ONLINE_WITHIN_SECONDS + 20)
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/sessions"
        self.handler.do_GET()
        self.assertEqual(self.sent["status"], 200)
        payload = self.sent["payload"]
        self.assertEqual(payload["connected"], 1)
        self.assertEqual(len(payload["sessions"]), 2)
        presence = {row["device"]: row["presence"] for row in payload["sessions"]}
        self.assertEqual(presence["Chromebook"], "online")
        self.assertEqual(presence["iPad"], "recent")

    def test_disable_broadcast_requests_localhost_bind(self):
        self.state.bind = "0.0.0.0"
        self.state.broadcast_enabled = True
        self.handler.client_address = ("127.0.0.1", 9)
        self.handler.path = "/api/broadcast"
        self.handler._read_json_body = lambda: {"enabled": False}
        self.handler.do_POST()
        self.assertFalse(self.state.broadcast_enabled)
        self.assertEqual(self.sent["payload"]["bind"], "127.0.0.1")
        self.handler._request_rebind.assert_called_once_with("127.0.0.1")


class AicImageProxyAllowlistTests(unittest.TestCase):
    def test_proxy_rejects_non_aic_urls(self):
        handler = serve.Handler.__new__(serve.Handler)
        handler.client_address = ("127.0.0.1", 9)
        sent = {}
        handler._send_error_json = lambda msg, status=400: sent.update({"error": msg, "status": status})
        handler._send_bytes = lambda *a, **k: sent.update({"bytes": True})
        handler.path = "/api/aic-image?u=https://example.com/secret.jpg"
        handler.do_GET()
        self.assertEqual(sent.get("status"), 400)
        self.assertNotIn("bytes", sent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
