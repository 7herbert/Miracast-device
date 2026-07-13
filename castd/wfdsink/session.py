"""Socket-driving wrapper around WfdNegotiator.

This is the thin I/O shell around rtsp.py's pure logic. It takes a
connected `socket.socket` (or in tests, anything duck-typed the same way --
see castd/tests/test_rtsp_integration.py, which uses `socket.socketpair()`
to run the full M1-M7 handshake against a scripted fake Windows source
in-process, no network or hardware required) and drives the negotiator
through the handshake, then hands off to the steady-state loop.

Deliberately excludes: the watchdog-timeout-triggers-killall logic from
d2.py's main loop. That responsibility now belongs to the FSM
(castd.fsm.state_machine), which the caller (main.py) drives from a single
place instead of duplicating "how long since we last saw data" bookkeeping
here as well.
"""
from __future__ import annotations

import logging
import socket
from typing import Protocol

from castd.wfdsink.rtsp import WfdCapabilities, WfdNegotiator, WfdSessionParams

logger = logging.getLogger(__name__)

RECV_BUFSIZE = 4096
WFD_CONTROL_PORT = 7236


class SocketLike(Protocol):
    def recv(self, bufsize: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...


def negotiate(sock: SocketLike, *, source_ip: str, capabilities: WfdCapabilities, sink_rtp_port: int = 1028) -> WfdSessionParams:
    """Run the full M1-M7 handshake over `sock`. Returns the negotiated
    session params on success. Raises NegotiationError (from rtsp.py) or
    socket.error/OSError on transport failure -- the caller (main.py) is
    expected to catch both and drive the FSM back to IDLE, not to treat a
    negotiation failure as fatal to the whole daemon."""
    negotiator = WfdNegotiator(capabilities, sink_rtp_port=sink_rtp_port)

    request = sock.recv(RECV_BUFSIZE).decode()
    logger.debug("M1 <- %r", request)
    response = negotiator.handle_m1_options(request)
    sock.sendall(response.encode())

    m2_request = negotiator.build_m2_options_request()
    sock.sendall(m2_request.encode())
    m2_response = sock.recv(RECV_BUFSIZE).decode()
    logger.debug("M2 response -> %r", m2_response)

    request = sock.recv(RECV_BUFSIZE).decode()
    logger.debug("M3 <- %r", request)
    response = negotiator.handle_m3_get_parameter(request)
    sock.sendall(response.encode())

    request = sock.recv(RECV_BUFSIZE).decode()
    logger.debug("M4 <- %r", request)
    response = negotiator.handle_m4_set_parameter(request)
    sock.sendall(response.encode())

    request = sock.recv(RECV_BUFSIZE).decode()
    logger.debug("M5 <- %r", request)
    response = negotiator.handle_m5_generic(request)
    sock.sendall(response.encode())

    m6_request = negotiator.build_m6_setup_request(source_ip)
    sock.sendall(m6_request.encode())
    m6_response = sock.recv(RECV_BUFSIZE).decode()
    session = negotiator.parse_m6_response(m6_response)

    m7_request = negotiator.build_m7_play_request(source_ip)
    sock.sendall(m7_request.encode())
    m7_response = sock.recv(RECV_BUFSIZE).decode()
    negotiator.confirm_streaming(m7_response)

    logger.info(
        "WFD negotiation complete: server_port=%s session_id=%s uibc_port=%s",
        session.server_port, session.session_id, session.uibc_port,
    )
    return session


def listen_for_sources(bind_ip: str, *, port: int = WFD_CONTROL_PORT, backlog: int = 1) -> socket.socket:
    """Create the sink-side RTSP listener socket.

    Direction matters and an earlier revision had it backwards (an
    open_control_connection() that dialed OUT to source:7236): per the WFD
    spec it is the SOURCE that initiates the TCP connection, to the port
    the sink advertises in its WFD Device Information subelement (7236 --
    see p2p/wfd_ie.py's build_device_info_subelement, control_port field).
    lazycast, this project's predecessor, listened for exactly this reason.
    The caller accept()s connections and hands each one to negotiate();
    the accepted peer address is how the sink learns the source's IP."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    sock.listen(backlog)
    return sock
