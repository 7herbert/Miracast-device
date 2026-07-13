"""wpa_supplicant D-Bus control for the P2P Group Owner.

Hardware-dependent module: requires `python3-dbus`, `python3-gi`, and a
running wpa_supplicant with the D-Bus interface enabled. Not importable on
a Windows dev box (tests/conftest.py stubs it out for import-time checks
only). As of 2026-07-13, GetInterface/CreateInterface/configure/GroupAdd
have been confirmed working against a real Pi 4B + RTL8832BU (rtw89_8852bu)
adapter on Raspberry Pi OS Bookworm -- see the project memory notes for the
two real bugs real hardware surfaced that no static check could have caught:
Bookworm's wpa_supplicant.service must be attached to wlan1 at its own
startup (a drop-in override adding `-i wlan1`), and a rapid GroupAdd retry
loop (systemd's default RestartSec=3) wedged the driver into rejecting every
subsequent attempt with nl80211 "Device or resource busy" regardless of
D-Bus argument correctness.

Still needs real-hardware verification:
  - GO with WFDIEs set actually appears in Windows' Miracast device list
    within a normal discovery timeout (P2P group creation confirmed; WFD
    discovery from a real Windows client not yet confirmed)
  - a fixed WPS PIN (config_methods=display, set via the wpa_p2p.conf file
    loaded at interface-creation time) is honored rather than Windows
    falling back to a dynamically shown PIN
  - GO survives a 72-hour soak with the target USB adapter without the
    group silently disappearing
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GObject as gobject

from castd.p2p.wfd_ie import build_wfd_ies, to_dbus_byte_array

logger = logging.getLogger(__name__)

WPAS_SERVICE = "fi.w1.wpa_supplicant1"
WPAS_OPATH = "/fi/w1/wpa_supplicant1"
IFACE_INTERFACE = WPAS_SERVICE + ".Interface"
IFACE_P2PDEVICE = IFACE_INTERFACE + ".P2PDevice"

# Raspberry Pi OS (Bookworm) starts wpa_supplicant.service in control-only
# mode (ExecStart has no -i, just -u -s -O DIR=/run/wpa_supplicant): it owns
# the fi.w1.wpa_supplicant1 D-Bus name but knows about zero interfaces at
# boot. Only one process may ever hold that D-Bus name, so starting a
# second `wpa_supplicant -i wlan1 ...` process (as earlier debugging tried)
# just fails to register and does nothing. The correct way to attach wlan1
# is to ask the *existing* process to create it, via D-Bus CreateInterface
# -- see __init__ below. This config file supplies config_methods=display
# (fixed-PIN mode) at interface-creation time instead of trying to patch it
# in afterwards through a P2PDeviceConfig property whose key name/type this
# project has not been able to confirm against a real wpa_supplicant build.
DEFAULT_WPA_CONF_PATH = "/etc/wpa_supplicant/wpa_p2p.conf"


@dataclass
class GroupInfo:
    group_object_path: str
    interface_name: str
    frequency_mhz: int


class P2PGroupOwner:
    """Owns the D-Bus connection to wpa_supplicant for one wireless
    interface and drives P2P Group Owner creation with a fixed WFD IE and a
    fixed WPS PIN (config_methods=display so the Pi shows the PIN and
    Windows types it in, rather than the reverse)."""

    def __init__(
        self,
        interface_name: str,
        *,
        device_name: str,
        freq_mhz: int,
        wps_pin: str,
        on_group_started: Callable[[GroupInfo], None],
        on_wps_failed: Callable[[str], None],
        wpa_conf_path: str = DEFAULT_WPA_CONF_PATH,
    ) -> None:
        self.interface_name = interface_name
        self.device_name = device_name
        self.freq_mhz = freq_mhz
        self.wps_pin = wps_pin
        self.wpa_conf_path = wpa_conf_path
        self._on_group_started = on_group_started
        self._on_wps_failed = on_wps_failed

        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.wpas_obj = self.bus.get_object(WPAS_SERVICE, WPAS_OPATH)
        self.wpas = dbus.Interface(self.wpas_obj, WPAS_SERVICE)

        try:
            self.iface_path = self.wpas.GetInterface(self.interface_name)
        except dbus.DBusException:
            logger.info(
                "wpa_supplicant does not know about %s yet; creating it via D-Bus "
                "(this is normal on Raspberry Pi OS Bookworm's control-only "
                "wpa_supplicant.service)",
                self.interface_name,
            )
            create_args = dbus.Dictionary(
                {
                    "Ifname": dbus.String(self.interface_name),
                    "Driver": dbus.String("nl80211"),
                    "ConfigFile": dbus.String(self.wpa_conf_path),
                },
                signature="sv",
            )
            try:
                self.iface_path = self.wpas.CreateInterface(create_args)
            except dbus.DBusException as exc:
                raise RuntimeError(
                    f"could not create wpa_supplicant interface for {self.interface_name!r} "
                    f"(config file {self.wpa_conf_path!r}): {exc}"
                ) from exc

        self.iface_obj = self.bus.get_object(WPAS_SERVICE, self.iface_path)
        self.p2p_iface = dbus.Interface(self.iface_obj, IFACE_P2PDEVICE)
        self.props_iface = dbus.Interface(self.iface_obj, dbus_interface=dbus.PROPERTIES_IFACE)

        self.bus.add_signal_receiver(
            self._handle_group_started, dbus_interface=IFACE_P2PDEVICE, signal_name="GroupStarted"
        )
        self.bus.add_signal_receiver(
            self._handle_wps_failed, dbus_interface=IFACE_P2PDEVICE, signal_name="WpsFailed"
        )

    def configure(self) -> None:
        self.props_iface.Set(
            IFACE_P2PDEVICE, "P2PDeviceConfig", dbus.Dictionary({"DeviceName": self.device_name}, signature="sv")
        )
        # config_methods=display (fixed-PIN mode, not the SHOW-PIN flow) is
        # set via wpa_conf_path's config_methods= line, loaded when
        # CreateInterface runs in __init__ -- NOT set here as a
        # P2PDeviceConfig property. An earlier attempt to Set it as
        # {"ConfigMethods": ...} on this property raised
        # org.freedesktop.DBus.Error.InvalidArgs ("invalid message format"),
        # and this project has not confirmed the correct key
        # name/type against a real wpa_supplicant build, so it is not
        # attempted here.

        wfd_bytes = build_wfd_ies()
        wfd_dbus_array = dbus.Array(
            [dbus.Byte(b) for b in to_dbus_byte_array(wfd_bytes)], signature=dbus.Signature("y")
        )
        dbus.Interface(self.wpas_obj, dbus_interface=dbus.PROPERTIES_IFACE).Set(
            WPAS_SERVICE, "WFDIEs", wfd_dbus_array
        )

    def _existing_group_interface(self) -> str | None:
        """Detect an already-running P2P group via /sys/class/net instead of
        a D-Bus property. Real-hardware testing found that this build's
        wpa_supplicant returns the exact same generic
        "Did not receive correct message arguments" DBusException both for
        malformed GroupAdd args AND for "a group already exists on this
        interface" -- the two cases are indistinguishable from the
        exception alone, so a filesystem check sidesteps needing to trust
        that error text's meaning at all."""
        prefix = f"p2p-{self.interface_name}-"
        try:
            return next((name for name in os.listdir("/sys/class/net") if name.startswith(prefix)), None)
        except OSError:
            return None

    def get_group_interface_name(self) -> str | None:
        """Public accessor for the currently active P2P group interface
        name (e.g. "p2p-wlan1-4"). Callers that need the real ifname --
        UxPlay's -bindif, for one -- must call this after start_group()
        returns rather than hardcoding "p2p-wlan1-0": real-hardware testing
        found the numeric suffix increments with every wpa_supplicant-
        internal attempt (including ones that failed) and is not
        guaranteed to be 0."""
        return self._existing_group_interface()

    def start_group(self) -> None:
        existing = self._existing_group_interface()
        if existing is not None:
            logger.info("P2P group interface %s already exists; skipping GroupAdd", existing)
            return

        # Every value must be an explicit dbus type (not a bare Python bool
        # or int) for the a{sv} signature to marshal correctly -- passing
        # a plain `False`/int here is what previously raised
        # fi.w1.wpa_supplicant1.InvalidArgs: "Did not receive correct
        # message arguments."
        groupadd_args = dbus.Dictionary(
            {"persistent": dbus.Boolean(False), "frequency": dbus.Int32(self.freq_mhz)},
            signature="sv",
        )
        try:
            self.p2p_iface.GroupAdd(groupadd_args)
        except dbus.DBusException as exc:
            raise RuntimeError(f"GroupAdd failed on {self.interface_name}: {exc}") from exc

    def set_wps_pin(self, group_object_path: str) -> None:
        group_obj = self.bus.get_object(WPAS_SERVICE, group_object_path)
        group_iface = dbus.Interface(group_obj, WPAS_SERVICE + ".Group")
        group_iface.WpsPin("any", self.wps_pin)

    def _handle_group_started(self, properties: dict) -> None:
        group_object_path = properties["group_object"]
        logger.info("P2P group started: %s", group_object_path)
        info = GroupInfo(
            group_object_path=group_object_path,
            interface_name=self.interface_name,
            frequency_mhz=self.freq_mhz,
        )
        try:
            self.set_wps_pin(group_object_path)
        except dbus.DBusException:
            logger.exception("failed to set fixed WPS PIN on new group")
        self._on_group_started(info)

    def _handle_wps_failed(self, status, *rest) -> None:
        logger.warning("WPS authentication failed: status=%s extra=%s", status, rest)
        self._on_wps_failed(str(status))

    def run_forever(self) -> None:
        """Blocks running the GLib main loop that delivers the D-Bus
        signals above. Call from a dedicated thread; main.py's asyncio loop
        talks to this thread via thread-safe callbacks only."""
        gobject.threads_init()
        gobject.MainLoop().run()
