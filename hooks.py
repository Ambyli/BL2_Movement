"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere.

Nothing here drives a slide. Entering one hands off to `lifecycle`, which starts a coroutine that
owns the slide for its whole life; these hooks only catch the three inputs that start it, jump out
of it, and carry its momentum across.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Any, cast

from mods_base import hook
from coroutines import Time
from uemath import Vector
from unrealsdk import unreal

from .constants import POST_LOG_EVERY
from .debug import every_n, log
from .lifecycle import enter_slide, server_set_slide_jump_velocity
from .movement import steer_heading
from .state import OWN_SLIDE_STATE, State, is_client, player_id, state_for

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
    verbose = every_n("handle_move", POST_LOG_EVERY)
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


def _view_basis(
    pc: WillowPlayerController,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Ground forward/right unit vectors for the controller's view yaw.

    UE yaw is 0 = +X, 16384 = +Y, so forward = (cos, sin). The engine builds world Acceleration from
    the input axes with right = (-sin, cos), and the decompose/recompose round-trip is the identity
    only when we use that same right - the mirrored sign (sin, -cos) reflects the heading across the
    view-forward axis, which makes the slide appear to track facing. Verified against an INJECT trace
    (intended dir vs. resulting Acceleration had a flipped Y).
    """
    theta = float(pc.Rotation.Yaw) * (math.tau / 65536.0)
    c, s = math.cos(theta), math.sin(theta)
    return (c, s), (-s, c)


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def inject_slide_heading(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Own pawn: steer from raw input, then overwrite the outgoing move with the slide heading + cap.

    Runs on: the machine whose local player is sliding (client, or a listen-server host who is also a
    player). PRE-hook so the axis writes land before PlayerMove reads them to build - and replicate -
    this frame's Acceleration.
    """
    state = OWN_SLIDE_STATE
    if not state.is_sliding:
        return
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    pin = getattr(pc, "PlayerInput", None)
    fwd, right = _view_basis(pc)

    # Read the player's RAW steering input (before we overwrite the axes) and project it to world.
    raw_f = raw_s = 0.0
    if pin is not None:
        try:
            raw_f = float(pin.aForward)
            raw_s = float(pin.aStrafe)
        except (
            Exception
        ):  # noqa: BLE001 - axis fields absent on some builds; no steering then
            raw_f = raw_s = 0.0
    state.input_x = raw_f * fwd[0] + raw_s * right[0]
    state.input_y = raw_f * fwd[1] + raw_s * right[1]

    # Steer the heading from that input, this frame (zero lag).
    steer_heading(state, Time.delta_time)

    # Overwrite the outgoing move to carry the slide heading: decompose the world heading back
    # into view-relative axes. |(af, as_)| == 1 for a unit heading, which is full input, so walking
    # physics accelerates to the full GroundSpeed * cap along the heading.
    af = state.dir_x * fwd[0] + state.dir_y * fwd[1]
    as_ = state.dir_x * right[0] + state.dir_y * right[1]
    if pin is not None:
        for name, val in (
            ("aForward", af),
            ("aBaseY", af),
            ("aStrafe", as_),
            ("aBaseX", as_),
        ):
            try:
                setattr(pin, name, val)
            except (
                Exception
            ):  # noqa: BLE001 - set whichever axis fields this build exposes
                pass

    # Hold the speed cap so the pawn's own walking physics runs at slide speed - both for local
    # feel and so this machine's ServerMove reports a slide-speed move for the host to reproduce.
    pawn.CrouchedPct = state.cap

    if every_n("inject", POST_LOG_EVERY):
        log.debug(
            f"inject dir=({state.dir_x:.2f},{state.dir_y:.2f}) cap={state.cap:.2f}"
            f" input=({state.input_x:.2f},{state.input_y:.2f})",
        )


@hook("Engine.PlayerController:MoveAutonomous")
def apply_remote_cap(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Host: hold a remote sliding proxy's speed cap so the server re-sim drives it at slide speed.

    Runs on: HOST only, for a remote client's pawn (MoveAutonomous re-sims autonomous proxies). The
    heading needs no help here - it arrives in the replicated Acceleration the client forced. We only
    lift the cap, recomputed each driver tick into `state.cap`, so the re-sim is not clamped to crouch
    speed. PRE-hook so the cap is in place before this move's walking physics runs.
    """
    if is_client():
        return
    pc = cast("WillowPlayerController", obj)
    if (state := state_for(pc)) is None:
        return
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    pawn.CrouchedPct = state.cap
    if every_n(f"remote_cap_{player_id(pc)}", POST_LOG_EVERY):
        log.debug(f"remote_cap player={player_id(pc)} cap={state.cap:.2f}")


# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, handle_duck, jump, inject_slide_heading, apply_remote_cap]
