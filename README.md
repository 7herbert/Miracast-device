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

Room settings live in **`/boot/firmware/receiver.conf`** on Raspberry Pi OS
Bookworm (the FAT boot partition is mounted there, so a room can be
configured by editing the SD card on any computer without booting Linux);
`/boot/receiver.conf` is the legacy Bullseye path. castd reads whichever
exists, preferring `/boot/firmware`, and logs the chosen file + `room_name`
at startup. See `castd/config.py` for the format; the WPS PIN there must
pass the WSC checksum, which config parsing enforces at startup.
`passphrase=` is currently **unused** — the group Wi-Fi password is
autogenerated fresh by wpa_supplicant on every boot and shown live on the
kiosk QR. Raspberry Pi OS Bookworm also needs a wpa_supplicant drop-in so
the service attaches `wlan1` at boot — without it castd creates the
interface itself over D-Bus on first start, which also works.

### Regulatory domain (required)

Pin the Wi-Fi country so the P2P GO comes up in a fixed regulatory domain:

```bash
sudo raspi-config nonint do_wifi_country TW      # your locale
# and, in /etc/wpa_supplicant/wpa_p2p.conf:
#   country=TW
```

Without this, associating any Wi-Fi STA (e.g. a management interface) makes
the kernel adopt the AP's country code at runtime, and that mid-flight
regulatory change kills the running P2P GO's beacon — Windows suddenly can't
find the receiver (observed 2026-07-22). Pinning it means the GO starts in
the final domain and nothing changes under it later.

### Network architecture (standalone)

For a small fleet (a few rooms) the receiver runs **standalone — no
management network**. The external adapter (`wlan1`) hosts the P2P GO;
`wlan0`/`eth0` are left to NetworkManager but unused for casting. Update or
debug via SD re-image, or plug Ethernet into a room that has a drop (Ethernet
has no radio/regulatory domain, so it never disturbs the GO — unlike a
Wi-Fi management STA). If you must manage over Wi-Fi, pin `country=` first
(above) or the GO drops. Keep `wlan1` + the group interfaces out of
NetworkManager:

```ini
# /etc/NetworkManager/conf.d/miracast.conf
[keyfile]
unmanaged-devices=interface-name:wlan0;interface-name:wlan1;interface-name:p2p-wlan1-*
```

### Pin interface names (production imaging)

castd hosts the GO on `wlan1` (override with `CASTD_P2P_INTERFACE`). The
kernel's wlan0/wlan1 ordering (built-in vs USB) is not guaranteed across
reboots or kernel updates, so pin them by MAC OUI — do this with console
access and verify `iw dev` afterward, since a bad rule can cost network
access:

```udev
# /etc/udev/rules.d/72-castd-wifi.rules  (adjust OUIs to your hardware:
#   cat /sys/class/net/wlan*/address)
SUBSYSTEM=="net", ACTION=="add", KERNEL=="wlan*", ATTR{address}=="08:be:ac:*", NAME="wlan1"  # external dongle
SUBSYSTEM=="net", ACTION=="add", KERNEL=="wlan*", ATTR{address}=="e4:5f:01:*", NAME="wlan0"  # Pi built-in
```

### Occupied-room behavior

While a source is presenting, castd advertises the WFD IE as "not available
for session" and stops UxPlay, so a second Windows/iPhone cannot interrupt
the person presenting; both are restored on disconnect. (First-connect-wins
is also enforced in the FSM/connect path regardless of what a source's UI
chooses to show.)

### Golden-image deployment (small fleet)

For a handful of rooms, provision one Pi fully, verify it, then clone the SD
card and change only the per-room config on each copy.

1. **Build the golden unit** per the steps above: dependencies; disable
   `dnsmasq`/`lightdm`; avahi config; `country=` pin; NetworkManager
   `unmanaged-devices`; the wpa_supplicant `-i wlan1` drop-in; and — with
   console access, verifying `iw dev` afterward — the interface-naming udev
   rule. Enable `castd`.
2. **Verify on the golden unit** after a fresh reboot: `systemctl is-active
   castd`; Windows Win+K mirrors at <1s; iPhone AirPlay works; the room name
   shows on the kiosk. Confirm no leftover test overrides —
   `ls /etc/systemd/system/castd.service.d/` should be empty (no
   `CASTD_*` env files from bring-up).
3. **Clone** the SD card to each room's card.
4. **Per room**, mount the FAT partition and edit
   `/boot/firmware/receiver.conf`: set `room_name` and `channel` (stagger
   36/40/44/48 across nearby rooms to avoid co-channel interference).
   `wps_pin`/`passphrase` are format-checked, but the live pairing PIN is
   generated per connection and the group Wi-Fi password is regenerated each
   boot, so their values here are not what clients actually use.
5. **Monitor** early on: the intermittent decoder cold-start freeze
   auto-recovers by re-rolling the render pipeline, but it is logged --
   `journalctl -u castd | grep -c "render recovered"` (freezes that
   self-healed) and `... | grep -c "still frozen after"` (gave up after 3
   re-rolls; should be 0 — if not, a re-roll is likely waiting on the
   source's next IDR and an RTSP `wfd-idr-request` should be added). Run the
   72-hour soak (`tools/soak_monitor.sh`) on at least the first unit.

## Hardware

| Item | Choice |
|------|--------|
| Board | Raspberry Pi 4B (keeps the H.264 hardware decoder Pi 5 dropped) |
| Wi-Fi adapter (P2P GO) | EDIMAX EW-7822UMX — enumerates on the **RTL8852BU** via the mainline in-kernel **`rtw89_8852bu`** driver (no out-of-tree build needed). Hosts the P2P Group Owner. **Required** — the Pi 4's built-in Wi-Fi (Broadcom CYW43455 / `brcmfmac`) advertises P2P-GO in `iw phy0 info` but its `wpa_supplicant` GroupAdd is rejected (`InvalidArgs`) on real hardware, so it cannot host the Miracast GO. A smaller dongle with any mainline `rtw88`/`rtw89` P2P-GO-capable chip is a drop-in replacement; verify a candidate with `iw phyN info` (must list `P2P-GO`) + a `CASTD_P2P_INTERFACE=<name>` functional test. |
| Built-in Wi-Fi (wlan0) | Optional management STA only (never P2P). Left unused in the standalone deployment below. |

## Hardening: reconnect stress + 72-hour soak

Built-in recovery already covers: uxplay crashing mid-session (FSM back
to IDLE + relaunch), a source dropping the RTSP channel, and a Miracast
stream that dies while its control channel stays up (stream watchdog:
render-process death or the group interface's rx counter flatlining for
10 s force the same clean teardown as a normal disconnect).

Both test procedures run on the same monitor:

```bash
# 72h soak -- start it and walk away; Ctrl+C prints the summary early:
sudo bash tools/soak_monitor.sh

# reconnect stress -- tight sampling, then manually cycle Windows/iPhone
# connects and disconnects (aim for 20+ cycles, both protocols,
# including mid-stream disconnects):
sudo bash tools/soak_monitor.sh 1 5
```

Every sample records service liveness, systemd restart count, castd and
uxplay memory, the `/health` endpoint's FSM state, and new journal ERROR
lines; anomalies print immediately. Pass criteria: zero castd restarts,
zero health outages, flat castd RSS, anomaly count 0.

## Next steps

1. Audio validation on both protocols (HDMI output)
2. 72-hour soak (procedure above) before multi-room deployment
3. Idle-screen QR code for one-scan AirPlay joining; read-only rootfs

## License

castd is licensed under the **Apache License 2.0** — see [LICENSE](LICENSE).

It invokes UxPlay (GPLv3), GStreamer (LGPL/GPL), wpa_supplicant (BSD), and
dnsmasq (GPL) as **separate processes** (exec / D-Bus), not linked in, so
castd itself is not a derivative work of them and its Apache-2.0 license
stands on its own. Those components keep their own licenses — see
[NOTICE](NOTICE). If you redistribute a full system image bundling those
binaries, comply with their licenses (notably UxPlay's GPLv3); internal
use within an organization is not distribution.
