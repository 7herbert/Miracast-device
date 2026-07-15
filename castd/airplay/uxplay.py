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

Client connect/disconnect detection works by parsing uxplay's own log
output (it offers no other signaling): an iPhone that connected while the
idle screen still held the DRM device got "cannot connect" (2026-07-14),
because UxPlay's kmssink could not become DRM master. The FSM's
AIRPLAY_CONNECTED -> STOP_RENDER_PIPELINE handoff needs to fire BEFORE
UxPlay builds its video pipeline, which is why connection detection keys
on the earliest reliable line (socket accept) rather than on mirroring
start. Every uxplay line is logged verbatim so the patterns can be
refined against real transcripts.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

_ACCEPT_RE = re.compile(r"Accepted .*client on socket", re.IGNORECASE)
_CLOSE_RE = re.compile(r"Connection closed|raop_rtp_mirror stopping", re.IGNORECASE)
_SERVER_STOP_RE = re.compile(r"Stopping RAOP Server", re.IGNORECASE)


class UxPlayClientTracker:
    """Coarse client-session tracking from uxplay's log lines. AirPlay
    opens several TCP connections per session, so this counts accepts and
    closes and only reports the 0->1 and 1->0 edges."""

    def __init__(self) -> None:
        self._open = 0

    def feed(self, line: str) -> str | None:
        if _SERVER_STOP_RE.search(line):
            had_clients = self._open > 0
            self._open = 0
            return "disconnected" if had_clients else None
        if _ACCEPT_RE.search(line):
            self._open += 1
            if self._open == 1:
                return "connected"
            return None
        if _CLOSE_RE.search(line) and self._open > 0:
            self._open -= 1
            if self._open == 0:
                return "disconnected"
        return None


@dataclass(frozen=True)
class UxPlayConfig:
    device_name: str


def build_uxplay_argv(config: UxPlayConfig) -> list[str]:
    # stdbuf -oL is load-bearing, not cosmetic: with stdout going to a
    # pipe, uxplay's stdio switches to block buffering and its log lines
    # sit unflushed inside uxplay until the process EXITS -- observed
    # live (2026-07-14) as the whole startup banner appearing in our
    # journal 14 seconds late, all at once, at shutdown. Client-connect
    # detection (and therefore the DRM handoff) only works if lines
    # arrive as they are printed.
    return [
        "stdbuf", "-oL", "-eL",
        "uxplay",
        "-n", config.device_name,
        "-nh",  # advertise exactly the room name, not "name@hostname"
        # Fixed legacy ports instead of the default random ones: castd
        # restarts uxplay around every Miracast session, and with random
        # ports an iPhone whose Screen Mirroring list cached the previous
        # advertisement dials a now-closed port and reports it cannot
        # connect (2026-07-14: taps produced zero uxplay log lines -- the
        # TCP connection never reached the new instance's sockets).
        "-p",
        # driver-name=vc4 for the same reason castd's own pipelines carry
        # it: the Pi 4 exposes two DRM devices (v3d render-only + vc4
        # display) and a bare kmssink can open the wrong one. A real
        # iPhone session got all the way to "Begin streaming to GStreamer
        # video pipeline" and then died with "kmssink_h264 ... general
        # resource error" for exactly this (2026-07-14).
        "-vs", "kmssink driver-name=vc4",
        "-as", "alsasink",
        # GPU decode + GPU conversion (-v4l2 = -vd v4l2h264dec -vc
        # v4l2convert), the same hardware path castd's Miracast pipeline
        # uses. Without it uxplay's default decodebin rejected the
        # iPhone's portrait 498x1080 stream into avdec_h264 (software)
        # feeding a double CPU videoconvert to RGB -- visibly stuttery
        # video on a real iPhone (2026-07-15). -bt709 is the colorimetry
        # flag UxPlay documents as needed on Raspberry Pi with v4l2.
        "-v4l2",
        "-bt709",
    ]


class UxPlayProcess:
    def __init__(
        self,
        config: UxPlayConfig,
        *,
        on_client_connected: Callable[[], None] | None = None,
        on_client_disconnected: Callable[[], None] | None = None,
        on_process_ended: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self._on_client_connected = on_client_connected
        self._on_client_disconnected = on_client_disconnected
        self._on_process_ended = on_process_ended
        self._proc: subprocess.Popen | None = None
        self._expect_exit = False

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("uxplay is already running")
        argv = build_uxplay_argv(self.config)
        logger.info("starting uxplay: %s", " ".join(argv))
        self._expect_exit = False
        # stdout is piped, but UNLIKE the earlier bug (a pipe nothing
        # read, which hid a fatal usage error) a dedicated thread relays
        # every line into our own log AND feeds the client tracker.
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=self._pump_output, args=(self._proc,), daemon=True).start()

    def _pump_output(self, proc: subprocess.Popen) -> None:
        tracker = UxPlayClientTracker()
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            logger.info("uxplay: %s", line)
            event = tracker.feed(line)
            if event == "connected" and self._on_client_connected is not None:
                self._on_client_connected()
            elif event == "disconnected" and self._on_client_disconnected is not None:
                self._on_client_disconnected()
        logger.info("uxplay output stream ended")
        # A real iPhone session crashed uxplay mid-stream (2026-07-14,
        # the un-fixed kmssink resource error): nothing noticed, the FSM
        # stayed in AIRPLAY, and the room was left with a black screen.
        # Only fire for exits castd did NOT ask for -- stop() sets
        # _expect_exit before terminating.
        if not self._expect_exit and self._on_process_ended is not None:
            self._on_process_ended()

    def stop(self, *, timeout: float = 3.0) -> None:
        if self._proc is None:
            return
        self._expect_exit = True
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=timeout)
        self._proc = None
