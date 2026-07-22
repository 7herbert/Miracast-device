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
import os
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
from castd.render.framebuffer import paint_framebuffer
from castd.render.gstreamer import RenderProcess, RenderTarget, build_wfd_pipeline_description
from castd.render.idle_screen import render_idle_screen
from castd import sdnotify
from castd.stream_watchdog import StreamWatchdog, read_interface_rx_bytes
from castd.wfdsink.rtsp import NegotiationError, WfdCapabilities, WfdNegotiator
from castd.wfdsink.session import RtspReader, listen_for_sources, negotiate, open_control_connection, run_steady_state

logger = logging.getLogger("castd")

# The config is meant to be editable by mounting the SD card's FAT boot
# partition on any computer (see config.py). On Raspberry Pi OS Bookworm
# that partition is mounted at /boot/firmware; only on older (Bullseye)
# images is it at /boot. Try the Bookworm path first so editing the FAT
# partition actually takes effect, and fall back to the legacy path.
RECEIVER_CONF_CANDIDATES = (
    Path("/boot/firmware/receiver.conf"),
    Path("/boot/receiver.conf"),
)


def find_receiver_conf() -> Path:
    for path in RECEIVER_CONF_CANDIDATES:
        if path.exists():
            return path
    return RECEIVER_CONF_CANDIDATES[0]  # Bookworm default, for the not-found error


IDLE_PNG_PATH = "/opt/castd/idle_screen.png"
WFD_UDP_PORT = 1028
# How many times to re-roll a render pipeline that cold-start-froze (no first
# frame). Each re-roll has ~85% chance of clearing the race, so 3 drives the
# residual freeze rate from ~15% to well under 1%.
RENDER_COLD_START_RETRIES = 3


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
        self._active_control_sock = None
        # See _stream_watch_loop: the FSM flips to MIRACAST the instant a
        # source connects, but self.render.start() for THIS session's WFD
        # pipeline only happens after the M1-M7 RTSP handshake finishes,
        # which is legitimately a 1-3s+ round trip. Checking render.is_
        # running before that point is a guaranteed false positive, not
        # an edge case -- every single connection raced it after the idle
        # screen stopped running a persistent RenderProcess pipeline
        # (2026-07-15's framebuffer rewrite closed the "leftover idle
        # pipeline still technically running" gap that had been
        # accidentally masking this). Real fix: only arm the "is it still
        # running" check once it has actually started.
        self._render_pipeline_expected = False
        self._lock = threading.Lock()

    def start(self) -> None:
        serve_forever(self.health)
        self._show_idle_screen()
        sdnotify.notify(ready=True, status="idle")
        threading.Thread(target=self._watchdog_heartbeat_loop, daemon=True).start()

        # Which radio hosts the P2P Group Owner. Defaults to the external
        # adapter (wlan1). CASTD_P2P_INTERFACE overrides it -- used to A/B
        # the Pi 4's built-in wlan0 against the external card without
        # editing code (2026-07-22: testing whether the built-in brcmfmac
        # chip can host a reliable Miracast GO and let us drop the dongle).
        p2p_ifname = os.environ.get("CASTD_P2P_INTERFACE", "wlan1")
        logger.info("bringing up P2P Group Owner on %s", p2p_ifname)
        self.p2p = P2PGroupOwner(
            p2p_ifname,
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
            raise RuntimeError(f"start_group() returned but no p2p-{p2p_ifname}-* interface exists")
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
        threading.Thread(target=self._stream_watch_loop, daemon=True).start()

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
        self._active_control_sock = sock
        try:
            negotiator = self.handle_miracast_connected(source_ip, sock, reader=reader)
            if negotiator is not None:
                self._pump_control_channel(sock, negotiator, reader=reader)
        finally:
            self._active_control_sock = None

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

    def _stream_watch_loop(self, interval_s: float = 2.0) -> None:
        """Auto-recovery for a Miracast session whose stream died while
        its RTSP control channel stayed up (seen live 2026-07-15: first
        session after boot froze on frame one and needed a manual
        reconnect). Two death signals: the render process exiting, and
        the group interface's rx counter flatlining (see
        stream_watchdog.py). Recovery = close the session's control
        socket, which funnels through the exact same clean teardown as a
        normal source disconnect.

        The render-process check is gated on _render_pipeline_expected,
        which only becomes true once handle_miracast_connected actually
        calls self.render.start() for this session. Without that gate
        this fired on essentially every connection attempt (2026-07-21):
        the FSM enters MIRACAST the instant a source connects, but the
        WFD render pipeline doesn't start until the M1-M7 RTSP handshake
        finishes -- a legitimate 1-3s+ window where render.is_running is
        correctly False, not evidence of anything dying. A watchdog tick
        landing in that window closed the session's control socket out
        from under the negotiation still in progress, surfacing as
        "Bad file descriptor" mid-handshake and a session that looked
        like it could never connect at all."""
        watchdog = StreamWatchdog()
        while True:
            time.sleep(interval_s)
            self._stream_watch_tick(watchdog)

    def _stream_watch_tick(self, watchdog: StreamWatchdog) -> None:
        """One sampling pass, split out from _stream_watch_loop so the
        gating logic (not just the sleep-forever wrapper) is directly
        unit-testable."""
        if self.arbiter.state is not State.MIRACAST:
            watchdog.reset()
            self._render_pipeline_expected = False
            return
        if not self._render_pipeline_expected:
            return
        if not self.render.is_running:
            self._trip_stream_watchdog("render pipeline process died")
            watchdog.reset()
            self._render_pipeline_expected = False
            return
        ifname = self.p2p.get_group_interface_name()
        if ifname is None:
            return
        try:
            rx_bytes = read_interface_rx_bytes(ifname)
        except (OSError, ValueError):
            return
        if watchdog.observe(rx_bytes, time.monotonic()):
            self._trip_stream_watchdog("no stream data for over 10s")
            watchdog.reset()

    def _trip_stream_watchdog(self, reason: str) -> None:
        logger.warning("stream watchdog: %s; forcing Miracast session teardown", reason)
        sock = self._active_control_sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

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
        # PNG and repaints it every time a peer requests one, so whatever
        # is on screen is always the PIN that will actually work right now.
        #
        # Painted via /dev/fb0, NOT a kmssink pipeline: an idle kmssink
        # holds DRM master, and UxPlay -- which opens its own kmssink once
        # at startup -- then never becomes master and every AirPlay
        # session dies at the first frame (2026-07-15). The framebuffer
        # path takes no master, so the DRM device stays free for whichever
        # streaming pipeline needs it. See render/framebuffer.py.
        ssid, password = self._wifi_credentials or (None, None)
        render_idle_screen(
            IDLE_PNG_PATH, room_name=self.config.room_name, pin=pin, wifi_ssid=ssid, wifi_password=password
        )
        try:
            paint_framebuffer(IDLE_PNG_PATH)
        except (OSError, ValueError):
            logger.exception("could not paint the idle screen to the framebuffer")

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
            pipeline = build_wfd_pipeline_description(udp_port=WFD_UDP_PORT, target=self.render_target)
            self.render.stop()
            self.render.start(pipeline)
            # Only from this point on has the stream watchdog's "is the
            # render process still running" check earned any meaning --
            # see _stream_watch_loop for why this flag exists at all.
            self._render_pipeline_expected = True
            logger.info("Miracast streaming started, session=%s", session.session_id)
            # The Pi's H.264 decoder intermittently cold-starts without ever
            # emitting a first frame (~15% of connects, 2026-07-22), leaving
            # the screen on the PIN until a manual reconnect. Recover it
            # automatically in the background so this handler can return and
            # start pumping RTSP keep-alives immediately (blocking here would
            # risk the source tearing the session down).
            threading.Thread(
                target=self._recover_render_cold_start, args=(pipeline,), daemon=True
            ).start()
            return negotiator
        except (NegotiationError, OSError):
            logger.exception("Miracast negotiation failed for %s", source_ip)
            self.handle_miracast_disconnected()
            return None

    def _recover_render_cold_start(self, pipeline_description: str) -> None:
        """Auto-recover the intermittent decoder cold-start freeze: if no
        decoded frame reaches kmssink within FIRST_FRAME_TIMEOUT_S, re-roll
        the LOCAL render pipeline. The RTSP session with the source stays up,
        so this is invisible beyond a couple extra seconds on the idle screen
        -- it does automatically what a manual reconnect used to. Bounded,
        and it bows out the moment the session ends or the render process is
        already gone, so it never fights a teardown or loops on a genuinely
        dead pipeline. Runs in its own thread so it never delays the RTSP
        keep-alive pump.

        NOTE: a re-rolled pipeline must wait for the source's next IDR before
        it can show a frame; Windows sends them frequently enough that the
        timeout covers it. If field data shows re-rolls timing out purely for
        lack of an IDR, add an RTSP wfd-idr-request after the restart."""
        timeout = RenderProcess.FIRST_FRAME_TIMEOUT_S
        for attempt in range(1, RENDER_COLD_START_RETRIES + 1):
            if self.render.wait_for_first_frame(timeout):
                if attempt > 1:
                    logger.info("render recovered after %d cold-start re-roll(s)", attempt - 1)
                return
            if self.arbiter.state is not State.MIRACAST or not self._render_pipeline_expected:
                return  # session ended; nothing to recover
            if not self.render.is_running:
                return  # a teardown/watchdog already took the pipeline down
            logger.warning(
                "render cold-start freeze: no frame in %.0fs; re-rolling render pipeline (%d/%d)",
                timeout, attempt, RENDER_COLD_START_RETRIES,
            )
            # Drop the watchdog gate across the brief stop/start so the stream
            # watchdog can't read the gap as "render process died".
            self._render_pipeline_expected = False
            self.render.stop()
            if self.arbiter.state is not State.MIRACAST:
                return  # disconnected during teardown -- do not resurrect render
            self.render.start(pipeline_description)
            self._render_pipeline_expected = True
        logger.error(
            "render still frozen after %d re-rolls; leaving it to the stream watchdog",
            RENDER_COLD_START_RETRIES,
        )

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
                # Flip the WFD Session Availability flag so a second source
                # sees the room as busy while someone is presenting (paused),
                # and available again once they leave (resumed). This only
                # re-publishes the discovery IE; it does not touch the active
                # group, so the current client is unaffected.
                p2p = getattr(self, "p2p", None)
                if p2p is not None:
                    p2p.set_session_available(action is Action.RESUME_MIRACAST_DISCOVERY)
        self.health.set_state(self.arbiter.state)
        self.health.heartbeat()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    conf_path = find_receiver_conf()
    try:
        config = parse_room_config(conf_path.read_text())
    except (ConfigError, OSError) as exc:
        logger.error("cannot start: %s", exc)
        return 1
    logger.info("loaded room config from %s: room_name=%s", conf_path, config.room_name)

    daemon = CastDaemon(config)
    daemon.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
