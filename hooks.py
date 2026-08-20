"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere.

Nothing here drives a slide. Entering one hands off to `lifecycle`, which starts a coroutine that
owns the slide for its whole life; these hooks only catch the three inputs that start it, jump out
of it, and carry its momentum across.
"""

from __future__ import annotations

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
# B* speed-cap probe. The CrouchedPct-replication probe showed our host-side write on a remote pawn
# persists untouched - but that only proves the WRITE sticks, not that the server's walking sim READS
# it as the speed cap. This tests exactly that: on the host, inside MoveAutonomous (which re-sims a
# remote client's move server-side), lift CrouchedPct well above normal crouch and watch whether the
# resulting server-side horizontal speed climbs toward GroundSpeed * lifted_cap. Run it by having the
# remote client crouch-WALK at full forward input, with NO mod slide active:
#   ratio (speed/GroundSpeed) climbs toward _PROBE_CAP  -> cap is live; B* can open it in MoveAutonomous
#   ratio stays at normal crouch (~0.5)                  -> cap computed elsewhere; need another lever
# Only fires for a ducking, non-sliding remote pawn on the host, so it never touches an active slide.
# Lifting the cap makes that player briefly rubber-band on their own screen - expected, it is the test.
_PROBE_CAP: float = 3.0
_cap_lifted: set[int] = set()


@hook("Engine.PlayerController:MoveAutonomous")
def probe_move_autonomous(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """DIAGNOSTIC (temporary): does the server's MoveAutonomous honor CrouchedPct as the speed cap?

    Runs on: HOST, for a remote client's pawn only - MoveAutonomous re-sims autonomous proxies; the
    host's own pawn uses PlayerMove. This is a PRE-hook, firing before this tick's walking physics, so
    the Velocity read here is the result of LAST tick's physics under the cap we lifted then.
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
    # Never probe an active mod slide - only plain crouch-walking, so the cap mechanism is isolated.
    if state_for(pc) is not None:
        return

    if not bool(pc.bDuck):
        # Player stood up: undo any cap we lifted, so they are not left stuck fast.
        if player in _cap_lifted:
            pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
            _cap_lifted.discard(player)
        return

    if every_n(f"moveauto_{player}", 30):
        vel = Vector(pawn.Velocity)
        vel.z = 0.0
        speed = vel.magnitude
        ground = max(float(pawn.GroundSpeed), 1.0)
        try:
            accel = Vector(args.newAccel)
            accel.z = 0.0
            accel_mag = accel.magnitude
        except Exception:  # noqa: BLE001 - arg name/shape varies; input magnitude is a nice-to-have
            accel_mag = -1.0
        log.info(
            f"MOVEAUTO_CAP player={player} speed={speed:.0f} ground={ground:.0f}"
            f" ratio={speed / ground:.3f} crouched_pct={pawn.CrouchedPct:.3f}"
            f" probe_cap={_PROBE_CAP:.2f} accel_mag={accel_mag:.0f}",
        )

    # Lift the cap for THIS tick's physics (which runs after this pre-hook returns).
    pawn.CrouchedPct = _PROBE_CAP
    _cap_lifted.add(player)


# ---------------------------------------------------------------------------------------------------

# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, handle_duck, jump, probe_move_autonomous]
