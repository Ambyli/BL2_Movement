"""Where the mod attaches to the game. Deliberately thin - all logic lives elsewhere."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, ClassVar, cast

from mods_base import get_pc, hook
from uemath import Vector
from unrealsdk import unreal
from unrealsdk.hooks import Block, Type, add_hook, remove_hook

from .debug import dbg
from .lifecycle import (
    client_exit_slide,
    enter_slide,
    exit_slide,
    server_set_slide_jump_velocity,
)
from .movement import apply_slide_physics, can_slide, slide
from .state import (
    CLIENTS_SLIDE_STATES,
    OWN_SLIDE_STATE,
    PlayerSlideState,
    State,
    world_time,
)

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


# --- shared frame bookkeeping ---------------------------------------------------------------------


class _Phys:
    """Cross-hook scratch shared by the two integration paths.

    `last_frame` is the world time of the last frame `_phys_sliding` handled the *local* player.
    `enforce_slide` reads it to stand down on frames PhysWalking already covered. `last_delta` is
    the last positive frame delta we saw, used only when a hook's own delta cannot be read.
    """

    reported: ClassVar[bool] = False
    last_frame: ClassVar[float] = -1.0
    last_delta: ClassVar[float] = 0.0
    last_world_time: ClassVar[float] = -1.0

    @staticmethod
    def now() -> float:
        """World time, or -1.0 if the world is not currently readable.

        Mid-load and mid-transition there is no world info to read. -1.0 never equals a stamped
        frame, so a failure here makes the dedup say "not handled", which costs at most one
        redundant physics write - the safe direction, versus silently skipping the only integrator.
        """
        try:
            return world_time()
        except Exception:  # noqa: BLE001 - no world yet
            return -1.0

    @classmethod
    def mark_local_frame(cls) -> None:
        """Record that PhysWalking drove the local player's slide on this frame."""
        cls.last_frame = cls.now()

    @classmethod
    def handled_local_frame(cls) -> bool:
        """Whether PhysWalking already drove the local player's slide on this frame."""
        return cls.last_frame >= 0.0 and cls.last_frame == cls.now()


def _arg_names(args: unreal.WrappedStruct) -> list[str]:
    """Every parameter name on a hook's args struct, for the one-shot probe below.

    Worth the awkwardness because the alternative is guessing. The physics rework read
    `getattr(args, "DeltaTime", 0.0)` and returned early on the 0.0 default, so a parameter named
    anything else - `deltaTime`, as UE3's C++ signature spells it - produced a hook that fired every
    frame and did nothing, with no log line to say so.
    """
    try:
        return [str(field.Name) for field in args._type._fields()]
    except Exception:  # noqa: BLE001 - a probe must never raise at its call site
        try:
            return [name for name in dir(args) if not name.startswith("_")]
        except Exception:  # noqa: BLE001
            return []


def _delta_from(args: unreal.WrappedStruct) -> float:
    """This frame's delta in seconds, however the hook happens to spell it.

    Falls back to differencing world time, and then to the last positive delta we saw - the latter
    covering the host running this for several pawns inside one frame, where the world-time
    difference is zero for every pawn after the first.
    """
    for name in ("DeltaTime", "deltaTime"):
        try:
            delta = float(getattr(args, name))
        except Exception:  # noqa: BLE001, S112 - wrong spelling is the expected case, not news
            continue
        if delta > 0.0:
            _Phys.last_delta = delta
            return delta

    now = _Phys.now()
    delta = now - _Phys.last_world_time
    _Phys.last_world_time = now
    if 0.0 < delta < 1.0:
        _Phys.last_delta = delta
        return delta
    return _Phys.last_delta


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
    """PRE hook on the walking-state per-frame move. Owns the local slide's clock.

    This hook is the one place the local slide is guaranteed to be advanced and to be ended. It is
    deliberately not `_phys_sliding`: a slide whose only clock lives in a hook that might not
    dispatch is a slide that runs forever, which is exactly what shipped in the physics rework -
    `PhysWalking` registered fine and then never did a frame of work, so nothing decayed and no exit
    condition was ever reached. PlayerMove is proven to fire; the clock lives here.

    Two other jobs. The slide-jump handoff spans two frames by nature: the Jump hook stashes
    horizontal velocity and sets `State.do_slide_jump`, then on this frame we `DoJump(True)` to
    leave the ground, and on the next fire (now airborne) we hand the stashed velocity to the host
    so its copy of the pawn keeps the momentum through the arc. And the physical exit gate -
    crouch released, or no longer on the ground - runs here through `can_slide`.

    Runs on: BOTH host and client, for the local player only. Remote pawns never run PlayerMove on
    this machine; the host advances their slides from `_phys_sliding` instead.
    """
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return

    # Jumping stands you up, which fails `can_slide` below, so the slide jump has to be settled
    # before any exit condition gets a chance to run. Leaving the ground ends the slide on the
    # *following* frame, by which point the momentum is already in the pawn's velocity.
    if State.do_slide_jump:
        if pawn.IsOnGroundOrShortFall():
            pawn.DoJump(True)
        else:
            server_set_slide_jump_velocity(
                State.horizontal_velocity.x, State.horizontal_velocity.y,
            )
            State.do_slide_jump = False
        return

    if not OWN_SLIDE_STATE.is_sliding:
        return

    # Physical exit conditions: crouch released, or off the ground. Called directly rather than
    # through `client_exit_slide`, which for the local player would be a queued network message to
    # itself - delivered a tick later, one message per player tick.
    if not can_slide(pc, pawn):
        exit_slide(pc)
        return

    # Advance the decay curve. `_phys_sliding` deliberately does not do this for the local player,
    # so there is no double-decay to guard against here whichever order the two hooks run in.
    delta_time = _delta_from(args)
    if delta_time <= 0.0:
        return
    if slide(pc, OWN_SLIDE_STATE, delta_time):
        exit_slide(pc)


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def enforce_slide(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Fallback integrator: force the slide onto the pawn once PlayerMove has recomputed movement.

    This is the pre-rework path, kept alive alongside `_phys_sliding` because we do not yet know
    which of the two the engine actually gives us. It writes velocity and lets native PhysWalking
    integrate it, where `_phys_sliding` blocks PhysWalking and integrates itself - so exactly one of
    them may run on any given frame. `_phys_sliding` stamps `_Phys.last_frame` when it handles the
    local player, and this hook stands down for that frame. POST PlayerMove runs after PhysWalking,
    so that stamp is always already in place by the time we read it.

    Once the log settles which path is live, the other one comes out.

    Runs on: BOTH, local player only.
    """
    if not OWN_SLIDE_STATE.is_sliding:
        return
    # PhysWalking already did this frame - anything we wrote now would be a second application.
    if _Phys.handled_local_frame():
        return
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    delta_time = _delta_from(args)
    if delta_time <= 0.0:
        return
    # A failure here would leave the slide state advancing with no visible motion at all. Log it
    # rather than raise, so a bug in the physics can't take the whole movement path down with it.
    try:
        apply_slide_physics(pawn, OWN_SLIDE_STATE, delta_time)
    except Exception as ex:  # noqa: BLE001 - never break the move path
        dbg(f"ENFORCE FAILED {type(ex).__name__}: {ex}")


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

    Runs on: BOTH. Fires per-frame on every pawn whose walking-physics is about to run - if the
    engine dispatches it through script at all, which the probe below exists to establish. Pawns
    that are not sliding fall through to native PhysWalking (return None); sliding pawns get their
    physics computed and integrated here and PhysWalking is skipped entirely.

    It owns the decay clock for *remote* pawns only. The local player's clock lives in `handle_move`
    on a hook we know fires, so that a slide can never outlive its duration just because this one
    turned out to be inert.
    """
    # One-shot probe, fired before every early return below. Its presence or absence in the log is
    # the whole answer to whether the engine dispatches PhysWalking through script at all, and its
    # parameter list settles what the frame delta is actually called. The rework shipped without
    # this and cost a session's play time to a hook that was silently inert.
    if not _Phys.reported:
        _Phys.reported = True
        dbg(f"PHYS fired args={_arg_names(args)}")

    pawn = cast("WillowPlayerPawn", obj)
    pc = getattr(pawn, "Controller", None)
    if pc is None:
        return None

    state = _state_for_pawn(pc)
    if state is None:
        return None

    delta_time = _delta_from(args)
    if delta_time <= 0.0:
        return None

    is_local = state is OWN_SLIDE_STATE

    try:
        # The local player's clock belongs to handle_move (PRE PlayerMove), which is guaranteed to
        # fire; advancing it again here would decay every local slide at double rate. Remote pawns
        # have no PlayerMove on this machine, so for those the host's clock is this hook.
        if not is_local:
            if slide(pc, state, delta_time):
                # `targeted`, so this reaches the one client that owns the slide.
                client_exit_slide(pc.PlayerReplicationInfo)
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

        # Tell enforce_slide to stand down for this frame - we have both written the velocity and
        # integrated it, and it would otherwise write a second time after PlayerMove returns.
        if is_local:
            _Phys.mark_local_frame()
        # Extra exit condition for remote pawns: their crouch released. The local player's copy of
        # this check lives in handle_move's can_slide gate.
        elif not bool(getattr(pc, "bDuck", False)):
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
all_hooks = [handle_move, enforce_slide, handle_duck, jump]
