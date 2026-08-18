"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, cast

from mods_base import get_pc, hook
from uemath import Vector
from unrealsdk import unreal
from unrealsdk.hooks import Block, Type, add_hook, remove_hook

from .debug import dbg
from .lifecycle import client_exit_slide, enter_slide, server_set_slide_jump_velocity
from .movement import apply_slide_physics, slide
from .state import CLIENTS_SLIDE_STATES, OWN_SLIDE_STATE, PlayerSlideState, State

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


@hook("WillowGame.WillowPlayerInput:Jump")
def jump(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Stash horizontal velocity on a slide jump, to hand back to the host a frame later.

    Runs on: whichever machine's local player pressed Jump (client or host). The slide-jump handoff
    to the server happens next frame from handle_move, not here.
    """
    if OWN_SLIDE_STATE.is_sliding:
        # Snapshot horizontal velocity now, before the engine's Jump processing changes it. The
        # actual handoff to the server (via server_set_slide_jump_velocity) happens one frame
        # later inside handle_move, once the pawn has left the ground.
        pc = cast("WillowPlayerController", obj.Outer)
        vel: Vector = Vector(pc.Pawn.Velocity)
        vel.z = 0
        State.horizontal_velocity = vel
        State.do_slide_jump = True


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def handle_move(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """PRE hook on the walking-state per-frame move - now only handles the slide-jump handoff.

    Runs on: BOTH host and client. All slide integration, decay, and exit-condition work has moved
    into the PhysWalking PRE hook (`_phys_sliding`). What remains here is the two-frame slide-jump
    dance: when Jump was pressed while sliding, the jump hook stashes horizontal velocity and sets
    `State.do_slide_jump`. On this frame's PlayerMove PRE, if we are still grounded we fire
    `DoJump(True)` to leave the ground; on the next fire (once airborne), we broadcast the stashed
    velocity to the host so its copy of the pawn keeps the momentum through the arc.
    """
    if not State.do_slide_jump:
        return

    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return

    if pawn.IsOnGroundOrShortFall():
        pawn.DoJump(True)
    else:
        server_set_slide_jump_velocity(
            State.horizontal_velocity.x, State.horizontal_velocity.y,
        )
        State.do_slide_jump = False


@hook("WillowGame.WillowPlayerInput:DuckPressed")
def handle_duck(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Crouching while sprinting starts a slide.

    Runs on: whichever machine's local player pressed duck. The enter_slide call is what fans the
    event out - broadcast RPC to the host, plus local prediction on this machine.
    """
    # DuckPressed's `obj` is the WillowPlayerInput, whose Outer is the WillowPlayerController.
    # That's the object the rest of the mod expects to work with.
    pc = cast("WillowPlayerController", obj.Outer)
    # One-shot diagnostic so we can tell from the log whether the sprint-state check ever passes.
    # A duck press that comes in without sprint is the majority - only the sprinting ones start
    # a slide.
    dbg(f"DUCK sprinting={bool(pc.bInSprintState)}")
    if pc.bInSprintState:
        # enter_slide fans out to a broadcast RPC and local prediction; a raise from either would
        # leave state half-populated. Log and continue so a diagnostic bug can't wedge the slide.
        try:
            enter_slide(pc)
        except Exception as ex:  # noqa: BLE001 - temporary diagnostics
            dbg(f"ENTER FAILED {type(ex).__name__}: {ex}")


# --- physics-mode slide ----------------------------------------------------------------------------
# PhysWalking is the pawn-side walking-physics function that the engine dispatches to each frame
# while the pawn is in PHYS_Walking. Hooking it PRE and returning Block lets us skip its native body
# and run our own slide physics inline - functionally equivalent to a bespoke PHYS_Sliding mode that
# we cannot add as an enum value. Because PhysWalking fires on both the client's own pawn (via
# PlayerMove -> PhysicsTick -> PhysWalking) and the host's copy of every remote pawn (via
# AutonomousPhysics -> PhysWalking), one hook covers both machines' paths.

PHYS_SLIDING_ID = "SlidingPhysWalking"
PHYS_SLIDING_FUNCS = (
    "Engine.Pawn:PhysWalking",
)


def _state_for_pawn(pc: unreal.UObject) -> PlayerSlideState | None:
    """Return the slide state that governs this controller's pawn this frame, or None if not
    sliding. Local player uses OWN_SLIDE_STATE; every other player looks up in CLIENTS_SLIDE_STATES.
    """
    try:
        local_pc = get_pc()
    except Exception:  # noqa: BLE001 - happens transiently during map load
        local_pc = None
    if pc == local_pc:
        return OWN_SLIDE_STATE if OWN_SLIDE_STATE.is_sliding else None
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            state = CLIENTS_SLIDE_STATES[player]
            return state if state.is_sliding else None
    return None


def _phys_sliding(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> type[Block] | None:
    """Slide-physics PRE hook. Returns Block to skip native PhysWalking when the pawn is sliding.

    Runs on: BOTH. Fires per-frame on every pawn whose walking-physics is about to run. Pawns that
    are not sliding fall through to native PhysWalking (return None); sliding pawns get their
    physics computed here and PhysWalking is skipped entirely.
    """
    pawn = cast("WillowPlayerPawn", obj)
    pc = getattr(pawn, "Controller", None)
    if pc is None:
        return None

    state = _state_for_pawn(pc)
    if state is None:
        return None

    try:
        delta_time = float(getattr(args, "DeltaTime", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return None
    if delta_time <= 0.0:
        return None

    try:
        # Decay speed_pct against the slope, and fire the targeted exit RPC if the floor is hit.
        # slide() may set state.is_sliding to False; check afterwards before continuing.
        slide(pc, state, delta_time)
        if not state.is_sliding:
            return None

        # Fallback heading lock. begin_slide_state on the host reads its own simulated pawn.Velocity
        # which can be stale on the first frame - if it was below the 1uu/s floor, dir/entry got
        # left at (0,0). By the time PhysWalking fires, pawn.Velocity has been replicated at least
        # once from BL2's normal pawn stream and is a truer sample. Use it to lock heading now.
        if state.dir_x == 0.0 and state.dir_y == 0.0:
            vel = Vector(pawn.Velocity)
            vel.z = 0
            if vel.magnitude >= 1.0:
                vel.normalize()
                state.dir_x = vel.x
                state.dir_y = vel.y
                state.entry_x = vel.x
                state.entry_y = vel.y

        # Steer (with back-cutoff + turn-cone clamp) and write Velocity/Acceleration/CrouchedPct
        # onto the pawn. Same math as the previous POST-hook enforce_slide path, minus the
        # post-integration assumption - we integrate ourselves next.
        apply_slide_physics(pawn, state, delta_time)

        # Integrate: move the pawn using the engine's own collision. MoveSmooth handles wall-slide
        # and step-up so we do not have to trace collision manually.
        vel = pawn.Velocity
        pawn.MoveSmooth(
            (float(vel.X) * delta_time, float(vel.Y) * delta_time, 0.0),
        )

        # Extra exit condition: duck released. slide() already checks speed floor and duration cap.
        if not bool(getattr(pc, "bDuck", False)):
            client_exit_slide(pc.PlayerReplicationInfo)
    except Exception as ex:  # noqa: BLE001 - defensive: on failure, fall back to native walking
        dbg(f"PHYS SLIDING FAILED {type(ex).__name__}: {ex}")
        return None

    return Block


def enable_phys_sliding() -> None:
    """Wire the PhysWalking PRE hook. Runs on: BOTH, at mod-enable."""
    for name in PHYS_SLIDING_FUNCS:
        try:
            added = add_hook(name, Type.PRE, PHYS_SLIDING_ID, _phys_sliding)
        except Exception as ex:  # noqa: BLE001 - if PhysWalking can't be hooked, log and continue
            dbg(f"PHYS SLIDING could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"PHYS SLIDING hook on {name}: {'added' if added else 'refused'}")


def disable_phys_sliding() -> None:
    """Unwire the PhysWalking PRE hook. Runs on: BOTH, at mod-disable."""
    for name in PHYS_SLIDING_FUNCS:
        with contextlib.suppress(Exception):
            remove_hook(name, Type.PRE, PHYS_SLIDING_ID)


# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, handle_duck, jump]
