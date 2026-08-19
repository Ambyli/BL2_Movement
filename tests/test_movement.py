"""Tests for movement.slide - decay curve and exit verdict.

These are pure-math tests. `slide()` reads pawn Z, mutates state, and *returns* whether the slide is
spent - it no longer sends the exit itself. Reporting rather than dispatching is what lets the local
player's exit be a direct `exit_slide` call instead of a queued network message to itself, and it is
what makes these tests able to assert the verdict without faking the networking layer at all.
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
    """Minimal fake PC: `slide()` only reads `pc.Pawn.Location.Z`."""
    return SimpleNamespace(
        Pawn=SimpleNamespace(Location=SimpleNamespace(Z=z)),
        PlayerReplicationInfo=object(),
    )


def _state(speed_pct: float = SLIDE_SPEED_DEFAULT, elapsed: float = 0.0, old_z: float = 0.0):
    return PlayerSlideState(old_z=old_z, is_sliding=True, speed_pct=speed_pct, elapsed=elapsed)


# --- decay curve ----------------------------------------------------------------------------------


def test_flat_ground_bleeds_speed_at_configured_decay_rate():
    """One second on flat ground -> speed drops by exactly `decay_rate.value`."""
    state = _state(speed_pct=SLIDE_SPEED_DEFAULT)
    spent = movement.slide(_pc_at(z=0.0), state, delta_time=1.0)

    assert state.speed_pct == pytest.approx(SLIDE_SPEED_DEFAULT - decay_rate.value)
    assert spent is False


def test_uphill_bleeds_faster_than_flat():
    """A frame that gained Z sheds extra speed on top of the time decay."""
    flat = _state()
    uphill = _state()

    movement.slide(_pc_at(z=0.0), flat, delta_time=1.0)
    movement.slide(_pc_at(z=50.0), uphill, delta_time=1.0)

    assert uphill.speed_pct < flat.speed_pct


def test_downhill_bleeds_slower_than_flat():
    """A frame that lost Z wins some speed back against the time decay."""
    flat = _state()
    downhill = _state()

    movement.slide(_pc_at(z=0.0), flat, delta_time=1.0)
    movement.slide(_pc_at(z=-50.0), downhill, delta_time=1.0)

    assert downhill.speed_pct > flat.speed_pct


def test_downhill_never_exceeds_starting_speed():
    """The `min(speed, SLIDE_SPEED_DEFAULT)` cap keeps a steep drop from re-accelerating past entry."""
    state = _state()
    # Absurd downhill - enough that the raw math would produce speed > SLIDE_SPEED_DEFAULT.
    movement.slide(_pc_at(z=-10_000.0), state, delta_time=0.001)

    assert state.speed_pct <= SLIDE_SPEED_DEFAULT


def test_old_z_advances_each_frame():
    state = _state(old_z=0.0)
    movement.slide(_pc_at(z=42.0), state, delta_time=0.5)

    assert state.old_z == 42.0


def test_elapsed_accumulates_each_frame():
    state = _state(elapsed=0.4)
    movement.slide(_pc_at(z=0.0), state, delta_time=0.25)

    assert state.elapsed == pytest.approx(0.65)


# --- exit verdict ---------------------------------------------------------------------------------


def test_reports_spent_when_speed_drops_below_floor():
    # 0.55 - 1.15 * 0.1 = 0.435 - one small frame past the floor.
    state = _state(speed_pct=0.55)
    spent = movement.slide(_pc_at(z=0.0), state, delta_time=0.1)

    assert spent is True
    assert state.speed_pct < CROUCHED_PCT_DEFAULT


def test_reports_spent_when_elapsed_hits_max_duration():
    """Even at full speed, the hard duration cap must end the slide."""
    state = _state(speed_pct=SLIDE_SPEED_DEFAULT, elapsed=max_duration.value - 0.01)
    spent = movement.slide(_pc_at(z=0.0), state, delta_time=0.02)

    assert spent is True
    # Speed is still healthy - the exit is purely time-driven here.
    assert state.speed_pct > CROUCHED_PCT_DEFAULT


def test_does_not_report_spent_mid_slide():
    """A healthy slide with time on the clock should not trigger an exit."""
    state = _state()
    spent = movement.slide(_pc_at(z=0.0), state, delta_time=0.05)

    assert spent is False


def test_flat_slide_is_spent_within_its_advertised_length():
    """The decay curve must actually terminate, at roughly the length the tuning advertises.

    The regression this file exists for: with decay 1.15 running 2.2 -> 0.5, the slide is about
    1.5s long. A slide that never reports spent is the bug that shipped in the physics rework.
    """
    state = _state()
    pc = _pc_at(z=0.0)
    frames = 0
    while not movement.slide(pc, state, delta_time=1 / 60):
        frames += 1
        assert frames < 600, "slide never reported spent"

    elapsed = frames / 60
    expected = (SLIDE_SPEED_DEFAULT - CROUCHED_PCT_DEFAULT) / decay_rate.value
    assert elapsed == pytest.approx(expected, abs=0.05)
    assert elapsed < max_duration.value
