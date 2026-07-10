# Miracast + AirPlay Meeting Room Receiver

Self-built replacement for the discontinued Microsoft 4K Wireless Display
Adapter: a Raspberry Pi 4B that acts as a single Wi-Fi Direct Group Owner
serving both Miracast (Windows Win+K) and AirPlay (iPhone/Mac legacy-STA +
QR join), for unattended use in commercial meeting rooms.

## Status: pre-hardware-validation

`castd/` is a from-scratch daemon (no lazycast dependency) with 90 passing
unit/integration tests and clean static analysis. **None of it has run
against a real Raspberry Pi, wpa_supplicant, Windows Miracast source, or
iPhone yet.** Everything below "Verified" is code-level correctness only.

### Verified (pytest + static checks, runs on any machine with Python 3.12)

- `config.py` — room config parsing/validation
- `fsm/state_machine.py` — Miracast/AirPlay arbitration state machine
- `wfdsink/rtsp.py` + `wfdsink/session.py` — WFD M1-M7 RTSP handshake,
  including a `socket.socketpair()`-based end-to-end test that replays a
  real captured Windows 10 negotiation
- `p2p/wfd_ie.py` — WFD Device Information IE byte encoding
- `airplay/qrcode_wifi.py` — Wi-Fi QR payload generation for AirPlay join
- `health.py`, `sdnotify.py` — health endpoint, systemd watchdog protocol

Run the suite:
```bash
python3 -m pytest castd/tests/ -v
```

### Not yet verified (needs real hardware — see project plan Phase 0)

- `p2p/dbus_go.py` — wpa_supplicant D-Bus P2P Group Owner control
- `render/gstreamer.py` — GStreamer/KMS video pipeline
- `airplay/uxplay.py` — UxPlay process management
- `main.py` — full daemon wiring

These import-check and lint clean, but have not been exercised against a
real wpa_supplicant, GStreamer/KMS, or Wi-Fi adapter.

## Architecture

One systemd unit (`castd/castd.service`) runs a single Python process that:
1. Brings up a P2P Group Owner on a fixed non-DFS 5 GHz channel (36/40/44/48)
2. Advertises Miracast (WFD IE) with a fixed WPS PIN and AirPlay (via a
   Wi-Fi QR code for legacy-STA join)
3. Arbitrates: whichever protocol connects first owns the display until it
   disconnects or a watchdog timeout fires
4. Renders through GStreamer/KMS — no X server, no VLC, no user switching

See `castd/main.py`, `castd/fsm/state_machine.py`, and `castd/castd.service`
for the concrete wiring.

## Hardware

| Item | Choice |
|------|--------|
| Board | Raspberry Pi 4B (keeps the H.264 hardware decoder Pi 5 dropped) |
| Wi-Fi adapter | Under evaluation — EDIMAX EW-7822UMX (RTL8832BU) is the current candidate; a 72-hour P2P GO soak test is required before committing to it for production rooms |

## Next steps (Phase 0)

1. D-Bus P2P GO creation + fixed WPS PIN against a real Windows 11 laptop
2. iPhone QR-join + UxPlay AirPlay mirroring against real iOS hardware
3. GStreamer `kmssink` hardware-decoded playback on a real Pi 4
