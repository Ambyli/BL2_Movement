"""The slide itself: how it decays, how it steers, and how it is forced onto the pawn."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, ClassVar, cast

from uemath import Vector

from .config import (
    CROUCHED_PCT_DEFAULT,
    SLIDE_BACK_CUTOFF,
    SLIDE_SPEED_DEFAULT,
    SLIDE_STEER_DEADZONE,
    decay_rate,
    max_duration,
    max_turn_degrees,
    start_speed,
    steer_rate,
)
from .debug import dbg
from .lifecycle import client_exit_slide
from .state import CLIENTS_SLIDE_STATES, OWN_SLIDE_STATE, PlayerSlideState, world_time

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


POST_LOG_EVERY: int = 30
"""One line per this many forced frames. See the note in `apply_slide_physics`."""


class _PostLog:
    frames: ClassVar[int] = 0


def _who(pawn: WillowPlayerPawn) -> str:
    """Name the pawn a forced frame belongs to.

    `apply_slide_physics` drives both our own slide and every remote one, so an untagged line cannot
    be attributed - two players' decay curves interleave and read as one curve jumping backwards.
    """
    try:
        return str(pawn.Controller.PlayerReplicationInfo.PlayerName)
    except Exception:  # noqa: BLE001 - a label is not worth raising over
        return "?"


def can_slide(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> bool:
    """Whether the local player's slide should still be running this frame.

    Runs on: whichever machine owns the slide (client or host). Read from handle_move as the
    per-frame exit gate. Note that it checks OWN_SLIDE_STATE, not the shared dict - this only
    speaks about the local player, never remote ones.
    """
    return OWN_SLIDE_STATE.is_sliding and bool(pc.bDuck) and pawn.IsOnGroundOrShortFall()


def slide(
    pc: WillowPlayerController,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Advance the decay curve and decide when the slide is spent. Called every frame.

    Owns nothing else: heading and velocity are applied in `apply_slide_physics`, from a post hook,
    where PlayerMove can no longer overwrite them.

    Runs on: BOTH. Called from the client's `handle_move` (PRE PlayerMove) for its own slide, from
    the host's `drive_hosted_slides` for the host's own slide entry, and from the host's
    `_drive_remote_slide` (POST MoveAutonomous) for remote clients. The exit trigger fires a
    targeted RPC back to whichever machine owns the slide.
    """
    # Height difference against last frame, in unreal units.
    z_diff: float = pc.Pawn.Location.Z - slide_data.old_z

    # Time decay applies on every frame. Upstream skipped it entirely whenever the frame was
    # downhill, so even the gentlest grade left a slide running indefinitely; now a slope only
    # offsets the decay, and has to be genuinely steep to offset it fully.
    speed = slide_data.speed_pct - delta_time * decay_rate.value
    if z_diff < 0:
        speed -= z_diff * 0.0005  # downhill, wins some speed back
    else:
        speed -= z_diff * 0.004  # uphill, sheds extra

    # A slope may sustain a slide, but never push it past the speed it opened at.
    speed = min(speed, SLIDE_SPEED_DEFAULT)

    # Persist the frame's result back into the state dataclass. `old_z` becomes the next frame's
    # baseline for the slope calc, `speed_pct` feeds into apply_slide_physics's speed derivation,
    # and `elapsed` is checked against the hard duration cap below.
    slide_data.old_z = pc.Pawn.Location.Z
    slide_data.speed_pct = speed
    slide_data.elapsed += delta_time

    # Exit triggers: either the decay bled below the walking-crouch floor or the duration cap has
    # hit. The RPC is `targeted`, so calling it from the host reaches the specific client that
    # owns this slide; calling it from the client with its own PRI is a same-machine invocation
    # that lands in `client_exit_slide` locally.
    if speed < CROUCHED_PCT_DEFAULT or slide_data.elapsed >= max_duration.value:
        client_exit_slide(pc.PlayerReplicationInfo)


def apply_slide_physics(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Force the slide's heading and speed onto the pawn, after the engine has had its say.

    PlayerMove recomputes velocity from input every frame, so anything written before it runs is
    thrown away - this must be called from a post hook to be the value the walking physics actually
    integrates. Acceleration is zeroed so the engine has nothing left to fight with, and the pawn's
    speed cap is held clear of the forced speed, or the cap clamps the slide back down the moment
    anything lowers GroundSpeed (aiming down sights being the obvious one).

    Runs on: BOTH. Called from `enforce_slide` (POST PlayerMove) on the machine that owns the
    local slide, and from `_drive_remote_slide` (POST MoveAutonomous) on the host for every
    remote slide. Both are POST hooks by design - see the docstring on either caller.
    """
    # Current slide heading, projected to the ground plane. If it's zero the entry heading was
    # never locked (opened from a standstill) and there is nothing to force onto the pawn.
    direction = Vector((slide_data.dir_x, slide_data.dir_y, 0.0))
    if direction.magnitude == 0:
        return

    # Read the pawn's current input as a candidate steering vector. Zero-magnitude input (no
    # movement keys held) skips the steering branch entirely and the slide runs in a straight
    # line at its current heading.
    accel = Vector(pawn.Acceleration)
    accel.z = 0
    if accel.magnitude > 0:
        accel.normalize()
        backwards = accel.dot(direction)
        if backwards > SLIDE_BACK_CUTOFF:
            # Drop the part of the input running back down the slide, then steer on whatever
            # sideways component survives - weighted by how sideways it actually is. Normalising it
            # unweighted turns a hair of residue from a near-backwards input into a full strength
            # turn, which is precisely how holding back used to spin the slide right around.
            if backwards < 0:
                accel = accel - direction * backwards
            strength = accel.magnitude
            if strength > SLIDE_STEER_DEADZONE:
                alpha = min(steer_rate.value * delta_time * strength, 1.0)
                direction = direction.lerp(accel.normalize(), alpha).normalize()

    # Backstop: never let steering accumulate far enough to reverse the slide, however the input is
    # fed in. Anything past the limit is pinned to the edge of the allowed cone.
    entry = Vector((slide_data.entry_x, slide_data.entry_y, 0.0))
    if entry.magnitude > 0:
        cos_limit = math.cos(math.radians(max_turn_degrees.value))
        along = direction.dot(entry)
        if along < cos_limit:
            perp = direction - entry * along
            if perp.magnitude > 0:
                perp.normalize()
                sin_limit = math.sqrt(max(1.0 - cos_limit * cos_limit, 0.0))
                direction = (entry * cos_limit + perp * sin_limit).normalize()

    # Write the (possibly steered, possibly clamped) heading back into the state so the next
    # frame's steering starts from where this one left off.
    slide_data.dir_x = direction.x
    slide_data.dir_y = direction.y

    # Derive absolute speed from the decay curve's speed_pct. SLIDE_SPEED_DEFAULT is where the
    # curve opens; dividing by it scales the raw tuning to whatever start_speed the user picked.
    speed = start_speed.value * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)

    # Forcing the pawn state. CrouchedPct is held clear of the actual slide speed so the engine's
    # cap (CrouchedPct * GroundSpeed) can't clamp us; Acceleration is zeroed so PhysWalking has
    # nothing left to fight the velocity write with; Velocity carries the actual forced motion.
    pawn.CrouchedPct = max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)
    pawn.Acceleration.X = 0.0
    pawn.Acceleration.Y = 0.0
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    # Throttled deliberately. Logging this every frame burned 391 of the debug log's 400 lines on
    # two slides and silently dropped the whole of the next session - the rare lines are the ones
    # worth having, and per-frame detail crowds them out.
    _PostLog.frames += 1
    if _PostLog.frames % POST_LOG_EVERY == 0:
        dbg(f"POST {_who(pawn)} pct={slide_data.speed_pct:.2f} set={speed:.0f}")


class _HostTick:
    """Which frame the host duty last ran on, and where it was driven from."""

    last_world_time: ClassVar[float] = -1.0
    reported_source: ClassVar[str] = ""


def drive_hosted_slides(
    local_pc: WillowPlayerController,
    delta_time: float,
    source: str,
) -> None:
    """Single entry point for the host duty, collapsed to once per frame.

    This is driven from more than one hook so it keeps running whatever state the host is in -
    PlayerMove stops firing the moment the host jumps or gets in a vehicle, which would strand
    every other player's slide. The dedup matters because those hooks can both fire on the same
    frame, and running twice would decay every slide at double rate.

    Runs on: HOST only. Callers are `handle_move` (PRE PlayerMove) and `_host_tick` (PRE
    PlayerTick). Whichever fires first this frame wins; the other is a no-op via the world-time
    dedup below.
    """
    # World-time dedup. Both callers may fire on the same frame; the first to arrive stamps this
    # frame's timestamp and does the work, and the second returns immediately.
    now = world_time()
    if now == _HostTick.last_world_time:
        return
    _HostTick.last_world_time = now

    # Log the transition when the driver changes (usually PlayerTick after enable, occasionally
    # PlayerMove when the tick hook failed to register). Steady-state produces no output.
    if _HostTick.reported_source != source:
        _HostTick.reported_source = source
        dbg(f"HOST TICK now driven by {source}")

    tick_hosted_slides(local_pc, delta_time)


def tick_hosted_slides(local_pc: WillowPlayerController, delta_time: float) -> None:
    """Drive every slide the host is responsible for. Host only, every frame, unconditionally.

    BL2 is server authoritative over movement, so a remote player's slide has to be forced on the
    server too. Without it the host simulates them from their replicated input - which, mid-slide
    with no key held, is nothing - decides they should be stationary, and corrects them out of the
    slide as fast as they predict themselves into it.

    Our own entry is only decayed here, not forced: the local pawn is driven from the post hook
    instead, after PlayerMove has stopped overwriting it.

    Runs on: HOST only. Called from `drive_hosted_slides` after the once-per-frame dedup. As of
    the MoveAutonomous POST refactor, this only touches the host's OWN slide entry - remote
    clients are decayed and forced from `_drive_remote_slide` on the client's move clock instead.
    """
    # Walk the dict once, sweeping dead weak-refs as we go. Only our own entry is decayed here
    # after the MoveAutonomous refactor; the rest of the dict is simulated on client move clocks.
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
            continue

        # An entry that's flagged not-sliding is a stale end-marker; we just leave it alone.
        state = CLIENTS_SLIDE_STATES[player]
        if not state.is_sliding:
            continue

        # Remote players are no longer driven from here. A remote pawn does not move on the host's
        # frame at all - it moves inside MoveAutonomous, at packet rate - so both their decay and
        # their forced velocity now live in the post hook on that function, where the writes land
        # after the physics instead of being recomputed by it. This loop keeps only our own entry.
        if _pc != local_pc:
            continue

        slide(_pc, state, delta_time)
        # Mirror our own progress out, so the local exit checks still see it when we are the host
        # and our state lives in this dict rather than in OWN_SLIDE_STATE.
        OWN_SLIDE_STATE.speed_pct = state.speed_pct
        OWN_SLIDE_STATE.elapsed = state.elapsed


__all__ = [
    "apply_slide_physics",
    "can_slide",
    "drive_hosted_slides",
    "slide",
    "tick_hosted_slides",
]
