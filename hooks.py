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
# Client-side injection test. Option (a) is dead - the host cannot steer a remote proxy; its
# direction (and whether it moves at all) comes solely from the client's replicated Acceleration. So
# B* needs the CLIENT to make its OUTGOING move carry the slide heading. This tests whether we can
# inject that. On the client, during crouch-walk (no mod slide), we override the outgoing heading to
# a fixed "full forward" via (1) the input axes and (2) Pawn.Acceleration directly; on the host we
# OBSERVE the replicated client_dir. Decisive run: crouch-walk while pressing NOTHING -
#   host sees client_dir != 0 (motion the player never pressed) -> injection reaches the wire; B* works
#   host sees client_dir == 0                                    -> this injection point does not stick
# We also log which input-axis fields actually exist (names vary across builds) to wire the real
# slide injection next. The client will lurch forward against your input during the test - expected.
_INJECT_MAG: float = 2048.0
_AXIS_CANDIDATES: tuple[str, ...] = ("aForward", "aStrafe", "aBaseX", "aBaseY", "aUp", "aTurn", "aLookUp")
_axes_discovered: bool = False


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def probe_client_inject(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """CLIENT: force the outgoing move heading during crouch-walk, to test if it replicates.

    Runs on: CLIENT only, for the local player, while ducking and not in a mod slide. PRE-hook, so
    axis writes land before PlayerMove reads them to compute this frame's Acceleration.
    """
    if not is_client():
        return
    pc = cast("WillowPlayerController", obj)
    if not bool(pc.bDuck) or OWN_SLIDE_STATE.is_sliding:
        return
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    pin = getattr(pc, "PlayerInput", None)

    global _axes_discovered  # noqa: PLW0603 - one-shot discovery latch
    if not _axes_discovered and pin is not None:
        _axes_discovered = True
        found: list[str] = []
        for name in _AXIS_CANDIDATES:
            try:
                found.append(f"{name}={float(getattr(pin, name)):.2f}")
            except Exception:  # noqa: BLE001 - probing which axis fields exist
                pass
        log.info(f"INJECT_DISCOVER axes=[{', '.join(found)}]")

    # (1) Input-axis injection: force full forward / no strafe, overriding real input. Names vary, so
    # set whichever exist.
    set_axes: list[str] = []
    if pin is not None:
        for name, val in (("aForward", 1.0), ("aBaseY", 1.0), ("aStrafe", 0.0), ("aBaseX", 0.0)):
            try:
                setattr(pin, name, val)
                set_axes.append(name)
            except Exception:  # noqa: BLE001 - only whatever exists
                pass
    # (2) Direct property injection: fixed WORLD +X heading (distinguishable from view-relative axes).
    pawn.Acceleration.X = _INJECT_MAG
    pawn.Acceleration.Y = 0.0
    pawn.Acceleration.Z = 0.0

    if every_n("client_inject", 30):
        a = Vector(pawn.Acceleration)
        a.z = 0.0
        log.info(f"CLIENT_INJECT set_axes={set_axes} pawn_accel=({a.x:.0f},{a.y:.0f})")


@hook("Engine.PlayerController:MoveAutonomous")
def probe_observe_client_dir(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """HOST: observe the replicated Acceleration for a ducking, non-sliding remote pawn.

    Runs on: HOST only. Observe-only - no forcing, no cap lift - so client_dir reflects exactly what
    the client sent. If injection worked, this shows the injected heading even when the player at the
    other end is pressing nothing.
    """
    if is_client():
        return
    pc = cast("WillowPlayerController", obj)
    player = player_id(pc)
    if player is None:
        return
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None or state_for(pc) is not None or not bool(pc.bDuck):
        return
    if every_n(f"observe_{player}", 30):
        try:
            na = Vector(args.newAccel)
            na.z = 0.0
            client_dir = f"({na.x:.0f},{na.y:.0f}) mag={na.magnitude:.0f}"
        except Exception:  # noqa: BLE001 - arg name/shape varies
            client_dir = "?"
        vel = Vector(pawn.Velocity)
        vel.z = 0.0
        log.info(f"OBSERVE_DIR player={player} client_dir={client_dir} vel_mag={vel.magnitude:.0f}")


# ---------------------------------------------------------------------------------------------------

# Passed explicitly to build_mod: it only gathers hooks from the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
all_hooks = [handle_move, handle_duck, jump, probe_client_inject, probe_observe_client_dir]
