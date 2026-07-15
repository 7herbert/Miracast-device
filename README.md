# Miracast + AirPlay Meeting Room Receiver

Self-built replacement for the discontinued Microsoft 4K Wireless Display
Adapter: a Raspberry Pi 4B that acts as a single Wi-Fi Direct Group Owner
serving both Miracast (Windows Win+K) and AirPlay (iPhone/Mac legacy-STA +
QR join), for unattended use in commercial meeting rooms.

## Status: both protocols working end-to-end on real hardware

`castd/` is a from-scratch daemon (no lazycast dependency) with 155 passing
unit/integration tests. Verified live on the target hardware (Pi 4B +
EDIMAX EW-7822UMX): **Windows 11 Miracast** (Win+K → dynamic WPS PIN on the
kiosk screen → 1080p30 hardware-decoded mirroring, mirror + extend modes)
and **iPhone AirPlay** (join the group Wi-Fi shown on the kiosk screen →
Screen Mirroring), with first-connect-wins arbitration between the two.
Remaining hardening: session watchdog/auto-recovery, audio validation, and
the 72-hour soak test before multi-room deployment.

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

### Partially verified on real hardware

- `p2p/dbus_go.py` — GO creation/discovery/PD confirmed live; the WPS
  registrar arming (`authorize_display_pin`) is the piece under test now
- `render/gstreamer.py` — kmssink idle-screen rendering confirmed live
- `p2p/group_network.py` — command construction unit-tested; dnsmasq child
  not yet exercised on the Pi
- `airplay/uxplay.py` — UxPlay process management, not yet exercised
- `main.py` — daemon wiring; boots and runs on the Pi, full Miracast
  session not yet achieved

## Architecture

One systemd unit (`castd/castd.service`) runs a single Python process that:
1. Brings up a P2P Group Owner on a fixed non-DFS 5 GHz channel (36/40/44/48)
2. Advertises Miracast (WFD IE, sink RTSP port 7236) and AirPlay (via a
   Wi-Fi QR code for legacy-STA join)
3. On each Windows connection attempt: shows wpa_supplicant's freshly
   generated WPS PIN on the HDMI kiosk screen AND arms the GO's WPS
   registrar with it (both are required — see `p2p/dbus_go.py`), then
   serves the source an IP via a dnsmasq child process (`p2p/group_network.py`)
   and accepts the source's inbound RTSP connection on 7236
4. Arbitrates: whichever protocol connects first owns the display until it
   disconnects or a watchdog timeout fires
5. Renders through GStreamer/KMS — no X server, no VLC, no user switching

See `castd/main.py`, `castd/fsm/state_machine.py`, and `castd/castd.service`
for the concrete wiring.

## Deploying on the Pi

```bash
sudo apt install python3-dbus python3-gi python3-pil dnsmasq \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good
# castd runs dnsmasq itself as a child process bound to the P2P group
# interface only -- the system-wide service must not race it for port 67:
sudo systemctl disable --now dnsmasq
# The desktop cannot hold DRM master while kmssink renders:
sudo systemctl disable --now lightdm

sudo cp castd/castd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now castd
```

If the Pi is also connected to an infrastructure network (Ethernet or
wlan0 for SSH), avahi will advertise those addresses too, and an AirPlay
client on the P2P network can resolve the receiver to an unreachable IP
and fail to connect. Restrict mDNS to the P2P side:

```bash
# /etc/avahi/avahi-daemon.conf:
#   use-ipv6=no                      (transport)
#   allow-interfaces=p2p-wlan1-0     (whitelist beats deny -- see below)
#   publish-aaaa-on-ipv4=no          (record content -- see below)
sudo sed -i -e 's/^#\?use-ipv6=.*/use-ipv6=no/' \
            -e 's/^#\?allow-interfaces=.*/allow-interfaces=p2p-wlan1-0/' \
            -e 's/^#\?publish-aaaa-on-ipv4=.*/publish-aaaa-on-ipv4=no/' \
            /etc/avahi/avahi-daemon.conf
sudo systemctl restart avahi-daemon
```

UxPlay itself must be built with `cmake -DUSE_DNS_SD=1` (and
`libavahi-compat-libdnssd-dev` installed): UxPlay 1.74's *default* is an
internal mDNS responder that ignores avahi entirely and advertises the
default-route interface's address -- on a multi-homed Pi that is the
infrastructure address, unreachable from P2P clients. Only the
`USE_DNS_SD` build routes registration through avahi, where the settings
above take effect.

The avahi non-defaults were found with a packet capture against a real
iPhone (2026-07-14):
- `publish-aaaa-on-ipv4=no`: `use-ipv6=no` only disables IPv6
  *transport*; avahi kept advertising the AAAA record inside IPv4 mDNS
  responses, the iPhone preferred that IPv6 link-local address, and
  UxPlay's IPv4-only sockets answered its AirPlay connection with RST.
- `allow-interfaces` (not `deny-interfaces`): with only a deny list,
  avahi's SRV/A answers on the P2P interface still carried the Pi's
  *infrastructure-side* address, which is unreachable from the P2P
  subnet -- the iPhone dialed it and timed out. Whitelisting the group
  interface leaves avahi knowing exactly one address, the right one.
  (If wpa_supplicant ever creates the group as p2p-wlan1-1 instead of
  -0, this line must follow.)

Room settings live in `/boot/receiver.conf` (see `castd/config.py` for the
format; the WPS PIN there must pass the WSC checksum, which config parsing
enforces at startup). Raspberry Pi OS Bookworm also needs a wpa_supplicant
drop-in so the service attaches `wlan1` at boot — without it castd creates
the interface itself over D-Bus on first start, which also works.

## Hardware

| Item | Choice |
|------|--------|
| Board | Raspberry Pi 4B (keeps the H.264 hardware decoder Pi 5 dropped) |
| Wi-Fi adapter | Under evaluation — EDIMAX EW-7822UMX (RTL8832BU) is the current candidate; a 72-hour P2P GO soak test is required before committing to it for production rooms |

## Next steps

1. End-to-end Miracast session against the Windows 11 laptop: registrar
   arming → association → WPS M1-M8 → DHCP → RTSP M1-M7 → video
2. iPhone QR-join + UxPlay AirPlay mirroring against real iOS hardware
3. 72-hour P2P GO soak test on the EDIMAX EW-7822UMX
