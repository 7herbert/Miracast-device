"""UxPlay subprocess manager.

Hardware-dependent (spawns the `uxplay` binary, which must be built with
GStreamer support; mDNS discovery additionally needs avahi-daemon running).

Real-hardware lessons (2026-07-14):
  * UxPlay has NO -bindif option (verified against the uxplay(1) man
    page); an earlier revision passed one and uxplay exited immediately
    with a usage error -- invisibly, because stdout/stderr were piped to
    a buffer nothing ever read (the same hide-the-fatal-error bug found
    in RenderProcess earlier). UxPlay listens on all interfaces; clients
    can only reach it over the P2P group's network anyway.
  * -pin takes UxPlay's own short on-screen pin scheme, not an 8-digit
    WPS PIN; joining the group's WPA2 network is already the access gate
    for a meeting room, so no -pin at all.

Still needs hardware validation: whether UxPlay's kmssink can take over
the DRM device from the idle screen when an AirPlay client connects (the
FSM's AIRPLAY_CONNECTED -> STOP_RENDER_PIPELINE handoff).
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UxPlayConfig:
    device_name: str


def build_uxplay_argv(config: UxPlayConfig) -> list[str]:
    return [
        "uxplay",
        "-n", config.device_name,
        "-nh",  # advertise exactly the room name, not "name@hostname"
        "-vs", "kmssink",
        "-as", "alsasink",
    ]


class UxPlayProcess:
    def __init__(self, config: UxPlayConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("uxplay is already running")
        argv = build_uxplay_argv(self.config)
        logger.info("starting uxplay: %s", " ".join(argv))
        # stdout/stderr inherit from castd (which runs under systemd) so
        # uxplay's own output lands in the journal -- piping it to an
        # unread buffer previously hid a fatal usage error entirely.
        self._proc = subprocess.Popen(argv)

    def stop(self, *, timeout: float = 3.0) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=timeout)
        self._proc = None
