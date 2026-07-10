import json
import urllib.error
import urllib.request

import pytest

from castd.fsm.state_machine import State
from castd.health import HealthState, serve_forever


def test_snapshot_reports_state_and_heartbeat_age():
    h = HealthState()
    h.set_state(State.MIRACAST)
    snap = h.snapshot()
    assert snap["state"] == "MIRACAST"
    assert snap["seconds_since_heartbeat"] >= 0


def test_is_healthy_true_immediately_after_heartbeat():
    h = HealthState()
    h.heartbeat()
    assert h.is_healthy(max_heartbeat_age_s=30.0)


def test_is_healthy_false_when_heartbeat_stale():
    h = HealthState()
    h.heartbeat()
    assert not h.is_healthy(max_heartbeat_age_s=-1.0)  # force "stale" without sleeping


@pytest.fixture
def running_server():
    h = HealthState()
    h.heartbeat()
    server = serve_forever(h, port=0)
    port = server.server_address[1]
    yield h, port
    server.shutdown()


def test_health_endpoint_returns_200_when_healthy(running_server):
    h, port = running_server
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body["state"] == "IDLE"


def test_health_endpoint_returns_404_for_other_paths(running_server):
    _, port = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=2)
    assert exc_info.value.code == 404
