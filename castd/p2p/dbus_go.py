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

Confirmed working since then: the GO appears in Windows' Win+K list (after
the PrimaryDeviceType category-7 fix in configure()), and Provision
Discovery request/response completes with Windows displaying the PIN this
class reports via on_display_pin_needed.

Still needs real-hardware verification:
  - the per-attempt registrar authorization (authorize_display_pin, added
    2026-07-13) actually carries Windows through 802.11 association and
    the WPS M1-M8 exchange end to end
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
    random PIN for every single ProvisionDiscoveryRequestDisplayPin.
    Completing a pairing therefore takes two actions per attempt, both
    driven from that signal's handler:
      1. show the generated PIN to the human (on_display_pin_needed), and
      2. authorize that same PIN on the GROUP interface's WPS registrar
         (authorize_display_pin) so the GO's beacons/probe responses gain
         the Selected Registrar attribute.
    Skipping step 2 produces the exact stall captured live on 2026-07-13:
    after the user clicks Connect, Windows re-probes the group SSID every
    1-2 s waiting for Selected Registrar to appear, never sends a single
    Authentication/Association frame, and eventually gives up with WCN
    "operation cancelled". A full unfiltered wpa_supplicant -dd trace of
    one attempt showed probe request/response traffic only, and the probe
    responses' WPS IE carried neither Selected Registrar (attr 0x1041) nor
    Device Password ID (attr 0x1012) -- the two attributes an armed
    registrar adds, and the ones Windows' WCN enrollee polls for."""

    def __init__(
        self,
        interface_name: str,
        *,
        device_name: str,
        freq_mhz: int,
        on_group_started: Callable[[GroupInfo], None],
        on_wps_failed: Callable[[str], None],
        on_display_pin_needed: Callable[[str], None],
        on_station_authorized: Callable[[str], None] | None = None,
        wpa_conf_path: str = DEFAULT_WPA_CONF_PATH,
    ) -> None:
        self.interface_name = interface_name
        self.device_name = device_name
        self.freq_mhz = freq_mhz
        self.wpa_conf_path = wpa_conf_path
        self._on_station_authorized = on_station_authorized
        # D-Bus object path of the active group interface (p2p-wlan1-N),
        # captured from GroupStarted; resolved lazily via GetInterface when
        # castd restarts against an already-existing group (GroupStarted
        # never re-fires in that case). See _group_interface_object().
        self._group_iface_path: str | None = None
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
        # AP-side association: the group interface (GO = AP mode) emits
        # StaAuthorized when a station completes association + WPS/4-way
        # handshake. This is the trigger for the whole Miracast session:
        # the authorized station will DHCP within seconds and then WAIT for
        # the sink to dial its advertised RTSP port (see main.py's
        # _connect_to_authorized_source).
        self.bus.add_signal_receiver(
            self._handle_sta_authorized, dbus_interface=IFACE_INTERFACE, signal_name="StaAuthorized"
        )
        self.bus.add_signal_receiver(
            self._make_signal_logger("StaDeauthorized"), dbus_interface=IFACE_INTERFACE, signal_name="StaDeauthorized"
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

    def _group_interface_object(self):
        """D-Bus object for the P2P *group* interface (p2p-wlan1-N), not the
        parent device interface. The WPS registrar whose state is reflected
        in the GO's beacon/probe-response WPS IE lives on the group
        interface: an earlier iteration called WPS.Start on wlan1's own
        object instead (that call "succeeded") but the frames Windows was
        actually watching never gained Selected Registrar, which is part of
        why the fixed-PIN experiment misled this project into deleting the
        registrar call entirely rather than re-targeting it."""
        path = self._group_iface_path
        if path is None:
            ifname = self._existing_group_interface()
            if ifname is None:
                raise RuntimeError("no P2P group interface exists to arm a WPS registrar on")
            path = self.wpas.GetInterface(ifname)
        return self.bus.get_object(WPAS_SERVICE, path)

    def authorize_display_pin(self, pin: str) -> None:
        """Arm the GO's WPS registrar with the PIN wpa_supplicant just
        generated for one ProvisionDiscoveryRequestDisplayPin attempt.
        This is what flips Selected Registrar=TRUE (+ Device Password ID)
        in the group's beacons/probe responses -- the signal Windows' WCN
        flow polls for after the user clicks Connect. Without this call the
        peer never even attempts 802.11 association (observed live on
        2026-07-13; see the class docstring)."""
        wps_iface = dbus.Interface(self._group_interface_object(), IFACE_WPS)
        wps_iface.Start(
            dbus.Dictionary(
                {
                    "Role": dbus.String("registrar"),
                    "Type": dbus.String("pin"),
                    "Pin": dbus.String(pin),
                },
                signature="sv",
            )
        )

    def _make_signal_logger(self, signal_name: str) -> Callable[..., None]:
        def handler(*args: object) -> None:
            logger.info("P2P signal %s: %s", signal_name, args)

        return handler

    def _handle_display_pin_request(self, peer_object: str, pin: str) -> None:
        # This IS the actual PIN for this specific negotiation attempt,
        # generated fresh by wpa_supplicant -- see the class docstring.
        logger.info("WPS display PIN requested by %s: %s", peer_object, pin)
        try:
            self.authorize_display_pin(str(pin))
            logger.info("WPS registrar armed with display PIN on group interface")
        except (dbus.DBusException, RuntimeError):
            # Still show the PIN below: a blank screen would hide the one
            # piece of live state a human in the room can act on, and the
            # exception text in the journal is the actual diagnostic.
            logger.exception("failed to arm WPS registrar with display PIN")
        self._on_display_pin_needed(str(pin))

    def _handle_group_started(self, properties: dict) -> None:
        group_object_path = properties["group_object"]
        # interface_object is the D-Bus path of the group interface itself
        # (p2p-wlan1-N). authorize_display_pin must target THAT interface's
        # WPS registrar, so capture it here where wpa_supplicant hands it
        # to us for free instead of re-resolving it per attempt.
        self._group_iface_path = str(properties.get("interface_object", "")) or None
        logger.info("P2P group started: %s (interface object %s)", group_object_path, self._group_iface_path)
        info = GroupInfo(
            group_object_path=group_object_path,
            interface_name=self.interface_name,
            frequency_mhz=self.freq_mhz,
        )
        # Deliberately NO WPS.Start here: a fixed PIN registered at
        # group-start time is wrong twice over (wrong PIN -- wpa_supplicant
        # generates a fresh one per attempt -- and wrong moment). The
        # per-attempt registrar arming lives in _handle_display_pin_request.
        self._on_group_started(info)

    def _handle_sta_authorized(self, mac, *rest) -> None:
        logger.info("station authorized on group interface: %s", mac)
        if self._on_station_authorized is not None:
            self._on_station_authorized(str(mac))

    def _handle_wps_failed(self, status, *rest) -> None:
        logger.warning("WPS authentication failed: status=%s extra=%s", status, rest)
        self._on_wps_failed(str(status))

    def run_forever(self) -> None:
        """Blocks running the GLib main loop that delivers the D-Bus
        signals above. Call from a dedicated thread; main.py's asyncio loop
        talks to this thread via thread-safe callbacks only."""
        gobject.threads_init()
        gobject.MainLoop().run()
