"""Tests for castd.stream_watchdog -- pure logic, driven with synthetic
byte counters and timestamps."""
from castd.stream_watchdog import StreamWatchdog


def test_first_sample_never_trips():
    wd = StreamWatchdog(stall_window_s=10, min_bytes_per_sample=5000)
    assert wd.observe(1_000_000, now=0.0) is False


def test_live_stream_never_trips():
    # A static desktop mirror still moves >100 kbps; 2s samples at that
    # floor are ~25 kB each, far above the 5 kB threshold.
    wd = StreamWatchdog(stall_window_s=10, min_bytes_per_sample=5000)
    rx = 0
    for i in range(20):
        rx += 25_000
        assert wd.observe(rx, now=i * 2.0) is False


def test_flatlined_stream_trips_after_window():
    wd = StreamWatchdog(stall_window_s=10, min_bytes_per_sample=5000)
    wd.observe(1_000_000, now=0.0)
    # keep-alive dribble only: a few hundred bytes per sample
    assert wd.observe(1_000_300, now=2.0) is False
    assert wd.observe(1_000_600, now=4.0) is False
    assert wd.observe(1_000_900, now=8.0) is False
    assert wd.observe(1_001_200, now=12.0) is True  # >10s without progress


def test_recovering_stream_resets_the_stall_clock():
    wd = StreamWatchdog(stall_window_s=10, min_bytes_per_sample=5000)
    wd.observe(0, now=0.0)
    assert wd.observe(100, now=8.0) is False  # nearly stalled...
    assert wd.observe(50_100, now=9.0) is False  # ...then data resumes
    assert wd.observe(50_200, now=18.0) is False  # window restarts at 9.0
    assert wd.observe(50_300, now=19.5) is True


def test_reset_forgets_history():
    wd = StreamWatchdog(stall_window_s=10, min_bytes_per_sample=5000)
    wd.observe(0, now=0.0)
    wd.observe(100, now=11.0)
    wd.reset()
    assert wd.observe(200, now=30.0) is False  # first sample again
