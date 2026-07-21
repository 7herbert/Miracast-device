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

# 200ms: enough to absorb normal decode/schedule jitter without ever
# letting the video branch drift seconds behind live -- see the
# build_wfd_pipeline_description docstring for why this exists.
VIDEO_QUEUE_MAX_TIME_NS = 200_000_000


@dataclass(frozen=True)
class RenderTarget:
    driver_name: str = "vc4"  # the Raspberry Pi 4's DRM/KMS driver
    connector_id: int | None = None  # None = let kmssink auto-pick the connected output
    # Output size the stream is hardware-scaled to (v4l2convert / ISP).
    # Forcing this means any negotiated source resolution fills the
    # display instead of sitting small in a corner of the DRM plane.
    width: int = 1920
    height: int = 1080


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
    # latency=100, NO drop-on-latency: the drop-on-latency=true latency=50
    # combination (tried for cursor lag) shredded the H.264 stream --
    # every dropped TS packet corrupts the frame chain until the next
    # IDR, and real Windows mirroring played back at a frame rate too low
    # for video (2026-07-15). 100 ms of buffer on a one-hop P2P link is
    # imperceptible next to the encode+decode latency; intact frames are
    # not.
    #
    # Audio branch: leaky queue + alsasink sync=false so it can NEVER
    # backpressure the video. alsasink's default sync=true paces to the
    # pipeline clock; when it falls behind, its queue fills, tsdemux
    # blocks on the audio pad, and the video branch starves -- the other
    # half of the same low-frame-rate symptom.
    #
    # Video queue is bounded + leaky too, for a lesson learned the hard
    # way (2026-07-15): a plain `queue` defaults to max-size-buffers=200 /
    # max-size-bytes=10MB / max-size-time=1s and does NOT drop -- it just
    # blocks once full. With both sinks sync=false (nothing paces
    # playback to a clock), the only thing standing between "decode is
    # ever so slightly slower than arrival" and unbounded backlog growth
    # was that 1 s ceiling, and once background threads on a Pi 4 (WPS
    # PIN repaints, watchdog polling, dnsmasq) stole a few ms here and
    # there over a multi-minute session, compressed frames piled up
    # toward that ceiling and stayed there -- a real session was measured
    # 5 s behind live while the (independently leaky) audio branch never
    # drifted. VIDEO_QUEUE_MAX_TIME_NS caps the same way the audio queue
    # already does: old undecoded frames get dropped to catch back up
    # instead of accumulating, at the cost of an occasional glitch until
    # the next IDR -- the same freshness-over-completeness trade already
    # made for the jitter buffer and the audio branch.
    return (
        f"udpsrc port={udp_port} "
        f"! application/x-rtp,media=video,encoding-name=MP2T,payload=33,clock-rate=90000 "
        f"! rtpjitterbuffer latency=100 "
        f"! rtpmp2tdepay "
        # tsdemux's latency property DEFAULTS TO 700 MS -- a deliberate
        # smooth-demuxing buffer paced by the TS PCR clock. Both sinks
        # here run sync=false (frames render the moment they're decoded),
        # so that buffer bought nothing and was the bulk of the ~1 s
        # glass-to-glass lag measured against real Windows mirroring
        # (2026-07-15).
        f"! tsdemux name=demux latency=50 "
        f"demux. ! queue max-size-buffers=0 max-size-bytes=0 "
        f"max-size-time={VIDEO_QUEUE_MAX_TIME_NS} leaky=downstream ! h264parse "
        f"! capssetter join=true replace=false caps=video/x-h264,profile=(string)high "
        f"! v4l2h264dec ! v4l2convert "
        f"! video/x-raw,width={target.width},height={target.height},pixel-aspect-ratio=1/1 "
        f"! kmssink driver-name={target.driver_name}{connector} sync=false "
        f"demux. ! queue leaky=downstream ! aacparse ! avdec_aac ! audioconvert ! audioresample "
        f"! alsasink sync=false"
    )


# NOTE: the idle screen deliberately does NOT go through this module any
# more. An idle kmssink pipeline holds DRM master and starves UxPlay's
# startup-time kmssink of it (2026-07-15) -- the idle image is painted
# via the framebuffer instead (render/framebuffer.py), leaving this
# module to the one true DRM client at a time: the Miracast stream.


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
