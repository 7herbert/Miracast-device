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


def open_control_connection(source_ip: str, *, timeout: float = 5.0) -> socket.socket:
    """Connect to the source's RTSP control port. `timeout` is not optional
    with a None default on purpose -- d2.py's original bug (#15 in the
    project retrospective) was exactly an un-timed-out connect() that could
    hang the whole process when a source stopped responding mid-handshake."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.connect((source_ip, WFD_CONTROL_PORT))
    sock.settimeout(None)
    return sock
