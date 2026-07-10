import pytest

from castd.sdnotify import build_notify_message, notify


def test_ready_message():
    assert build_notify_message(ready=True) == b"READY=1"


def test_watchdog_message():
    assert build_notify_message(watchdog=True) == b"WATCHDOG=1"


def test_combined_message_with_status():
    msg = build_notify_message(ready=True, watchdog=True, status="idle")
    assert msg == b"READY=1\nWATCHDOG=1\nSTATUS=idle"


def test_empty_call_rejected():
    with pytest.raises(ValueError):
        build_notify_message()


def test_notify_is_noop_without_notify_socket_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify(ready=True)  # must not raise even though there is no systemd
