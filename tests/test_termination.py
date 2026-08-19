"""The slide must always end, and must always hand the view model back.

This is the regression file for the bug the physics rework shipped: every exit condition lived in
`hooks._phys_sliding`, a PRE hook on `Engine.Pawn:PhysWalking`. The hook registered cleanly and then
never did a frame of work, so nothing decayed, nothing reached an exit, and both the slide state and
the dipped weapon pose stayed latched on for the rest of the session.

Nothing in the existing suite could see it. `tests/simulate_slide.py` calls `_phys_sliding` directly,
so it proves the physics math and assumes away the only question that mattered - whether the engine
ever calls that hook. These tests drive `handle_move` instead, the PRE PlayerMove hook that is proven
to fire, and assert on the outcome the player actually experiences: the slide stops, and the gun
comes back up.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from BL2_Movement import events, hooks, lifecycle
from BL2_Movement.config import CROUCHED_PCT_DEFAULT, SLIDE_SPEED_DEFAULT, decay_rate, max_duration
from BL2_Movement.state import OWN_SLIDE_STATE, State

FRAME = 1 / 60


class _Pawn:
    def __init__(self, *, grounded: bool = True, z: float = 0.0):
        self.Location = SimpleNamespace(Z=z)
        self.Velocity = SimpleNamespace(X=0.0, Y=0.0, Z=0.0)
        self.Acceleration = SimpleNamespace(X=0.0, Y=0.0, Z=0.0)
        self.CrouchedPct = SLIDE_SPEED_DEFAULT
        self.GroundSpeed = 440.0
        self.grounded = grounded

    def IsOnGroundOrShortFall(self) -> bool:
        return self.grounded

    def DoJump(self, _b: bool) -> None:
        pass


def _pc(*, ducking: bool = True, grounded: bool = True):
    return SimpleNamespace(
        Pawn=_Pawn(grounded=grounded),
        bDuck=ducking,
        bInSprintState=True,
        PlayerReplicationInfo=SimpleNamespace(PlayerName="Tester", PlayerID=1),
    )


@pytest.fixture(autouse=True)
def _fresh_slide():
    """Every test starts mid-slide with clean cross-frame scratch, and leaves none behind."""
    OWN_SLIDE_STATE.is_sliding = True
    OWN_SLIDE_STATE.speed_pct = SLIDE_SPEED_DEFAULT
    OWN_SLIDE_STATE.elapsed = 0.0
    OWN_SLIDE_STATE.old_z = 0.0
    OWN_SLIDE_STATE.dir_x, OWN_SLIDE_STATE.dir_y = 1.0, 0.0
    OWN_SLIDE_STATE.entry_x, OWN_SLIDE_STATE.entry_y = 1.0, 0.0
    State.do_slide_jump = False
    hooks._Phys.last_frame = -1.0
    yield
    OWN_SLIDE_STATE.is_sliding = False
    State.do_slide_jump = False


@pytest.fixture
def ended():
    """Records every `slide_ended` fire, then unsubscribes."""
    calls: list[object] = []
    events.slide_ended.append(calls.append)
    yield calls
    events.slide_ended.remove(calls.append)


def _run(pc, frames: int = 600) -> int:
    """Pump handle_move until the slide ends. Returns the frame count it took."""
    for frame in range(1, frames + 1):
        hooks.handle_move(pc, SimpleNamespace(DeltaTime=FRAME), None, None)
        if not OWN_SLIDE_STATE.is_sliding:
            return frame
    pytest.fail(f"slide still running after {frames} frames")
    return -1  # unreachable, keeps the type checker happy


# --- the slide ends -------------------------------------------------------------------------------


def test_decay_ends_the_slide(ended):
    """Left alone on flat ground, the slide terminates on the decay curve."""
    frames = _run(_pc())

    expected = (SLIDE_SPEED_DEFAULT - CROUCHED_PCT_DEFAULT) / decay_rate.value
    assert frames * FRAME == pytest.approx(expected, abs=0.05)
    assert len(ended) == 1


def test_duration_cap_ends_the_slide(ended, monkeypatch):
    """A decay slow enough to run forever still hits the hard duration cap."""
    monkeypatch.setattr(decay_rate, "value", 0.0)
    frames = _run(_pc())

    assert frames * FRAME == pytest.approx(max_duration.value, abs=0.05)
    assert len(ended) == 1


def test_releasing_crouch_ends_the_slide_immediately(ended):
    """The gate that the shipped build lost: crouch released is an instant exit."""
    assert _run(_pc(ducking=False)) == 1
    assert len(ended) == 1


def test_leaving_the_ground_ends_the_slide_immediately(ended):
    """The other gate that went missing - airborne is an instant exit."""
    assert _run(_pc(grounded=False)) == 1
    assert len(ended) == 1


# --- the view model always comes back --------------------------------------------------------------


def test_slide_ended_fires_even_when_the_pawn_is_gone(ended):
    """A None pawn is routine at respawn, and must not strand the weapon in its slide pose."""
    pc = SimpleNamespace(Pawn=None, PlayerReplicationInfo=SimpleNamespace(PlayerName="x", PlayerID=1))
    lifecycle.exit_slide(pc)

    assert OWN_SLIDE_STATE.is_sliding is False
    assert len(ended) == 1


def test_exit_is_idempotent(ended):
    """Two exits on the same slide fire the view model once, not twice."""
    pc = _pc()
    lifecycle.exit_slide(pc)
    lifecycle.exit_slide(pc)

    assert len(ended) == 1


def test_crouch_multiplier_is_released_on_exit():
    """CrouchedPct pinned at slide speed is what left the player permanently fast."""
    pc = _pc()
    lifecycle.exit_slide(pc)

    assert pc.Pawn.CrouchedPct == CROUCHED_PCT_DEFAULT


def test_a_finished_slide_can_be_restarted(ended):
    """`enter_slide` refuses re-entry while the flag is set, so a stuck flag locks sliding out."""
    _run(_pc())
    assert OWN_SLIDE_STATE.is_sliding is False

    hooks.handle_duck(SimpleNamespace(Outer=_pc()), None, None, None)
    assert OWN_SLIDE_STATE.is_sliding is True
