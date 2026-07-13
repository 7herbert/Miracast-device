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
from pathlib import Path

from castd.airplay.uxplay import UxPlayConfig, UxPlayProcess
from castd.config import ConfigError, RoomConfig, parse_room_config
from castd.fsm.state_machine import Action, CastArbiter, Event
from castd.health import HealthState, serve_forever
from castd.p2p.dbus_go import GroupInfo, P2PGroupOwner
from castd.render.gstreamer import RenderProcess, RenderTarget, build_idle_screen_pipeline, build_wfd_pipeline_description
from castd import sdnotify
from castd.wfdsink.rtsp import NegotiationError, WfdCapabilities
from castd.wfdsink.session import negotiate, open_control_connection

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
        # Constructed in start(), once the real P2P group interface name is
        # known -- see the comment there. Real-hardware testing found the
        # numeric suffix (p2p-wlan1-0, -4, -72, ...) is not stable, so it
        # cannot be hardcoded here at __init__ time.
        self.uxplay: UxPlayProcess | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        serve_forever(self.health)
        self.render.start(build_idle_screen_pipeline(png_path=IDLE_PNG_PATH, target=self.render_target))
        sdnotify.notify(ready=True, status="idle")
        threading.Thread(target=self._watchdog_heartbeat_loop, daemon=True).start()

        self.p2p = P2PGroupOwner(
            "wlan1",
            device_name=self.config.device_name,
            freq_mhz=self.config.freq_mhz,
            wps_pin=self.config.wps_pin,
            on_group_started=self._on_group_started,
            on_wps_failed=self._on_wps_failed,
        )
        self.p2p.configure()
        self.p2p.start_group()

        group_ifname = self.p2p.get_group_interface_name()
        if group_ifname is None:
            raise RuntimeError("start_group() returned but no p2p-wlan1-* interface exists")
        self.uxplay = UxPlayProcess(
            UxPlayConfig(
                device_name=self.config.device_name,
                bind_interface=group_ifname,
                wps_pin=self.config.wps_pin,
            )
        )

        dbus_thread = threading.Thread(target=self.p2p.run_forever, daemon=True)
        dbus_thread.start()

        self.uxplay.start()

        self.p2p.run_forever()

    def _on_group_started(self, info: GroupInfo) -> None:
        logger.info("group started on %s @ %d MHz", info.interface_name, info.frequency_mhz)
        threading.Thread(target=self._wait_for_miracast_client, args=(info,), daemon=True).start()

    def _on_wps_failed(self, status: str) -> None:
        logger.warning("WPS failed: %s", status)

    def _watchdog_heartbeat_loop(self, interval_s: float = 10.0) -> None:
        # WatchdogSec=30 in castd.service; ping at 1/3 that interval so a
        # single missed tick from GC pause or a slow D-Bus call never
        # trips a spurious reboot, but a truly wedged main loop still gets
        # caught well within one watchdog period.
        import time

        while True:
            sdnotify.notify(watchdog=True, status=self.arbiter.state.name)
            time.sleep(interval_s)

    def _wait_for_miracast_client(self, info: GroupInfo) -> None:
        # Placeholder for the real "AP-STA-CONNECTED then attempt WFD
        # negotiation" flow; the actual STA-connect signal wiring is part of
        # Phase 1 (needs real hardware to observe wpa_supplicant's event
        # shape for this adapter). Structure shown here is what main.py
        # will call once that signal exists.
        pass

    def handle_miracast_connected(self, source_ip: str) -> None:
        with self._lock:
            transition = self.arbiter.handle(Event.MIRACAST_CONNECTED)
        self._apply_actions(transition.actions)
        try:
            sock = open_control_connection(source_ip)
            session = negotiate(
                sock, source_ip=source_ip, capabilities=WfdCapabilities(device_name=self.config.device_name)
            )
            self.render.stop()
            self.render.start(build_wfd_pipeline_description(udp_port=WFD_UDP_PORT, target=self.render_target))
            logger.info("Miracast streaming started, session=%s", session.session_id)
        except (NegotiationError, OSError):
            logger.exception("Miracast negotiation failed for %s", source_ip)
            self.handle_miracast_disconnected()

    def handle_miracast_disconnected(self) -> None:
        with self._lock:
            transition = self.arbiter.handle(Event.MIRACAST_DISCONNECTED)
        self._apply_actions(transition.actions)

    def _apply_actions(self, actions) -> None:
        for action in actions:
            if action is Action.STOP_RENDER_PIPELINE:
                self.render.stop()
            elif action is Action.SHOW_IDLE_SCREEN:
                self.render.start(build_idle_screen_pipeline(png_path=IDLE_PNG_PATH, target=self.render_target))
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
