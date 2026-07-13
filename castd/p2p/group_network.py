"""P2P group interface L3 setup: sink IP + DHCP for the connecting source.

wpa_supplicant only takes the GO as far as 802.11 + WPS. The moment a
source (Windows) finishes the WPS handshake and associates, it sends DHCP
Discover on the new link -- and nothing on the Pi answers it: Raspberry Pi
OS Bookworm's NetworkManager/dhcpcd do not manage p2p-wlan1-N interfaces.
Without this module the connection dies right after WPS with the source
timing out on address acquisition -- one failure later in the chain than
the Selected Registrar stall fixed the same day in dbus_go.py.

The sink claims SINK_IP on the group interface (matching the 192.168.173.x
convention the RTSP tests already assume for the sink side), and dnsmasq --
run as a castd child process, NOT the system dnsmasq.service -- leases the
source an address in the same /24. Requires the dnsmasq binary
(`apt install dnsmasq`); disable the system-wide unit
(`systemctl disable --now dnsmasq`) so it does not race this one for
port 67 across interfaces.

Command construction is kept in standalone functions so it can be unit
tested without root, a group interface, or dnsmasq installed.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

SINK_IP = "192.168.173.1"
PREFIX_LEN = 24
DHCP_RANGE_START = "192.168.173.50"
DHCP_RANGE_END = "192.168.173.150"
DHCP_LEASE_TIME = "12h"
LEASE_FILE = "/run/castd-dnsmasq.leases"


def build_ip_assign_command(interface_name: str, sink_ip: str = SINK_IP, prefix_len: int = PREFIX_LEN) -> list[str]:
    # `replace`, not `add`: castd restarts against a still-existing group
    # must be idempotent, and `ip addr add` of an address that is already
    # present exits non-zero.
    return ["ip", "addr", "replace", f"{sink_ip}/{prefix_len}", "dev", interface_name]


def build_dnsmasq_command(interface_name: str) -> list[str]:
    return [
        "dnsmasq",
        "--keep-in-foreground",  # child process under castd, not a daemon
        "--conf-file=/dev/null",  # never inherit /etc/dnsmasq.conf surprises
        "--port=0",  # DHCP only; no DNS service at all
        f"--interface={interface_name}",
        "--bind-interfaces",  # port 67 on the group interface only
        f"--dhcp-range={DHCP_RANGE_START},{DHCP_RANGE_END},{DHCP_LEASE_TIME}",
        f"--dhcp-leasefile={LEASE_FILE}",
    ]


class GroupNetwork:
    """Owns the sink IP assignment and the dnsmasq child for one group
    interface. start() is idempotent while dnsmasq is alive on the same
    interface; the group interface name changes across wpa_supplicant
    group re-creations (p2p-wlan1-0, -1, ...), in which case start()
    tears the old child down and brings the new interface up."""

    def __init__(self) -> None:
        self._dnsmasq: subprocess.Popen | None = None
        self._interface_name: str | None = None

    def start(self, interface_name: str) -> None:
        if (
            self._dnsmasq is not None
            and self._dnsmasq.poll() is None
            and self._interface_name == interface_name
        ):
            return
        self.stop()
        subprocess.run(build_ip_assign_command(interface_name), check=True)
        self._dnsmasq = subprocess.Popen(build_dnsmasq_command(interface_name))
        self._interface_name = interface_name
        logger.info(
            "group network up on %s: sink=%s/%d dhcp=%s-%s",
            interface_name, SINK_IP, PREFIX_LEN, DHCP_RANGE_START, DHCP_RANGE_END,
        )

    def stop(self) -> None:
        if self._dnsmasq is not None:
            self._dnsmasq.terminate()
            try:
                self._dnsmasq.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._dnsmasq.kill()
                self._dnsmasq.wait()
            self._dnsmasq = None
        self._interface_name = None
