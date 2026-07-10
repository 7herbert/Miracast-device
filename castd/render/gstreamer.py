"""GStreamer render pipeline: replaces VLC/X11 entirely.

Hardware-dependent module (needs gstreamer1.0-plugins-{base,good,bad},
gstreamer1.0-libav or the Pi's v4l2 h264 decoder plugin, and a KMS-capable
DRM device node). Not runnable on this Windows dev box; verified here only
by syntax check (py_compile) and by testing the pure pipeline-string builder
functions, which have no GStreamer dependency at all.

Why kmssink instead of the old VLC/X11 stack: it writes directly to
/dev/dri/cardN, so there is no X server, no `su - pi` user switch, no
fighting over an X11 DISPLAY between the idle-screen player and the
streaming player (project retrospective items #12-#14, #18). One process,
one output, whichever protocol's session owns it at the time.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderTarget:
    drm_device: str = "/dev/dri/card0"
    connector_id: int | None = None  # None = let kmssink auto-pick the connected output


def build_wfd_pipeline_description(*, udp_port: int, target: RenderTarget) -> str:
    """gst-launch-1.0 style pipeline string for a Miracast/WFD MPEG-TS/H.264
    stream arriving over RTP on `udp_port`. v4l2h264dec uses the Pi 4's
    hardware decoder; kmssink writes straight to the DRM plane."""
    connector = f" connector-id={target.connector_id}" if target.connector_id is not None else ""
    return (
        f"udpsrc port={udp_port} "
        f"! application/x-rtp,media=video,encoding-name=MP2T,payload=33 "
        f"! rtpjitterbuffer latency=200 "
        f"! rtpmp2tdepay "
        f"! tsdemux name=demux "
        f"demux. ! queue ! h264parse ! v4l2h264dec ! kmssink device={target.drm_device}{connector} sync=false "
        f"demux. ! queue ! aacparse ! avdec_aac ! audioconvert ! alsasink"
    )


def build_idle_screen_pipeline(*, png_path: str, target: RenderTarget) -> str:
    """Static idle screen (room name / PIN / QR code) via GStreamer instead
    of fbi, so the idle path and the streaming path share one process model
    and one place to reason about DRM master ownership handoff."""
    connector = f" connector-id={target.connector_id}" if target.connector_id is not None else ""
    return (
        f"filesrc location={png_path} "
        f"! decodebin ! imagefreeze "
        f"! kmssink device={target.drm_device}{connector} sync=false"
    )


class RenderProcess:
    """Owns the single running gst-launch-1.0 child process. Only one may
    run at a time (idle screen XOR active stream) -- enforced by main.py's
    FSM, not by this class, so this class stays a dumb process wrapper."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, pipeline_description: str) -> None:
        if self.is_running:
            raise RuntimeError("a render pipeline is already running; stop() it first")
        argv = ["gst-launch-1.0", "-e"] + pipeline_description.split()
        logger.info("starting render pipeline: %s", pipeline_description)
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self, *, timeout: float = 3.0) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("render pipeline did not exit after SIGTERM, killing")
            self._proc.kill()
            self._proc.wait(timeout=timeout)
        self._proc = None
