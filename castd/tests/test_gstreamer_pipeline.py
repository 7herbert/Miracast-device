"""Tests for the pure pipeline-string builders in castd.render.gstreamer.
No GStreamer required -- these lock in wire-format details that real
hardware proved fatal when wrong."""
from castd.render.gstreamer import RenderTarget, build_idle_screen_pipeline, build_wfd_pipeline_description


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


def test_wfd_pipeline_uses_a_short_p2p_appropriate_jitter_buffer():
    # One-hop P2P link: 200ms of jitter buffer read as visible mouse lag
    # on real hardware; stale packets are dropped, not played late.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "rtpjitterbuffer latency=50 drop-on-latency=true" in desc


def test_wfd_pipeline_rewrites_constrained_high_profile_for_the_decoder():
    # Real-hardware failure (2026-07-14): Windows streams H.264
    # constrained-high, which the bcm2835 V4L2 decoder's profile menu
    # does not list (baseline/constrained-baseline/main/high only), so
    # the caps intersection is empty and the decoder refuses the stream.
    # capssetter overrides the profile field to "high" (a strict
    # superset, lossless to decode as) before the decoder sees it.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "h264parse ! capssetter join=true replace=false caps=video/x-h264,profile=(string)high ! v4l2h264dec" in desc


def test_wfd_pipeline_audio_branch_can_resample_for_alsa():
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert "audioconvert ! audioresample ! alsasink" in desc


def test_idle_pipeline_renders_png_to_kms():
    desc = build_idle_screen_pipeline(png_path="/opt/castd/idle_screen.png", target=RenderTarget())
    assert desc.startswith("filesrc location=/opt/castd/idle_screen.png")
    assert "kmssink driver-name=vc4" in desc
