"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mods_base import hook
from uemath import Vector
from unrealsdk import unreal
from unrealsdk.hooks import Type

from .config import CROUCHED_PCT_DEFAULT, max_duration
from .debug import dbg
from .lifecycle import enter_slide, exit_slide, server_set_slide_jump_velocity
from .movement import apply_slide_physics, can_slide, slide
from .state import CLIENTS_SLIDE_STATES, OWN_SLIDE_STATE, State, is_client

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


@hook("WillowGame.WillowPlayerInput:Jump")
def jump(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Stash horizontal velocity on a slide jump, to hand back to the host a frame later."""
    if OWN_SLIDE_STATE.is_sliding:
        pc = cast("WillowPlayerController", obj.Outer)
        vel: Vector = Vector(pc.Pawn.Velocity)
        vel.z = 0
        State.horizontal_velocity = vel
        State.do_slide_jump = True


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def handle_move(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)

    # Jumping stands you up, which fails the slide exit conditions below, so the slide jump has to
    # be handled before any of them get a chance to run.
    if State.do_slide_jump:
        if pawn.IsOnGroundOrShortFall():
            pawn.DoJump(True)
        else:
            server_set_slide_jump_velocity(State.horizontal_velocity.x, State.horizontal_velocity.y)
            State.do_slide_jump = False
            return

    if not can_slide(pc, pawn):
        exit_slide(pc)
        return

    if not is_client():
        for player in CLIENTS_SLIDE_STATES.copy():
            if (_pc := player()) is None:
                CLIENTS_SLIDE_STATES.pop(player)
            else:
                state = CLIENTS_SLIDE_STATES[player]
                slide(_pc, state, args.DeltaTime)
                if _pc == pc:
                    # Mirror our own progress back out, so the exit check below still sees it when
                    # we are the host and our state lives in the clients dict rather than ours.
                    OWN_SLIDE_STATE.speed_pct = state.speed_pct
                    OWN_SLIDE_STATE.elapsed = state.elapsed
    else:
        slide(pc, OWN_SLIDE_STATE, args.DeltaTime)

    if (
        OWN_SLIDE_STATE.speed_pct < CROUCHED_PCT_DEFAULT
        or OWN_SLIDE_STATE.elapsed >= max_duration.value
    ):
        exit_slide(pc)


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def enforce_slide(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Reassert the slide once PlayerMove has finished recomputing movement from input."""
    if not OWN_SLIDE_STATE.is_sliding:
        return
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    try:
        apply_slide_physics(pawn, OWN_SLIDE_STATE, args.DeltaTime)
    except Exception as ex:  # noqa: BLE001 - temporary diagnostics
        dbg(f"POST FAILED {type(ex).__name__}: {ex}")


@hook("WillowGame.WillowPlayerInput:DuckPressed")
def handle_duck(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Crouching while sprinting starts a slide."""
    pc = cast("WillowPlayerController", obj.Outer)
    dbg(f"DUCK sprinting={bool(pc.bInSprintState)}")
    if pc.bInSprintState:
        try:
            enter_slide(pc)
        except Exception as ex:  # noqa: BLE001 - temporary diagnostics
            dbg(f"ENTER FAILED {type(ex).__name__}: {ex}")


# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, enforce_slide, handle_duck, jump]
