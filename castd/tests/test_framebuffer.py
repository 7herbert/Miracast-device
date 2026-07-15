"""Tests for castd.render.framebuffer -- runs against temp files, no real
framebuffer. Locks in the DRM-master-free idle screen design: an idle
kmssink pipeline starved UxPlay's startup-time kmssink of DRM master and
killed every AirPlay session at the first frame (2026-07-15)."""
from PIL import Image

from castd.render.framebuffer import FbGeometry, paint_framebuffer, read_fb_geometry


def _save_red_png(path, size=(4, 2)):
    Image.new("RGB", size, color=(255, 0, 0)).save(path)


def test_paints_32bpp_bgrx(tmp_path):
    png = tmp_path / "in.png"
    fb = tmp_path / "fb0"
    _save_red_png(png)

    paint_framebuffer(str(png), str(fb), FbGeometry(width=4, height=2, bits_per_pixel=32, stride=16))

    data = fb.read_bytes()
    assert len(data) == 4 * 2 * 4
    assert data[0:3] == bytes([0, 0, 255])  # red pixel stored as B,G,R


def test_pads_rows_to_stride(tmp_path):
    png = tmp_path / "in.png"
    fb = tmp_path / "fb0"
    _save_red_png(png)

    paint_framebuffer(str(png), str(fb), FbGeometry(width=4, height=2, bits_per_pixel=32, stride=20))

    data = fb.read_bytes()
    assert len(data) == 20 * 2
    assert data[16:20] == bytes(4)  # padding after the first row


def test_scales_image_to_framebuffer_size(tmp_path):
    png = tmp_path / "in.png"
    fb = tmp_path / "fb0"
    _save_red_png(png, size=(640, 360))

    paint_framebuffer(str(png), str(fb), FbGeometry(width=8, height=4, bits_per_pixel=32, stride=32))

    assert len(fb.read_bytes()) == 8 * 4 * 4


def test_paints_16bpp_rgb565(tmp_path):
    png = tmp_path / "in.png"
    fb = tmp_path / "fb0"
    _save_red_png(png)

    paint_framebuffer(str(png), str(fb), FbGeometry(width=4, height=2, bits_per_pixel=16, stride=8))

    data = fb.read_bytes()
    assert len(data) == 4 * 2 * 2
    # red in RGB565 little-endian = 0x00 0xF8
    assert data[0:2] == bytes([0x00, 0xF8])


def test_read_fb_geometry_parses_sysfs(tmp_path):
    (tmp_path / "virtual_size").write_text("1920,1080\n")
    (tmp_path / "bits_per_pixel").write_text("32\n")
    (tmp_path / "stride").write_text("7680\n")

    geo = read_fb_geometry(str(tmp_path))
    assert geo == FbGeometry(width=1920, height=1080, bits_per_pixel=32, stride=7680)


def test_read_fb_geometry_derives_stride_when_missing(tmp_path):
    (tmp_path / "virtual_size").write_text("1920,1080\n")
    (tmp_path / "bits_per_pixel").write_text("32\n")

    geo = read_fb_geometry(str(tmp_path))
    assert geo.stride == 1920 * 4
