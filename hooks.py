"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere.

Nothing here drives a slide. Entering one hands off to `lifecycle`, which starts a coroutine that
owns the slide for its whole life; these hooks only catch the three inputs that start it, jump out
of it, and carry its momentum across.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Any, cast

from mods_base import hook
from uemath import Vector
from unrealsdk import unreal

from .config import CROUCHED_PCT_DEFAULT
from .debug import every_n, log
from .lifecycle import enter_slide, server_set_slide_jump_velocity
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
# Direction-authority test. We proved the server's MoveAutonomous drives a remote proxy at
# GroundSpeed * CrouchedPct along the REPLICATED Acceleration (the client's raw input). B* needs the
# proxy to follow the SLIDE HEADING instead, which differs from input whenever the player steers or
# holds nothing. This tests whether the HOST can dictate that direction: in the MoveAutonomous
# pre-hook we overwrite Pawn.Acceleration with a heading deliberately DIFFERENT from what the client
# is pressing, then watch which way the resulting server-side velocity actually goes.
#   - client pressing something: forced dir = perpendicular to their input.
#   - client pressing nothing:   forced dir = a fixed world axis (also tests "can the host move a
#                                zero-input proxy at all?", the core no-input slide case).
# Read DIR_AUTH's angles (of the observed velocity, i.e. LAST tick's physics under the accel we set):
#   ang_forced -> 0, ang_client -> 90  => host CONTROLS direction; option (a) works, branch is ~40
#                                          host-only lines.
#   ang_client -> 0, ang_forced -> 90  => MoveAutonomous overwrote our Acceleration from the move
#                                          param; host cannot steer -> need option (b) (client forces
#                                          its outgoing move to carry the slide heading).
#   zero-input + speed>0 along forced  => host can drive a no-input proxy; strongly favors (a).
# Isolated to a ducking, non-sliding remote pawn on the host, so it never touches an active slide.
# Lifting the cap + forcing a cross direction makes that player veer/rubber-band - expected, it is
# the test. Have the remote client crouch-WALK (steady input, then also try releasing all keys).
_PROBE_CAP: float = 3.0
_FORCE_MAG: float = 2048.0  # full-input Acceleration magnitude observed on the wire
_cap_lifted: set[int] = set()
_last_forced: dict[int, tuple[float, float]] = {}
_last_client: dict[int, tuple[float, float]] = {}


def _angle_deg(ax: float, ay: float, bx: float, by: float) -> float:
    """Unsigned angle between two ground vectors, or -1 if either is ~zero."""
    ma = math.hypot(ax, ay)
    mb = math.hypot(bx, by)
    if ma < 1e-6 or mb < 1e-6:
        return -1.0
    c = max(-1.0, min(1.0, (ax * bx + ay * by) / (ma * mb)))
    return math.degrees(math.acos(c))


@hook("Engine.PlayerController:MoveAutonomous")
def probe_move_autonomous(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """DIAGNOSTIC (temporary): can the host dictate a remote proxy's server-side movement direction?

    Runs on: HOST, for a remote client's pawn only - MoveAutonomous re-sims autonomous proxies; the
    host's own pawn uses PlayerMove. PRE-hook: the Velocity we read is the result of LAST tick's
    physics under the Acceleration we forced then; we then force a new cross-direction for this tick.
    """
    if is_client():
        return
    pc = cast("WillowPlayerController", obj)
    player = player_id(pc)
    if player is None:
        return
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    # Never probe an active mod slide - only plain crouch-walking, so the mechanism is isolated.
    if state_for(pc) is not None:
        return

    if not bool(pc.bDuck):
        # Player stood up: undo any cap we lifted and clear stored dirs, so they are not left stuck.
        if player in _cap_lifted:
            pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
            _cap_lifted.discard(player)
            _last_forced.pop(player, None)
            _last_client.pop(player, None)
        return

    # Observed result of last tick's physics (under the accel we forced last tick), read before we
    # overwrite anything this tick.
    vel = Vector(pawn.Velocity)
    vel.z = 0.0
    speed = vel.magnitude

    # This tick's incoming client input direction, from the replicated move param.
    try:
        na = Vector(args.newAccel)
        client_x, client_y = float(na.x), float(na.y)
    except Exception:  # noqa: BLE001 - arg name/shape varies across builds
        client_x, client_y = 0.0, 0.0
    client_mag = math.hypot(client_x, client_y)

    # Forced heading: perpendicular to the client's input, or a fixed axis when they press nothing.
    if client_mag > 1.0:
        fx, fy = -client_y / client_mag, client_x / client_mag
    else:
        fx, fy = 1.0, 0.0

    if every_n(f"dirauth_{player}", 30):
        lf = _last_forced.get(player)
        lc = _last_client.get(player)
        # Angle of the OBSERVED velocity (last tick's physics) to what we forced last tick vs. what
        # the client pressed last tick. Whichever is small is what actually steered the proxy.
        ang_forced = _angle_deg(vel.x, vel.y, lf[0], lf[1]) if lf else -1.0
        ang_client = _angle_deg(vel.x, vel.y, lc[0], lc[1]) if lc else -1.0
        if ang_forced < 0 or ang_client < 0:
            verdict = "pending"
        elif ang_forced < ang_client:
            verdict = "HOST_CONTROLS"
        else:
            verdict = "client_controls"
        if speed > 1e-3:
            vel_dir = f"({vel.x / speed:.2f},{vel.y / speed:.2f})"
        else:
            vel_dir = "(0,0)"
        log.info(
            f"DIR_AUTH player={player} speed={speed:.0f} vel_dir={vel_dir}"
            f" client_dir=({client_x:.0f},{client_y:.0f}) forced_dir=({fx:.2f},{fy:.2f})"
            f" ang_to_forced={ang_forced:.1f} ang_to_client={ang_client:.1f} verdict={verdict}",
        )

    # Force the cross-direction and lift the cap for THIS tick's physics.
    pawn.Acceleration.X = fx * _FORCE_MAG
    pawn.Acceleration.Y = fy * _FORCE_MAG
    pawn.Acceleration.Z = 0.0
    pawn.CrouchedPct = _PROBE_CAP
    _cap_lifted.add(player)
    _last_forced[player] = (fx, fy)
    _last_client[player] = (client_x, client_y)


# ---------------------------------------------------------------------------------------------------

# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, handle_duck, jump, probe_move_autonomous]
