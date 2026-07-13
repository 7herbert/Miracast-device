from PIL import Image

from castd.render.idle_screen import render_idle_screen


def test_renders_a_valid_png_with_requested_dimensions(tmp_path):
    out = tmp_path / "idle.png"
    render_idle_screen(str(out), room_name="MR-3F-A", width=640, height=360)
    assert out.exists()
    with Image.open(out) as img:
        assert img.size == (640, 360)
        assert img.format == "PNG"


def test_renders_without_pin_shows_hint_not_crash(tmp_path):
    out = tmp_path / "idle_no_pin.png"
    render_idle_screen(str(out), room_name="MR-1F-B", pin=None, width=640, height=360)
    assert out.exists()


def test_renders_with_pin(tmp_path):
    out = tmp_path / "idle_with_pin.png"
    render_idle_screen(str(out), room_name="MR-1F-B", pin="68123457", width=640, height=360)
    assert out.exists()


def test_creates_parent_directories(tmp_path):
    out = tmp_path / "nested" / "dir" / "idle.png"
    render_idle_screen(str(out), room_name="X", width=320, height=180)
    assert out.exists()
