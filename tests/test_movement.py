"""Tests for movement.slide — decay curve and exit triggers.

These are pure-math tests. `slide()` reads pawn Z, mutates state, and calls `client_exit_slide` when
it hits either the speed floor or the duration cap. Tests spy on `client_exit_slide` via monkeypatch
rather than trusting the conftest's no-op wrapper, so we can positively assert that the trigger
fired (or didn't).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from BL2_Movement import movement
from BL2_Movement.config import (
    CROUCHED_PCT_DEFAULT,
    SLIDE_SPEED_DEFAULT,
    decay_rate,
    max_duration,
)
from BL2_Movement.state import PlayerSlideState


def _pc_at(z: float):
    """Minimal fake PC: `slide()` only reads `pc.Pawn.Location.Z` and `pc.PlayerReplicationInfo`."""
    return SimpleNamespace(
        Pawn=SimpleNamespace(Location=SimpleNamespace(Z=z)),
        PlayerReplicationInfo=object(),
    )


def _state(speed_pct: float = SLIDE_SPEED_DEFAULT, elapsed: float = 0.0, old_z: float = 0.0):
    return PlayerSlideState(old_z=old_z, is_sliding=True, speed_pct=speed_pct, elapsed=elapsed)


@pytest.fixture
def spy_exit(monkeypatch):
    """Replace `client_exit_slide` with a call recorder for the duration of the test."""
    calls: list[object] = []
    monkeypatch.setattr(movement, "client_exit_slide", calls.append)
    return calls


# --- decay curve ----------------------------------------------------------------------------------


def test_flat_ground_bleeds_speed_at_configured_decay_rate(spy_exit):
    """One second on flat ground → speed drops by exactly `decay_rate.value`."""
    state = _state(speed_pct=SLIDE_SPEED_DEFAULT)
    movement.slide(_pc_at(z=0.0), state, delta_time=1.0)

    assert state.speed_pct == pytest.approx(SLIDE_SPEED_DEFAULT - decay_rate.value)
    assert spy_exit == []


def test_uphill_bleeds_faster_than_flat(spy_exit):
    """A frame that gained Z sheds extra speed on top of the time decay."""
    flat = _state()
    uphill = _state()

    movement.slide(_pc_at(z=0.0), flat, delta_time=1.0)
    movement.slide(_pc_at(z=50.0), uphill, delta_time=1.0)

    assert uphill.speed_pct < flat.speed_pct


def test_downhill_bleeds_slower_than_flat(spy_exit):
    """A frame that lost Z wins some speed back against the time decay."""
    flat = _state()
    downhill = _state()

    movement.slide(_pc_at(z=0.0), flat, delta_time=1.0)
    movement.slide(_pc_at(z=-50.0), downhill, delta_time=1.0)

    assert downhill.speed_pct > flat.speed_pct


def test_downhill_never_exceeds_starting_speed(spy_exit):
    """The `min(speed, SLIDE_SPEED_DEFAULT)` cap keeps a steep drop from re-accelerating past entry."""
    state = _state()
    # Absurd downhill — enough that the raw math would produce speed > SLIDE_SPEED_DEFAULT.
    movement.slide(_pc_at(z=-10_000.0), state, delta_time=0.001)

    assert state.speed_pct <= SLIDE_SPEED_DEFAULT


def test_old_z_advances_each_frame(spy_exit):
    state = _state(old_z=0.0)
    movement.slide(_pc_at(z=42.0), state, delta_time=0.5)

    assert state.old_z == 42.0


def test_elapsed_accumulates_each_frame(spy_exit):
    state = _state(elapsed=0.4)
    movement.slide(_pc_at(z=0.0), state, delta_time=0.25)

    assert state.elapsed == pytest.approx(0.65)


# --- exit triggers --------------------------------------------------------------------------------


def test_exits_when_speed_drops_below_floor(spy_exit):
    # 0.55 - 1.15 * 0.1 = 0.435 — one small frame past the floor.
    state = _state(speed_pct=0.55)
    movement.slide(_pc_at(z=0.0), state, delta_time=0.1)

    assert len(spy_exit) == 1
    assert state.speed_pct < CROUCHED_PCT_DEFAULT


def test_exits_when_elapsed_hits_max_duration(spy_exit):
    """Even at full speed, the hard duration cap must end the slide."""
    state = _state(speed_pct=SLIDE_SPEED_DEFAULT, elapsed=max_duration.value - 0.01)
    movement.slide(_pc_at(z=0.0), state, delta_time=0.02)

    assert len(spy_exit) == 1
    # Speed is still healthy — the exit is purely time-driven here.
    assert state.speed_pct > CROUCHED_PCT_DEFAULT


def test_does_not_exit_mid_slide(spy_exit):
    """A healthy slide with time on the clock should not trigger an exit."""
    state = _state()
    movement.slide(_pc_at(z=0.0), state, delta_time=0.05)

    assert spy_exit == []
