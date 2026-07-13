"""Per-room configuration for castd.

Loaded from a plain key=value text file (deployed as /boot/receiver.conf so a
room can be configured by editing the FAT boot partition without booting
Linux). Parsing is separated from file I/O so it can be unit tested with
in-memory strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Non-DFS 5 GHz channels only: DFS channels require radar detection and can
# be silently vacated by the driver mid-meeting, which is unacceptable here.
ALLOWED_CHANNELS = {36: 5180, 40: 5200, 44: 5220, 48: 5240}

_PIN_RE = re.compile(r"^\d{8}$")
_KEY_VALUE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class ConfigError(ValueError):
    pass


def wps_pin_checksum(seven_digits: int) -> int:
    """WSC (Wi-Fi Simple Config) device password checksum digit for a
    7-digit PIN prefix. Real-hardware testing found an 8-digit WPS PIN
    with an invalid checksum digit (31415926 -- chosen for its pi digits,
    not spec compliance) gets silently rejected somewhere in the
    wpa_supplicant/Windows WPS exchange: the D-Bus calls to arm it all
    succeed without error, but the peer never completes pairing. Any
    fixed keypad-mode WPS PIN must satisfy this checksum or the failure
    mode is exactly this silent, hard-to-diagnose hang."""
    accum = 0
    accum += 3 * ((seven_digits // 1000000) % 10)
    accum += 1 * ((seven_digits // 100000) % 10)
    accum += 3 * ((seven_digits // 10000) % 10)
    accum += 1 * ((seven_digits // 1000) % 10)
    accum += 3 * ((seven_digits // 100) % 10)
    accum += 1 * ((seven_digits // 10) % 10)
    accum += 3 * (seven_digits % 10)
    return (10 - accum % 10) % 10


def is_valid_wps_pin(pin: str) -> bool:
    if not _PIN_RE.match(pin):
        return False
    prefix, checksum_digit = int(pin[:7]), int(pin[7])
    return wps_pin_checksum(prefix) == checksum_digit


@dataclass(frozen=True)
class RoomConfig:
    room_name: str
    wps_pin: str
    passphrase: str
    channel: int

    @property
    def freq_mhz(self) -> int:
        return ALLOWED_CHANNELS[self.channel]

    @property
    def device_name(self) -> str:
        # Advertised as the P2P/WFD device name so Windows shows the room,
        # not "raspberrypi", in the Connect/Win+K device list.
        return self.room_name


def parse_room_config(text: str) -> RoomConfig:
    """Parse receiver.conf contents. Raises ConfigError with a specific,
    actionable message on any problem (this file gets hand-edited on a FAT
    partition by whoever is racking the Pi, so terse KeyError-style failures
    are not acceptable)."""
    values: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEY_VALUE_RE.match(line)
        if not m:
            raise ConfigError(f"receiver.conf line {lineno}: cannot parse {raw_line!r}")
        values[m.group(1)] = m.group(2)

    required = ("room_name", "wps_pin", "passphrase", "channel")
    missing = [k for k in required if k not in values]
    if missing:
        raise ConfigError(f"receiver.conf missing required key(s): {', '.join(missing)}")

    room_name = values["room_name"]
    if not room_name or len(room_name) > 32:
        raise ConfigError("room_name must be 1-32 characters (P2P device name limit)")

    wps_pin = values["wps_pin"]
    if not _PIN_RE.match(wps_pin):
        raise ConfigError(f"wps_pin must be exactly 8 digits, got {wps_pin!r}")
    if not is_valid_wps_pin(wps_pin):
        correct_checksum = wps_pin_checksum(int(wps_pin[:7]))
        raise ConfigError(
            f"wps_pin {wps_pin!r} fails the WSC checksum digit (last digit must be "
            f"{correct_checksum}, i.e. {wps_pin[:7]}{correct_checksum}) -- an invalid "
            "checksum is silently rejected during WPS pairing instead of raising a "
            "clear error, so this is checked here instead"
        )

    passphrase = values["passphrase"]
    if not (8 <= len(passphrase) <= 63):
        raise ConfigError("passphrase must be 8-63 characters (WPA2-PSK limit)")

    try:
        channel = int(values["channel"])
    except ValueError:
        raise ConfigError(f"channel must be an integer, got {values['channel']!r}") from None
    if channel not in ALLOWED_CHANNELS:
        allowed = ", ".join(str(c) for c in sorted(ALLOWED_CHANNELS))
        raise ConfigError(f"channel must be one of the non-DFS 5GHz channels: {allowed}")

    return RoomConfig(room_name=room_name, wps_pin=wps_pin, passphrase=passphrase, channel=channel)
