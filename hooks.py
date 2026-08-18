"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere."""

from __future__ import annotations

import contextlib
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
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """PRE hook on the walking-state per-frame move.

    Runs on: BOTH host and client. Behaviour branches on `is_client()` - the host drives every
    remote slide from here (as a fallback for the PlayerTick hook), while both sides advance their
    own local slide and check the exit conditions.
    """
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
            server_set_slide_jump_velocity(
                State.horizontal_velocity.x, State.horizontal_velocity.y
            )
            State.do_slide_jump = False
            return

    # Physical exit conditions: duck released, off the ground, or OWN_SLIDE_STATE flag already
    # cleared by a targeted client_exit_slide message from the host. Any of these ends the slide
    # cleanly before the decay math below has a chance to touch the state.
    if not can_slide(pc, pawn):
        exit_slide(pc)
        return

    # A client owns only its own slide; the host already advanced its entry above.
    if is_client():
        slide(pc, OWN_SLIDE_STATE, args.DeltaTime)

    # Speed-and-duration exit: decay has bled the slide below the walking-crouch multiplier, or
    # the hard duration cap has hit. Either way we end the slide from this frame instead of the
    # next, so exit_slide's server_exit_slide broadcast leaves the wire as promptly as possible.
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
    """Reassert the slide once PlayerMove has finished recomputing movement from input.

    Runs on: BOTH, but only does work for the machine that owns the local slide. Twin of
    moveflags._drive_remote_slide, which does the equivalent job for remote pawns on the host.
    """
    # We only touch the pawn if this machine's local player is the one sliding. Every other player
    # is either the host's simulation of a remote client (handled by drive_remote_slide) or a
    # non-sliding pawn we do not need to touch.
    if not OWN_SLIDE_STATE.is_sliding:
        return
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    # Pawn can be None during respawn or level transitions where PlayerMove still fires against
    # a stub controller. Bail rather than reach into a null.
    if pawn is None:
        return
    # apply_slide_physics is the only place velocity actually lands on the pawn, so a failure here
    # would leave the slide state advancing without any visible motion. Log rather than raise, so
    # a diagnostic bug doesn't take the whole movement path down.
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
    """PRE hook on PlayerTick, driving the host duty every frame regardless of movement state.

    Runs on: HOST only. Fires on every rendered frame - including while the host is airborne, in
    a vehicle, or has a menu open, all cases where PlayerMove stops firing. drive_hosted_slides
    dedupes by world time, so this becomes a no-op on frames where PlayerMove already ran.
    """
    # Client machines never own remote slide state; only the host has anything to drive.
    if is_client():
        return
    # PlayerTick's args carry DeltaTime, but which variant this hook lands on isn't knowable in
    # advance (WillowPlayerController override or Engine.PlayerController base). Use getattr so
    # the wrong variant just becomes a zero and returns below rather than raising.
    delta_time = float(getattr(args, "DeltaTime", 0.0) or 0.0)
    if delta_time <= 0.0:
        return
    # An exception in the host duty must never propagate up into PlayerTick, or every remote
    # player's controller stops ticking. Log and swallow; the next frame gets another chance.
    try:
        drive_hosted_slides(
            cast("WillowPlayerController", obj), delta_time, "PlayerTick"
        )
    except Exception as ex:  # noqa: BLE001 - must never break the controller's tick
        dbg(f"HOST TICK FAILED {type(ex).__name__}: {ex}")


def enable_host_tick() -> None:
    """Wire the PlayerTick host-duty hook. Runs on: BOTH, at mod-enable.

    Both candidate function names are attempted; whichever exists in this build wins, and if
    both do, the per-frame dedup handles the overlap.
    """
    # Try each candidate in turn. One or both may be valid depending on whether
    # WillowPlayerController overrides PlayerTick in this build; a raise here means the name did
    # not resolve, which is expected for the losing candidate and never fatal.
    for name in PLAYER_TICK_FUNCS:
        try:
            added = add_hook(name, Type.PRE, HOST_TICK_ID, _host_tick)
        except (
            Exception
        ) as ex:  # noqa: BLE001 - a missing candidate is expected, not fatal
            dbg(f"HOST TICK could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        # Log the outcome per candidate so the debug log tells us exactly which of the two took
        # (or if neither did, in which case the host duty falls back to PlayerMove alone).
        dbg(f"HOST TICK hook on {name}: {'added' if added else 'refused'}")


def disable_host_tick() -> None:
    """Unwire the PlayerTick host-duty hook. Runs on: BOTH, at mod-disable."""
    for name in PLAYER_TICK_FUNCS:
        # A missing hook is expected - it may have failed to register on `enable_host_tick`.
        with contextlib.suppress(Exception):
            remove_hook(name, Type.PRE, HOST_TICK_ID)


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


class _CorrectionLog:
    reported_miss: ClassVar[bool] = False


def _is_remote_sliding(pc: unreal.UObject) -> bool:
    """Whether the host has a live slide recorded for this controller.

    Runs on: HOST only. Called from _suppress_correction to decide whether a correction packet
    that's about to be sent to `pc` should be dropped.
    """
    # Walk the dict, sweeping GC'd weak pointers on the way. `.copy()` because we may mutate
    # inside the loop.
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            # Found this controller. The is_sliding flag is authoritative here; a stale entry
            # with is_sliding=False is exactly the same as "no entry", which is what we want.
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

    Runs on: BOTH, with different tests. On the host it fires when the server is about to send a
    correction to a client; on the client it fires when the correction has arrived. Same block
    result either way.
    """
    # Feature switch. Left off, this hook is registered but always no-ops.
    if not smooth_coop_slides.value:
        return None

    # Client-side firing: only suppress if this client is the one sliding. Other clients'
    # corrections have nothing to do with our slide state.
    if is_client():
        if not OWN_SLIDE_STATE.is_sliding:
            return None
    # Host-side firing: only suppress corrections addressed to a player the host believes is
    # sliding. A correction to any other player is a legitimate walking-physics disagreement.
    elif not _is_remote_sliding(obj):
        # Say so once. A correction going out to a player the host does not believe is sliding is
        # the whole failure, and it is invisible unless it is named.
        if not _CorrectionLog.reported_miss:
            _CorrectionLog.reported_miss = True
            try:
                who = str(obj.PlayerReplicationInfo.PlayerName)
            except Exception:  # noqa: BLE001
                who = "?"
            dbg(f"SUPPRESS MISS correcting={who} but no live slide on file for them")
        return None

    # Count the suppression for the per-slide diagnostic, then return the Block sentinel - which
    # tells pyunrealsdk to drop the native call. On the host side that means the correction
    # packet is never sent; on the client it means the correction that just arrived is discarded.
    note_suppressed()
    return Block


def enable_correction_suppression() -> None:
    """Wire the correction-blocking PRE hook. Runs on: BOTH, at mod-enable.

    All four `ClientAdjustPosition` variants are attempted so we catch whichever BL2 uses for a
    given correction size (very-short / short / long / normal).
    """
    # Try each variant. Only some will resolve in a given BL2 build; the ones that raise are
    # expected and get logged, not treated as failure. Any correction path that BL2 uses in the
    # end has to land in one of these four.
    for name in CORRECTION_FUNCS:
        try:
            added = add_hook(name, Type.PRE, CORRECTION_ID, _suppress_correction)
        except (
            Exception
        ) as ex:  # noqa: BLE001 - a missing variant is expected, not fatal
            dbg(f"SUPPRESS could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"SUPPRESS hook on {name}: {'added' if added else 'refused'}")


def disable_correction_suppression() -> None:
    """Unwire correction-blocking hooks. Runs on: BOTH, at mod-disable."""
    # Same symmetry - remove all four whether or not they were successfully registered. A missing
    # hook is exactly what we want here anyway; catching the exception avoids a distracting
    # traceback at mod-disable time.
    for name in CORRECTION_FUNCS:
        try:
            remove_hook(name, Type.PRE, CORRECTION_ID)
        except Exception:  # noqa: BLE001, S110 - nothing to do if it was never added
            pass


# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, enforce_slide, handle_duck, jump]
