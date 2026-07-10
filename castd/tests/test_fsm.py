from castd.fsm.state_machine import Action, CastArbiter, Event, State


def test_idle_to_miracast_pauses_airplay_advertising():
    arb = CastArbiter()
    t = arb.handle(Event.MIRACAST_CONNECTED)
    assert t.next_state is State.MIRACAST
    assert arb.state is State.MIRACAST
    assert Action.PAUSE_AIRPLAY_ADVERTISING in t.actions
    # Render pipeline start is NOT an FSM action: it only happens after WFD
    # negotiation yields a real port (see castd.main.handle_miracast_connected).


def test_idle_to_airplay_pauses_miracast_and_releases_drm_device():
    arb = CastArbiter()
    t = arb.handle(Event.AIRPLAY_CONNECTED)
    assert t.next_state is State.AIRPLAY
    assert Action.PAUSE_MIRACAST_DISCOVERY in t.actions
    assert Action.STOP_RENDER_PIPELINE in t.actions


def test_airplay_connect_rejected_while_miracast_active():
    """This is the core arbitration guarantee: a second protocol connecting
    while one is already presenting must NOT interrupt the display."""
    arb = CastArbiter()
    arb.handle(Event.MIRACAST_CONNECTED)
    t = arb.handle(Event.AIRPLAY_CONNECTED)
    assert arb.state is State.MIRACAST, "AirPlay must not preempt an active Miracast session"
    assert t.actions == (Action.LOG_REJECTED_CONNECTION,)


def test_miracast_connect_rejected_while_airplay_active():
    arb = CastArbiter()
    arb.handle(Event.AIRPLAY_CONNECTED)
    t = arb.handle(Event.MIRACAST_CONNECTED)
    assert arb.state is State.AIRPLAY
    assert t.actions == (Action.LOG_REJECTED_CONNECTION,)


def test_miracast_disconnect_returns_to_idle_and_resumes_airplay():
    arb = CastArbiter()
    arb.handle(Event.MIRACAST_CONNECTED)
    t = arb.handle(Event.MIRACAST_DISCONNECTED)
    assert t.next_state is State.IDLE
    assert Action.RESUME_AIRPLAY_ADVERTISING in t.actions
    assert Action.STOP_RENDER_PIPELINE in t.actions
    assert Action.SHOW_IDLE_SCREEN in t.actions


def test_airplay_disconnect_returns_to_idle_and_resumes_miracast():
    arb = CastArbiter()
    arb.handle(Event.AIRPLAY_CONNECTED)
    t = arb.handle(Event.AIRPLAY_DISCONNECTED)
    assert t.next_state is State.IDLE
    assert Action.RESUME_MIRACAST_DISCOVERY in t.actions


def test_watchdog_timeout_forces_teardown_from_miracast():
    arb = CastArbiter()
    arb.handle(Event.MIRACAST_CONNECTED)
    t = arb.handle(Event.WATCHDOG_TIMEOUT)
    assert t.next_state is State.IDLE
    assert Action.FORCE_TEARDOWN_MIRACAST in t.actions
    assert Action.RESUME_AIRPLAY_ADVERTISING in t.actions


def test_watchdog_timeout_forces_teardown_from_airplay():
    arb = CastArbiter()
    arb.handle(Event.AIRPLAY_CONNECTED)
    t = arb.handle(Event.WATCHDOG_TIMEOUT)
    assert t.next_state is State.IDLE
    assert Action.FORCE_TEARDOWN_AIRPLAY in t.actions
    assert Action.RESUME_MIRACAST_DISCOVERY in t.actions


def test_stray_disconnect_from_idle_is_a_noop_not_a_crash():
    arb = CastArbiter()
    t = arb.handle(Event.MIRACAST_DISCONNECTED)
    assert arb.state is State.IDLE
    assert t.actions == (Action.LOG_REJECTED_CONNECTION,)


def test_full_cycle_50_times_always_returns_cleanly_to_idle():
    """Directly exercises the project's own soak-test concern (#3 in the
    'unresolved issues' list: repeated connect/disconnect cycling must not
    accumulate state or drift into a bad transition)."""
    arb = CastArbiter()
    for _ in range(50):
        arb.handle(Event.MIRACAST_CONNECTED)
        assert arb.state is State.MIRACAST
        arb.handle(Event.MIRACAST_DISCONNECTED)
        assert arb.state is State.IDLE
        arb.handle(Event.AIRPLAY_CONNECTED)
        assert arb.state is State.AIRPLAY
        arb.handle(Event.AIRPLAY_DISCONNECTED)
        assert arb.state is State.IDLE
