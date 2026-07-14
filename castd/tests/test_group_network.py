"""Tests for castd.p2p.group_network -- command construction is pure logic
(runs anywhere); GroupNetwork's process management is tested against
recording fakes for subprocess.run/Popen, never a real dnsmasq."""
from __future__ import annotations

import subprocess

import pytest

from castd.p2p import group_network
from castd.p2p.group_network import (
    DHCP_RANGE_END,
    DHCP_RANGE_START,
    SINK_IP,
    GroupNetwork,
    build_dnsmasq_command,
    build_ip_assign_command,
    find_lease_ip,
)


def test_ip_assign_uses_replace_for_restart_idempotency():
    cmd = build_ip_assign_command("p2p-wlan1-4")
    assert cmd[:3] == ["ip", "addr", "replace"]
    assert f"{SINK_IP}/24" in cmd
    assert cmd[-2:] == ["dev", "p2p-wlan1-4"]


def test_dnsmasq_serves_dhcp_only_on_the_group_interface():
    cmd = build_dnsmasq_command("p2p-wlan1-4")
    assert cmd[0] == "dnsmasq"
    assert "--port=0" in cmd  # no DNS service
    assert "--interface=p2p-wlan1-4" in cmd
    assert "--bind-interfaces" in cmd  # never claims port 67 elsewhere
    assert "--keep-in-foreground" in cmd  # stays a castd child process
    assert "--conf-file=/dev/null" in cmd  # immune to /etc/dnsmasq.conf
    assert any(a.startswith(f"--dhcp-range={DHCP_RANGE_START},{DHCP_RANGE_END}") for a in cmd)


def test_dhcp_range_excludes_the_sink_address():
    start = int(DHCP_RANGE_START.rsplit(".", 1)[1])
    end = int(DHCP_RANGE_END.rsplit(".", 1)[1])
    sink = int(SINK_IP.rsplit(".", 1)[1])
    assert not (start <= sink <= end)


class FakePopen:
    def __init__(self, cmd):
        self.cmd = cmd
        self.terminated = False
        self._returncode: int | None = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode


@pytest.fixture
def fake_subprocess(monkeypatch):
    calls = {"run": [], "popen": []}

    def fake_run(cmd, check=False):
        calls["run"].append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    def fake_popen(cmd):
        proc = FakePopen(cmd)
        calls["popen"].append(proc)
        return proc

    monkeypatch.setattr(group_network.subprocess, "run", fake_run)
    monkeypatch.setattr(group_network.subprocess, "Popen", fake_popen)
    return calls


def test_start_assigns_ip_before_starting_dnsmasq(fake_subprocess):
    net = GroupNetwork()
    net.start("p2p-wlan1-0")
    assert fake_subprocess["run"] == [build_ip_assign_command("p2p-wlan1-0")]
    assert len(fake_subprocess["popen"]) == 1
    assert fake_subprocess["popen"][0].cmd == build_dnsmasq_command("p2p-wlan1-0")


def test_start_twice_same_interface_is_idempotent(fake_subprocess):
    net = GroupNetwork()
    net.start("p2p-wlan1-0")
    net.start("p2p-wlan1-0")
    assert len(fake_subprocess["popen"]) == 1


def test_start_on_new_interface_replaces_old_dnsmasq(fake_subprocess):
    net = GroupNetwork()
    net.start("p2p-wlan1-0")
    net.start("p2p-wlan1-1")
    assert len(fake_subprocess["popen"]) == 2
    assert fake_subprocess["popen"][0].terminated
    assert fake_subprocess["popen"][1].cmd == build_dnsmasq_command("p2p-wlan1-1")


def test_stop_terminates_dnsmasq(fake_subprocess):
    net = GroupNetwork()
    net.start("p2p-wlan1-0")
    net.stop()
    assert fake_subprocess["popen"][0].terminated


def test_stop_without_start_is_a_noop(fake_subprocess):
    GroupNetwork().stop()
    assert fake_subprocess["popen"] == []


# Real lease line shape from the 2026-07-14 capture: the Windows source's
# P2P interface MAC and hostname exactly as dnsmasq recorded them.
REAL_LEASE_LINE = "1784419513 12:5f:ad:5c:f4:13 192.168.173.93 DESKTOP-2NPNAR3 01:12:5f:ad:5c:f4:13\n"


def test_find_lease_ip_returns_ip_for_known_mac(tmp_path):
    lease_file = tmp_path / "leases"
    lease_file.write_text(REAL_LEASE_LINE)
    assert find_lease_ip("12:5f:ad:5c:f4:13", str(lease_file)) == "192.168.173.93"


def test_find_lease_ip_matches_mac_case_insensitively(tmp_path):
    lease_file = tmp_path / "leases"
    lease_file.write_text(REAL_LEASE_LINE)
    assert find_lease_ip("12:5F:AD:5C:F4:13", str(lease_file)) == "192.168.173.93"


def test_find_lease_ip_returns_none_when_file_missing(tmp_path):
    assert find_lease_ip("12:5f:ad:5c:f4:13", str(tmp_path / "nope")) is None


def test_find_lease_ip_returns_none_for_unknown_mac(tmp_path):
    lease_file = tmp_path / "leases"
    lease_file.write_text(REAL_LEASE_LINE)
    assert find_lease_ip("aa:bb:cc:dd:ee:ff", str(lease_file)) is None
