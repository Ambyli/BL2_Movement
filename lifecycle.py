"""Entering and leaving a slide, driving it while it runs, and the replication that keeps host and
client agreeing.

The network messages live here rather than in a module of their own on purpose: ending a slide sends
`server_exit_slide`, and the arriving `server_enter_slide` calls straight back into `begin_slide`.
They are two halves of one protocol, and splitting them would buy a tidier file listing at the cost
of a genuine import cycle.

A slide is driven by a coroutine rather than a game hook. The coroutine tick is a plain per-frame
viewport tick: it does not care whose pawn a slide belongs to, whether that pawn's physics ran this
frame, or which machine is authoritative over it. One driver therefore serves both our own slide and,
on the host, every remote one - and it advances at frame cadence on both machines, so two copies of
the same slide run down the same curve rather than one of them stepping at packet rate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from coroutines import Time, WaitWhile, start_coroutine_tick
from mods_base import get_pc
from networking.decorators import host
from uemath import Vector
from unrealsdk.unreal import WeakPointer

from . import events
from .config import CROUCHED_PCT_DEFAULT, SLIDE_SPEED_DEFAULT, start_speed
from .debug import every_n, log
from .movement import apply_slide_physics, can_slide, slide
from .state import (
    OWN_SLIDE_STATE,
    SLIDE_STATES,
    PlayerSlideState,
    begin_slide_state,
    heading_from,
    is_client,
    player_id,
    state_for,
)

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn
    from coroutines import TickCoroutine


def _paused() -> bool:
    """Whether the game is paused.

    A read that fails mid-transition counts as not paused, so the driver falls through to its own
    teardown check on the next tick rather than stalling forever on a controller that is gone.
    """
    verbose = every_n("_paused", 30)
    if verbose:
        log.debug("_paused enter")
    try:
        result = bool(get_pc().IsPaused())
    except Exception as ex:  # noqa: BLE001 - no controller mid-load
        if verbose:
            log.debug(f"_paused exit result=False reason={type(ex).__name__}")
        return False
    if verbose:
        log.debug(f"_paused exit result={result}")
    return result


def _forget(state: PlayerSlideState) -> None:
    """Drop a slide from the live set.

    By identity rather than by key, so this still works when the controller the entry was keyed
    under has already been destroyed.
    """
    log.info(f"_forget enter is_sliding={state.is_sliding} live_slides={len(SLIDE_STATES)}")
    state.is_sliding = False
    removed: list[int] = []
    for player, other in list(SLIDE_STATES.items()):
        if other is state:
            del SLIDE_STATES[player]
            removed.append(player)
    log.info(f"_forget exit removed={removed} live_slides={len(SLIDE_STATES)}")


def _end_slide(
    pc: WillowPlayerController | None,
    pawn: WillowPlayerPawn | None,
    state: PlayerSlideState,
) -> None:
    """Stop a slide and hand the pawn back to ordinary crouch physics.

    Idempotent, and it has to be: the driver, the exit message and a jump can all reach this for the
    same slide within a frame of each other.

    Runs on: BOTH, for whichever slide it is handed.
    """
    log.info(
        f"_end_slide enter is_sliding={state.is_sliding} is_own={state is OWN_SLIDE_STATE}"
        f" elapsed={state.elapsed:.2f} has_pc={pc is not None} has_pawn={pawn is not None}",
    )
    if not state.is_sliding:
        log.info("_end_slide exit reason=already_ended")
        return
    # Clear the flag first and unconditionally. Everything below may fail; this line is what makes
    # such a failure recoverable, because a slide still flagged on refuses to be re-entered.
    _forget(state)
    if pawn is not None:
        pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
        log.info(f"_end_slide reset CrouchedPct={pawn.CrouchedPct:.3f}")
    if state is not OWN_SLIDE_STATE:
        log.info("_end_slide exit reason=remote_slide")
        return

    # Our own slide, so there is a host to tell and a view model to put back.
    try:
        log.info(f"EXIT client={is_client()} elapsed={state.elapsed:.2f}")
        # Only worth sending while we still have a controller - and isolated, because addressing the
        # host walks the player list looking for the party leader and raises if it finds nobody.
        # This runs inside the driver coroutine, where an escaping exception would take the whole
        # coroutine runner's frame with it rather than just this slide.
        if pc is not None:
            server_exit_slide()
    except Exception as ex:  # noqa: BLE001 - a failed send must never escape the driver
        log.warning(f"EXIT SEND FAILED {type(ex).__name__}: {ex}")
    finally:
        # In the `finally` because the view model getting stuck in its slide pose is the single most
        # visible way this function can fail.
        if pc is not None:
            events.fire(events.slide_ended, pc)
        log.info("_end_slide exit reason=own_slide_torn_down")


def _drive_slide(
    pc_ref: WeakPointer[WillowPlayerController],
    state: PlayerSlideState,
) -> TickCoroutine:
    """Advance one slide, one frame at a time, until it ends.

    Runs on: whichever machine started it - the owning machine for our own slide, the host for every
    remote one. A weak pointer rather than the controller itself so a player who disconnects or
    changes level mid-slide takes their driver with them instead of keeping the object alive.
    """
    log.info(f"_drive_slide enter is_own={state is OWN_SLIDE_STATE}")
    while True:
        yield WaitWhile(_paused)

        verbose = every_n("_drive_slide", 30)
        pc = pc_ref()
        pawn = None if pc is None else cast("WillowPlayerPawn", pc.Pawn)
        if pc is None or pawn is None:
            # Death, disconnect or a level change. Nothing left to drive, and nothing to restore
            # either - the pawn this state described no longer exists.
            log.info(f"_drive_slide teardown reason=pc={pc is not None},pawn={pawn is not None}")
            _end_slide(pc, pawn, state)
            log.info("_drive_slide exit reason=weakref_gone")
            return

        delta_time = Time.delta_time
        if delta_time <= 0.0:
            if verbose:
                log.debug(f"_drive_slide skip reason=delta<=0 delta={delta_time:.4f}")
            continue

        if verbose:
            log.debug(
                f"_drive_slide tick delta={delta_time:.4f}"
                f" pawn_vel=({pawn.Velocity.X:.0f},{pawn.Velocity.Y:.0f})",
            )

        # Physical gate first, then the decay curve. Either ending the slide ends the driver.
        if not can_slide(pc, pawn, state) or slide(pawn, state, delta_time):
            log.info("_drive_slide teardown reason=gate_or_decay")
            _end_slide(pc, pawn, state)
            log.info("_drive_slide exit reason=slide_ended")
            return

        # Refresh the frame's steering input on the machine that owns this slide - only ever our own
        # here, since the host's copy of a remote slide has its input written by server_slide_input.
        # On a client, also forward the sample to the host so its copy reads what we are pressing
        # instead of Unreal's replicated Acceleration.
        if state is OWN_SLIDE_STATE:
            accel = Vector(pawn.Acceleration)
            state.input_x = accel.x
            state.input_y = accel.y
            if verbose:
                log.debug(
                    f"_drive_slide sampled input=({state.input_x:.2f},{state.input_y:.2f})"
                    f" is_client={is_client()}",
                )
            if is_client():
                try:
                    server_slide_input(state.input_x, state.input_y)
                except Exception as ex:  # noqa: BLE001 - a failed send must never break the driver
                    log.warning(f"INPUT SEND FAILED {type(ex).__name__}: {ex}")

        apply_slide_physics(pawn, state, delta_time)


def begin_slide(
    pc: WillowPlayerController,
    dir_x: float,
    dir_y: float,
    speed: float,
    state: PlayerSlideState | None = None,
) -> bool:
    """Start driving a slide for this player on this machine. Idempotent.

    Runs on: BOTH - the owning machine calls it from `enter_slide` with its own state object and
    its own slider value, and the host calls it from the enter message for every other player,
    passing the heading and start speed that arrived with the message.

    Returns True if this call was the one that started it.
    """
    log.info(
        f"begin_slide enter dir=({dir_x:.3f},{dir_y:.3f}) speed={speed:.0f}"
        f" have_state={state is not None} live_slides={len(SLIDE_STATES)}",
    )
    if (player := player_id(pc)) is None or (pawn := cast("WillowPlayerPawn", pc.Pawn)) is None:
        log.info(f"begin_slide exit result=False reason=no_player_or_pawn player={player}")
        return False
    # Membership is liveness - an entry exists only while its slide runs, so finding one means this
    # is a duplicate and the pawn is already being driven.
    if player in SLIDE_STATES:
        log.info(f"begin_slide exit result=False reason=duplicate player={player}")
        return False

    if state is None:
        state = PlayerSlideState(old_z=pawn.Location.Z, is_sliding=True)
        log.info(f"begin_slide constructed fresh state old_z={pawn.Location.Z:.2f}")
    else:
        state.old_z = pawn.Location.Z
        state.is_sliding = True
        log.info(f"begin_slide reused own state old_z={pawn.Location.Z:.2f}")
    begin_slide_state(state, dir_x, dir_y, speed)
    SLIDE_STATES[player] = state

    # Boost the replicated crouch multiplier so the speed cap is clear of the forced slide speed
    # from the very first frame, before the driver's first tick lands.
    pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    start_coroutine_tick(_drive_slide(WeakPointer(pc), state))
    log.info(f"begin_slide exit result=True player={player} live_slides={len(SLIDE_STATES)}")
    return True


def enter_slide(pc: WillowPlayerController) -> None:
    """The local player wants to slide: start driving it here, and tell the host.

    Runs on: whichever machine's local player pressed duck-while-sprinting.
    """
    log.info(f"enter_slide enter pc={pc}")
    if (pawn := cast("WillowPlayerPawn", pc.Pawn)) is None:
        log.info("enter_slide exit reason=no_pawn")
        return
    dir_x, dir_y = heading_from(pawn)
    speed = start_speed.value
    # Register locally before sending, so the slide never depends on the message for anything. On a
    # listen server the message below comes back to us a tick later and has to find this entry
    # already present, or it would start a second driver against the same pawn. And if the send
    # fails, our own slide is already running by then, so the cost is a host that never hears about
    # it rather than a slide that never happens.
    if not begin_slide(pc, dir_x, dir_y, speed, OWN_SLIDE_STATE):
        log.info("enter_slide exit reason=begin_slide_declined")
        return
    log.info(
        f"ENTER client={is_client()} speed={speed:.0f}"
        f" dir=({dir_x:.2f},{dir_y:.2f}) n={len(SLIDE_STATES)}",
    )
    events.fire(events.slide_started, pc)
    try:
        server_enter_slide(dir_x, dir_y, speed)
    except Exception as ex:  # noqa: BLE001 - our slide is already running; only the host misses out
        log.warning(f"ENTER SEND FAILED {type(ex).__name__}: {ex}")
    log.info("enter_slide exit reason=started")


@host.json_message
def server_enter_slide(dir_x: float, dir_y: float, speed: float) -> None:
    """Start the host's copy of a player's slide, on the heading and speed they opened it on.

    Runs on: HOST only - `host` addresses the message there and nowhere else, so no net-mode guard
    is needed in the body.

    Heading and start speed travel with the message rather than being resampled or re-read from the
    host's own slider here. The host's copy of a remote pawn's velocity is whatever its own
    simulation last produced (by the time this arrives, neither the client's heading nor
    necessarily above the floor `heading_from` needs), and the host's slider value is its own -
    neither is what the client just opened the slide on.
    """
    log.info(f"server_enter_slide enter dir=({dir_x:.3f},{dir_y:.3f}) speed={speed:.0f}")
    pc = cast("WillowPlayerController", server_enter_slide.sender.Owner)
    if pc is None:
        log.info("server_enter_slide exit reason=no_sender_owner")
        return
    started = begin_slide(pc, dir_x, dir_y, speed)
    if started:
        log.info(
            f"SLIDE_ON who={player_id(pc)} speed={speed:.0f}"
            f" dir=({dir_x:.2f},{dir_y:.2f}) n={len(SLIDE_STATES)}",
        )
    log.info(f"server_enter_slide exit started={started}")


@host.json_message
def server_slide_input(input_x: float, input_y: float) -> None:
    """Write the sender's live steering input into the host's copy of their slide.

    Runs on: HOST only. Fires once per client-side driver tick during a slide, so the host's copy
    reads the same steering vector the client's `apply_slide_physics` did on the same frame rather
    than whatever Unreal's Acceleration replication last produced. No-op if the host has not yet
    started (or has already ended) its copy of that player's slide - order between this and the
    enter/exit messages is not something the driver relies on.
    """
    verbose = every_n("server_slide_input", 30)
    if verbose:
        log.debug(f"server_slide_input enter input=({input_x:.2f},{input_y:.2f})")
    pc = cast("WillowPlayerController", server_slide_input.sender.Owner)
    if pc is None or (state := state_for(pc)) is None:
        if verbose:
            log.debug(
                f"server_slide_input exit reason=no_state has_pc={pc is not None}",
            )
        return
    state.input_x = input_x
    state.input_y = input_y
    if verbose:
        log.debug(f"server_slide_input exit stored player={player_id(pc)}")


@host.message
def server_exit_slide() -> None:
    """Stop the host's copy of a player's slide. Runs on: HOST only."""
    log.info("server_exit_slide enter")
    pc = cast("WillowPlayerController", server_exit_slide.sender.Owner)
    if pc is None or (state := state_for(pc)) is None:
        log.info(f"server_exit_slide exit reason=no_state has_pc={pc is not None}")
        return
    _end_slide(pc, cast("WillowPlayerPawn", pc.Pawn), state)
    log.info(f"SLIDE_OFF who={player_id(pc)}")
    log.info("server_exit_slide exit reason=ended")


@host.json_message
def server_set_slide_jump_velocity(vel_x: float, vel_y: float) -> None:
    """Carry a slide's momentum into the jump that ended it, on the machine that owns movement.

    The client force-calls `DoJump` on its own pawn, because crouching swallows the ordinary jump
    input and a slide holds crouch throughout. That call is purely local - it sets no replicated
    flag - so without this the host never learns the player left the ground, keeps simulating them
    walking, and corrects the jump away as fast as it is predicted. Hence the `DoJump` here too: the
    host has to make the same move, not merely be told the resulting velocity.

    Runs on: HOST only.
    """
    log.info(f"server_set_slide_jump_velocity enter vel=({vel_x:.0f},{vel_y:.0f})")
    pc = cast("WillowPlayerController", server_set_slide_jump_velocity.sender.Owner)
    if pc is None or (pawn := pc.Pawn) is None:
        log.info(f"server_set_slide_jump_velocity exit reason=no_pawn has_pc={pc is not None}")
        return
    # If this arrived on the same frame the client's DoJump ran, the host's copy of the pawn is
    # still grounded. Kick it into the falling state so the velocity write survives walking physics.
    grounded = pawn.IsOnGroundOrShortFall()
    if grounded:
        pawn.DoJump(True)
        log.info("server_set_slide_jump_velocity forced DoJump grounded=True")
    pawn.Velocity.X = vel_x
    pawn.Velocity.Y = vel_y
    log.info(
        f"SERVER_JUMP who={player_id(pc)} vel=({vel_x:.0f},{vel_y:.0f}) prior_grounded={grounded}",
    )
    log.info("server_set_slide_jump_velocity exit reason=velocity_written")


# Passed explicitly to add_network_functions: it only scans the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
network_functions = [
    server_enter_slide,
    server_exit_slide,
    server_slide_input,
    server_set_slide_jump_velocity,
]

# Pinned rather than left to the library default of "<module>:<qualname>", which begins with the
# mod's *directory name* - so the same mod unzipped into `sliding` on one machine and
# `BL2_Movement-main` on another produces different identifiers, and every message is discarded on
# arrival as unknown, in both directions, with nothing but a console warning to show for it. Both
# players still need matching builds; they no longer need matching folder names.
PROTOCOL_PREFIX = "sliding"

for _func in network_functions:
    _func.network_identifier = f"{PROTOCOL_PREFIX}:{_func.__wrapped__.__qualname__}"
