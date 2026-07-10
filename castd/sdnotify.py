"""Minimal sd_notify client -- no libsystemd/python3-systemd dependency.

The castd.service unit is Type=notify with WatchdogSec=30 (see castd.service
comment: this is the hard reboot backstop for a wedged main loop that the
project retrospective's plain `Restart=always` never had). systemd expects
READY=1 once at startup and WATCHDOG=1 at least once per WatchdogSec/2 or it
kills and restarts the unit.

`build_notify_message` is pure and unit-tested. `notify` does the actual
AF_UNIX datagram send and is a no-op (not an error) when NOTIFY_SOCKET is
unset, which is always true on this Windows dev box and in plain pytest
runs -- so calling it in tests is harmless.
"""
from __future__ import annotations

import os
import socket


def build_notify_message(*, ready: bool = False, watchdog: bool = False, status: str | None = None) -> bytes:
    lines = []
    if ready:
        lines.append("READY=1")
    if watchdog:
        lines.append("WATCHDOG=1")
    if status:
        lines.append(f"STATUS={status}")
    if not lines:
        raise ValueError("build_notify_message called with nothing to report")
    return "\n".join(lines).encode()


def notify(*, ready: bool = False, watchdog: bool = False, status: str | None = None) -> None:
    socket_path = os.environ.get("NOTIFY_SOCKET")
    if not socket_path:
        return  # not running under systemd (e.g. dev box, plain pytest) - silently skip
    message = build_notify_message(ready=ready, watchdog=watchdog, status=status)
    addr = ("\0" + socket_path[1:]) if socket_path.startswith("@") else socket_path
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.send(message)
