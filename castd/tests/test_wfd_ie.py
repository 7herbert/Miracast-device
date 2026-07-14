import pytest

from castd.p2p.wfd_ie import (
    WFD_PRIMARY_SINK,
    WFD_SOURCE,
    build_associated_bssid_subelement,
    build_coupled_sink_subelement,
    build_device_info_subelement,
    build_mice_hostname_subelement,
    build_mice_ip_subelement,
    build_wfd_ies,
    to_dbus_byte_array,
)


def test_device_info_subelement_structure():
    data = build_device_info_subelement(control_port=7236, max_throughput_mbps=50)
    assert data[0] == 0  # subelement id
    length = int.from_bytes(data[1:3], "big")
    assert length == 6
    assert len(data) == 3 + length
    bitmap = int.from_bytes(data[3:5], "big")
    assert bitmap & 0b11 == WFD_PRIMARY_SINK
    port = int.from_bytes(data[5:7], "big")
    assert port == 7236
    throughput = int.from_bytes(data[7:9], "big")
    assert throughput == 50


def test_device_info_defaults_to_primary_sink_and_available():
    data = build_device_info_subelement()
    bitmap = int.from_bytes(data[3:5], "big")
    assert bitmap & 0b11 == WFD_PRIMARY_SINK
    assert bitmap & (1 << 4)  # WFD_AVAILABLE_FOR_SESSION


def test_session_availability_field_is_exactly_01_not_reserved():
    # Bits 4-5 are ONE two-bit field: 01 = available, 11 = reserved. A
    # previous revision set both bits (mislabeling bit 5) and a real
    # Windows 11 source completed WPS+DHCP but never opened RTSP against
    # that reserved value. Lock the field to the only valid "available"
    # encoding.
    data = build_device_info_subelement()
    bitmap = int.from_bytes(data[3:5], "big")
    availability = (bitmap >> 4) & 0b11
    assert availability == 0b01


def test_device_info_claims_no_unimplemented_capabilities():
    # WSD (bit 6), TDLS preference (bit 7), content protection (bit 8),
    # and time sync (bit 9) are promises the RTSP layer does not honor --
    # none may be advertised.
    data = build_device_info_subelement()
    bitmap = int.from_bytes(data[3:5], "big")
    assert bitmap & 0b1111000000 == 0


def test_device_info_can_be_built_as_source_type():
    data = build_device_info_subelement(device_type=WFD_SOURCE)
    bitmap = int.from_bytes(data[3:5], "big")
    assert bitmap & 0b11 == WFD_SOURCE


@pytest.mark.parametrize("bad_port", [-1, 0x10000, 100000])
def test_control_port_out_of_range_rejected(bad_port):
    with pytest.raises(ValueError):
        build_device_info_subelement(control_port=bad_port)


def test_coupled_sink_subelement_id_and_length():
    data = build_coupled_sink_subelement()
    assert data[0] == 6
    length = int.from_bytes(data[1:3], "big")
    assert length == 7
    assert len(data) == 3 + length


def test_associated_bssid_requires_six_bytes():
    with pytest.raises(ValueError):
        build_associated_bssid_subelement(b"\x00" * 5)
    data = build_associated_bssid_subelement(b"\xaa\xbb\xcc\xdd\xee\xff")
    assert data[0] == 1
    assert data[3:] == b"\xaa\xbb\xcc\xdd\xee\xff"


def test_build_wfd_ies_concatenates_device_info_and_coupled_sink():
    blob = build_wfd_ies(control_port=7236, max_throughput_mbps=50)
    device_info = build_device_info_subelement(control_port=7236, max_throughput_mbps=50)
    coupled_sink = build_coupled_sink_subelement()
    assert blob == device_info + coupled_sink


def test_mice_hostname_subelement_roundtrip():
    data = build_mice_hostname_subelement("MR-3F-A")
    assert data[:2] == bytes([0x20, 0x02])
    length = int.from_bytes(data[2:4], "big")
    assert length == len("MR-3F-A")
    assert data[4:].decode() == "MR-3F-A"


def test_mice_ip_subelement_roundtrip():
    data = build_mice_ip_subelement("192.168.173.1")
    assert data[:2] == bytes([0x20, 0x05])
    assert data[4:].decode() == "192.168.173.1"


def test_to_dbus_byte_array_is_plain_int_list():
    result = to_dbus_byte_array(b"\x00\x01\xff")
    assert result == [0, 1, 255]
    assert all(isinstance(b, int) for b in result)
