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
IFACE_WPS = IFACE_INTERFACE + ".WPS"

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
    interface and drives P2P Group Owner creation with a WFD IE and
    config_methods=display.

    Real-hardware testing found there is no such thing as a usable *fixed*
    WPS PIN for this flow: config_methods=display tells peers "ask this
    device to display a PIN", and wpa_supplicant generates a brand new
    random PIN for every single ProvisionDiscoveryRequestDisplayPin,
    regardless of the Pin= value this class's WPS.Start call sets up at
    group-start time (that call still runs, and still succeeds, but does
    not control this). The only way to actually complete pairing is to
    show the caller (via on_display_pin_needed) whatever PIN wpa_supplicant
    just generated, live, so a human can read and type it in."""

    def __init__(
        self,
        interface_name: str,
        *,
        device_name: str,
        freq_mhz: int,
        wps_pin: str,
        on_group_started: Callable[[GroupInfo], None],
        on_wps_failed: Callable[[str], None],
        on_display_pin_needed: Callable[[str], None],
        wpa_conf_path: str = DEFAULT_WPA_CONF_PATH,
    ) -> None:
        self.interface_name = interface_name
        self.device_name = device_name
        self.freq_mhz = freq_mhz
        self.wps_pin = wps_pin
        self.wpa_conf_path = wpa_conf_path
        self._on_group_started = on_group_started
        self._on_wps_failed = on_wps_failed
        self._on_display_pin_needed = on_display_pin_needed

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
        self.bus.add_signal_receiver(
            self._handle_display_pin_request,
            dbus_interface=IFACE_P2PDEVICE,
            signal_name="ProvisionDiscoveryRequestDisplayPin",
        )
        # Diagnostic-only: these were never subscribed to before, which
        # meant a real Windows negotiation attempt produced literally zero
        # log output regardless of what wpa_supplicant was actually doing
        # -- easy to mistake for "nothing is happening" when it was really
        # "we never asked to be told". Log every one generically so the
        # next real-hardware test shows the actual P2P/WPS negotiation
        # sequence instead of silence.
        for signal_name in (
            "DeviceFound",
            "DeviceFoundProperties",
            "DeviceLost",
            "GONegotiationRequest",
            "GONegotiationSuccess",
            "GONegotiationFailure",
            "ProvisionDiscoveryResponseDisplayPin",
            "ProvisionDiscoveryRequestEnterPin",
            "ProvisionDiscoveryResponseEnterPin",
            "ProvisionDiscoveryPBCRequest",
            "ProvisionDiscoveryPBCResponse",
            "ProvisionDiscoveryFailure",
            "GroupFormationFailure",
            "InvitationResult",
            "GroupFinished",
        ):
            self.bus.add_signal_receiver(
                self._make_signal_logger(signal_name), dbus_interface=IFACE_P2PDEVICE, signal_name=signal_name
            )
        # WPS.Event/Credentials are on a DIFFERENT interface (IFACE_WPS, not
        # IFACE_P2PDEVICE) and were never subscribed to either -- this is
        # exactly where the actual M1-M8 WPS message exchange progress
        # (or failure) after a peer submits a PIN would show up. Without
        # these, a real negotiation attempt going quiet after
        # ProvisionDiscoveryRequestDisplayPin is indistinguishable from
        # "the peer never tried" vs "the peer tried and failed inside WPS".
        for signal_name in ("Event", "Credentials", "PropertiesChanged"):
            self.bus.add_signal_receiver(
                self._make_signal_logger(f"WPS.{signal_name}"), dbus_interface=IFACE_WPS, signal_name=signal_name
            )

    def configure(self) -> None:
        self.props_iface.Set(
            IFACE_P2PDEVICE, "P2PDeviceConfig", dbus.Dictionary({"DeviceName": self.device_name}, signature="sv")
        )
        # WSC Primary Device Type: category=7 (Displays), OUI=00:50:F2:00,
        # sub-category=0. Real-hardware testing found the WFD subelems were
        # being broadcast correctly (confirmed via a second wpa_supplicant
        # instance's `wpa_cli p2p_peer` output) but Windows' Connect app
        # still would not list the device -- with PrimaryDeviceType unset,
        # `p2p_peer` reported "pri_dev_type=0-00000000-0" (category 0,
        # uncategorized/generic-computer). Windows' Miracast picker very
        # likely filters candidates by this WSC category to exclude other
        # P2P devices (printers, phones) that also advertise WFD-unrelated
        # P2P capability, so an unset category silently drops us from the
        # list even with correct WFD subelems. Byte values match the
        # PrimaryDeviceType this project's predecessor (lazycast's
        # newmice.py) set successfully.
        self.props_iface.Set(
            IFACE_P2PDEVICE,
            "P2PDeviceConfig",
            dbus.Dictionary(
                {
                    "PrimaryDeviceType": dbus.Array(
                        [dbus.Byte(b) for b in (0x00, 0x07, 0x00, 0x50, 0xF2, 0x00, 0x00, 0x00)],
                        signature=dbus.Signature("y"),
                    )
                },
                signature="sv",
            ),
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

    def set_wps_pin(self) -> None:
        # Real-hardware testing found neither the Group metadata object
        # (.../Groups/XX, from the GroupStarted signal) nor the interface
        # object reachable via P2PDevice's "Group" property implement the
        # WPS interface -- only wlan1's own top-level interface object
        # does (confirmed via full Introspect(): P2PDevice and WPS are
        # both implemented on that SAME object, not on any
        # group-specific object). Call Start on self.iface_obj directly.
        wps_iface = dbus.Interface(self.iface_obj, IFACE_WPS)
        wps_iface.Start(
            dbus.Dictionary(
                {
                    "Role": dbus.String("registrar"),
                    "Type": dbus.String("pin"),
                    "Pin": dbus.String(self.wps_pin),
                },
                signature="sv",
            )
        )

    def _make_signal_logger(self, signal_name: str) -> Callable[..., None]:
        def handler(*args: object) -> None:
            logger.info("P2P signal %s: %s", signal_name, args)

        return handler

    def _handle_display_pin_request(self, peer_object: str, pin: str) -> None:
        # This IS the actual PIN a human must type into the connecting
        # device -- see the class docstring. It is generated fresh by
        # wpa_supplicant for this specific negotiation attempt and is
        # unrelated to self.wps_pin.
        logger.info("WPS display PIN requested by %s: %s", peer_object, pin)
        self._on_display_pin_needed(str(pin))

    def _handle_group_started(self, properties: dict) -> None:
        group_object_path = properties["group_object"]
        logger.info("P2P group started: %s", group_object_path)
        info = GroupInfo(
            group_object_path=group_object_path,
            interface_name=self.interface_name,
            frequency_mhz=self.freq_mhz,
        )
        # Real-hardware testing found calling set_wps_pin() here -- WPS.Start
        # with a fixed Pin= -- actively BREAKS the display-PIN flow rather
        # than being merely useless: it registers self.wps_pin as the only
        # value the WPS registrar will accept, while wpa_supplicant
        # separately auto-generates and validates its OWN fresh PIN per
        # ProvisionDiscoveryRequestDisplayPin request. The two conflict, so
        # even a peer that correctly enters the displayed (live, correct)
        # PIN fails to authenticate. config_methods=display alone (set via
        # the config file at CreateInterface time) is sufficient for
        # wpa_supplicant to manage this on its own; do not call
        # set_wps_pin() here.
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
