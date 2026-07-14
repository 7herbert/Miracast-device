"""Tests for the UxPlay argv construction -- pure logic, no uxplay binary.
Locks in the 2026-07-14 real-hardware lesson: uxplay(1) has no -bindif
option, and an unknown flag makes it exit immediately with a usage error
that piped-and-unread output rendered invisible."""
from castd.airplay.uxplay import UxPlayConfig, build_uxplay_argv


def test_argv_uses_only_documented_uxplay_options():
    argv = build_uxplay_argv(UxPlayConfig(device_name="MR-3F-A"))
    assert argv[0] == "uxplay"
    assert "-bindif" not in argv  # does not exist in uxplay(1); fatal at launch
    assert "-pin" not in argv  # WPS PINs are not UxPlay pins; the Wi-Fi is the gate


def test_argv_advertises_the_room_name_verbatim():
    argv = build_uxplay_argv(UxPlayConfig(device_name="MR-3F-A"))
    assert argv[argv.index("-n") + 1] == "MR-3F-A"
    assert "-nh" in argv  # no "@hostname" suffix in the AirPlay list


def test_argv_renders_via_kms_and_alsa():
    argv = build_uxplay_argv(UxPlayConfig(device_name="X"))
    assert argv[argv.index("-vs") + 1] == "kmssink"
    assert argv[argv.index("-as") + 1] == "alsasink"
