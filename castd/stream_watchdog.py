"""Stall detection for an active Miracast session.

The failure mode this catches was seen live (2026-07-15): the first
Windows session after boot froze on its first frame -- RTSP control
channel alive and keep-alives flowing, so nothing tore the session down,
and the room needed a manual reconnect. The stream itself is UDP with no
in-band liveness, but a dead stream is plainly visible as the group
interface's rx byte counter flatlining; a live mirror never drops below
~100 kbps even for a completely static desktop, since the source keeps
encoding frames.

Pure logic here (feed it byte counts and timestamps); main.py samples
/sys/class/net/<group-if>/statistics/rx_bytes and closes the session's
RTSP control socket when this trips, which funnels the teardown through
the exact same path as a normal source disconnect.
"""
from __future__ import annotations

from pathlib import Path


class StreamWatchdog:
    """Trips when consecutive samples show (almost) no new bytes for
    longer than the stall window. min_bytes_per_sample is deliberately
    far below any live mirror's floor and far above keep-alive noise."""

    def __init__(self, *, stall_window_s: float = 10.0, min_bytes_per_sample: int = 5000) -> None:
        self._stall_window_s = stall_window_s
        self._min_bytes = min_bytes_per_sample
        self._last_rx: int | None = None
        self._last_progress: float | None = None

    def reset(self) -> None:
        self._last_rx = None
        self._last_progress = None

    def observe(self, rx_bytes: int, now: float) -> bool:
        """Feed one sample; True means the stream has been stalled for
        longer than the window and the session should be torn down."""
        if self._last_rx is None:
            self._last_rx = rx_bytes
            self._last_progress = now
            return False
        if rx_bytes - self._last_rx >= self._min_bytes:
            self._last_progress = now
        self._last_rx = rx_bytes
        return (now - self._last_progress) > self._stall_window_s


def read_interface_rx_bytes(interface_name: str) -> int:
    return int(Path(f"/sys/class/net/{interface_name}/statistics/rx_bytes").read_text())
