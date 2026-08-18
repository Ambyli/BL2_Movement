"""Tests for state.begin_slide_state — the entry-heading lock."""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest
from BL2_Movement.config import SLIDE_SPEED_DEFAULT
from BL2_Movement.state import PlayerSlideState, begin_slide_state


def _pawn(vx: float = 0.0, vy: float = 0.0, vz: float = 0.0):
    """Minimal fake pawn: only `.Velocity.X/Y/Z` are read by `begin_slide_state`."""
    return SimpleNamespace(Velocity=SimpleNamespace(X=vx, Y=vy, Z=vz))


def _fresh_state() -> PlayerSlideState:
    # speed_pct and elapsed start at non-default values so we can prove the function resets them.
    return PlayerSlideState(old_z=0.0, is_sliding=True, speed_pct=1.7, elapsed=9.9)


def test_locks_normalized_heading_from_velocity():
    state = _fresh_state()
    begin_slide_state(_pawn(vx=3.0, vy=4.0), state)

    # 3-4-5 triangle: normalized is (0.6, 0.8).
    assert state.dir_x == pytest.approx(0.6)
    assert state.dir_y == pytest.approx(0.8)
    assert math.hypot(state.dir_x, state.dir_y) == pytest.approx(1.0)


def test_entry_heading_matches_direction_at_start():
    state = _fresh_state()
    begin_slide_state(_pawn(vx=3.0, vy=4.0), state)

    assert state.entry_x == state.dir_x
    assert state.entry_y == state.dir_y


def test_resets_speed_and_elapsed():
    state = _fresh_state()
    begin_slide_state(_pawn(vx=3.0, vy=4.0), state)

    assert state.speed_pct == SLIDE_SPEED_DEFAULT
    assert state.elapsed == 0.0


def test_ignores_z_component():
    """Sliding is a ground-plane thing. Vertical velocity must not tilt the heading."""
    state = _fresh_state()
    begin_slide_state(_pawn(vx=3.0, vy=4.0, vz=999.0), state)

    assert math.hypot(state.dir_x, state.dir_y) == pytest.approx(1.0)


def test_stationary_pawn_gets_zero_heading():
    """Below the 1.0-unit magnitude floor, heading zeroes rather than dividing by ~0."""
    state = _fresh_state()
    begin_slide_state(_pawn(vx=0.5, vy=0.5), state)  # magnitude ~0.71

    assert state.dir_x == 0.0
    assert state.dir_y == 0.0
    assert state.entry_x == 0.0
    assert state.entry_y == 0.0
    # Speed and elapsed still reset even on the zero-heading path.
    assert state.speed_pct == SLIDE_SPEED_DEFAULT
    assert state.elapsed == 0.0
