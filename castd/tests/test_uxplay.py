"""Tests for the UxPlay argv construction -- pure logic, no uxplay binary.
Locks in the 2026-07-14 real-hardware lesson: uxplay(1) has no -bindif
option, and an unknown flag makes it exit immediately with a usage error
that piped-and-unread output rendered invisible."""
from castd.airplay.uxplay import UxPlayClientTracker, UxPlayConfig, build_uxplay_argv


def test_argv_uses_only_documented_uxplay_options():
    argv = build_uxplay_argv(UxPlayConfig(device_name="MR-3F-A"))
    assert "uxplay" in argv
    assert "-bindif" not in argv  # does not exist in uxplay(1); fatal at launch
    assert "-pin" not in argv  # WPS PINs are not UxPlay pins; the Wi-Fi is the gate


def test_argv_pins_fixed_ports_across_restarts():
    # castd restarts uxplay around every Miracast session; random ports
    # plus a client-side mDNS cache meant an iPhone dialed the previous
    # instance's dead port (2026-07-14).
    argv = build_uxplay_argv(UxPlayConfig(device_name="MR-3F-A"))
    assert "-p" in argv


def test_argv_forces_line_buffered_output():
    # Block-buffered pipe output delayed uxplay's log lines until process
    # exit (observed 2026-07-14), making real-time client detection -- and
    # therefore the DRM handoff -- impossible.
    argv = build_uxplay_argv(UxPlayConfig(device_name="MR-3F-A"))
    assert argv[:3] == ["stdbuf", "-oL", "-eL"]


def test_argv_advertises_the_room_name_verbatim():
    argv = build_uxplay_argv(UxPlayConfig(device_name="MR-3F-A"))
    assert argv[argv.index("-n") + 1] == "MR-3F-A"
    assert "-nh" in argv  # no "@hostname" suffix in the AirPlay list


def test_argv_renders_via_kms_and_alsa():
    argv = build_uxplay_argv(UxPlayConfig(device_name="X"))
    # driver-name=vc4: the Pi 4 has two DRM devices and a bare kmssink
    # can open the render-only one -- a real iPhone session died with
    # "kmssink_h264 ... general resource error" (2026-07-14).
    assert argv[argv.index("-vs") + 1] == "kmssink driver-name=vc4 sync=false"
    assert argv[argv.index("-as") + 1] == "alsasink"


def test_tracker_reports_first_accept_as_connected():
    t = UxPlayClientTracker()
    assert t.feed("Accepted IPv4 client on socket 12") == "connected"
    # additional connections within the same session stay silent
    assert t.feed("Accepted IPv4 client on socket 13") is None


def test_tracker_reports_disconnect_only_when_all_connections_close():
    t = UxPlayClientTracker()
    t.feed("Accepted IPv4 client on socket 12")
    t.feed("Accepted IPv4 client on socket 13")
    assert t.feed("Connection closed for socket 12") is None
    assert t.feed("Connection closed for socket 13") == "disconnected"


def test_tracker_server_stop_flushes_active_session():
    t = UxPlayClientTracker()
    t.feed("Accepted IPv4 client on socket 12")
    assert t.feed("Stopping RAOP Server...") == "disconnected"


def test_tracker_server_stop_without_clients_is_silent():
    # castd itself stops uxplay whenever a Miracast session starts; that
    # must not produce a phantom AirPlay-disconnect event.
    t = UxPlayClientTracker()
    assert t.feed("Stopping RAOP Server...") is None


def test_tracker_ignores_unrelated_lines():
    t = UxPlayClientTracker()
    assert t.feed("UxPlay 1.74: An Open-Source AirPlay mirroring and audio-streaming server.") is None
    assert t.feed("Initialized server socket(s)") is None
