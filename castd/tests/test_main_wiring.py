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
    def __init__(self, config=None) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


def make_daemon(monkeypatch) -> main_module.CastDaemon:
    config = RoomConfig(room_name="MR-TEST", wps_pin="12345670", passphrase="abcdefghij", channel=36)
    monkeypatch.setattr(main_module, "UxPlayProcess", FakeUxPlayProcess)
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
    monkeypatch.setattr(main_module, "open_control_connection", lambda source_ip, **k: object())
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)

    daemon.handle_miracast_connected("192.168.173.80")

    assert daemon.arbiter.state is State.MIRACAST
    assert "stop" in daemon.uxplay.calls  # AirPlay advertising paused
    assert any("start:" in c for c in daemon.render.calls)
    # render was stopped once (idle screen torn down) and started twice
    # (idle screen at daemon construction time is not part of this fake, so
    # just check the streaming pipeline start happened after a stop).
    assert daemon.render.calls[-2:] == ["stop", daemon.render.calls[-1]]


def test_miracast_connect_failure_falls_back_to_idle(monkeypatch):
    daemon = make_daemon(monkeypatch)

    def boom(source_ip, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(main_module, "open_control_connection", boom)

    daemon.handle_miracast_connected("192.168.173.80")

    assert daemon.arbiter.state is State.IDLE
    assert "start" in daemon.uxplay.calls  # AirPlay advertising resumed after failed attempt


def test_miracast_disconnect_stops_render_and_shows_idle_screen(monkeypatch):
    daemon = make_daemon(monkeypatch)
    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=1, session_id="1")
    monkeypatch.setattr(main_module, "open_control_connection", lambda source_ip, **k: object())
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)

    daemon.handle_miracast_connected("192.168.173.80")
    daemon.handle_miracast_disconnected()

    assert daemon.arbiter.state is State.IDLE
    assert daemon.render.calls[-1].startswith("start:")  # idle screen pipeline restarted
    assert "start" in daemon.uxplay.calls


def test_health_state_reflects_arbiter_after_actions(monkeypatch):
    daemon = make_daemon(monkeypatch)
    fake_session = WfdSessionParams(sink_rtp_port=1028, server_port=1, session_id="1")
    monkeypatch.setattr(main_module, "open_control_connection", lambda source_ip, **k: object())
    monkeypatch.setattr(main_module, "negotiate", lambda sock, **k: fake_session)

    daemon.handle_miracast_connected("192.168.173.80")
    assert daemon.health.snapshot()["state"] == "MIRACAST"
