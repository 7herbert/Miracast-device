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
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Opt-in latency diagnosis, scoped to ONLY this pipeline's subprocess.
# A 2026-07-15 attempt to trace where a ~5s lag came from set GST_DEBUG/
# GST_TRACERS via `systemctl edit castd` -- a systemd Environment= line
# applies to the whole unit, so it leaked into UxPlayProcess's subprocess
# too (also a GStreamer app) and its trace lines came back mixed in with
# the real target's, and separately that test's Miracast connection never
# actually completed, so no real data came out of it either. Setting
# CASTD_TRACE_RENDER_LATENCY=1 in castd's own environment is read here
# and translated into GST_DEBUG/GST_TRACERS ONLY on the copy of the
# environment handed to the WFD render subprocess -- castd's own os.environ
# is never touched, so UxPlayProcess (which inherits from os.environ same
# as always) sees nothing different.
_TRACE_ENV_FLAG = "CASTD_TRACE_RENDER_LATENCY"


@dataclass(frozen=True)
class RenderTarget:
    driver_name: str = "vc4"  # the Raspberry Pi 4's DRM/KMS driver
    connector_id: int | None = None  # None = let kmssink auto-pick the connected output
    # Output size the stream is hardware-scaled to (v4l2convert / ISP).
    # Forcing this means any negotiated source resolution fills the
    # display instead of sitting small in a corner of the DRM plane.
    width: int = 1920
    height: int = 1080


def build_wfd_pipeline_description(
    *,
    udp_port: int,
    target: RenderTarget,
    video_queue: str = "queue",
    video_decode: str = "v4l2h264dec ! v4l2convert",
) -> str:
    """gst-launch-1.0 style pipeline string for a Miracast/WFD MPEG-TS/H.264
    stream arriving over RTP on `udp_port`. v4l2h264dec uses the Pi 4's
    hardware decoder; kmssink writes straight to the DRM plane.

    video_queue / video_decode default to the production elements and are
    overridden only by the diagnostic variants in wfd_variant_params (see
    there) while localizing the video-branch-only ~5s lag (2026-07-22).

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
    # Two things TRIED AND REVERTED while chasing a ~5s lag report
    # (2026-07-15), left here so they are not tried again blind:
    #   - leaky=downstream on the VIDEO queue (bounding it the same way
    #     the audio queue is bounded, to stop backlog from accumulating
    #     unboundedly toward the plain queue's ~1s default ceiling).
    #     Correlated with a real connection regression -- worse than the
    #     unbounded-queue baseline -- on the very next hardware test.
    #     Suspected mechanism: leaky dropping is at the mercy of WHERE in
    #     the compressed byte stream it lands; drop the wrong buffer
    #     (e.g. one carrying SPS/PPS right as h264parse is still trying
    #     to lock onto the stream at connection start) and parsing never
    #     recovers, so capssetter/v4l2h264dec never get valid caps and
    #     the whole pipeline fails to negotiate -- the compressed-domain
    #     version of the same "dropping anything mid-stream is dangerous"
    #     lesson already learned from the RTP-level drop-on-latency
    #     experiment. The backlog-accumulation theory this was meant to
    #     fix was also never confirmed independently (the 5s number did
    #     not move when this queue was tried, for whatever that is worth).
    #   - rtpjitterbuffer mode=0 (below every other place mode= would
    #     show up if re-tried) -- untested in isolation, tested only
    #     stacked with the queue change above, so no clean verdict either
    #     way; worth retrying alone before assuming it caused anything.
    # The pipeline below is deliberately back to the last hardware-
    # verified-stable shape (plain, unbounded, non-leaky video queue;
    # default rtpjitterbuffer mode) while root-causing the lag separately.
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
        f"demux. ! {video_queue} ! h264parse "
        f"! capssetter join=true replace=false caps=video/x-h264,profile=(string)high "
        f"! {video_decode} "
        f"! video/x-raw,width={target.width},height={target.height},pixel-aspect-ratio=1/1 "
        f"! kmssink driver-name={target.driver_name}{connector} sync=false "
        f"demux. ! queue leaky=downstream ! aacparse ! avdec_aac ! audioconvert ! audioresample "
        f"! alsasink sync=false"
    )


# Diagnostic pipeline variants, selected by the CASTD_WFD_VARIANT env var
# (mapped in main.py). The 2026-07-22 latency picture: audio is instant
# while video lags a FIXED ~5s from the first frame, so the delay is not in
# the shared upstream (jitterbuffer/tsdemux -- those would delay audio too)
# but in one video-branch element after the tsdemux split. The GStreamer
# latency tracer can't measure it here (its per-buffer instrumentation
# starves this Pi's realtime decode path and freezes every session at frame
# one -- confirmed twice), so instead each variant swaps exactly ONE
# suspect element and a glass-to-glass A/B says which one holds the 5s.
# All variants are non-destructive and revert to production by unsetting
# the env var; the winning finding gets baked in as the default afterwards.
_WFD_VARIANTS: dict[str, dict[str, str]] = {
    # Production. Plain unbounded queue + Pi 4 hardware decode/convert.
    "default": {},
    # Bound the compressed video queue. Non-leaky: it only ever BLOCKS,
    # never drops, so it cannot lose an SPS/PPS buffer the way the reverted
    # leaky experiment could. If the fixed 5s collapses, the plain queue
    # was silently sitting full of a multi-second backlog (its 200-buffer
    # default ceiling is ~6s at 30fps whenever the time limit is inactive).
    "qcap": {"video_queue": "queue max-size-buffers=8 max-size-bytes=0 max-size-time=0"},
    # Software decode+convert instead of the V4L2 hardware path. 1080p30 in
    # software may drop frames on a Pi 4, but if the fixed 5s vanishes even
    # so, the hardware decoder (or the ISP convert) was imposing it.
    "swdec": {"video_decode": "avdec_h264 ! videoconvert ! videoscale"},
}


def wfd_variant_params(name: str) -> dict[str, str]:
    """Map a CASTD_WFD_VARIANT name to build_wfd_pipeline_description kwargs.
    Unknown names fall back to production so a typo in an env override can
    never wedge the receiver -- it just streams normally."""
    return _WFD_VARIANTS.get(name, {})


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
        env = None
        if os.environ.get(_TRACE_ENV_FLAG) == "1":
            env = dict(os.environ)
            env["GST_DEBUG"] = "GST_TRACER:7"
            env["GST_TRACERS"] = "latency(flags=pipeline+element)"
            logger.info("%s=1: tracing this render pipeline's latency", _TRACE_ENV_FLAG)
        self._proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, env=env)

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
