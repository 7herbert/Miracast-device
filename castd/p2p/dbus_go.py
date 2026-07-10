"""wpa_supplicant D-Bus control for the P2P Group Owner.

Hardware-dependent module: requires `python3-dbus`, `python3-gi`, and a
running wpa_supplicant with the D-Bus interface enabled (dbus_ctrl_interface
in wpa_supplicant.conf) and P2P support compiled in. None of that is present
on a Windows dev box, so this module is syntax-checked (py_compile) only in
this repo's CI-less verification pass -- it has NOT been exercised against a
real wpa_supplicant. It is a direct structural port of the D-Bus calls
proven working in lazycast's newmice.py (GroupAdd, WFDIEs property,
GroupStarted/WpsFailed signals), replacing polling+wpa_cli text parsing with
native D-Bus signals so the failure modes documented in the project
retrospective (pgrep seeing a zombie process, ctrl-socket death going
undetected) cannot occur here: a lost D-Bus connection raises immediately
instead of returning stale-but-plausible data.

Needs real-hardware verification (Phase 0 of the project plan) before this
is trusted for anything beyond a bench test:
  - GO creation with WFDIEs set actually appears in Windows' Miracast device
    list within a normal discovery timeout
  - a fixed WPS PIN (config_methods=display / keypad, see NegotiationError-
    grade edge case wps_pin vs SHOW-PIN in the project's open-issues list)
    is honored rather than Windows falling back to a dynamically shown PIN
  - GO survives a 72-hour soak with the target USB adapter (see hardware
    trial plan) without the group silently disappearing
"""
from __future__ import annotations

import logging
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
    ) -> None:
        self.interface_name = interface_name
        self.device_name = device_name
        self.freq_mhz = freq_mhz
        self.wps_pin = wps_pin
        self._on_group_started = on_group_started
        self._on_wps_failed = on_wps_failed

        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        self.wpas_obj = self.bus.get_object(WPAS_SERVICE, WPAS_OPATH)
        self.wpas = dbus.Interface(self.wpas_obj, WPAS_SERVICE)

        try:
            self.iface_path = self.wpas.GetInterface(self.interface_name)
        except dbus.DBusException as exc:
            raise RuntimeError(
                f"wpa_supplicant does not know about interface {self.interface_name!r} "
                "(is it up and passed to wpa_supplicant -i?)"
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
        # config_methods bit for "display": the sink displays a PIN, the
        # peer (Windows) is prompted to type it in. This is the fix for the
        # open issue where Windows was instead driving a dynamically
        # generated SHOW-PIN flow.
        self.props_iface.Set(
            IFACE_P2PDEVICE,
            "P2PDeviceConfig",
            dbus.Dictionary({"ConfigMethods": dbus.String("display")}, signature="sv"),
        )

        wfd_bytes = build_wfd_ies()
        wfd_dbus_array = dbus.Array(
            [dbus.Byte(b) for b in to_dbus_byte_array(wfd_bytes)], signature=dbus.Signature("y")
        )
        dbus.Interface(self.wpas_obj, dbus_interface=dbus.PROPERTIES_IFACE).Set(
            WPAS_SERVICE, "WFDIEs", wfd_dbus_array
        )

    def start_group(self) -> None:
        try:
            self.p2p_iface.GroupAdd({"persistent": False, "frequency": dbus.Int32(self.freq_mhz)})
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
