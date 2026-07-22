"""Idle/Miracast/AirPlay arbitration state machine.

Pure transition logic: no sockets, no subprocess, no D-Bus. Given the current
state and an incoming event, returns the next state plus a list of side-effect
actions for the caller to execute. This is what replaces the old shell main
loop's ad-hoc pgrep/kill checks -- every transition is an explicit, testable
table entry instead of a script re-deriving "what state are we probably in"
from process lists.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class State(Enum):
    IDLE = auto()
    MIRACAST = auto()
    AIRPLAY = auto()


class Event(Enum):
    MIRACAST_CONNECTED = auto()
    MIRACAST_DISCONNECTED = auto()
    AIRPLAY_CONNECTED = auto()
    AIRPLAY_DISCONNECTED = auto()
    WATCHDOG_TIMEOUT = auto()  # negotiated but no data flowing for N seconds


class Action(Enum):
    SHOW_IDLE_SCREEN = auto()
    STOP_RENDER_PIPELINE = auto()
    PAUSE_AIRPLAY_ADVERTISING = auto()
    RESUME_AIRPLAY_ADVERTISING = auto()
    PAUSE_MIRACAST_DISCOVERY = auto()
    RESUME_MIRACAST_DISCOVERY = auto()
    FORCE_TEARDOWN_MIRACAST = auto()
    FORCE_TEARDOWN_AIRPLAY = auto()
    LOG_REJECTED_CONNECTION = auto()


@dataclass(frozen=True)
class Transition:
    next_state: State
    actions: tuple[Action, ...]


class ArbitrationPolicyError(Exception):
    """Raised only for events that should be structurally impossible (a bug
    upstream), never for ordinary contention -- ordinary contention is
    handled by staying in place and emitting LOG_REJECTED_CONNECTION."""


class CastArbiter:
    """First-connect-wins arbitration: whichever protocol connects first owns
    the display until it disconnects or the watchdog fires; the other
    protocol's advertising is paused so a second device can't interrupt an
    in-progress presentation."""

    def __init__(self) -> None:
        self.state = State.IDLE

    def handle(self, event: Event) -> Transition:
        transition = _TABLE.get((self.state, event))
        if transition is None:
            # Unmodeled (state, event) pairs are contention/no-ops, e.g. a
            # second AIRPLAY_CONNECTED while already in AIRPLAY (a client
            # reconnect blip), or a DISCONNECTED for a protocol that isn't
            # currently holding the display. Stay put; log for diagnostics.
            transition = Transition(self.state, (Action.LOG_REJECTED_CONNECTION,))
        self.state = transition.next_state
        return transition


_TABLE: dict[tuple[State, Event], Transition] = {
    # No render-start action here: the Miracast pipeline can only start once
    # WFD negotiation yields a real RTP port (see castd.main.
    # handle_miracast_connected), so main.py starts it explicitly after a
    # successful negotiate() call rather than the FSM triggering it blind.
    (State.IDLE, Event.MIRACAST_CONNECTED): Transition(
        State.MIRACAST,
        # PAUSE_AIRPLAY_ADVERTISING stops uxplay so an iPhone can't barge in;
        # PAUSE_MIRACAST_DISCOVERY flips the WFD IE to "busy" so a SECOND
        # Windows sees the room as occupied instead of interrupting the
        # person presenting. Both restored on MIRACAST_DISCONNECTED.
        (Action.PAUSE_AIRPLAY_ADVERTISING, Action.PAUSE_MIRACAST_DISCOVERY),
    ),
    # UxPlay owns its own GStreamer pipeline once a client connects (see
    # airplay/uxplay.py's -vs kmssink); the only thing this sink needs to do
    # is release the DRM device from the idle-screen pipeline so UxPlay can
    # acquire it.
    (State.IDLE, Event.AIRPLAY_CONNECTED): Transition(
        State.AIRPLAY,
        (Action.PAUSE_MIRACAST_DISCOVERY, Action.STOP_RENDER_PIPELINE),
    ),
    (State.MIRACAST, Event.MIRACAST_DISCONNECTED): Transition(
        State.IDLE,
        (
            Action.STOP_RENDER_PIPELINE,
            Action.RESUME_AIRPLAY_ADVERTISING,
            Action.RESUME_MIRACAST_DISCOVERY,
            Action.SHOW_IDLE_SCREEN,
        ),
    ),
    (State.MIRACAST, Event.WATCHDOG_TIMEOUT): Transition(
        State.IDLE,
        (
            Action.FORCE_TEARDOWN_MIRACAST,
            Action.STOP_RENDER_PIPELINE,
            Action.RESUME_AIRPLAY_ADVERTISING,
            Action.RESUME_MIRACAST_DISCOVERY,
            Action.SHOW_IDLE_SCREEN,
        ),
    ),
    (State.AIRPLAY, Event.AIRPLAY_DISCONNECTED): Transition(
        State.IDLE,
        (Action.STOP_RENDER_PIPELINE, Action.RESUME_MIRACAST_DISCOVERY, Action.SHOW_IDLE_SCREEN),
    ),
    (State.AIRPLAY, Event.WATCHDOG_TIMEOUT): Transition(
        State.IDLE,
        (
            Action.FORCE_TEARDOWN_AIRPLAY,
            Action.STOP_RENDER_PIPELINE,
            Action.RESUME_MIRACAST_DISCOVERY,
            Action.SHOW_IDLE_SCREEN,
        ),
    ),
}
