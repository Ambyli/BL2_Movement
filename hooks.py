"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere.

Nothing here drives a slide. Entering one hands off to `lifecycle`, which starts a coroutine that
owns the slide for its whole life; these hooks only catch the three inputs that start it, jump out
of it, and carry its momentum across.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from mods_base import hook
from uemath import Vector
from unrealsdk import unreal

from .debug import every_n, log
from .lifecycle import enter_slide, server_set_slide_jump_velocity
from .state import OWN_SLIDE_STATE, State, player_id

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


@hook("WillowGame.WillowPlayerInput:Jump")
def jump(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Stash horizontal velocity on a slide jump, to hand to the host a frame later.

    Runs on: whichever machine's local player pressed Jump. The handoff itself happens next frame
    from `handle_move`, not here.
    """
    log.info(f"jump enter is_sliding={OWN_SLIDE_STATE.is_sliding}")
    if OWN_SLIDE_STATE.is_sliding:
        # Snapshot before the engine's Jump processing changes it.
        pc = cast("WillowPlayerController", obj.Outer)
        vel: Vector = Vector(pc.Pawn.Velocity)
        vel.z = 0
        State.horizontal_velocity = vel
        State.do_slide_jump = True
        log.info(
            f"jump stashed vel=({vel.x:.0f},{vel.y:.0f}) do_slide_jump=True",
        )
    log.info("jump exit")


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def handle_move(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """The slide jump's two-frame handoff, which spans two frames by nature.

    The `Jump` hook stashes horizontal velocity; on this frame we `DoJump(True)` to leave the
    ground, and on the next fire - now airborne - we hand the stashed velocity to the host so its
    copy of the pawn keeps the momentum through the arc. Leaving the ground is also what ends the
    slide, one tick later, through the driver's own gate.

    Runs on: BOTH, for the local player only.
    """
    verbose = every_n("handle_move", 30)
    if verbose:
        log.debug(f"handle_move enter do_slide_jump={State.do_slide_jump}")
    if not State.do_slide_jump:
        if verbose:
            log.debug("handle_move exit reason=no_pending_jump")
        return

    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        # Respawn or level transition mid-jump. Drop the pending handoff rather than leaving it set
        # for whatever pawn arrives next.
        State.do_slide_jump = False
        log.info("handle_move exit reason=no_pawn dropped_handoff")
        return

    if pawn.IsOnGroundOrShortFall():
        pawn.DoJump(True)
        log.info("handle_move DoJump called reason=still_grounded")
        return

    # Clear the flag before sending, not after: a send that raises would otherwise leave the handoff
    # pending and retry - and fail - on every airborne frame.
    State.do_slide_jump = False
    server_set_slide_jump_velocity(
        State.horizontal_velocity.x,
        State.horizontal_velocity.y,
    )
    log.info(
        f"handle_move exit sent vel=({State.horizontal_velocity.x:.0f},{State.horizontal_velocity.y:.0f})",
    )


@hook("WillowGame.WillowPlayerInput:DuckPressed")
def handle_duck(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Crouching while sprinting starts a slide.

    Runs on: whichever machine's local player pressed duck.
    """
    # DuckPressed's `obj` is the WillowPlayerInput, whose Outer is the controller the rest of the
    # mod works with.
    pc = cast("WillowPlayerController", obj.Outer)
    log.info(f"handle_duck enter sprinting={bool(pc.bInSprintState)}")
    if pc.bInSprintState:
        # enter_slide starts a driver and sends a message; a raise from either would leave state
        # half-populated. Log and continue so one bad slide cannot wedge every later one.
        try:
            enter_slide(pc)
        except Exception as ex:  # noqa: BLE001 - never break the input path
            log.warning(f"ENTER FAILED {type(ex).__name__}: {ex}")
    log.info("handle_duck exit")


# DIAGNOSTIC (temporary) --------------------------------------------------------------------------
# Measure the real inter-arrival interval of the client's server-move RPCs on the host, per player
# and per variant, to settle whether the ~4Hz teleport cadence is the move-send rate (NetMoveDelta
# predicts ~45Hz) or something downstream. These execute server-side, so they fire on the HOST only,
# and only for remote clients' pawns (the host's own pawn is authoritative locally and never routes
# movement through them). Function names come from the prior discovery.py: the move RPCs live on
# Engine.PlayerController (Willow does not override them), except ShortServerMove which is Willow's.
# Tracked per (player, variant) so we see exactly which path BL2 uses and its rate, without any one
# variant's count polluting another's. Remove this whole block once the send rate is confirmed.
_sm_last: dict[tuple[int, str], float] = {}
_sm_sum: dict[tuple[int, str], float] = {}
_sm_count: dict[tuple[int, str], int] = {}


def _record_servermove(obj: unreal.UObject, variant: str) -> None:
    """Fold one server-move receipt into the per-(player, variant) interval average, logging every 30th."""
    pc = cast("WillowPlayerController", obj)
    player = player_id(pc)
    if player is None:
        return
    key = (player, variant)
    now = time.perf_counter()
    last = _sm_last.get(key)
    _sm_last[key] = now
    if last is None:
        return
    dt = now - last
    _sm_sum[key] = _sm_sum.get(key, 0.0) + dt
    _sm_count[key] = _sm_count.get(key, 0) + 1
    if every_n(f"sm_{player}_{variant}", 30):
        n = _sm_count[key]
        avg = _sm_sum[key] / n if n else 0.0
        hz = 1.0 / avg if avg > 0 else 0.0
        log.info(
            f"SERVERMOVE_RATE player={player} variant={variant}"
            f" avg_interval={avg * 1000:.1f}ms rate={hz:.1f}Hz last_dt={dt * 1000:.1f}ms"
            f" samples={n}",
        )
        _sm_sum[key] = 0.0
        _sm_count[key] = 0


# One hook per known server-move RPC. PCServerMoveInner is the consolidated inner handler the others
# funnel into, so it should show the true aggregate rate; the rest show which specific path fires.
@hook("Engine.PlayerController:ServerMove")
def probe_server_move(obj: unreal.UObject, _a: unreal.WrappedStruct, _r: Any, _f: unreal.BoundFunction) -> None:
    """DIAGNOSTIC (temporary): ServerMove receipt. Runs on: HOST, remote pawns only."""
    _record_servermove(obj, "ServerMove")


@hook("Engine.PlayerController:DualServerMove")
def probe_dual_server_move(obj: unreal.UObject, _a: unreal.WrappedStruct, _r: Any, _f: unreal.BoundFunction) -> None:
    """DIAGNOSTIC (temporary): DualServerMove receipt. Runs on: HOST, remote pawns only."""
    _record_servermove(obj, "DualServerMove")


@hook("Engine.PlayerController:PCServerMoveInner")
def probe_pc_server_move_inner(obj: unreal.UObject, _a: unreal.WrappedStruct, _r: Any, _f: unreal.BoundFunction) -> None:
    """DIAGNOSTIC (temporary): consolidated inner move handler. Runs on: HOST, remote pawns only."""
    _record_servermove(obj, "PCServerMoveInner")


@hook("WillowGame.WillowPlayerController:ShortServerMove")
def probe_short_server_move(obj: unreal.UObject, _a: unreal.WrappedStruct, _r: Any, _f: unreal.BoundFunction) -> None:
    """DIAGNOSTIC (temporary): Willow's compressed move variant. Runs on: HOST, remote pawns only."""
    _record_servermove(obj, "ShortServerMove")


# ---------------------------------------------------------------------------------------------------

# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [
    handle_move,
    handle_duck,
    jump,
    probe_server_move,
    probe_dual_server_move,
    probe_pc_server_move_inner,
    probe_short_server_move,
]
