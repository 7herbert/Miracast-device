"""Tests that castd.main actually imports and wires the FSM to real action
handlers correctly. Relies on tests/conftest.py's dbus/gi stubs so the
import succeeds on a machine without those hardware libraries -- see that
file's docstring for exactly what is and is not verified by doing so.

Scope: this exercises CastDaemon's action-dispatch logic (_apply_actions)
and the connect/disconnect handlers with the render/uxplay/negotiate calls
replaced by recording fakes. It does NOT exercise real D-Bus, GStreamer, or
UxPlay behavior -- those need the Phase 0 hardware experiments.
"""
from __future__ import annotations

import castd.main as main_module
from castd.config import RoomConfig
from castd.fsm.state_machine import State
from castd.wfdsink.rtsp import WfdSessionParams


class FakeRenderProcess:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_running = False

    def start(self, pipeline_description: str) -> None:
        self.calls.append(f"start:{pipeline_description}")
        self.is_running = True

    def stop(self) -> None:
        self.calls.append("stop")
        self.is_running = False


class FakeUxPlayProcess:
    def __init__(self, config=None, **kwargs) -> None:
        self.calls: list[str] = []
        self.is_running = False

    def start(self) -> None:
        self.calls.append("start")
        self.is_running = True

    def stop(self) -> None:
        self.calls.append("stop")
        self.is_running = False


def make_daemon(monkeypatch) -> main_module.CastDaemon:
    config = RoomConfig(room_name="MR-TEST", wps_pin="12345670", passphrase="abcdefghij", channel=36)
    monkeypatch.setattr(main_module, "UxPlayProcess", FakeUxPlayProcess)
    # render_idle_screen writes a real PNG to disk (IDLE_PNG_PATH); fake it
    # out here so _apply_actions(SHOW_IDLE_SCREEN) doesn't touch the
    # filesystem during these wiring tests, same as render/uxplay/negotiate.
    monkeypatch.setattr(main_module, "render_idle_screen", lambda *a, **k: None)
    daemon = main_module.CastDaemon(config)
    daemon.render = FakeRenderProcess()
    daemon.uxplay = FakeUxPlayProcess()
    return daemon


def test_module_imports_cleanly():
    # If this test file collects at all, the import at the top already
    # succeeded -- but assert explicitly so intent is visible in results.
    assert hasattr(main_module, "CastDaemon")


def test_miracast_connect_success_starts_streaming_pipeline(monkeypatch):
    daemon = make_daemon(monkeypatch)

    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=48753, session_id="123")
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)

    # The source dials us and the accept loop hands the connected socket
    # in -- the daemon never opens an outbound control connection.
    daemon.handle_miracast_connected("192.168.173.80", sock=object())

    assert daemon.arbiter.state is State.MIRACAST
    assert "stop" in daemon.uxplay.calls  # AirPlay advertising paused
    assert any("start:" in c for c in daemon.render.calls)
    # render was stopped once (idle screen torn down) and started twice
    # (idle screen at daemon construction time is not part of this fake, so
    # just check the streaming pipeline start happened after a stop).
    assert daemon.render.calls[-2:] == ["stop", daemon.render.calls[-1]]


def test_miracast_connect_failure_falls_back_to_idle(monkeypatch):
    daemon = make_daemon(monkeypatch)

    def boom(sock, **k):
        raise OSError("connection reset during handshake")

    monkeypatch.setattr(main_module, "negotiate", boom)

    daemon.handle_miracast_connected("192.168.173.80", sock=object())

    assert daemon.arbiter.state is State.IDLE
    assert "start" in daemon.uxplay.calls  # AirPlay advertising resumed after failed attempt


def test_miracast_disconnect_stops_render_and_shows_idle_screen(monkeypatch):
    daemon = make_daemon(monkeypatch)
    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=1, session_id="1")
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)

    daemon.handle_miracast_connected("192.168.173.80", sock=object())
    daemon.handle_miracast_disconnected()

    assert daemon.arbiter.state is State.IDLE
    assert daemon.render.calls[-1].startswith("start:")  # idle screen pipeline restarted
    assert "start" in daemon.uxplay.calls


def test_health_state_reflects_arbiter_after_actions(monkeypatch):
    daemon = make_daemon(monkeypatch)
    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=1, session_id="1")
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)

    daemon.handle_miracast_connected("192.168.173.80", sock=object())
    assert daemon.health.snapshot()["state"] == "MIRACAST"


class FakeControlSock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_control_channel_close_returns_daemon_to_idle(monkeypatch):
    # The 2026-07-14 regression: session negotiated, socket dropped, but
    # the FSM stayed in MIRACAST forever, so every reconnect was rejected
    # with "already in a Miracast session".
    daemon = make_daemon(monkeypatch)
    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=1, session_id="1")
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)
    monkeypatch.setattr(main_module, "run_steady_state", lambda sock, neg, **k: None)

    sock = FakeControlSock()
    negotiator = daemon.handle_miracast_connected("192.168.173.80", sock=sock)
    assert negotiator is not None
    assert daemon.arbiter.state is State.MIRACAST

    daemon._pump_control_channel(sock, negotiator)

    assert sock.closed
    assert daemon.arbiter.state is State.IDLE
    assert "start" in daemon.uxplay.calls  # AirPlay advertising resumed


def test_failed_negotiation_returns_none(monkeypatch):
    daemon = make_daemon(monkeypatch)

    def boom(sock, **k):
        raise OSError("handshake died")

    monkeypatch.setattr(main_module, "negotiate", boom)
    assert daemon.handle_miracast_connected("192.168.173.80", sock=object()) is None


def test_station_authorized_runs_the_full_session_lifecycle(monkeypatch):
    # The full post-WPS chain: StaAuthorized MAC -> lease lookup -> sink
    # DIALS the source (never listens for it) -> M1-M7 -> control channel
    # pumped while in MIRACAST -> source leaves -> clean return to IDLE.
    daemon = make_daemon(monkeypatch)
    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=1, session_id="1")
    dialed = []
    states_while_pumping = []
    sock = FakeControlSock()

    monkeypatch.setattr(
        main_module, "find_lease_ip", lambda mac, *a, **k: "192.168.173.93" if mac == "12:5f:ad:5c:f4:13" else None
    )

    def fake_dial(source_ip, **k):
        dialed.append(source_ip)
        return sock

    monkeypatch.setattr(main_module, "open_control_connection", fake_dial)
    monkeypatch.setattr(main_module, "negotiate", lambda s, **k: fake_session)
    monkeypatch.setattr(
        main_module, "run_steady_state", lambda s, n, **k: states_while_pumping.append(daemon.arbiter.state)
    )

    daemon._connect_to_authorized_source("12:5f:ad:5c:f4:13")

    assert dialed == ["192.168.173.93"]
    assert states_while_pumping == [State.MIRACAST]  # streaming while the channel was pumped
    assert daemon.arbiter.state is State.IDLE  # clean teardown after the source left
    assert sock.closed


def test_station_authorized_gives_up_cleanly_without_a_lease(monkeypatch):
    daemon = make_daemon(monkeypatch)
    monkeypatch.setattr(main_module, "find_lease_ip", lambda mac, *a, **k: None)

    daemon._connect_to_authorized_source("12:5f:ad:5c:f4:13", lease_timeout_s=0)

    assert daemon.arbiter.state is State.IDLE


def test_legacy_station_without_rtsp_service_stays_idle(monkeypatch):
    # An iPhone joining the group's Wi-Fi for AirPlay triggers the same
    # StaAuthorized path as a Miracast source but runs no RTSP server --
    # the probe must fail quietly and leave the FSM alone.
    daemon = make_daemon(monkeypatch)
    monkeypatch.setattr(main_module, "find_lease_ip", lambda mac, *a, **k: "192.168.173.123")

    def refused(source_ip, **k):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(main_module, "open_control_connection", refused)

    daemon._connect_to_authorized_source("7e:b3:6f:08:3b:2a")

    assert daemon.arbiter.state is State.IDLE


def test_uxplay_crash_mid_airplay_recovers_to_idle_and_relaunches(monkeypatch):
    # The 2026-07-14 failure mode: uxplay crashed mid-stream, nothing
    # noticed, the FSM stayed in AIRPLAY and the room showed a black
    # screen with no way back until a Miracast session cycled uxplay.
    daemon = make_daemon(monkeypatch)
    monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

    daemon._on_airplay_connected()
    assert daemon.arbiter.state is State.AIRPLAY
    daemon.uxplay.is_running = False  # the crash

    daemon._on_uxplay_exited()

    assert daemon.arbiter.state is State.IDLE
    assert daemon.render.is_running  # idle screen back
    assert daemon.uxplay.is_running  # relaunched


def test_airplay_connect_releases_display_and_disconnect_reclaims_it(monkeypatch):
    # The DRM handoff: UxPlay's own kmssink cannot become DRM master while
    # the idle screen holds it -- a real iPhone got "cannot connect"
    # (2026-07-14). AIRPLAY_CONNECTED must stop our render pipeline.
    daemon = make_daemon(monkeypatch)
    daemon.render.start("idle")
    assert daemon.render.is_running

    daemon._on_airplay_connected()
    assert daemon.arbiter.state is State.AIRPLAY
    assert not daemon.render.is_running  # DRM released for UxPlay

    daemon._on_airplay_disconnected()
    assert daemon.arbiter.state is State.IDLE
    assert daemon.render.is_running  # idle screen back on
