"""GStreamer render pipeline: replaces VLC/X11 entirely.

Hardware-dependent module (needs gstreamer1.0-plugins-{base,good,bad},
gstreamer1.0-libav or the Pi's v4l2 h264 decoder plugin, and a KMS-capable
DRM device node). Not runnable on this Windows dev box; verified here only
by syntax check (py_compile) and by testing the pure pipeline-string builder
functions, which have no GStreamer dependency at all.

Why kmssink instead of the old VLC/X11 stack: it writes directly to the
DRM/KMS plane, so there is no X server, no `su - pi` user switch, no
fighting over an X11 DISPLAY between the idle-screen player and the
streaming player (project retrospective items #12-#14, #18). One process,
one output, whichever protocol's session owns it at the time. Requires a
desktop environment/display manager (lightdm, etc.) to NOT be running --
real-hardware testing found kmssink cannot get DRM master while one owns
the display, and separately found this GStreamer build's kmssink has no
"device" property at all (a real bug in earlier code here): the actual
properties are driver-name/bus-id/connector-id/plane-id, confirmed via
`gst-inspect-1.0 kmssink` on the target hardware.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderTarget:
    driver_name: str = "vc4"  # the Raspberry Pi 4's DRM/KMS driver
    connector_id: int | None = None  # None = let kmssink auto-pick the connected output


def build_wfd_pipeline_description(*, udp_port: int, target: RenderTarget) -> str:
    """gst-launch-1.0 style pipeline string for a Miracast/WFD MPEG-TS/H.264
    stream arriving over RTP on `udp_port`. v4l2h264dec uses the Pi 4's
    hardware decoder; kmssink writes straight to the DRM plane.

    Two lessons this string encodes from real hardware (2026-07-14):
      * clock-rate=90000 is mandatory in the udpsrc caps -- RTP caps must
        be fully fixed before preroll, omitting it died with "Filter caps
        do not completely specify the output format". 90000 Hz is the
        fixed RTP clock for MPEG-TS (RFC 3551, payload 33).
      * v4l2convert between the decoder and kmssink -- with RTP actually
        flowing, direct v4l2h264dec ! kmssink died with "streaming
        stopped, reason not-negotiated (-4)": the decoder outputs YUV and
        the DRM plane kmssink picks need not accept it. v4l2convert is the
        Pi's hardware ISP path (zero CPU), the canonical Pi 4 bridge for
        exactly this pairing.
      * capssetter rewriting profile to "high" -- the Windows 11 source
        streams H.264 constrained-high (captured caps: profile=(string)
        constrained-high, level=4.2), but the bcm2835 V4L2 decoder's
        profile menu only lists baseline/constrained-baseline/main/high,
        so GStreamer's caps intersection with the parsed stream is EMPTY
        and the decoder sink refuses the caps -- the second face of the
        same not-negotiated error. Constrained-high is a strict subset
        of high, so decoding it as high is lossless and safe; capssetter
        (accept-anything sink template) breaks the doomed intersection
        and hands the decoder a profile string its driver does list."""
    connector = f" connector-id={target.connector_id}" if target.connector_id is not None else ""
    return (
        f"udpsrc port={udp_port} "
        f"! application/x-rtp,media=video,encoding-name=MP2T,payload=33,clock-rate=90000 "
        f"! rtpjitterbuffer latency=200 "
        f"! rtpmp2tdepay "
        f"! tsdemux name=demux "
        f"demux. ! queue ! h264parse "
        f"! capssetter join=true replace=false caps=video/x-h264,profile=(string)high "
        f"! v4l2h264dec ! v4l2convert "
        f"! kmssink driver-name={target.driver_name}{connector} sync=false "
        f"demux. ! queue ! aacparse ! avdec_aac ! audioconvert ! audioresample ! alsasink"
    )


def build_idle_screen_pipeline(*, png_path: str, target: RenderTarget) -> str:
    """Static idle screen (room name / PIN / QR code) via GStreamer instead
    of fbi, so the idle path and the streaming path share one process model
    and one place to reason about DRM master ownership handoff."""
    connector = f" connector-id={target.connector_id}" if target.connector_id is not None else ""
    return (
        f"filesrc location={png_path} "
        f"! decodebin ! imagefreeze "
        f"! kmssink driver-name={target.driver_name}{connector} sync=false"
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
        # stderr=subprocess.PIPE with nothing ever reading it was a real bug:
        # gst-launch-1.0 failing immediately (e.g. the "no property 'device'
        # in element kmssink" pipeline error found on real hardware) produced
        # a defunct/zombie process with its error text sitting unread in the
        # pipe -- invisible in journalctl, making a real, fatal pipeline
        # error look like silent, inexplicable failure. Let stderr inherit
        # from this process (itself running under systemd) so it lands in
        # the journal automatically.
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL)

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
