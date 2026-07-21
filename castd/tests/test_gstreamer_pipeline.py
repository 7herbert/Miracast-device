"""Tests for the pure pipeline-string builders in castd.render.gstreamer.
No GStreamer required -- these lock in wire-format details that real
hardware proved fatal when wrong."""
from castd.render.gstreamer import RenderTarget, build_wfd_pipeline_description


def test_wfd_pipeline_rtp_caps_are_fully_fixed():
    # Real-hardware failure (2026-07-14): without clock-rate the RTP caps
    # stay unfixed and gst-launch dies at preroll with "Filter caps do not
    # completely specify the output format". 90000 Hz is RFC 3551's fixed
    # clock for MPEG-TS payload 33.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "clock-rate=90000" in desc
    assert "payload=33" in desc
    assert "encoding-name=MP2T" in desc


def test_wfd_pipeline_decodes_aac_to_match_advertised_capability():
    # main.py advertises audio_codec="AAC" in the M3 capability response
    # precisely because this branch decodes AAC; if this assertion breaks,
    # re-align both sides or Windows will ship audio the pipeline kills
    # itself on.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "aacparse" in desc
    assert "avdec_aac" in desc


def test_wfd_pipeline_uses_pi4_hardware_decode_and_kms():
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "v4l2h264dec" in desc
    assert "kmssink driver-name=vc4" in desc


def test_wfd_pipeline_bridges_decoder_to_kms_via_hardware_convert():
    # Real-hardware failure (2026-07-14): with RTP flowing, direct
    # v4l2h264dec ! kmssink died "not-negotiated (-4)" -- the DRM plane
    # kmssink picks need not accept the decoder's YUV output. v4l2convert
    # is the Pi's zero-CPU ISP bridge between them, and scaling to the
    # display size there means any source resolution fills the screen.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    # pixel-aspect-ratio pinned to 1/1: without it a non-16:9 source mode
    # gets aspect-compensated during scaling and crops at the display
    # edges instead of stretching uniformly.
    assert "v4l2h264dec ! v4l2convert ! video/x-raw,width=1920,height=1080,pixel-aspect-ratio=1/1 ! kmssink" in desc


def test_wfd_pipeline_jitter_buffer_never_drops_packets():
    # drop-on-latency=true shredded the H.264 stream: one dropped TS
    # packet corrupts everything until the next IDR, and real Windows
    # mirroring played at a frame rate too low for video (2026-07-15).
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "rtpjitterbuffer latency=100" in desc
    assert "drop-on-latency" not in desc


def test_wfd_pipeline_jitter_buffer_uses_default_mode():
    # mode=0 was tried against a real ~5s lag report (2026-07-15) but only
    # ever tested stacked with the video-queue change below, which itself
    # correlated with a connection regression -- no clean verdict on mode
    # either way, so it stays at its default (mode=slave) until it can be
    # retried in isolation. See build_wfd_pipeline_description's docstring.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "rtpjitterbuffer latency=100 " in desc
    assert "mode=0" not in desc


def test_wfd_pipeline_overrides_tsdemux_700ms_default_latency():
    # tsdemux defaults to a 700 ms smooth-demuxing buffer -- the bulk of
    # a measured ~1 s glass-to-glass lag (2026-07-15). Useless here: both
    # sinks are sync=false so nothing is paced by timestamps anyway.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "tsdemux name=demux latency=50" in desc


def test_wfd_pipeline_rewrites_constrained_high_profile_for_the_decoder():
    # Real-hardware failure (2026-07-14): Windows streams H.264
    # constrained-high, which the bcm2835 V4L2 decoder's profile menu
    # does not list (baseline/constrained-baseline/main/high only), so
    # the caps intersection is empty and the decoder refuses the stream.
    # capssetter overrides the profile field to "high" (a strict
    # superset, lossless to decode as) before the decoder sees it.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "capssetter join=true replace=false caps=video/x-h264,profile=(string)high ! v4l2h264dec" in desc


def test_wfd_pipeline_video_queue_is_plain_not_leaky():
    # A bounded+leaky video queue was tried against a real ~5s lag report
    # (2026-07-15) and correlated with a connection regression on the very
    # next hardware test: leaky dropping in the compressed domain is at
    # the mercy of WHERE it lands, and dropping the wrong buffer (e.g. one
    # carrying SPS/PPS while h264parse is still locking onto the stream
    # at connection start) can mean parsing never recovers and the
    # decoder never gets valid caps. Reverted to plain (unbounded,
    # non-leaky) pending root-causing the lag a different way -- see
    # build_wfd_pipeline_description's docstring.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "demux. ! queue ! h264parse" in desc
    assert "leaky=downstream ! h264parse" not in desc


def test_wfd_pipeline_audio_branch_cannot_backpressure_video():
    # alsasink's default sync=true paces to the pipeline clock; when it
    # falls behind, its queue fills, tsdemux blocks, and the VIDEO branch
    # starves. Leaky queue + sync=false make audio strictly best-effort.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "queue leaky=downstream ! aacparse" in desc
    assert "audioconvert ! audioresample ! alsasink sync=false" in desc


def test_no_idle_pipeline_builder_exists():
    # The idle screen must never be a kmssink pipeline again: it holds DRM
    # master and starves UxPlay's startup-time kmssink (2026-07-15). It is
    # painted via render/framebuffer.py instead.
    import castd.render.gstreamer as gstreamer

    assert not hasattr(gstreamer, "build_idle_screen_pipeline")
