"""UxPlay subprocess manager.

Hardware-dependent (spawns the `uxplay` binary, which must be built with
GStreamer support and bound to the P2P group interface). Not runnable on
this dev box; syntax-checked only. Needs Phase 0 hardware validation:
whether avahi/mDNS actually reaches an Apple device that joined the P2P
group as a legacy WPA2 station (see project plan's "legacy STA join" risk),
and whether UxPlay's own GStreamer video sink can be pointed at the same
kmssink render target used for Miracast without contending for the DRM
device.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UxPlayConfig:
    device_name: str
    bind_interface: str  # the P2P group interface, e.g. "p2p-wlan1-0"
    wps_pin: str | None = None  # UxPlay's own "-pin" registration lock, distinct from Miracast WPS
    drm_device: str = "/dev/dri/card0"


def build_uxplay_argv(config: UxPlayConfig) -> list[str]:
    argv = [
        "uxplay",
        "-n", config.device_name,
        "-bindif", config.bind_interface,
        "-vs", "kmssink",
        "-as", "alsasink",
    ]
    if config.wps_pin:
        argv += ["-pin", config.wps_pin]
    return argv


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
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

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
