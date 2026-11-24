#!/usr/bin/env python3
"""
launch.py — shared one-click starter for FOXAI Time Machine.

Starts the existing serve.py on 127.0.0.1:8420 (or reuses it if already
running), opens the default browser, and waits until the server stops.
Does not implement a second server — only launches serve.py.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SERVE_SCRIPT = PROJECT_ROOT / "serve.py"
HOST = "127.0.0.1"
PORT = 8420
BASE_URL = f"http://{HOST}:{PORT}"
READY_PATH = "/api/sources"
READY_WAIT_SECONDS = 12.0
READY_POLL_SECONDS = 0.25


def fail(message: str, code: int = 1) -> None:
    print()
    print("Time Machine could not start.")
    print(message)
    print()
    if sys.platform.startswith("win"):
        try:
            input("Press Enter to close this window… ")
        except EOFError:
            pass
    sys.exit(code)


def port_is_listening(host: str = HOST, port: int = PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def fetch_sources():
    req = urllib.request.Request(
        BASE_URL + READY_PATH,
        headers={"User-Agent": "FOXAITimeMachine-Launcher/1.0"},
    )
    with urllib.request.urlopen(req, timeout=2.5) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw), resp.headers


def is_timemachine_instance() -> bool:
    """True when something on :8420 looks like this app's /api/sources."""
    try:
        data, headers = fetch_sources()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return False
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list) or not sources:
        return False
    ids = {s.get("id") for s in sources if isinstance(s, dict)}
    # Distinctive enough for this project without being brittle.
    if not {"nasa", "loc"}.issubset(ids):
        return False
    server = (headers.get("Server") or "") if headers else ""
    if server and "FOXAITimeMachine" not in server:
        # Still accept JSON shape match — some proxies strip Server.
        pass
    return True


def wait_until_ready(timeout: float = READY_WAIT_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_timemachine_instance():
            return True
        time.sleep(READY_POLL_SECONDS)
    return False


def open_app() -> None:
    webbrowser.open(BASE_URL)


def start_server() -> subprocess.Popen:
    if not SERVE_SCRIPT.is_file():
        fail(f"Could not find serve.py next to launch.py.\nLooked in:\n  {PROJECT_ROOT}")

    cmd = [sys.executable, str(SERVE_SCRIPT), "--bind", HOST, "--port", str(PORT)]
    # Inherit stdout/stderr so the launcher window shows server logs.
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=None,
        stderr=None,
    )


def main() -> int:
    print("FOXAI Time Machine")
    print(f"Project: {PROJECT_ROOT}")
    print(f"URL:     {BASE_URL}")
    print()

    if port_is_listening():
        if is_timemachine_instance():
            print("Time Machine is already running — opening your browser.")
            open_app()
            print("Use “Stop Time Machine” in the app when you are done.")
            if sys.platform.startswith("win"):
                try:
                    input("Press Enter to close this window… ")
                except EOFError:
                    pass
            return 0
        fail(
            f"Port {PORT} is already in use by another program, not Time Machine.\n"
            f"Close whatever is using {BASE_URL} (or free port {PORT}), then try again.\n"
            "Time Machine will not kill unrelated processes."
        )

    print("Starting serve.py…")
    try:
        proc = start_server()
    except OSError as e:
        fail(f"Could not start Python server process:\n  {e}")

    if not wait_until_ready():
        # Avoid leaving a half-dead child if readiness never arrived.
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        fail(
            "The server process started but did not become ready in time.\n"
            f"Try the fallback from this folder:\n  {sys.executable} serve.py\n"
            f"Then open {BASE_URL}"
        )

    print("Ready — opening your browser.")
    print("Leave this window open while you use Time Machine.")
    print("In the app, click “Stop Time Machine” to shut down cleanly.")
    print()
    open_app()

    try:
        return_code = proc.wait()
    except KeyboardInterrupt:
        print("\nStopping…")
        if proc.poll() is None:
            try:
                # Prefer the app's localhost shutdown when possible.
                req = urllib.request.Request(
                    BASE_URL + "/api/shutdown",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "User-Agent": "FOXAITimeMachine-Launcher/1.0"},
                    method="POST",
                )
                try:
                    urllib.request.urlopen(req, timeout=3).read()
                except Exception:
                    proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except OSError:
                    pass
        return_code = 0

    if return_code not in (0, None):
        print(f"Server exited with code {return_code}.")
    else:
        print("Time Machine stopped.")
    return 0 if return_code in (0, None) else int(return_code)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        fail(f"Unexpected launcher error:\n  {type(e).__name__}: {e}")
