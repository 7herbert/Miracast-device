"""Sink-side RTSP negotiator for the Wi-Fi Display (Miracast) M1-M7 handshake.

This is a from-scratch rewrite that replaces lazycast's d2.py, but the wire
content it produces (message order, headers, capability strings) is
deliberately kept identical to d2.py -- that script is proven against real
Windows 10/11 Miracast sources in this project's own testing, and there is
no value in "improving" bytes that a real source already accepts. What is
new here:

  * All socket I/O is removed. Every method takes a request string and
    returns a response string (or vice versa), so the whole handshake is
    unit-testable without a network stack.
  * CSeq/Content-Length/Transport-header parsing uses regexes instead of
    d2.py's fixed-offset string slicing. d2.py's server_port extraction
    (`serverport[12:17]`) silently produces garbage if the port string is
    not exactly 5 digits -- a real latent bug carried over from the
    original project. This rewrite parses by field name and raises
    NegotiationError on anything unexpected instead of miscomputing a port
    number or raising an unhandled IndexError deep in a socket loop.
  * Explicit state tracking (`self.state`) instead of "whichever recv()
    call happens to run next" -- so a message arriving out of order is a
    detected error, not a silent misparse.

Bidirectional roles per the Wi-Fi Display spec: the source (Windows PC) is
the RTSP client for OPTIONS/GET_PARAMETER/SET_PARAMETER used in capability
negotiation (M1-M4), but the SINK (this code) is the RTSP client for
SETUP/PLAY/TEARDOWN (M6/M7) even though media flows sink-ward -- this
looks backwards compared to a normal client/server RTSP relationship, but
it is correct per spec and is exactly what d2.py implements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_CSEQ_RE = re.compile(r"CSeq:\s*(\d+)", re.IGNORECASE)
_SERVER_PORT_RE = re.compile(r"server_port=(\d+)(?:-(\d+))?")
_SESSION_RE = re.compile(r"Session:\s*([\w.]+)")
_UIBC_PORT_RE = re.compile(r"port=(\S+?)(?:;|\r|\n|$)")


class NegotiationError(Exception):
    """Raised for anything that does not match the expected WFD handshake
    shape -- an out-of-order message, a missing required header, or a
    malformed field. Callers should treat this as "tear down this attempt
    and go back to IDLE", not a crash."""


def _extract_cseq(message: str) -> str:
    m = _CSEQ_RE.search(message)
    if not m:
        raise NegotiationError(f"no CSeq header in message: {message!r}")
    return m.group(1)


@dataclass
class WfdSessionParams:
    sink_rtp_port: int
    server_port: int | None = None
    session_id: str | None = None
    uibc_port: str | None = None
    use_hidc: bool = False
    audio_codec: str = "LPCM"
    negotiated_edid: bytes | None = None


@dataclass
class WfdCapabilities:
    """What this sink declares to the source in the M3 response. Field
    values are ported verbatim from d2.py's `msg` construction."""
    audio_codec: str = "LPCM"  # "LPCM" or "AAC"
    allow_1080p60: bool = False
    device_name: str = "lazycast"
    sink_model: str = "castd"
    sink_version: str = "1.0"
    enable_uibc: bool = False
    edid: bytes | None = None


STATE_WAIT_M1 = "WAIT_M1"
STATE_WAIT_M3 = "WAIT_M3"
STATE_WAIT_M4 = "WAIT_M4"
STATE_WAIT_M5 = "WAIT_M5"
STATE_WAIT_M6_RESPONSE = "WAIT_M6_RESPONSE"
STATE_WAIT_M7_RESPONSE = "WAIT_M7_RESPONSE"
STATE_STREAMING = "STREAMING"
STATE_TORN_DOWN = "TORN_DOWN"


class WfdNegotiator:
    def __init__(self, capabilities: WfdCapabilities, sink_rtp_port: int = 1028) -> None:
        self.capabilities = capabilities
        self.session = WfdSessionParams(sink_rtp_port=sink_rtp_port)
        self.state = STATE_WAIT_M1
        self._own_cseq = 0

    def _next_cseq(self) -> int:
        self._own_cseq += 1
        return self._own_cseq

    # -- M1: source sends OPTIONS, we reply with our supported methods --
    def handle_m1_options(self, request: str) -> str:
        if self.state != STATE_WAIT_M1:
            raise NegotiationError(f"M1 received in unexpected state {self.state}")
        cseq = _extract_cseq(request)
        self.state = STATE_WAIT_M3
        return f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nPublic: org.wfa.wfd1.0, SET_PARAMETER, GET_PARAMETER\r\n\r\n"

    # -- M2: we send OPTIONS to the source (reverse probe) --
    def build_m2_options_request(self) -> str:
        cseq = self._next_cseq()
        return f"OPTIONS * RTSP/1.0\r\nCSeq: {cseq}\r\nRequire: org.wfa.wfd1.0\r\n\r\n"

    # -- M3: source sends GET_PARAMETER, we reply with our capabilities --
    def handle_m3_get_parameter(self, request: str) -> str:
        if self.state != STATE_WAIT_M3:
            raise NegotiationError(f"M3 received in unexpected state {self.state}")
        cseq = _extract_cseq(request)
        body = self._build_capability_body(request)
        self.state = STATE_WAIT_M4
        return (
            f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n"
            f"Content-Type: text/parameters\r\nContent-Length: {len(body)}\r\n\r\n{body}"
        )

    def _build_capability_body(self, m3_request: str) -> str:
        cap = self.capabilities
        video_fmt_mask = "0001FFFF" if cap.allow_1080p60 else "0001FEFF"
        # Native-resolution byte: (index into CEA table << 3) | table-id 0.
        # 0x40 = CEA index 8 = 1920x1080p60; 0x00 = CEA index 0 = 640x480.
        # Real Windows 11 treats this as the sink's preferred resolution:
        # with 00 it streamed 1024x768 to a 1080p TV (observed 2026-07-14),
        # which then got upscaled to mush. Advertise what the display
        # actually is.
        native = "40" if cap.allow_1080p60 else "00"
        lines = [
            f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {self.session.sink_rtp_port} 0 mode=play",
            f"wfd_audio_codecs: {'LPCM 00000002 00' if cap.audio_codec == 'LPCM' else 'AAC 00000001 00'}",
            f"wfd_video_formats: {native} 00 02 10 {video_fmt_mask} 3FFFFFFF 00000FFF 00 0000 0000 00 none none",
            "wfd_3d_video_formats: none",
            "wfd_coupled_sink: none",
            "wfd_connector_type: 05",
            "wfd_uibc_capability: input_category_list=GENERIC, HIDC;generic_cap_list=Keyboard, Mouse;"
            "hidc_cap_list=Keyboard/USB, Mouse/USB;port=none"
            if not cap.enable_uibc
            else "wfd_uibc_capability: input_category_list=GENERIC, HIDC;generic_cap_list=Keyboard, Mouse;"
            "hidc_cap_list=Keyboard/USB, Mouse/USB;port=7336",
            "wfd_standby_resume_capability: none",
            "wfd_content_protection: none",
        ]
        if "wfd_display_edid" in m3_request and cap.edid:
            edid_len_field = (len(cap.edid) // 256 + 1)
            lines.append(f"wfd_display_edid: {edid_len_field:04X} {cap.edid.hex()}")
        if "intel_friendly_name" in m3_request:
            lines.append(f"intel_friendly_name: {cap.device_name}")
        if "intel_sink_manufacturer_name" in m3_request:
            lines.append(f"intel_sink_manufacturer_name: {cap.device_name}")
        if "intel_sink_model_name" in m3_request:
            lines.append(f"intel_sink_model_name: {cap.sink_model}")
        if "intel_sink_version" in m3_request:
            lines.append(f"intel_sink_version: {cap.sink_version}")
        if "intel_sink_device_URL" in m3_request:
            lines.append("intel_sink_device_URL: none")
        if "wfd_idr_request_capability" in m3_request:
            lines.append("wfd_idr_request_capability: 1")
        return "\r\n".join(lines) + "\r\n"

    # -- M4: source sends SET_PARAMETER with chosen params, we ack --
    def handle_m4_set_parameter(self, request: str) -> str:
        if self.state != STATE_WAIT_M4:
            raise NegotiationError(f"M4 received in unexpected state {self.state}")
        cseq = _extract_cseq(request)
        self._parse_uibc_port(request)
        self.state = STATE_WAIT_M5
        return f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n"

    def _parse_uibc_port(self, request: str) -> None:
        for entry in request.split("\r\n\r\n"):
            if "wfd_uibc_capability:" not in entry:
                continue
            m = _UIBC_PORT_RE.search(entry)
            if m and m.group(1) != "none":
                self.session.uibc_port = m.group(1)
                self.session.use_hidc = True

    # -- M5: a second ack-only message (matches d2.py's generic M5 recv) --
    def handle_m5_generic(self, request: str) -> str:
        if self.state != STATE_WAIT_M5:
            raise NegotiationError(f"M5 received in unexpected state {self.state}")
        cseq = _extract_cseq(request)
        self.state = STATE_WAIT_M6_RESPONSE
        return f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n"

    # -- M6: we send SETUP, source responds with server_port + Session --
    def build_m6_setup_request(self, source_ip: str) -> str:
        cseq = self._next_cseq()
        return (
            f"SETUP rtsp://{source_ip}/wfd1.0/streamid=0 RTSP/1.0\r\n"
            f"CSeq: {cseq}\r\n"
            f"Transport: RTP/AVP/UDP;unicast;client_port={self.session.sink_rtp_port}\r\n\r\n"
        )

    def parse_m6_response(self, response: str) -> WfdSessionParams:
        if self.state != STATE_WAIT_M6_RESPONSE:
            raise NegotiationError(f"M6 response received in unexpected state {self.state}")
        port_match = _SERVER_PORT_RE.search(response)
        if not port_match:
            raise NegotiationError(f"no server_port in M6 response: {response!r}")
        session_match = _SESSION_RE.search(response)
        if not session_match:
            raise NegotiationError(f"no Session id in M6 response: {response!r}")

        self.session.server_port = int(port_match.group(1))
        self.session.session_id = session_match.group(1)
        self.state = STATE_WAIT_M7_RESPONSE
        return self.session

    # -- M7: we send PLAY, source acks, streaming begins --
    def build_m7_play_request(self, source_ip: str) -> str:
        if self.session.session_id is None:
            raise NegotiationError("cannot build M7 before M6 response is parsed")
        cseq = self._next_cseq()
        return (
            f"PLAY rtsp://{source_ip}/wfd1.0/streamid=0 RTSP/1.0\r\n"
            f"CSeq: {cseq}\r\nSession: {self.session.session_id}\r\n\r\n"
        )

    def confirm_streaming(self, response: str) -> None:
        if self.state != STATE_WAIT_M7_RESPONSE:
            raise NegotiationError(f"M7 response received in unexpected state {self.state}")
        if "200 OK" not in response:
            raise NegotiationError(f"M7 not acknowledged: {response!r}")
        self.state = STATE_STREAMING

    # -- Steady state: source may TEARDOWN or send more GET/SET_PARAMETER --
    def is_teardown(self, message: str) -> bool:
        return len(message) == 0 or "wfd_trigger_method: TEARDOWN" in message

    def build_steady_state_ack(self, message: str) -> list[str]:
        """A steady-state message can bundle multiple RTSP requests
        separated by blank lines (matches d2.py's observed behavior with
        real Windows sources); ack each GET_PARAMETER/SET_PARAMETER found."""
        if self.state != STATE_STREAMING:
            raise NegotiationError(f"steady-state message received in unexpected state {self.state}")
        acks = []
        for entry in message.split("\r\n\r\n"):
            if "GET_PARAMETER" not in entry and "SET_PARAMETER" not in entry:
                continue
            cseq = _extract_cseq(entry)
            acks.append(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n\r\n")
        return acks

    def mark_torn_down(self) -> None:
        self.state = STATE_TORN_DOWN
