"""Tests for RenderProcess's subprocess launch -- monkeypatches subprocess
so this runs without a real gst-launch-1.0 binary (this dev box doesn't
have one).

Locks in the 2026-07-15 lesson: a first attempt to trace WFD pipeline
latency set GST_DEBUG/GST_TRACERS via `systemctl edit castd`, a
systemd Environment= line that applies to the WHOLE unit -- it leaked
into UxPlayProcess's subprocess too (also GStreamer-based) and its trace
output came back mixed in with the pipeline actually being investigated.
CASTD_TRACE_RENDER_LATENCY must only ever affect the env dict passed to
THIS ONE subprocess.Popen call, never castd's own os.environ.
"""
from __future__ import annotations

import subprocess

import pytest

from castd.render import gstreamer
from castd.render.gstreamer import RenderProcess


class FakePopen:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.env = kwargs.get("env")
        # start() drains stdout in a reader thread; None makes that a no-op.
        self.stdout = None
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        return self._returncode


@pytest.fixture
def fake_popen(monkeypatch):
    calls = []

    def fake(argv, **kwargs):
        proc = FakePopen(argv, **kwargs)
        calls.append(proc)
        return proc

    monkeypatch.setattr(gstreamer.subprocess, "Popen", fake)
    return calls


def test_default_start_does_not_override_environment(fake_popen, monkeypatch):
    monkeypatch.delenv("CASTD_TRACE_RENDER_LATENCY", raising=False)
    RenderProcess().start("videotestsrc ! fakesink")
    assert fake_popen[0].env is None  # inherits castd's own environ untouched


def test_trace_flag_adds_gst_debug_only_to_this_subprocess(fake_popen, monkeypatch):
    monkeypatch.setenv("CASTD_TRACE_RENDER_LATENCY", "1")
    RenderProcess().start("videotestsrc ! fakesink")

    env = fake_popen[0].env
    assert env is not None
    assert env["GST_DEBUG"] == "GST_TRACER:7"
    assert env["GST_TRACERS"] == "latency(flags=pipeline+element)"
    # castd's own process environment must be untouched -- otherwise
    # UxPlayProcess's subprocess.Popen (which inherits os.environ with no
    # env= override) would pick up the same trace flags and its output
    # would come back mixed in with the pipeline under test, exactly the
    # confusion a real attempt at this hit (2026-07-15).
    import os

    assert "GST_DEBUG" not in os.environ
    assert "GST_TRACERS" not in os.environ


def test_trace_flag_off_by_default(fake_popen, monkeypatch):
    monkeypatch.delenv("CASTD_TRACE_RENDER_LATENCY", raising=False)
    RenderProcess().start("videotestsrc ! fakesink")
    assert fake_popen[0].env is None


class _FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)

    def poll(self):
        return None


# Real -v lines captured on the Pi 4 (2026-07-22).
_KMS_SINK_CAPS = (
    "/GstPipeline:pipeline0/GstKMSSink:kmssink0.GstPad:sink: caps = "
    "video/x-raw(memory:DMABuf), format=(string)DMA_DRM, width=(int)1920, height=(int)1080\n"
)
_DECODER_INPUT_CAPS = (
    "/GstPipeline:pipeline0/v4l2h264dec:v4l2h264dec0.GstPad:sink: caps = "
    "video/x-h264, stream-format=(string)byte-stream, width=(int)1920, height=(int)1080\n"
)


def test_drain_output_detects_first_frame_when_kms_gets_raw_caps():
    rp = RenderProcess()
    rp._drain_output(_FakeProc([_DECODER_INPUT_CAPS, _KMS_SINK_CAPS]), 0.0)
    assert rp.first_frame_seen


def test_drain_output_does_not_false_trigger_on_decoder_input_caps():
    # The decoder's SINK pad negotiates video/x-h264 BEFORE any frame is
    # decoded -- it must not be mistaken for a first frame, or the freeze
    # detector would think a frozen session is fine.
    rp = RenderProcess()
    rp._drain_output(_FakeProc([_DECODER_INPUT_CAPS, "Setting pipeline to PLAYING ...\n"]), 0.0)
    assert not rp.first_frame_seen
