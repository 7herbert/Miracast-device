"""End-to-end M1-M7 handshake test using socket.socketpair() to stand in for
a real TCP connection to a Windows Miracast source -- no network, no
hardware, runs anywhere Python runs.

The scripted "fake Windows source" thread below sends request bodies
modeled directly on the real capture recorded in lazycast's
d2win10debug.py (an actual Wireshark-derived Windows 10 M3 GET_PARAMETER
body, including the `microsoft_*`/`intel_*` vendor extensions and the
`wfd2_*` fields real Windows sources use). This test proves the negotiator
in castd.wfdsink.rtsp/session correctly drives the full handshake to
STREAMING against that realistic shape. It does NOT prove a real Windows
machine will accept castd's responses over the air -- only a live device
test can prove that (see project plan Phase 0/1). What it does prove: the
state machine, regex parsing, and response construction are internally
consistent end-to-end, not just correct in isolated units.
"""
from __future__ import annotations

import socket
import threading

from castd.wfdsink.rtsp import STATE_STREAMING, WfdCapabilities, WfdNegotiator
from castd.wfdsink.session import RtspReader, listen_for_sources, negotiate, run_steady_state

SINK_IP = "192.168.173.1"
SOURCE_IP = "192.168.173.80"

# Modeled on the real Windows 10 M3 request body captured in
# lazycast/d2win10debug.py -- field names and vendor extensions a real
# source actually queries for.
REAL_WINDOWS_M3_QUERY_NAMES = (
    "wfd_video_formats\r\n"
    "wfd_audio_codecs\r\n"
    "wfd_uibc_capability\r\n"
    "wfd_idr_request_capability\r\n"
    "intel_friendly_name\r\n"
    "intel_sink_manufacturer_name\r\n"
    "intel_sink_model_name\r\n"
    "intel_sink_version\r\n"
    "intel_sink_device_URL\r\n"
)

REAL_WINDOWS_M4_BODY = (
    "wfd_video_formats: 00 00 02 10 00 001FEFF 3FFFFFFF 00000FFF 00 0000 0000 00 none none\r\n"
    "wfd_audio_codecs: LPCM 00000002 00\r\n"
    "wfd_uibc_capability: input_category_list=HIDC;"
    "hidc_cap_list=Keyboard/USB, Mouse/USB, MultiTouch/USB, Gesture/USB, RemoteControl/USB, Joystick/USB;"
    "port=none\r\n"
)


def _paired_sockets() -> tuple[socket.socket, socket.socket]:
    """socketpair with a hard 10s timeout on both ends: on this project's
    Windows dev box the loopback emulation intermittently drops a message,
    and without timeouts that turns the whole test run into a silent hang
    instead of one failing test with a traceback."""
    a, b = socket.socketpair()
    a.settimeout(10)
    b.settimeout(10)
    return a, b


def _rtsp(method_line: str, cseq: int, body: str = "") -> bytes:
    headers = f"{method_line}\r\nCSeq: {cseq}\r\n"
    if body:
        headers += f"Content-Type: text/parameters\r\nContent-Length: {len(body)}\r\n"
    return (headers + "\r\n" + body).encode()


def fake_windows_source(sock: socket.socket, errors: list[Exception]) -> None:
    # The fake reads through RtspReader for the same reason production
    # does: the sink legitimately sends message pairs back-to-back (M1
    # response + M2 request, M5 response + M6 request) and TCP may deliver
    # each pair coalesced into one segment or split -- raw one-recv-per-
    # message reads made this test hang on whichever race lost.
    reader = RtspReader(sock)
    try:
        # M1: source asks the sink what methods it supports.
        sock.sendall(_rtsp("OPTIONS * RTSP/1.0", 1))
        m1_response = reader.next_message()
        assert "200 OK" in m1_response
        assert "Public: org.wfa.wfd1.0" in m1_response

        # M2: sink probes the source back; source just acks.
        m2_request = reader.next_message()
        assert m2_request.startswith("OPTIONS * RTSP/1.0")
        m2_cseq = m2_request.split("CSeq:")[1].split("\r")[0].strip()
        sock.sendall(f"RTSP/1.0 200 OK\r\nCSeq: {m2_cseq}\r\n\r\n".encode())

        # M3: source queries sink capabilities using real Windows field names.
        sock.sendall(_rtsp("GET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0", 2, REAL_WINDOWS_M3_QUERY_NAMES))
        m3_response = reader.next_message()
        assert "wfd_client_rtp_ports: RTP/AVP/UDP;unicast 1028" in m3_response
        assert "intel_friendly_name: MR-3F-A" in m3_response

        # M4: source pushes chosen params.
        sock.sendall(_rtsp("SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0", 3, REAL_WINDOWS_M4_BODY))
        m4_response = reader.next_message()
        assert "200 OK" in m4_response

        # M5: a second ack-only SET_PARAMETER (matches real capture behavior).
        sock.sendall(_rtsp("SET_PARAMETER rtsp://x/wfd1.0 RTSP/1.0", 4))
        m5_response = reader.next_message()
        assert "200 OK" in m5_response

        # M6: sink sends SETUP; source assigns a real 5-digit ephemeral port.
        m6_request = reader.next_message()
        assert m6_request.startswith(f"SETUP rtsp://{SOURCE_IP}/wfd1.0/streamid=0")
        assert "client_port=1028" in m6_request
        sock.sendall(
            (
                "RTSP/1.0 200 OK\r\nCSeq: 5\r\nSession: 8734659201\r\n"
                "Transport: RTP/AVP/UDP;unicast;client_port=1028;server_port=48753\r\n\r\n"
            ).encode()
        )

        # M7: sink sends PLAY with the session id we just handed it.
        m7_request = reader.next_message()
        assert m7_request.startswith(f"PLAY rtsp://{SOURCE_IP}/wfd1.0/streamid=0")
        assert "Session: 8734659201" in m7_request
        sock.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 6\r\n\r\n")
    except Exception as exc:  # surfaced on the main thread via the errors list
        errors.append(exc)


def test_full_handshake_against_realistic_windows_source():
    sink_sock, source_sock = _paired_sockets()
    errors: list[Exception] = []
    source_thread = threading.Thread(target=fake_windows_source, args=(source_sock, errors))
    source_thread.start()

    session = negotiate(
        sink_sock,
        source_ip=SOURCE_IP,
        capabilities=WfdCapabilities(device_name="MR-3F-A"),
    )

    source_thread.join(timeout=5)
    assert not source_thread.is_alive(), "fake source thread did not finish in time"
    assert not errors, f"fake source thread raised: {errors}"

    assert session.server_port == 48753
    assert session.session_id == "8734659201"

    sink_sock.close()
    source_sock.close()


def _streaming_negotiator() -> WfdNegotiator:
    neg = WfdNegotiator(WfdCapabilities(device_name="MR-3F-A"))
    neg.state = STATE_STREAMING
    return neg


def test_steady_state_acks_keepalive_and_exits_when_source_closes():
    # The M16 keep-alive loop after M7. Real-hardware lesson (2026-07-14):
    # without this loop the sink-side socket got GC-closed 82 ms after
    # PLAY and Windows tore the whole session down within half a second.
    sink_sock, source_sock = _paired_sockets()
    neg = _streaming_negotiator()
    # keepalive_timeout doubles as the hang bound for the pump thread
    pump = threading.Thread(target=run_steady_state, args=(sink_sock, neg), kwargs={"keepalive_timeout": 10})
    pump.start()

    source_sock.sendall(b"GET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0\r\nCSeq: 10\r\n\r\n")
    ack = source_sock.recv(4096).decode()
    assert "200 OK" in ack
    assert "CSeq: 10" in ack

    source_sock.close()  # source ends the session
    pump.join(timeout=5)
    assert not pump.is_alive()
    sink_sock.close()


def test_steady_state_acks_then_exits_on_teardown_trigger():
    sink_sock, source_sock = _paired_sockets()
    neg = _streaming_negotiator()
    pump = threading.Thread(target=run_steady_state, args=(sink_sock, neg), kwargs={"keepalive_timeout": 10})
    pump.start()

    # A real teardown trigger is a proper SET_PARAMETER with Content-Length
    # (RTSP requires it for bodied messages, and real Windows sends it) --
    # the reader frames the body by that header.
    source_sock.sendall(_rtsp("SET_PARAMETER rtsp://localhost/wfd1.0 RTSP/1.0", 11, "wfd_trigger_method: TEARDOWN\r\n"))
    ack = source_sock.recv(4096).decode()
    assert "200 OK" in ack

    pump.join(timeout=5)
    assert not pump.is_alive()
    sink_sock.close()
    source_sock.close()


def test_sink_listener_accepts_an_inbound_source_connection():
    # Direction check: the SOURCE dials the SINK (per the WFD spec and the
    # 7236 port advertised in our WFD IE), so the sink side must be a
    # listener. port=0 lets the OS pick a free port so this runs anywhere.
    listener = listen_for_sources("127.0.0.1", port=0)
    try:
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn, (peer_ip, _peer_port) = listener.accept()
        assert peer_ip == "127.0.0.1"
        client.close()
        conn.close()
    finally:
        listener.close()
