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
import re
import socket
import time
from typing import Protocol

from castd.wfdsink.rtsp import WfdCapabilities, WfdNegotiator, WfdSessionParams

logger = logging.getLogger(__name__)

RECV_BUFSIZE = 4096
WFD_CONTROL_PORT = 7236

_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


class SocketLike(Protocol):
    def recv(self, bufsize: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...


class RtspReader:
    """Splits the TCP byte stream into individual RTSP messages (headers
    up to the blank line, plus a body of exactly Content-Length bytes).

    The naive one-recv-per-message pattern this replaces was a latent
    protocol bug, not just test flakiness: TCP is free to deliver the
    peer's back-to-back messages (its M2 response immediately followed by
    its M3 request) coalesced into one segment, and the second message
    silently disappeared into whatever recv() happened to read it. One
    reader instance must own the socket's inbound side for the whole
    session -- bytes it has buffered are invisible to raw recv() calls."""

    def __init__(self, sock: SocketLike) -> None:
        self._sock = sock
        self._buf = b""

    def next_message(self) -> str:
        """Return the next complete RTSP message, or "" when the peer
        closes the connection. Propagates the socket's own timeout."""
        while True:
            head_end = self._buf.find(b"\r\n\r\n")
            if head_end != -1:
                header = self._buf[: head_end + 4]
                m = _CONTENT_LENGTH_RE.search(header)
                total = head_end + 4 + (int(m.group(1)) if m else 0)
                if len(self._buf) >= total:
                    message = self._buf[:total]
                    self._buf = self._buf[total:]
                    return message.decode(errors="replace")
            data = self._sock.recv(RECV_BUFSIZE)
            if not data:
                message = self._buf.decode(errors="replace")
                self._buf = b""
                return message
            self._buf += data


def negotiate(
    sock: SocketLike,
    *,
    source_ip: str,
    capabilities: WfdCapabilities,
    sink_rtp_port: int = 1028,
    negotiator: WfdNegotiator | None = None,
    reader: RtspReader | None = None,
) -> WfdSessionParams:
    """Run the full M1-M7 handshake over `sock`. Returns the negotiated
    session params on success. Raises NegotiationError (from rtsp.py) or
    socket.error/OSError on transport failure -- the caller (main.py) is
    expected to catch both and drive the FSM back to IDLE, not to treat a
    negotiation failure as fatal to the whole daemon.

    Pass `negotiator` to keep a handle on the session's state machine --
    required for run_steady_state below, which continues on the same
    negotiator after M7. Pass `reader` (and reuse it for run_steady_state)
    when calling both, so a message the peer coalesced across the
    handshake/steady-state boundary is not lost."""
    if negotiator is None:
        negotiator = WfdNegotiator(capabilities, sink_rtp_port=sink_rtp_port)
    if reader is None:
        reader = RtspReader(sock)

    request = reader.next_message()
    logger.debug("M1 <- %r", request)
    response = negotiator.handle_m1_options(request)
    sock.sendall(response.encode())

    m2_request = negotiator.build_m2_options_request()
    sock.sendall(m2_request.encode())
    m2_response = reader.next_message()
    logger.debug("M2 response -> %r", m2_response)

    request = reader.next_message()
    logger.debug("M3 <- %r", request)
    response = negotiator.handle_m3_get_parameter(request)
    sock.sendall(response.encode())

    request = reader.next_message()
    logger.debug("M4 <- %r", request)
    response = negotiator.handle_m4_set_parameter(request)
    sock.sendall(response.encode())

    request = reader.next_message()
    logger.debug("M5 <- %r", request)
    response = negotiator.handle_m5_generic(request)
    sock.sendall(response.encode())

    m6_request = negotiator.build_m6_setup_request(source_ip)
    sock.sendall(m6_request.encode())
    m6_response = reader.next_message()
    session = negotiator.parse_m6_response(m6_response)

    m7_request = negotiator.build_m7_play_request(source_ip)
    sock.sendall(m7_request.encode())
    m7_response = reader.next_message()
    negotiator.confirm_streaming(m7_response)

    logger.info(
        "WFD negotiation complete: server_port=%s session_id=%s uibc_port=%s",
        session.server_port, session.session_id, session.uibc_port,
    )
    return session


def run_steady_state(
    sock: SocketLike,
    negotiator: WfdNegotiator,
    *,
    keepalive_timeout: float = 60.0,
    reader: RtspReader | None = None,
) -> None:
    """Pump the RTSP control connection for as long as the session lives:
    ack the source's periodic M16 GET_PARAMETER keep-alives (and any
    SET_PARAMETER), returning when the source ends the session -- explicit
    TEARDOWN trigger, closed connection (empty recv), socket error, or
    keep-alive silence past `keepalive_timeout` (sources send M16 at least
    every ~30 s).

    This loop is not optional. The 2026-07-14 live run had negotiate()
    return and the socket fall out of scope: CPython closed it on GC,
    tcpdump showed our FIN 82 ms after the PLAY ack, and Windows tore the
    entire session down (StaDeauthorized) half a second later."""
    if reader is None:
        reader = RtspReader(sock)
    if hasattr(sock, "settimeout"):
        sock.settimeout(keepalive_timeout)
    try:
        while True:
            try:
                message = reader.next_message()
            except TimeoutError:
                logger.warning("no RTSP traffic for %.0fs; treating session as dead", keepalive_timeout)
                return
            except OSError:
                logger.info("RTSP control connection error; session over")
                return
            if message:
                for ack in negotiator.build_steady_state_ack(message):
                    sock.sendall(ack.encode())
            if negotiator.is_teardown(message):
                logger.info("source ended the RTSP session (teardown or connection close)")
                return
    finally:
        negotiator.mark_torn_down()


def open_control_connection(
    source_ip: str,
    *,
    port: int = WFD_CONTROL_PORT,
    attempts: int = 10,
    retry_delay: float = 1.0,
    timeout: float = 5.0,
) -> socket.socket:
    """Dial the SOURCE's RTSP control port. The connection direction was
    settled empirically on 2026-07-14: a Windows 11 source that had
    completed association, WPS, and DHCP never dialed our 7236 listener
    (packet capture showed zero SYNs), while its own source WFD IE
    advertised port 7236 -- "connect to ME here". The sink initiates the
    TCP connection to source:7236 and the source then sends RTSP M1
    (OPTIONS) over it; lazycast, which worked against real Windows, dialed
    out the same way using the IP from the DHCP lease it had just issued.

    Retries because the source's RTSP server may start listening a beat
    after its DHCP exchange completes. `timeout` is never None during
    connect on purpose -- d2.py's original bug (#15 in the project
    retrospective) was an un-timed-out connect() hanging the process."""
    last_exc: OSError = OSError("no connection attempts made")
    for _ in range(attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.connect((source_ip, port))
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last_exc = exc
            sock.close()
            time.sleep(retry_delay)
    raise last_exc


def listen_for_sources(bind_ip: str, *, port: int = WFD_CONTROL_PORT, backlog: int = 1) -> socket.socket:
    """Sink-side listener on 7236. NOT the primary Miracast path -- see
    open_control_connection above for the actual connection direction.
    Kept as a diagnostic net (anything dialing us here gets logged and
    handled) and as groundwork for MS-MICE, where the source genuinely
    does dial the sink (on 7250)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    sock.listen(backlog)
    return sock
