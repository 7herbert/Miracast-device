"""Idle/status kiosk screen image generation.

Pure PIL rendering, no GStreamer/subprocess/D-Bus dependency -- unlike the
rest of castd.render, this module is testable on any machine with Pillow
installed (including the Windows dev box this project was written on).

Real-hardware testing found the WPS "display PIN" flow does not use any
value this project sets in advance (see castd/p2p/dbus_go.py's module
docstring and the ProvisionDiscoveryRequestDisplayPin handling in
_handle_display_pin_request): wpa_supplicant generates a fresh PIN for
every negotiation attempt. The only way to actually complete pairing is
to show whatever PIN wpa_supplicant just generated, live, on the kiosk
screen -- so this function is called every time a peer requests one, not
just once at startup.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1920
HEIGHT = 1080
BACKGROUND = (10, 10, 20)
FOREGROUND = (255, 255, 255)
ACCENT = (120, 200, 255)

# Common Raspberry Pi OS / Debian font locations, checked in order.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    # load_default(size=...) needs Pillow >= 9.2; older Pillow ignores the
    # kwarg and returns a small fixed-size bitmap font -- acceptable
    # degraded fallback, not a crash.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int, y: float, fill: tuple[int, int, int]) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    x = (width - text_width) / 2
    draw.text((x, y), text, font=font, fill=fill)


def render_idle_screen(
    output_path: str,
    *,
    room_name: str,
    pin: str | None = None,
    wifi_ssid: str | None = None,
    wifi_password: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> None:
    """Render the kiosk idle screen PNG. Called with pin=None at startup
    (before any connection attempt exists) and again with a real,
    freshly-generated PIN every time wpa_supplicant asks to display one.
    wifi_ssid/wifi_password are the P2P group's own network -- an iPhone
    that joins it can AirPlay to this receiver, so they stay visible in a
    footer on every variant of the screen."""
    img = Image.new("RGB", (width, height), color=BACKGROUND)
    draw = ImageDraw.Draw(img)

    room_font = _load_font(140)
    hint_font = _load_font(56)
    pin_label_font = _load_font(64)
    pin_font = _load_font(200)
    footer_font = _load_font(48)

    _draw_centered(draw, room_name, room_font, width, height * 0.20, FOREGROUND)

    if pin:
        _draw_centered(draw, "Enter this PIN on your PC", pin_label_font, width, height * 0.42, ACCENT)
        spaced_pin = " ".join(pin)
        _draw_centered(draw, spaced_pin, pin_font, width, height * 0.56, FOREGROUND)
    else:
        _draw_centered(draw, "Press Win+K on your PC to connect", hint_font, width, height * 0.52, ACCENT)

    if wifi_ssid and wifi_password:
        _draw_centered(
            draw, "iPhone / Mac: join this Wi-Fi, then AirPlay to this room", footer_font, width, height * 0.80, ACCENT
        )
        _draw_centered(
            draw, f"Wi-Fi: {wifi_ssid}    Password: {wifi_password}", footer_font, width, height * 0.88, FOREGROUND
        )

    # Atomic replace: a plain save() truncates the file in place, and the
    # still-running idle pipeline's filesrc can catch it mid-write --
    # observed live (2026-07-14) as gst dying with "Can't typefind empty
    # stream" during the startup re-render.
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    img.save(tmp, format="PNG")
    tmp.replace(out)
