"""Tests for the pure pipeline-string builders in castd.render.gstreamer.
No GStreamer required -- these lock in wire-format details that real
hardware proved fatal when wrong."""
from castd.render.gstreamer import RenderTarget, build_wfd_pipeline_description, wfd_variant_params


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


def test_default_variant_is_production_pipeline():
    # An unset/unknown CASTD_WFD_VARIANT must never change the shipped
    # pipeline -- a typo in an env override cannot be allowed to wedge the
    # receiver, so it falls back to exactly the production string.
    prod = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget())
    assert wfd_variant_params("default") == {}
    assert wfd_variant_params("nonsense-typo") == {}
    assert build_wfd_pipeline_description(udp_port=1028, target=RenderTarget(), **wfd_variant_params("default")) == prod


def test_qcap_variant_bounds_the_video_queue_without_leaking():
    # Diagnostic (2026-07-22): does the plain video queue silently hold the
    # fixed ~5s? Bound it -- but non-leaky, so it can only block, never drop
    # an SPS/PPS buffer (the trap the reverted leaky experiment fell into).
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget(), **wfd_variant_params("qcap"))
    assert "demux. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 ! h264parse" in desc
    # The video branch (between the two demux. tees) must carry no leaky=;
    # only the audio branch (the last tee) is allowed to be leaky.
    video_branch = desc.split("demux.")[1]
    assert "leaky" not in video_branch
    assert "v4l2h264dec ! v4l2convert" in desc  # decoder path untouched by this variant


def test_swdec_variant_swaps_hardware_decode_for_software():
    # Diagnostic (2026-07-22): if the fixed ~5s survives software decode,
    # the V4L2 hardware decoder/convert was not the element holding it.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget(), **wfd_variant_params("swdec"))
    assert "avdec_h264 ! videoconvert ! videoscale" in desc
    assert "v4l2h264dec" not in desc
    assert "demux. ! queue ! h264parse" in desc  # queue path untouched by this variant


def test_swconv_variant_keeps_hardware_decode_but_software_converts():
    # Diagnostic (2026-07-22): qcap cleared the queue and full swdec was too
    # heavy to flow, so isolate the last two suspects -- keep hardware
    # v4l2h264dec, move only the format-convert to software. If the fixed
    # ~5s vanishes, v4l2convert held it; if it survives, the decoder does.
    desc = build_wfd_pipeline_description(udp_port=1028, target=RenderTarget(), **wfd_variant_params("swconv"))
    assert "v4l2h264dec ! videoconvert ! videoscale" in desc
    assert "v4l2convert" not in desc
    assert "demux. ! queue ! h264parse" in desc  # queue path untouched


def test_no_idle_pipeline_builder_exists():
    # The idle screen must never be a kmssink pipeline again: it holds DRM
    # master and starves UxPlay's startup-time kmssink (2026-07-15). It is
    # painted via render/framebuffer.py instead.
    import castd.render.gstreamer as gstreamer

    assert not hasattr(gstreamer, "build_idle_screen_pipeline")
