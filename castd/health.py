"""Minimal /health HTTP endpoint for external monitoring (e.g. Uptime Kuma).

Pure stdlib (http.server), no hardware dependencies -- this module runs and
is tested on any machine. Reports the FSM's current state and the last
sd_notify watchdog ping time so an external monitor can catch a Pi whose
process is alive but whose main loop has wedged (the systemd watchdog
catches that case locally with a reboot; this endpoint lets a central
dashboard notice it happened, across every room at once).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from castd.fsm.state_machine import State


class HealthState:
    """Thread-safe shared state the HTTP handler reads and the main loop
    writes. Kept separate from the HTTP server class so it can be unit
    tested without binding a socket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = State.IDLE
        self._last_heartbeat = time.monotonic()

    def set_state(self, state: State) -> None:
        with self._lock:
            self._state = state

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.name,
                "seconds_since_heartbeat": round(time.monotonic() - self._last_heartbeat, 1),
            }

    def is_healthy(self, *, max_heartbeat_age_s: float = 30.0) -> bool:
        return self.snapshot()["seconds_since_heartbeat"] <= max_heartbeat_age_s


def _make_handler(health: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
            pass  # avahi-style access logs on every poll are just noise here

        def do_GET(self) -> None:
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(health.snapshot()).encode()
            status = 200 if health.is_healthy() else 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve_forever(health: HealthState, *, port: int = 8973) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(health))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
