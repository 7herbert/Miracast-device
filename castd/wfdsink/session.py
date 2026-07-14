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
import time
from typing import Protocol

from castd.wfdsink.rtsp import WfdCapabilities, WfdNegotiator, WfdSessionParams

logger = logging.getLogger(__name__)

RECV_BUFSIZE = 4096
WFD_CONTROL_PORT = 7236


class SocketLike(Protocol):
    def recv(self, bufsize: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...


def negotiate(
    sock: SocketLike,
    *,
    source_ip: str,
    capabilities: WfdCapabilities,
    sink_rtp_port: int = 1028,
    negotiator: WfdNegotiator | None = None,
) -> WfdSessionParams:
    """Run the full M1-M7 handshake over `sock`. Returns the negotiated
    session params on success. Raises NegotiationError (from rtsp.py) or
    socket.error/OSError on transport failure -- the caller (main.py) is
    expected to catch both and drive the FSM back to IDLE, not to treat a
    negotiation failure as fatal to the whole daemon.

    Pass `negotiator` to keep a handle on the session's state machine --
    required for run_steady_state below, which continues on the same
    negotiator after M7."""
    if negotiator is None:
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


def run_steady_state(sock: SocketLike, negotiator: WfdNegotiator, *, keepalive_timeout: float = 60.0) -> None:
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
    if hasattr(sock, "settimeout"):
        sock.settimeout(keepalive_timeout)
    try:
        while True:
            try:
                data = sock.recv(RECV_BUFSIZE)
            except TimeoutError:
                logger.warning("no RTSP traffic for %.0fs; treating session as dead", keepalive_timeout)
                return
            except OSError:
                logger.info("RTSP control connection error; session over")
                return
            message = data.decode(errors="replace")
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
