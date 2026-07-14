import pytest

from castd.wfdsink.rtsp import NegotiationError, WfdCapabilities, WfdNegotiator


def make_negotiator(**kwargs):
    return WfdNegotiator(WfdCapabilities(**kwargs))


def test_m1_response_echoes_cseq_and_advances_state():
    neg = make_negotiator()
    response = neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\nRequire: org.wfa.wfd1.0\r\n\r\n")
    assert "CSeq: 1" in response
    assert "200 OK" in response
    assert "Public: org.wfa.wfd1.0" in response
    assert neg.state == "WAIT_M3"


def test_m1_out_of_order_raises():
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    with pytest.raises(NegotiationError):
        neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 2\r\n\r\n")


def test_m1_missing_cseq_raises():
    neg = make_negotiator()
    with pytest.raises(NegotiationError, match="CSeq"):
        neg.handle_m1_options("OPTIONS * RTSP/1.0\r\n\r\n")


def test_m2_request_has_incrementing_cseq():
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    m2 = neg.build_m2_options_request()
    assert "CSeq: 1" in m2
    assert "Require: org.wfa.wfd1.0" in m2


def test_m3_capability_body_includes_client_rtp_port():
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    response = neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    assert "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 1028 0 mode=play" in response
    assert "Content-Length:" in response
    assert neg.state == "WAIT_M4"


def test_m3_reports_content_length_matching_actual_body():
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    response = neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    header, _, body = response.partition("\r\n\r\n")
    length_lines = [line for line in header.split("\r\n") if line.startswith("Content-Length:")]
    declared_length = int(length_lines[0].split(":")[1])
    assert declared_length == len(body)


def test_m3_advertises_1080p60_native_resolution_when_allowed():
    # Native byte 0x40 = CEA table index 8 = 1920x1080p60; mask 0001FFFF
    # includes the 1080p60 bit. With native left at 00 (CEA index 0 =
    # 640x480) a real Windows 11 source streamed 1024x768 to a 1080p TV
    # (observed 2026-07-14).
    neg = make_negotiator(allow_1080p60=True)
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    response = neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    assert "wfd_video_formats: 40 00 02 10 0001FFFF" in response


def test_m3_advertises_only_16_9_cea_modes():
    # VESA and HH mode masks must stay zero: with VESA open, a real
    # Windows 11 source in extend mode picked a non-16:9 mode for the
    # virtual display and the picture cropped at the panel edges
    # (2026-07-14). Every CEA mode is 16:9.
    neg = make_negotiator(allow_1080p60=True)
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    response = neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    assert "0001FFFF 00000000 00000000" in response


def test_m3_default_is_1080p30_native_without_1080p50_60_modes():
    # 1080p60 froze the Pi 4's v4l2 decode path after the first frames
    # (2026-07-14); the default advertises 1080p30 native (0x38 = CEA
    # index 7) and a mask with no 1080p50/p60/interlaced bits.
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    response = neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    assert "wfd_video_formats: 38 00 02 10 00019CFF" in response
    mask = 0x00019CFF
    assert not mask & (1 << 8)  # 1080p60
    assert not mask & (1 << 13)  # 1080p50
    assert not mask & (1 << 9) and not mask & (1 << 14)  # interlaced 1080i
    assert mask & (1 << 7)  # 1080p30 stays offered


def test_m3_includes_intel_fields_only_when_requested():
    neg = make_negotiator(device_name="MR-3F-A")
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    request_without = "GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\nwfd_video_formats\r\n\r\n"
    response = neg.handle_m3_get_parameter(request_without)
    assert "intel_friendly_name" not in response

    neg2 = make_negotiator(device_name="MR-3F-A")
    neg2.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    request_with = "GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\nintel_friendly_name\r\n\r\n"
    response2 = neg2.handle_m3_get_parameter(request_with)
    assert "intel_friendly_name: MR-3F-A" in response2


def test_m4_acks_and_extracts_uibc_port_when_present():
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    m4_request = (
        "SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 3\r\n"
        "Content-Type: text/parameters\r\nContent-Length: 90\r\n\r\n"
        "wfd_uibc_capability: input_category_list=HIDC;hidc_cap_list=Keyboard/USB;port=7336\r\n\r\n"
    )
    response = neg.handle_m4_set_parameter(m4_request)
    assert "CSeq: 3" in response
    assert neg.session.uibc_port == "7336"
    assert neg.session.use_hidc is True
    assert neg.state == "WAIT_M5"


def test_m4_uibc_port_none_does_not_enable_hidc():
    neg = make_negotiator()
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    m4_request = (
        "SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 3\r\n\r\n"
        "wfd_uibc_capability: input_category_list=HIDC;port=none\r\n\r\n"
    )
    neg.handle_m4_set_parameter(m4_request)
    assert neg.session.use_hidc is False
    assert neg.session.uibc_port is None


def _advance_to_wait_m6_response(neg):
    neg.handle_m1_options("OPTIONS * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
    neg.handle_m3_get_parameter("GET_PARAMETER * RTSP/1.0\r\nCSeq: 2\r\n\r\n")
    neg.handle_m4_set_parameter("SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 3\r\n\r\n")
    neg.handle_m5_generic("SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 4\r\n\r\n")


def test_m6_setup_request_uses_sink_rtp_port():
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    req = neg.build_m6_setup_request("192.168.173.80")
    assert "client_port=1028" in req
    assert "SETUP rtsp://192.168.173.80/wfd1.0/streamid=0" in req


@pytest.mark.parametrize(
    "response,expected_port,expected_session",
    [
        (
            "RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 12345678\r\n"
            "Transport: RTP/AVP/UDP;unicast;client_port=1028;server_port=19000\r\n\r\n",
            19000,
            "12345678",
        ),
        # A short 4-digit port must not be mis-sliced (this is the exact bug
        # class d2.py's fixed serverport[12:17] slicing was exposed to).
        (
            "RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 999\r\n"
            "Transport: RTP/AVP/UDP;unicast;client_port=1028;server_port=8080\r\n\r\n",
            8080,
            "999",
        ),
        # A port range (seen in d2win10debug.py's real capture) is parsed
        # via the first number in the range.
        (
            "RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 42\r\n"
            "Transport: RTP/AVP/UDP;unicast;client_port=1028-1029;server_port=50000-50001\r\n\r\n",
            50000,
            "42",
        ),
    ],
)
def test_m6_response_parsing_is_robust_to_port_digit_count(response, expected_port, expected_session):
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    session = neg.parse_m6_response(response)
    assert session.server_port == expected_port
    assert session.session_id == expected_session
    assert neg.state == "WAIT_M7_RESPONSE"


def test_m6_response_missing_server_port_raises():
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    with pytest.raises(NegotiationError, match="server_port"):
        neg.parse_m6_response("RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 1\r\n\r\n")


def test_m7_requires_session_id_from_m6_first():
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    with pytest.raises(NegotiationError, match="before M6"):
        neg.build_m7_play_request("192.168.173.80")


def test_m7_and_confirm_streaming_transitions_to_streaming():
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    neg.parse_m6_response(
        "RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 12345678\r\n"
        "Transport: RTP/AVP/UDP;unicast;server_port=19000\r\n\r\n"
    )
    m7 = neg.build_m7_play_request("192.168.173.80")
    assert "Session: 12345678" in m7
    neg.confirm_streaming("RTSP/1.0 200 OK\r\nCSeq: 6\r\n\r\n")
    assert neg.state == "STREAMING"


def test_confirm_streaming_rejects_non_ok():
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    neg.parse_m6_response("RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 1\r\nserver_port=19000\r\n\r\n")
    neg.build_m7_play_request("192.168.173.80")
    with pytest.raises(NegotiationError, match="not acknowledged"):
        neg.confirm_streaming("RTSP/1.0 454 Session Not Found\r\nCSeq: 6\r\n\r\n")


def test_is_teardown_detects_trigger_and_empty_message():
    neg = make_negotiator()
    assert neg.is_teardown("")
    assert neg.is_teardown("SET_PARAMETER * RTSP/1.0\r\n\r\nwfd_trigger_method: TEARDOWN\r\n\r\n")
    assert not neg.is_teardown("SET_PARAMETER * RTSP/1.0\r\n\r\nwfd_idr_request\r\n\r\n")


def test_steady_state_ack_handles_bundled_messages():
    neg = WfdNegotiator(WfdCapabilities(), sink_rtp_port=1028)
    _advance_to_wait_m6_response(neg)
    neg.parse_m6_response("RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 1\r\nserver_port=19000\r\n\r\n")
    neg.build_m7_play_request("192.168.173.80")
    neg.confirm_streaming("RTSP/1.0 200 OK\r\nCSeq: 6\r\n\r\n")

    bundled = (
        "GET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 100\r\n\r\n"
        "SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0\r\nCSeq: 101\r\n\r\n"
    )
    acks = neg.build_steady_state_ack(bundled)
    assert len(acks) == 2
    assert "CSeq: 100" in acks[0]
    assert "CSeq: 101" in acks[1]
