#!/usr/bin/env python3
"""castd entry point -- single systemd unit, single process, single event loop.

Replaces: run.sh, start-cast.sh, the desktop-autostart path, and lazycast's
patch-on-clone d2.py entirely. Everything the shell main loop used to do by
polling pgrep/iw output is now driven by explicit callbacks from
wpa_supplicant's D-Bus signals (castd.p2p.dbus_go) and by the FSM
(castd.fsm.state_machine), which is the single source of truth for "what is
this Pi doing right now" instead of that answer being implicit in which
processes happen to be alive.

Hardware-dependent: imports castd.p2p.dbus_go (needs python3-dbus/gi) and
spawns real subprocesses (gst-launch-1.0, uxplay). Not runnable on the
Windows dev box this was written on -- verified here with py_compile only.
Wiring correctness (does GO-started actually lead to a WFD connection
attempt, does AIRPLAY_CONNECTED actually pause Miracast discovery) is
exercised by castd/tests/test_main_wiring.py, which fakes the hardware
layer's callbacks and asserts the FSM + subprocess calls that result --
that test *does* run here, since it only imports castd.fsm and a fake.

Still needs real Pi/Windows/iPhone hardware before this is trusted:
  - the three Phase 0 experiments from the project plan (D-Bus GO + fixed
    PIN against real Windows, legacy-STA QR join + UxPlay against a real
    iPhone, GStreamer kmssink against the Pi 4's actual DRM/KMS setup)
  - a 72-hour P2P GO soak test on the target USB adapter
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from castd.airplay.uxplay import UxPlayConfig, UxPlayProcess
from castd.config import ConfigError, RoomConfig, parse_room_config
from castd.fsm.state_machine import Action, CastArbiter, Event, State
from castd.health import HealthState, serve_forever
from castd.p2p.dbus_go import GroupInfo, P2PGroupOwner
from castd.p2p.group_network import SINK_IP, GroupNetwork, find_lease_ip
from castd.render.gstreamer import RenderProcess, RenderTarget, build_idle_screen_pipeline, build_wfd_pipeline_description
from castd.render.idle_screen import render_idle_screen
from castd import sdnotify
from castd.wfdsink.rtsp import NegotiationError, WfdCapabilities, WfdNegotiator
from castd.wfdsink.session import RtspReader, listen_for_sources, negotiate, open_control_connection, run_steady_state

logger = logging.getLogger("castd")

RECEIVER_CONF_PATH = Path("/boot/receiver.conf")
IDLE_PNG_PATH = "/opt/castd/idle_screen.png"
WFD_UDP_PORT = 1028


class CastDaemon:
    def __init__(self, config: RoomConfig) -> None:
        self.config = config
        self.arbiter = CastArbiter()
        self.render = RenderProcess()
        self.render_target = RenderTarget()
        self.health = HealthState()
        self.group_network = GroupNetwork()
        # Constructed in start(), once the real P2P group interface name is
        # known -- see the comment there. Real-hardware testing found the
        # numeric suffix (p2p-wlan1-0, -4, -72, ...) is not stable, so it
        # cannot be hardcoded here at __init__ time.
        self.uxplay: UxPlayProcess | None = None
        self._rtsp_listener = None
        self._wifi_credentials: tuple[str, str] | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        serve_forever(self.health)
        self._show_idle_screen()
        sdnotify.notify(ready=True, status="idle")
        threading.Thread(target=self._watchdog_heartbeat_loop, daemon=True).start()

        self.p2p = P2PGroupOwner(
            "wlan1",
            device_name=self.config.device_name,
            freq_mhz=self.config.freq_mhz,
            on_group_started=self._on_group_started,
            on_wps_failed=self._on_wps_failed,
            on_display_pin_needed=self._on_display_pin_needed,
            on_station_authorized=self._on_station_authorized,
        )
        self.p2p.configure()
        self.p2p.start_group()

        group_ifname = self.p2p.get_group_interface_name()
        if group_ifname is None:
            raise RuntimeError("start_group() returned but no p2p-wlan1-* interface exists")
        # Covers BOTH startup paths: fresh GroupAdd (GroupStarted will also
        # fire and call this again, harmlessly -- both pieces are
        # idempotent) and castd restarting against a group that already
        # exists, where GroupStarted never re-fires.
        self._ensure_group_services(group_ifname)
        self._wifi_credentials = self.p2p.get_group_credentials()
        self._show_idle_screen()  # re-render now that the Wi-Fi footer is known
        self.uxplay = UxPlayProcess(
            UxPlayConfig(device_name=self.config.device_name),
            on_client_connected=self._on_airplay_connected,
            on_client_disconnected=self._on_airplay_disconnected,
            on_process_ended=self._on_uxplay_exited,
        )

        dbus_thread = threading.Thread(target=self.p2p.run_forever, daemon=True)
        dbus_thread.start()

        self.uxplay.start()

        # Real-hardware testing found run_forever() was called a SECOND
        # time here (on the main thread, after already starting it above
        # on dbus_thread) -- two GLib main loops pumping the same default
        # main context from different OS threads simultaneously. Synchronous
        # D-Bus calls (GroupAdd, property Set/Get) still worked because
        # they don't depend on main-loop-driven dispatch, which is why
        # every fix up to this point tested fine; but asynchronous P2P
        # signals arriving from an incoming peer's WPS negotiation
        # (GONegotiationRequest, ProvisionDiscoveryRequestEnterPin,
        # WpsFailed, etc.) were going through the contended/undefined-
        # behavior dual main loop and never reliably reaching our signal
        # handlers -- matching the symptom of Windows showing "connecting"
        # while castd's log showed zero new activity. Join the thread
        # instead of running a second main loop on this one.
        dbus_thread.join()

    def _on_group_started(self, info: GroupInfo) -> None:
        logger.info("group started on %s @ %d MHz", info.interface_name, info.frequency_mhz)
        group_ifname = self.p2p.get_group_interface_name()
        if group_ifname is None:
            logger.error("GroupStarted fired but no p2p group interface exists in /sys/class/net")
            return
        self._ensure_group_services(group_ifname)
        # A (re)created group means a fresh SSID/passphrase -- update the
        # AirPlay footer on the kiosk screen to match.
        self._wifi_credentials = self.p2p.get_group_credentials()
        self._show_idle_screen()

    def _ensure_group_services(self, group_ifname: str) -> None:
        """Everything a live GO needs beyond what wpa_supplicant provides:
        the sink IP + DHCP server (a source DHCPs immediately after WPS
        completes -- nothing else on the Pi would answer it), and the RTSP
        control listener the source then connects to on port 7236 (the port
        advertised in our WFD IE). Idempotent; called from both startup
        paths, see start()."""
        self.group_network.start(group_ifname)
        if self._rtsp_listener is None:
            self._rtsp_listener = listen_for_sources(SINK_IP)
            threading.Thread(target=self._accept_sources_loop, daemon=True).start()
            logger.info("RTSP sink listening on %s:7236", SINK_IP)

    def _on_airplay_connected(self) -> None:
        # Runs on UxPlayProcess's output-pump thread. The critical action
        # is STOP_RENDER_PIPELINE: releasing DRM master so UxPlay's own
        # kmssink can take the display -- without it a real iPhone got
        # "cannot connect" (2026-07-14).
        logger.info("AirPlay client connected; handing the display to UxPlay")
        with self._lock:
            transition = self.arbiter.handle(Event.AIRPLAY_CONNECTED)
        self._apply_actions(transition.actions)

    def _on_airplay_disconnected(self) -> None:
        logger.info("AirPlay client disconnected; reclaiming the display")
        with self._lock:
            transition = self.arbiter.handle(Event.AIRPLAY_DISCONNECTED)
        self._apply_actions(transition.actions)

    def _on_uxplay_exited(self) -> None:
        # uxplay died on its own (crashed mid-session on 2026-07-14 --
        # NOT castd stopping it around a Miracast session; that path sets
        # the expected-exit flag and never reaches here). Recover: drive
        # the FSM out of AIRPLAY so the idle screen comes back, then
        # relaunch so the room isn't left without AirPlay until the next
        # Miracast cycle happens to restart it.
        logger.warning("uxplay exited unexpectedly; recovering")
        if self.arbiter.state is State.AIRPLAY:
            self._on_airplay_disconnected()
        if self.uxplay is not None and not self.uxplay.is_running:
            time.sleep(2.0)  # keep a hard crash from looping tightly
            self.uxplay.start()

    def _on_station_authorized(self, mac: str) -> None:
        # Runs on the GLib signal thread; hand off immediately.
        threading.Thread(target=self._connect_to_authorized_source, args=(mac,), daemon=True).start()

    def _connect_to_authorized_source(self, mac: str, lease_timeout_s: float = 20.0) -> None:
        """A station just completed WPS and associated (StaAuthorized). It
        will DHCP within a couple of seconds, then LISTEN on its advertised
        RTSP port waiting for us -- the sink dials the source, settled by
        the 2026-07-14 capture (see session.open_control_connection). Poll
        dnsmasq's lease file for its IP, dial, negotiate."""
        if self.arbiter.state is State.MIRACAST:
            logger.info("already in a Miracast session; ignoring StaAuthorized for %s", mac)
            return
        deadline = time.monotonic() + lease_timeout_s
        source_ip = None
        while time.monotonic() < deadline:
            source_ip = find_lease_ip(mac)
            if source_ip:
                break
            time.sleep(0.5)
        if not source_ip:
            logger.warning("no DHCP lease for %s within %.0fs; cannot start RTSP", mac, lease_timeout_s)
            return
        logger.info("station %s leased %s; probing for a Miracast RTSP server", mac, source_ip)
        try:
            sock = open_control_connection(source_ip)
        except OSError as exc:
            # Not an error: every station that joins the group's Wi-Fi
            # lands here, and legacy clients (an iPhone joining for
            # AirPlay, a laptop joining by hand) run no RTSP server. A
            # real 2026-07-14 iPhone join was logged as a scary traceback
            # by an earlier revision of this path.
            logger.info(
                "no RTSP service at %s:7236 (%s); treating %s as a legacy/AirPlay client, not a Miracast source",
                source_ip, exc, mac,
            )
            return
        self._run_source_session(source_ip, sock)

    def _run_source_session(self, source_ip: str, sock) -> None:
        """One source's whole session lifetime, on the current thread:
        M1-M7 handshake, then pump the control channel until the source
        leaves, then FSM back to IDLE. One RtspReader owns the socket's
        inbound side across both phases so a message coalesced over the
        handshake/steady-state boundary is not lost."""
        reader = RtspReader(sock)
        negotiator = self.handle_miracast_connected(source_ip, sock, reader=reader)
        if negotiator is not None:
            self._pump_control_channel(sock, negotiator, reader=reader)

    def _pump_control_channel(self, sock, negotiator: WfdNegotiator, reader: RtspReader | None = None) -> None:
        try:
            run_steady_state(sock, negotiator, reader=reader)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            logger.info("RTSP control channel closed; tearing down Miracast session")
            self.handle_miracast_disconnected()

    def _accept_sources_loop(self) -> None:
        while True:
            try:
                sock, (source_ip, source_port) = self._rtsp_listener.accept()
            except OSError:
                logger.info("RTSP listener closed; accept loop exiting")
                return
            logger.info("RTSP control connection from %s:%d", source_ip, source_port)
            threading.Thread(
                target=self._run_source_session, args=(source_ip, sock), daemon=True
            ).start()

    def _on_wps_failed(self, status: str) -> None:
        logger.warning("WPS failed: %s", status)

    def _show_idle_screen(self, pin: str | None = None) -> None:
        # Real-hardware testing found the WPS "display PIN" wpa_supplicant
        # generates is different on every negotiation attempt (see
        # p2p/dbus_go.py's module docstring) -- there is no fixed PIN to
        # bake into a static image once at startup. This regenerates the
        # PNG and restarts the render pipeline every time a peer requests
        # one, so whatever is on screen is always the PIN that will
        # actually work right now.
        ssid, password = self._wifi_credentials or (None, None)
        render_idle_screen(
            IDLE_PNG_PATH, room_name=self.config.room_name, pin=pin, wifi_ssid=ssid, wifi_password=password
        )
        if self.render.is_running:
            self.render.stop()
        self.render.start(build_idle_screen_pipeline(png_path=IDLE_PNG_PATH, target=self.render_target))

    def _on_display_pin_needed(self, pin: str) -> None:
        logger.info("displaying live WPS PIN on kiosk screen: %s", pin)
        self._show_idle_screen(pin=pin)

    def _watchdog_heartbeat_loop(self, interval_s: float = 10.0) -> None:
        # WatchdogSec=30 in castd.service; ping at 1/3 that interval so a
        # single missed tick from GC pause or a slow D-Bus call never
        # trips a spurious reboot, but a truly wedged main loop still gets
        # caught well within one watchdog period.
        import time

        while True:
            sdnotify.notify(watchdog=True, status=self.arbiter.state.name)
            time.sleep(interval_s)

    def handle_miracast_connected(self, source_ip: str, sock, reader: RtspReader | None = None) -> WfdNegotiator | None:
        """Run the WFD M1-M7 handshake over an RTSP control connection to
        the source. Returns the session's negotiator on success -- the
        caller MUST then keep pumping the control channel with it (see
        _pump_control_channel); dropping the socket ends the session from
        the source's point of view. Returns None on failure (FSM already
        driven back to IDLE)."""
        with self._lock:
            transition = self.arbiter.handle(Event.MIRACAST_CONNECTED)
        self._apply_actions(transition.actions)
        # audio_codec must match what the render pipeline actually decodes
        # (aacparse ! avdec_aac -- see render/gstreamer.py). The default
        # WfdCapabilities value is LPCM, and advertising that while
        # decoding AAC means Windows ships LPCM PES packets the audio
        # branch can't parse, killing the whole pipeline mid-session.
        # Video capability defaults to 1080p30 native: 1080p60 froze the
        # Pi 4 decode path after the first frames (2026-07-14) -- see
        # rtsp.py's _build_capability_body for the full story.
        negotiator = WfdNegotiator(WfdCapabilities(device_name=self.config.device_name, audio_codec="AAC"))
        try:
            # Bounded handshake: an un-timed-out recv() hanging the whole
            # session forever was d2.py's original bug (#15 in the project
            # retrospective). run_steady_state sets its own keep-alive
            # timeout afterwards.
            if hasattr(sock, "settimeout"):
                sock.settimeout(15.0)
            session = negotiate(
                sock, source_ip=source_ip, capabilities=negotiator.capabilities, negotiator=negotiator, reader=reader
            )
            self.render.stop()
            self.render.start(build_wfd_pipeline_description(udp_port=WFD_UDP_PORT, target=self.render_target))
            logger.info("Miracast streaming started, session=%s", session.session_id)
            return negotiator
        except (NegotiationError, OSError):
            logger.exception("Miracast negotiation failed for %s", source_ip)
            self.handle_miracast_disconnected()
            return None

    def handle_miracast_disconnected(self) -> None:
        with self._lock:
            transition = self.arbiter.handle(Event.MIRACAST_DISCONNECTED)
        self._apply_actions(transition.actions)

    def _apply_actions(self, actions) -> None:
        for action in actions:
            if action is Action.STOP_RENDER_PIPELINE:
                self.render.stop()
            elif action is Action.SHOW_IDLE_SCREEN:
                self._show_idle_screen()
            elif action is Action.PAUSE_AIRPLAY_ADVERTISING:
                self.uxplay.stop()
            elif action is Action.RESUME_AIRPLAY_ADVERTISING:
                self.uxplay.start()
            elif action is Action.FORCE_TEARDOWN_MIRACAST:
                logger.warning("watchdog timeout: forcing Miracast teardown")
            elif action is Action.FORCE_TEARDOWN_AIRPLAY:
                logger.warning("watchdog timeout: forcing AirPlay teardown")
            elif action in (Action.PAUSE_MIRACAST_DISCOVERY, Action.RESUME_MIRACAST_DISCOVERY):
                # Phase 2 gap: pausing P2P discoverability while AirPlay is
                # presenting needs a wpa_supplicant D-Bus call not yet
                # implemented in p2p/dbus_go.py. Logged explicitly instead
                # of silently dropped so this doesn't look "handled".
                logger.info("%s requested but not yet implemented", action.name)
        self.health.set_state(self.arbiter.state)
        self.health.heartbeat()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        config = parse_room_config(RECEIVER_CONF_PATH.read_text())
    except (ConfigError, OSError) as exc:
        logger.error("cannot start: %s", exc)
        return 1

    daemon = CastDaemon(config)
    daemon.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
