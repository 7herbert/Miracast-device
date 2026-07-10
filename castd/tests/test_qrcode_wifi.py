import pytest

from castd.airplay.qrcode_wifi import build_wifi_qr_payload


def test_basic_payload_shape():
    payload = build_wifi_qr_payload(ssid="MR-3F-A", passphrase="correcthorse")
    assert payload == "WIFI:T:WPA;S:MR-3F-A;P:correcthorse;;"


def test_hidden_network_flag():
    payload = build_wifi_qr_payload(ssid="MR-3F-A", passphrase="correcthorse", hidden=True)
    assert "H:true;" in payload
    assert payload.endswith(";;")


@pytest.mark.parametrize(
    "raw,escaped",
    [
        ("pass;word", "pass\\;word"),
        ("pass,word", "pass\\,word"),
        ('pass"word', 'pass\\"word'),
        ("pass:word", "pass\\:word"),
        ("pass\\word", "pass\\\\word"),
    ],
)
def test_special_characters_in_passphrase_are_escaped(raw, escaped):
    payload = build_wifi_qr_payload(ssid="Room", passphrase=raw)
    assert f"P:{escaped};" in payload


def test_special_characters_in_ssid_are_escaped():
    payload = build_wifi_qr_payload(ssid="Room;1,Floor", passphrase="abcdefgh")
    assert "S:Room\\;1\\,Floor;" in payload


def test_backslash_escaped_before_other_chars_to_avoid_double_escaping():
    # A literal backslash followed by a semicolon must become \\\; not \\;
    # (which a scanner would parse as an escaped semicolon, losing the
    # original backslash entirely).
    payload = build_wifi_qr_payload(ssid="Room", passphrase="ab\\;cdef")
    assert "P:ab\\\\\\;cdef;" in payload


def test_empty_ssid_rejected():
    with pytest.raises(ValueError):
        build_wifi_qr_payload(ssid="", passphrase="abcdefgh")


@pytest.mark.parametrize("bad_pass", ["short", "x" * 64])
def test_passphrase_length_validated(bad_pass):
    with pytest.raises(ValueError):
        build_wifi_qr_payload(ssid="Room", passphrase=bad_pass)
