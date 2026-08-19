"""Tests for movement.apply_slide_physics — steering, turn-cone clamp, and pawn writes.

`apply_slide_physics` runs from the POST-hook path (local player) and from the host tick (remote
pawns). It reads pawn.Acceleration for steering intent, blends the slide's direction toward that
input, clamps rotation to a cone around the entry heading, and force-writes the resulting velocity
and CrouchedPct back onto the pawn.

These tests exercise the pure math — steering blend, cone clamp, back-cutoff filter — without any
game hook involvement.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from BL2_Movement import movement
from BL2_Movement.config import (
    SLIDE_SPEED_DEFAULT,
    max_turn_degrees,
    start_speed,
)
from BL2_Movement.state import PlayerSlideState


def _pawn(ax: float = 0.0, ay: float = 0.0, ground_speed: float = 440.0):
    """Fake pawn with mutable Acceleration/Velocity fields matching the pieces `apply_slide_physics`
    reads and writes."""
    return SimpleNamespace(
        Acceleration=SimpleNamespace(X=ax, Y=ay, Z=0.0),
        Velocity=SimpleNamespace(X=0.0, Y=0.0, Z=0.0),
        CrouchedPct=SLIDE_SPEED_DEFAULT,
        GroundSpeed=ground_speed,
    )


def _state(dir_x: float = 1.0, dir_y: float = 0.0, entry_x: float | None = None, entry_y: float | None = None):
    """State with an entry heading. Defaults: sliding straight +X with entry matching direction."""
    return PlayerSlideState(
        old_z=0.0,
        is_sliding=True,
        dir_x=dir_x,
        dir_y=dir_y,
        entry_x=dir_x if entry_x is None else entry_x,
        entry_y=dir_y if entry_y is None else entry_y,
        speed_pct=SLIDE_SPEED_DEFAULT,
        elapsed=0.0,
    )


def _drive(pawn, state, ax: float, ay: float, frames: int, dt: float = 0.016):
    """Sustain an input across many frames. `apply_slide_physics` zeros Acceleration at the end of
    each call, so tests have to re-inject it every frame."""
    for _ in range(frames):
        pawn.Acceleration.X = ax
        pawn.Acceleration.Y = ay
        movement.apply_slide_physics(pawn, state, dt)


# --- short-circuits -------------------------------------------------------------------------------


def test_zero_direction_returns_before_touching_pawn():
    """A slide with no heading (below-floor entry velocity) must not write to the pawn."""
    state = _state(dir_x=0.0, dir_y=0.0, entry_x=0.0, entry_y=0.0)
    pawn = _pawn()
    original_velocity = (pawn.Velocity.X, pawn.Velocity.Y)

    movement.apply_slide_physics(pawn, state, delta_time=0.016)

    assert original_velocity == (pawn.Velocity.X, pawn.Velocity.Y)


def test_no_input_leaves_direction_unchanged():
    """Acceleration=(0,0) skips the whole steering branch."""
    state = _state()
    pawn = _pawn(ax=0.0, ay=0.0)

    movement.apply_slide_physics(pawn, state, delta_time=0.1)

    assert state.dir_x == pytest.approx(1.0)
    assert state.dir_y == pytest.approx(0.0)


# --- back-cutoff ----------------------------------------------------------------------------------


def test_direct_backwards_input_is_ignored():
    """Input pointing straight back (dot = -1) is past SLIDE_BACK_CUTOFF (-0.5), skipped entirely."""
    state = _state()
    pawn = _pawn(ax=-1.0, ay=0.0)  # opposite of dir=(1,0)

    movement.apply_slide_physics(pawn, state, delta_time=0.5)

    assert state.dir_x == pytest.approx(1.0)
    assert state.dir_y == pytest.approx(0.0)


# --- steering -------------------------------------------------------------------------------------


def test_sideways_input_rotates_direction_toward_input():
    """One frame of purely sideways input nudges direction slightly toward the input side."""
    state = _state()
    pawn = _pawn(ax=0.0, ay=1.0)

    movement.apply_slide_physics(pawn, state, delta_time=0.016)

    assert state.dir_y > 0.0
    assert state.dir_x < 1.0
    assert math.hypot(state.dir_x, state.dir_y) == pytest.approx(1.0)


# --- turn-cone clamp ------------------------------------------------------------------------------


def test_turn_cone_clamps_to_max_turn_degrees():
    """Sustained sideways input rotates direction until it pins at the cone edge, then stays."""
    state = _state()
    pawn = _pawn()

    _drive(pawn, state, ax=0.0, ay=1.0, frames=400)

    cos_limit = math.cos(math.radians(max_turn_degrees.value))
    sin_limit = math.sin(math.radians(max_turn_degrees.value))

    # Direction pinned exactly at the +Y edge of the cone around (1,0).
    assert state.dir_x == pytest.approx(cos_limit, abs=1e-6)
    assert state.dir_y == pytest.approx(sin_limit, abs=1e-6)


def test_cone_can_pin_to_either_side():
    """The cone is symmetric - sustained input on the -Y side pins to the mirror edge."""
    state = _state()
    pawn = _pawn()

    _drive(pawn, state, ax=0.0, ay=-1.0, frames=400)

    cos_limit = math.cos(math.radians(max_turn_degrees.value))
    sin_limit = math.sin(math.radians(max_turn_degrees.value))

    assert state.dir_x == pytest.approx(cos_limit, abs=1e-6)
    assert state.dir_y == pytest.approx(-sin_limit, abs=1e-6)


def test_direction_stays_within_cone_throughout():
    """Snapshot the max angle reached at any point during a sustained turn — never past the limit."""
    state = _state()
    pawn = _pawn()

    max_offset_seen = 0.0
    for _ in range(400):
        pawn.Acceleration.X = 0.0
        pawn.Acceleration.Y = 1.0
        movement.apply_slide_physics(pawn, state, 0.016)
        along = state.dir_x * state.entry_x + state.dir_y * state.entry_y
        angle = math.degrees(math.acos(max(-1.0, min(1.0, along))))
        max_offset_seen = max(max_offset_seen, angle)

    # A tiny numerical slop budget on top of the configured limit.
    assert max_offset_seen <= max_turn_degrees.value + 1e-4


# --- pawn writes ----------------------------------------------------------------------------------


def test_writes_velocity_from_direction_and_speed():
    state = _state()
    pawn = _pawn()

    movement.apply_slide_physics(pawn, state, delta_time=0.016)

    # speed_pct == SLIDE_SPEED_DEFAULT so speed == start_speed.value.
    expected = start_speed.value
    assert pytest.approx(expected * state.dir_x) == pawn.Velocity.X
    assert pytest.approx(expected * state.dir_y) == pawn.Velocity.Y


def test_zeros_pawn_acceleration():
    """Acceleration is cleared so the engine has nothing to fight the forced velocity with."""
    state = _state()
    pawn = _pawn(ax=0.0, ay=1.0)

    movement.apply_slide_physics(pawn, state, delta_time=0.016)

    assert pawn.Acceleration.X == 0.0
    assert pawn.Acceleration.Y == 0.0


def test_speed_scales_with_speed_pct():
    """Halfway through the decay curve → half the base slide speed."""
    state = _state()
    # speed_pct halfway between the cap (SLIDE_SPEED_DEFAULT=2.2) and the floor (implicitly ~0)
    state.speed_pct = SLIDE_SPEED_DEFAULT / 2.0
    pawn = _pawn()

    movement.apply_slide_physics(pawn, state, delta_time=0.016)

    expected = start_speed.value * 0.5
    assert math.hypot(pawn.Velocity.X, pawn.Velocity.Y) == pytest.approx(expected)


# --- acceleration is the client's only outgoing steering signal --------------------------------------


def test_acceleration_is_zeroed_when_we_are_the_authority(monkeypatch):
    """Host or single player: nothing downstream depends on Acceleration, so clear it."""
    monkeypatch.setattr(movement, "is_client", lambda: False)
    pawn = _pawn(ax=1.0, ay=0.0)
    movement.apply_slide_physics(pawn, _state(), delta_time=1 / 60)

    assert pawn.Acceleration.X == 0.0
    assert pawn.Acceleration.Y == 0.0


def test_acceleration_is_preserved_on_a_client(monkeypatch):
    """On a client, Acceleration is what the ServerMove packet carries about our heading - the host
    steers its copy of our pawn by it and by nothing else. Wiping it left the host guessing, which
    is what made a client's slide run off along a fixed bearing regardless of where they set off.
    """
    monkeypatch.setattr(movement, "is_client", lambda: True)
    pawn = _pawn(ax=0.6, ay=-0.8)
    movement.apply_slide_physics(pawn, _state(), delta_time=1 / 60)

    assert (pawn.Acceleration.X, pawn.Acceleration.Y) == (0.6, -0.8)


def test_velocity_is_still_forced_on_a_client(monkeypatch):
    """Keeping Acceleration must not cost us the forced velocity - that is the slide itself."""
    monkeypatch.setattr(movement, "is_client", lambda: True)
    pawn = _pawn()
    movement.apply_slide_physics(pawn, _state(dir_x=1.0, dir_y=0.0), delta_time=1 / 60)

    assert pawn.Velocity.X > 0.0
    assert pawn.Velocity.Y == 0.0
