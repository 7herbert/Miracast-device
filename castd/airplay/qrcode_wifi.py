"""Wi-Fi network QR code payload generator.

Produces the "WIFI:" URI format that iOS/Android camera apps recognize
natively (no app install needed) so an Apple device can join the Pi's P2P
group as a legacy WPA2-PSK station, at which point avahi/UxPlay on that
interface makes AirPlay show up in Control Center.

Format reference: this is the de-facto standard popularized by ZXing/Barcode
Scanner, documented at https://github.com/zxing/zxing/wiki/Barcode-Contents
under "Wi-Fi Network config". Special characters in SSID/password
(';', ',', '"', '\\', ':') must be backslash-escaped or scanners mis-split
the fields -- this is the actual bug class to watch for, not something
theoretical: a room name or passphrase containing a comma will silently
truncate on some scanners if unescaped.
"""
from __future__ import annotations

_ESCAPE_CHARS = ('\\', ';', ',', '"', ':')


def _escape(value: str) -> str:
    out = value
    # Backslash must be escaped first, or the escaping of the other
    # characters would introduce fresh backslashes that get re-escaped.
    for ch in _ESCAPE_CHARS:
        out = out.replace(ch, "\\" + ch)
    return out


def build_wifi_qr_payload(*, ssid: str, passphrase: str, hidden: bool = False) -> str:
    """Build a WIFI: URI for a WPA2-PSK network. `ssid`/`passphrase` are the
    raw (unescaped) values; escaping is applied here."""
    if not ssid:
        raise ValueError("ssid must not be empty")
    if not (8 <= len(passphrase) <= 63):
        raise ValueError("passphrase must be 8-63 characters (WPA2-PSK limit)")

    parts = [
        "WIFI:",
        "T:WPA;",
        f"S:{_escape(ssid)};",
        f"P:{_escape(passphrase)};",
    ]
    if hidden:
        parts.append("H:true;")
    parts.append(";")
    return "".join(parts)
