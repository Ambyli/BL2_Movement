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

import math
from typing import TYPE_CHECKING, cast

from coroutines import Time, WaitWhile, start_coroutine_tick
from mods_base import get_pc
from networking.decorators import host
from unrealsdk.unreal import WeakPointer

from . import events
from .config import CROUCHED_PCT_DEFAULT, SLIDE_SPEED_DEFAULT
from .debug import every_n, log
from .movement import can_slide, compute_cap, slide
from .state import (
    OWN_SLIDE_STATE,
    PLAYER_SETTINGS,
    SLIDE_STATES,
    PlayerSlideSettings,
    PlayerSlideState,
    begin_slide_state,
    default_settings,
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


def _log_slide_snapshot(
    pc: WillowPlayerController,
    pawn: WillowPlayerPawn,
    state: PlayerSlideState,
    pre_speed: float,
    delta_time: float,
) -> None:
    """Emit one consolidated `SLIDE_TICK` line summarising a slide in progress.

    Called from the driver on the throttled cadence, so a scan of the log reads as one line per
    slide-frame rather than a stream of individual field dumps. Every field a reader would ask
    about a slide in progress lives here:

    - **who** - which player, in the Name#Id form the rest of the log uses.
    - **pos** - absolute location; correlates with position corrections in the netcode log when
      investigating desync.
    - **facing_deg** - view yaw in degrees, converted from Unreal's 65536-unit rotation.
    - **dir** - unit heading the slide is being forced along.
    - **side_deg** - unsigned angle between facing and slide direction; how sideways the player
      is looking as they slide.
    - **turn_deg** - unsigned angle from entry heading to current heading; how much the slide has
      already curved (compare against `max_turn_degrees`).
    - **input** - the raw steering vector driving that turn (sampled locally or arrived via
      `server_slide_input`).
    - **speed_forced / speed_actual** - what `apply_slide_physics` just wrote vs. what the engine
      had entering the tick. A persistent gap between them is the signature of something else
      (correction, walking physics, another mod) fighting our writes.
    - **pct** - decay curve position, the same value `slide()` logs.
    - **elapsed / max, progress** - how close to the hard duration cap this slide is.
    - **ground, crouched_pct** - the two engine flags most likely to end a slide unexpectedly.
    - **moved / expected / motion** - DIAGNOSTIC. `moved` is how far Location actually travelled
      since the previous logged frame; `expected` is how far the forced horizontal speed should
      have carried it over `delta`. With the driver throttle at 1 (one log per frame) these read
      per-frame, and the `motion` tag classifies each frame: `coast` (moved ~= expected, the pawn
      is riding our velocity smoothly), `SNAP` (moved >> expected, Location jumped - a ServerMove
      correction landed), `STALL` (moved ~= 0 while expected is real - the pawn sat still waiting
      for a net update). A slide that is `coast` throughout is smooth on the host; runs of STALL
      punctuated by SNAP are the signature of net-driven position. Only meaningful at throttle 1.

    Kept in its own function to keep the driver body focused on the state machine. All the math
    below only runs when the driver is on a verbose tick, so extracting the call does not shift
    per-frame cost.
    """
    yaw_units = float(pc.Rotation.Yaw)
    yaw_deg = math.fmod(yaw_units * 360.0 / 65536.0, 360.0)
    yaw_rad = math.radians(yaw_deg)
    facing_x = math.cos(yaw_rad)
    facing_y = math.sin(yaw_rad)
    slide_dir_mag = math.hypot(state.dir_x, state.dir_y)
    if slide_dir_mag > 0:
        side_dot = max(-1.0, min(1.0, (facing_x * state.dir_x + facing_y * state.dir_y) / slide_dir_mag))
        side_deg = math.degrees(math.acos(side_dot))
    else:
        side_deg = 0.0
    entry_mag = math.hypot(state.entry_x, state.entry_y)
    if entry_mag > 0 and slide_dir_mag > 0:
        turn_dot = max(-1.0, min(1.0, (state.entry_x * state.dir_x + state.entry_y * state.dir_y) / (entry_mag * slide_dir_mag)))
        turn_deg = math.degrees(math.acos(turn_dot))
    else:
        turn_deg = 0.0
    forced_speed = state.start_speed * (state.speed_pct / SLIDE_SPEED_DEFAULT)
    progress = state.elapsed / state.max_duration if state.max_duration > 0 else 0.0

    # DIAGNOSTIC (temporary): distance Location actually moved since the previous logged frame vs.
    # how far our forced horizontal speed should have carried it. Classifies each frame as coast /
    # SNAP / STALL so a scan of the log settles whether the host coasts the pawn smoothly or the
    # engine snaps its position from ServerMoves. Only meaningful with the driver throttle at 1.
    loc_x = float(pawn.Location.X)
    loc_y = float(pawn.Location.Y)
    expected = forced_speed * delta_time
    if state.has_prev_loc:
        moved = math.hypot(loc_x - state.prev_loc_x, loc_y - state.prev_loc_y)
        ratio = moved / expected if expected > 0.001 else 0.0
        if moved > max(expected * 2.0, 15.0):
            motion = "SNAP"
        elif expected > 5.0 and moved < expected * 0.25:
            motion = "STALL"
        else:
            motion = "coast"
        motion_field = f" moved={moved:.1f} expected={expected:.1f} ratio={ratio:.2f} motion={motion}"
    else:
        motion_field = f" moved=? expected={expected:.1f} ratio=? motion=first"
    state.prev_loc_x = loc_x
    state.prev_loc_y = loc_y
    state.has_prev_loc = True

    log.debug(
        f"SLIDE_TICK who={player_id(pc)}"
        f" pos=({pawn.Location.X:.0f},{pawn.Location.Y:.0f},{pawn.Location.Z:.0f})"
        f" facing_deg={yaw_deg:.1f} dir=({state.dir_x:.3f},{state.dir_y:.3f})"
        f" side_deg={side_deg:.1f} turn_deg={turn_deg:.1f}"
        f" input=({state.input_x:.2f},{state.input_y:.2f})"
        f" speed_forced={forced_speed:.0f} speed_actual={pre_speed:.0f}"
        f" pct={state.speed_pct:.3f}"
        f" elapsed={state.elapsed:.2f}/{state.max_duration:.2f} progress={progress * 100:.0f}%"
        f" ground={pawn.IsOnGroundOrShortFall()} crouched_pct={pawn.CrouchedPct:.3f}"
        f" delta={delta_time:.4f}"
        f"{motion_field}",
    )


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

        # DIAGNOSTIC: back to 30 (coast-vs-snap is settled). Set to 1 again if you need a per-frame
        # `moved`/`motion` trace; at 1 the log fills ~100 lines/s and rotates fast.
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

        # Read the pawn's velocity BEFORE physics writes to it, so `_log_slide_snapshot` below can
        # compare what the engine had this frame against what we are about to force onto it.
        pre_speed = math.hypot(pawn.Velocity.X, pawn.Velocity.Y) if verbose else 0.0

        # Physical gate first, then the decay curve. Either ending the slide ends the driver.
        if not can_slide(pc, pawn, state) or slide(pawn, state, delta_time):
            log.info("_drive_slide teardown reason=gate_or_decay")
            _end_slide(pc, pawn, state)
            log.info("_drive_slide exit reason=slide_ended")
            return

        # Compute only - no pawn writes here. Steering rotates the heading from the owning player's
        # live input (which the injection hook in `hooks` stashed into state before overwriting the
        # pawn's axes); the host does not steer a remote slide, whose heading arrives already-steered
        # in the replicated Acceleration. The speed cap is recomputed each tick and stamped onto the
        # pawn elsewhere - the injection hook for our own pawn, the MoveAutonomous hook for a remote.
        state.cap = compute_cap(state, pawn.GroundSpeed)

        if verbose:
            _log_slide_snapshot(pc, pawn, state, pre_speed, delta_time)


def begin_slide(
    pc: WillowPlayerController,
    dir_x: float,
    dir_y: float,
    settings: PlayerSlideSettings,
    state: PlayerSlideState | None = None,
) -> bool:
    """Start driving a slide for this player on this machine. Idempotent.

    Runs on: BOTH - the owning machine calls it from `enter_slide` with its own state object and
    its own settings, and the host calls it from the enter message for every other player, using
    the heading that arrived with the message and the settings previously announced by that
    client (or defaults if none announced).

    Returns True if this call was the one that started it.
    """
    log.info(
        f"begin_slide enter dir=({dir_x:.3f},{dir_y:.3f}) start_speed={settings.start_speed:.0f}"
        f" decay={settings.decay_rate:.3f} max_duration={settings.max_duration:.2f}"
        f" steer={settings.steer_rate:.2f} max_turn={settings.max_turn_degrees:.1f}"
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
    begin_slide_state(state, dir_x, dir_y, settings)
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
    settings = default_settings()

    # Announce our settings before the enter RPC. RPC ordering from a single sender is preserved,
    # so the host will process this first and its `server_enter_slide` will find fresh values in
    # PLAYER_SETTINGS when it runs. Also covers the case where the on_change handler has never
    # fired (fresh session, no slider changes yet, no announce sent).
    try:
        _announce_settings_to_host(settings)
    except Exception as ex:  # noqa: BLE001 - the slide is more important than a settings sync
        log.warning(f"ANNOUNCE SEND FAILED {type(ex).__name__}: {ex}")

    # Register locally before sending, so the slide never depends on the message for anything. On a
    # listen server the message below comes back to us a tick later and has to find this entry
    # already present, or it would start a second driver against the same pawn. And if the send
    # fails, our own slide is already running by then, so the cost is a host that never hears about
    # it rather than a slide that never happens.
    if not begin_slide(pc, dir_x, dir_y, settings, OWN_SLIDE_STATE):
        log.info("enter_slide exit reason=begin_slide_declined")
        return
    log.info(
        f"ENTER client={is_client()} speed={settings.start_speed:.0f}"
        f" dir=({dir_x:.2f},{dir_y:.2f}) n={len(SLIDE_STATES)}",
    )
    events.fire(events.slide_started, pc)
    try:
        server_enter_slide(dir_x, dir_y)
    except Exception as ex:  # noqa: BLE001 - our slide is already running; only the host misses out
        log.warning(f"ENTER SEND FAILED {type(ex).__name__}: {ex}")
    log.info("enter_slide exit reason=started")


def _announce_settings_to_host(settings: PlayerSlideSettings) -> None:
    """Wrap the announce RPC so both call sites (enter_slide, config on_change) share one path.

    Pulled out because the on_change hook in `config` needs a callable that will not raise into
    the mods_base slider bookkeeping if the game has no world yet - the try/except lives at each
    call site, but the message-building lives here so it is not duplicated.
    """
    log.info(
        f"_announce_settings_to_host enter start_speed={settings.start_speed:.0f}"
        f" decay={settings.decay_rate:.3f} max_duration={settings.max_duration:.2f}"
        f" steer={settings.steer_rate:.2f} max_turn={settings.max_turn_degrees:.1f}",
    )
    server_announce_settings(
        settings.start_speed,
        settings.decay_rate,
        settings.max_duration,
        settings.steer_rate,
        settings.max_turn_degrees,
    )
    log.info("_announce_settings_to_host exit")


@host.json_message
def server_announce_settings(
    start_speed_v: float,
    decay_rate_v: float,
    max_duration_v: float,
    steer_rate_v: float,
    max_turn_degrees_v: float,
) -> None:
    """Cache the sender's five slider values on the host, keyed by their PlayerID.

    Runs on: HOST only. Fired by every client whenever its own sliders change, and once by every
    client at the top of `enter_slide` so the host has fresh values before the enter RPC arrives.

    Never touches a slide already in progress: the values that shape the current slide were
    snapshotted onto its `PlayerSlideState` at open time on both machines, and updating them
    retroactively on the host alone would drift the two simulations. A mid-slide slider change
    therefore takes effect at the sender's next slide, not this one.
    """
    log.info(
        f"server_announce_settings enter start_speed={start_speed_v:.0f}"
        f" decay={decay_rate_v:.3f} max_duration={max_duration_v:.2f}"
        f" steer={steer_rate_v:.2f} max_turn={max_turn_degrees_v:.1f}",
    )
    pc = cast("WillowPlayerController", server_announce_settings.sender.Owner)
    if pc is None or (player := player_id(pc)) is None:
        log.info(f"server_announce_settings exit reason=no_player has_pc={pc is not None}")
        return
    PLAYER_SETTINGS[player] = PlayerSlideSettings(
        start_speed=start_speed_v,
        decay_rate=decay_rate_v,
        max_duration=max_duration_v,
        steer_rate=steer_rate_v,
        max_turn_degrees=max_turn_degrees_v,
    )
    log.info(
        f"server_announce_settings exit stored player={player} known_players={len(PLAYER_SETTINGS)}",
    )


def _log_net_probe(pawn: WillowPlayerPawn, pc: WillowPlayerController) -> None:
    """DIAGNOSTIC (temporary): dump the native net / movement knobs on a remote pawn, once per slide.

    We suspect the host renders a remote sliding pawn from BL2's own movement replication rather
    than from our forced-velocity writes, so the levers that would fix the teleporting live on these
    engine properties, not in our code. This logs which of them actually exist on this build and
    their current values, so we know what is tunable before trying to tune anything.

    Every read is guarded: the exact property set varies across UE3 builds, and a name that is
    absent here is itself a useful result (`<absent>`), not an error. Runs on the HOST only, from
    `server_enter_slide`, so `pawn` is always a remote client's proxy - exactly the object whose
    position is teleporting.
    """
    pawn_names = [
        "NetUpdateFrequency",
        "NetPriority",
        "bReplicateMovement",
        "bSmoothNetUpdates",
        "bNetDirty",
        "Physics",
        "Role",
        "RemoteRole",
        "bAlwaysRelevant",
        "NetUpdateTime",
        "LastNetUpdateTime",
        "bSimGravityDisabled",
        "CustomTimeDilation",
        "MeshTranslationOffset",
    ]
    pc_names = ["NetUpdateFrequency", "bUpdatePosition", "PlayerReplicationInfo"]
    for name in pawn_names:
        try:
            log.info(f"NET_PROBE pawn.{name} = {getattr(pawn, name)}")
        except Exception as ex:  # noqa: BLE001 - probing which props exist; absence is the result
            log.info(f"NET_PROBE pawn.{name} = <absent:{type(ex).__name__}>")
    for name in pc_names:
        try:
            log.info(f"NET_PROBE pc.{name} = {getattr(pc, name)}")
        except Exception as ex:  # noqa: BLE001 - see above
            log.info(f"NET_PROBE pc.{name} = <absent:{type(ex).__name__}>")
    # CurrentNetSpeed (the per-connection bandwidth cap that throttles ServerMove rate) lives on the
    # Player object, a hop off the controller - probed separately so a missing controller-side name
    # above does not skip it.
    try:
        log.info(f"NET_PROBE pc.Player.CurrentNetSpeed = {pc.Player.CurrentNetSpeed}")
    except Exception as ex:  # noqa: BLE001 - see above
        log.info(f"NET_PROBE pc.Player.CurrentNetSpeed = <absent:{type(ex).__name__}>")


@host.json_message
def server_enter_slide(dir_x: float, dir_y: float) -> None:
    """Start the host's copy of a player's slide, on the heading they opened it on.

    Runs on: HOST only - `host` addresses the message there and nowhere else, so no net-mode guard
    is needed in the body.

    Heading travels with the message; the five physics dials come from `PLAYER_SETTINGS`, populated
    by `server_announce_settings` before this arrives. If the announce never landed (a fresh
    connection where the client's on_change never fired and its enter-time announce was dropped),
    `default_settings()` is used instead so the slide still opens with reasonable numbers.

    The host's copy of a remote pawn's velocity is whatever its own simulation last produced by
    the time this arrives, so `heading_from` cannot be re-run here and the heading has to travel
    with the message.
    """
    log.info(f"server_enter_slide enter dir=({dir_x:.3f},{dir_y:.3f})")
    pc = cast("WillowPlayerController", server_enter_slide.sender.Owner)
    if pc is None or (player := player_id(pc)) is None:
        log.info(f"server_enter_slide exit reason=no_player has_pc={pc is not None}")
        return
    settings = PLAYER_SETTINGS.get(player)
    if settings is None:
        log.info(f"server_enter_slide fallback player={player} reason=no_announce_yet")
        settings = default_settings()
    else:
        log.info(f"server_enter_slide using announced settings player={player}")
    started = begin_slide(pc, dir_x, dir_y, settings)
    if started:
        log.info(
            f"SLIDE_ON who={player} start_speed={settings.start_speed:.0f}"
            f" dir=({dir_x:.2f},{dir_y:.2f}) n={len(SLIDE_STATES)}",
        )
        # DIAGNOSTIC (temporary): one dump of the remote pawn's native net knobs at slide open.
        if (pawn := cast("WillowPlayerPawn", pc.Pawn)) is not None:
            _log_net_probe(pawn, pc)
    log.info(f"server_enter_slide exit started={started}")


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
    server_announce_settings,
    server_enter_slide,
    server_exit_slide,
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
