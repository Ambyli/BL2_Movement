"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mods_base import hook
from uemath import Vector
from unrealsdk import unreal
from unrealsdk.hooks import Block, Type, add_hook, remove_hook

from .config import CROUCHED_PCT_DEFAULT, max_duration, smooth_coop_slides
from .debug import dbg, note_suppressed
from .lifecycle import enter_slide, exit_slide, server_set_slide_jump_velocity
from .movement import apply_slide_physics, can_slide, drive_hosted_slides, slide
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

    # Host duty first, and unconditionally - it must not sit behind our own slide's exit checks
    # below. Usually the player tick hook has already covered this frame and the call is a no-op;
    # it stays as a fallback for the case where that hook never registers.
    if not is_client():
        drive_hosted_slides(pc, args.DeltaTime, "PlayerMove")

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

    # A client owns only its own slide; the host already advanced its entry above.
    if is_client():
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


# --- host tick -----------------------------------------------------------------------------------
# PlayerMove only fires while the host is in the walking state, so the moment the host jumps, boards
# a vehicle or opens a menu it stops driving everyone else's slide. PlayerTick runs every frame in
# every state, which is what the host duty actually needs.
#
# Registered by hand rather than with the decorator because it is not knowable from outside the game
# whether WillowPlayerController overrides PlayerTick or inherits it - hooking the wrong one would
# silently never fire. Both are attempted; if the override exists and chains to its parent then both
# fire, which the per-frame dedup in drive_hosted_slides already handles.

HOST_TICK_ID = "SlidingHostTick"
PLAYER_TICK_FUNCS = (
    "WillowGame.WillowPlayerController:PlayerTick",
    "Engine.PlayerController:PlayerTick",
)


def _host_tick(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    if is_client():
        return
    delta_time = float(getattr(args, "DeltaTime", 0.0) or 0.0)
    if delta_time <= 0.0:
        return
    try:
        drive_hosted_slides(cast("WillowPlayerController", obj), delta_time, "PlayerTick")
    except Exception as ex:  # noqa: BLE001 - must never break the controller's tick
        dbg(f"HOST TICK FAILED {type(ex).__name__}: {ex}")


def enable_host_tick() -> None:
    for name in PLAYER_TICK_FUNCS:
        try:
            added = add_hook(name, Type.PRE, HOST_TICK_ID, _host_tick)
        except Exception as ex:  # noqa: BLE001 - a missing candidate is expected, not fatal
            dbg(f"HOST TICK could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"HOST TICK hook on {name}: {'added' if added else 'refused'}")


def disable_host_tick() -> None:
    for name in PLAYER_TICK_FUNCS:
        try:
            remove_hook(name, Type.PRE, HOST_TICK_ID)
        except Exception:  # noqa: BLE001, S110 - nothing useful to do if it was never added
            pass


# --- correction suppression ------------------------------------------------------------------------
# With the host now told about a client's slide, it simulates one at ~960 uu/s against the client's
# predicted 970. That last percent is enough for the server to disagree every packet, and it corrects
# around forty times a second for the whole slide - small individually, constant stutter in aggregate.
#
# Blocking those for the duration of a slide is a real trade, not a free win: the client's prediction
# becomes authoritative for ~1.5s, and any genuine disagreement lands as one correction at the end
# rather than being bled off continuously. In co-op PvE that is the better bargain, but it is behind
# an option so it can be turned off and compared.

CORRECTION_ID = "SlidingSuppressCorrection"
CORRECTION_FUNCS = (
    "Engine.PlayerController:ClientAdjustPosition",
    "Engine.PlayerController:LongClientAdjustPosition",
    "Engine.PlayerController:ShortClientAdjustPosition",
    "Engine.PlayerController:VeryShortClientAdjustPosition",
)


def _is_remote_sliding(pc: unreal.UObject) -> bool:
    """Whether the host has a live slide recorded for this controller."""
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            return CLIENTS_SLIDE_STATES[player].is_sliding
    return False


def _suppress_correction(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> type[Block] | None:
    """Drop a position correction for a slide, at whichever end sees it.

    Blocking on the host matters more than blocking on the client. `ClientAdjustPosition` is a
    client function the server *calls*, so stopping it there means the correction is never sent at
    all - no bandwidth spent, and the client's move acknowledgement bookkeeping stays consistent
    with the server's. Blocking it on the client only discards a packet that has already been sent
    and already been counted against us.
    """
    if not smooth_coop_slides.value:
        return None

    if is_client():
        if not OWN_SLIDE_STATE.is_sliding:
            return None
    elif not _is_remote_sliding(obj):
        return None

    note_suppressed()
    return Block


def enable_correction_suppression() -> None:
    for name in CORRECTION_FUNCS:
        try:
            added = add_hook(name, Type.PRE, CORRECTION_ID, _suppress_correction)
        except Exception as ex:  # noqa: BLE001 - a missing variant is expected, not fatal
            dbg(f"SUPPRESS could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"SUPPRESS hook on {name}: {'added' if added else 'refused'}")


def disable_correction_suppression() -> None:
    for name in CORRECTION_FUNCS:
        try:
            remove_hook(name, Type.PRE, CORRECTION_ID)
        except Exception:  # noqa: BLE001, S110 - nothing to do if it was never added
            pass


# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, enforce_slide, handle_duck, jump]
