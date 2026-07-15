"""Idle-screen output via the Linux framebuffer (/dev/fb0), replacing the
persistent idle kmssink pipeline.

Root cause this replaces (2026-07-15): a kmssink that displays the idle
screen also holds DRM master, and UxPlay opens ITS kmssink once at process
startup -- if master is taken at that moment it never becomes master and
every AirPlay session dies with "general resource error" at the first
mirrored frame, even though castd released the display before the frame
arrived. The exact same uxplay binary mirrored an iPhone perfectly the
moment castd (and its idle pipeline) was stopped.

The framebuffer console path takes no DRM master at all: painting the
idle PNG straight into /dev/fb0 leaves the DRM device permanently free
for whichever streaming pipeline needs it (castd's Miracast kmssink or
UxPlay's). fbcon restores the framebuffer scanout when a DRM master
exits, so repainting after each session brings the kiosk screen back.

Pure file I/O + PIL; geometry is injectable so everything is testable
against a temp file with no real framebuffer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

FB_DEVICE = "/dev/fb0"
FB_SYSFS = "/sys/class/graphics/fb0"


@dataclass(frozen=True)
class FbGeometry:
    width: int
    height: int
    bits_per_pixel: int
    stride: int  # bytes per scanline, may exceed width * bytes-per-pixel


def read_fb_geometry(sysfs_path: str = FB_SYSFS) -> FbGeometry:
    base = Path(sysfs_path)
    width_s, height_s = base.joinpath("virtual_size").read_text().strip().split(",")
    bpp = int(base.joinpath("bits_per_pixel").read_text().strip())
    try:
        stride = int(base.joinpath("stride").read_text().strip())
    except OSError:
        stride = int(width_s) * bpp // 8
    return FbGeometry(width=int(width_s), height=int(height_s), bits_per_pixel=bpp, stride=stride)


def paint_framebuffer(
    image_path: str,
    fb_path: str = FB_DEVICE,
    geometry: FbGeometry | None = None,
) -> None:
    """Scale `image_path` to the framebuffer and write it in the fb's own
    pixel format. 32 bpp is XRGB little-endian (raw mode BGRX); 16 bpp is
    RGB565 (raw mode BGR;16)."""
    if geometry is None:
        geometry = read_fb_geometry()
    with Image.open(image_path) as source:
        img = source.convert("RGB").resize((geometry.width, geometry.height))
        if geometry.bits_per_pixel == 32:
            row_bytes = geometry.width * 4
            data = img.tobytes("raw", "BGRX")
        elif geometry.bits_per_pixel == 16:
            # RGB565 little-endian, packed by hand: Pillow 12 dropped the
            # BGR;16 raw packer. The Pi's fb0 is 32 bpp in practice, so
            # this path is completeness, not the hot path.
            row_bytes = geometry.width * 2
            rgb = img.tobytes()
            out = bytearray(len(rgb) // 3 * 2)
            for i in range(0, len(rgb), 3):
                value = ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
                j = i // 3 * 2
                out[j] = value & 0xFF
                out[j + 1] = value >> 8
            data = bytes(out)
        else:
            raise ValueError(f"unsupported framebuffer depth: {geometry.bits_per_pixel} bpp")

    if geometry.stride > row_bytes:
        padding = bytes(geometry.stride - row_bytes)
        data = b"".join(
            data[row * row_bytes : (row + 1) * row_bytes] + padding for row in range(geometry.height)
        )

    Path(fb_path).write_bytes(data)
