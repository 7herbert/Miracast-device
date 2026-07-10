"""Wi-Fi Display (WFD) Device Information subelement encoder.

Pure byte-building logic, independent of the D-Bus transport that carries it
(wpa_supplicant's "WFDIEs" global property, or the per-interface WPS vendor
extension used for the MICE hostname/IP subelements in lazycast's
newmice.py). Kept separate from dbus_go.py so the bit-packing can be unit
tested on any machine, no wpa_supplicant or D-Bus required.

Reference: Wi-Fi Alliance Wi-Fi Display Technical Specification v1.0,
section 5.1.2 (WFD Device Information subelement). Field layout mirrors
what lazycast's newmice.py sends to a real Windows source successfully,
cross-checked against the subelement structure documented in the spec.
"""
from __future__ import annotations

# WFD device type bitmap (bits 0-1 of the 2-byte device info field)
WFD_SOURCE = 0b00
WFD_PRIMARY_SINK = 0b01
WFD_SECONDARY_SINK = 0b10
WFD_SOURCE_OR_SINK = 0b11

# Optional capability bits (bit positions per spec section 5.1.2)
WFD_AVAILABLE_FOR_SESSION = 1 << 4
WFD_SERVICE_DISCOVERY_SUPPORTED = 1 << 5
WFD_PREFERRED_CONNECTIVITY_P2P = 1 << 6
WFD_CONTENT_PROTECTION_SUPPORTED = 1 << 8
WFD_TIME_SYNCHRONIZATION_SUPPORTED = 1 << 9


def build_device_info_subelement(
    *,
    device_type: int = WFD_PRIMARY_SINK,
    control_port: int = 7236,
    max_throughput_mbps: int = 50,
    available_for_session: bool = True,
) -> bytes:
    """Build subelement ID 0 (WFD Device Information): 1-byte ID + 2-byte
    length + 6-byte payload = 9 bytes total."""
    if not (0 <= control_port <= 0xFFFF):
        raise ValueError(f"control_port out of range: {control_port}")
    if not (0 <= max_throughput_mbps <= 0xFFFF):
        raise ValueError(f"max_throughput_mbps out of range: {max_throughput_mbps}")

    bitmap = device_type & 0b11
    if available_for_session:
        bitmap |= WFD_AVAILABLE_FOR_SESSION
    bitmap |= WFD_SERVICE_DISCOVERY_SUPPORTED

    payload = bitmap.to_bytes(2, "big") + control_port.to_bytes(2, "big") + max_throughput_mbps.to_bytes(2, "big")
    subelem_id = 0
    length = len(payload)
    return bytes([subelem_id]) + length.to_bytes(2, "big") + payload


def build_associated_bssid_subelement(bssid: bytes) -> bytes:
    """Subelement ID 1: WFD Associated BSSID (6-byte MAC). Not required for
    a fresh P2P GO with no prior association; included for completeness."""
    if len(bssid) != 6:
        raise ValueError(f"bssid must be 6 bytes, got {len(bssid)}")
    return bytes([1]) + len(bssid).to_bytes(2, "big") + bssid


def build_coupled_sink_subelement() -> bytes:
    """Subelement ID 6: WFD Coupled Sink Information, status=not coupled."""
    payload = bytes([0x00]) + b"\x00" * 6
    return bytes([6]) + len(payload).to_bytes(2, "big") + payload


def build_wfd_ies(*, control_port: int = 7236, max_throughput_mbps: int = 50) -> bytes:
    """Full WFDIEs blob as consumed by wpa_supplicant's global 'WFDIEs'
    D-Bus property (a plain byte array, no vendor-IE 0xDD/OUI wrapper --
    wpa_supplicant adds that framing itself when it beacons)."""
    return build_device_info_subelement(
        control_port=control_port,
        max_throughput_mbps=max_throughput_mbps,
    ) + build_coupled_sink_subelement()


def build_mice_hostname_subelement(hostname: str) -> bytes:
    """MS-MICE vendor extension subelement 0x2002: hostname, used when the
    sink also wants to advertise itself for Miracast-over-Infrastructure.
    Ported from newmice.py's capandhostmessage construction."""
    name_bytes = hostname.encode("utf-8")
    if len(name_bytes) > 0xFFFF:
        raise ValueError("hostname too long")
    return bytes([0x20, 0x02]) + len(name_bytes).to_bytes(2, "big") + name_bytes


def build_mice_ip_subelement(ip_address: str) -> bytes:
    """MS-MICE vendor extension subelement 0x2005: IP address string."""
    ip_bytes = ip_address.encode("utf-8")
    if len(ip_bytes) > 0xFFFF:
        raise ValueError("ip_address too long")
    return bytes([0x20, 0x05]) + len(ip_bytes).to_bytes(2, "big") + ip_bytes


def to_dbus_byte_array(data: bytes) -> list[int]:
    """Convert to the plain list-of-ints form that the D-Bus layer wraps in
    dbus.Byte/dbus.Array. Kept here so the hardware layer has zero encoding
    logic of its own -- it only wraps already-correct bytes."""
    return list(data)
