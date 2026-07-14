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


def test_idle_pipeline_renders_png_to_kms():
    desc = build_idle_screen_pipeline(png_path="/opt/castd/idle_screen.png", target=RenderTarget())
    assert desc.startswith("filesrc location=/opt/castd/idle_screen.png")
    assert "kmssink driver-name=vc4" in desc
