import pytest

from castd.config import ConfigError, is_valid_wps_pin, parse_room_config, wps_pin_checksum


def test_parses_valid_config():
    text = """
    room_name = MR-3F-A
    wps_pin = 12345670
    passphrase = correcthorsebattery
    channel = 36
    """
    cfg = parse_room_config(text)
    assert cfg.room_name == "MR-3F-A"
    assert cfg.wps_pin == "12345670"
    assert cfg.channel == 36
    assert cfg.freq_mhz == 5180
    assert cfg.device_name == "MR-3F-A"


def test_ignores_blank_lines_and_comments():
    text = """
    # this is a comment

    room_name = MR-1F-B
    wps_pin = 00000000
    passphrase = anotherpassphrase
    channel = 44
    """
    cfg = parse_room_config(text)
    assert cfg.room_name == "MR-1F-B"
    assert cfg.channel == 44


@pytest.mark.parametrize("channel", [36, 40, 44, 48])
def test_all_non_dfs_channels_accepted(channel):
    text = f"room_name=X\nwps_pin=12345670\npassphrase=abcdefgh\nchannel={channel}\n"
    cfg = parse_room_config(text)
    assert cfg.channel == channel


@pytest.mark.parametrize("channel", [1, 6, 11, 52, 100, 149])
def test_dfs_and_2ghz_channels_rejected(channel):
    text = f"room_name=X\nwps_pin=12345670\npassphrase=abcdefgh\nchannel={channel}\n"
    with pytest.raises(ConfigError, match="non-DFS"):
        parse_room_config(text)


def test_missing_required_key_reports_which_one():
    text = "room_name=X\nwps_pin=12345670\nchannel=36\n"
    with pytest.raises(ConfigError, match="passphrase"):
        parse_room_config(text)


@pytest.mark.parametrize("bad_pin", ["1234567", "123456789", "abcdefgh", ""])
def test_invalid_wps_pin_rejected(bad_pin):
    text = f"room_name=X\nwps_pin={bad_pin}\npassphrase=abcdefgh\nchannel=36\n"
    with pytest.raises(ConfigError, match="wps_pin"):
        parse_room_config(text)


@pytest.mark.parametrize("bad_pass", ["short", "x" * 64])
def test_invalid_passphrase_length_rejected(bad_pass):
    text = f"room_name=X\nwps_pin=12345670\npassphrase={bad_pass}\nchannel=36\n"
    with pytest.raises(ConfigError, match="passphrase"):
        parse_room_config(text)


def test_unparseable_line_reports_line_number():
    text = "room_name=X\nthis is not key=value\nwps_pin=12345670\npassphrase=abcdefgh\nchannel=36\n"
    with pytest.raises(ConfigError, match="line 2"):
        parse_room_config(text)


def test_non_integer_channel_rejected():
    text = "room_name=X\nwps_pin=12345670\npassphrase=abcdefgh\nchannel=fortyfour\n"
    with pytest.raises(ConfigError, match="channel must be an integer"):
        parse_room_config(text)


@pytest.mark.parametrize(
    "prefix,expected_checksum",
    [
        (3141592, 7),  # real-hardware bug: 31415926 (checksum 6) was silently
        # rejected during WPS pairing; 7 is the correct digit for this prefix.
        (1234567, 0),
        (0, 0),
    ],
)
def test_wps_pin_checksum_matches_known_values(prefix, expected_checksum):
    assert wps_pin_checksum(prefix) == expected_checksum


def test_is_valid_wps_pin_accepts_correct_checksum():
    assert is_valid_wps_pin("31415927")
    assert is_valid_wps_pin("12345670")


def test_is_valid_wps_pin_rejects_wrong_checksum():
    assert not is_valid_wps_pin("31415926")


def test_config_rejects_wps_pin_with_invalid_checksum():
    text = "room_name=X\nwps_pin=31415926\npassphrase=abcdefgh\nchannel=36\n"
    with pytest.raises(ConfigError, match="checksum") as exc_info:
        parse_room_config(text)
    assert "31415927" in str(exc_info.value)


def test_config_accepts_wps_pin_with_correct_checksum():
    text = "room_name=X\nwps_pin=31415927\npassphrase=abcdefgh\nchannel=36\n"
    cfg = parse_room_config(text)
    assert cfg.wps_pin == "31415927"
