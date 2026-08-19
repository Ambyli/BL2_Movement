"""The slide itself: how it decays, how it steers, and how it is forced onto the pawn."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, ClassVar

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
from .state import OWN_SLIDE_STATE, PlayerSlideState, is_client

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
) -> bool:
    """Advance the decay curve and report whether the slide is spent. Called every frame.

    Owns nothing else: heading and velocity are applied in `apply_slide_physics`, and ending the
    slide is the caller's job. Returning the verdict rather than acting on it is what lets the two
    callers end a slide the way that suits them - `handle_move` calls `exit_slide` directly for the
    local player, while `_phys_sliding` sends the targeted RPC for a remote one. Dispatching from
    in here meant the local player's own exit took a queued network round-trip to itself, one
    message per player tick, to say something it already knew.

    Runs on: BOTH. Called from `hooks.handle_move` (PRE PlayerMove) for the local slide, and from
    `hooks._phys_sliding` for whichever pawn that hook is running against.

    Returns True when the slide is spent and the caller should end it.
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

    # Exit verdict: either the decay bled below the walking-crouch floor, or the duration cap hit.
    return speed < CROUCHED_PCT_DEFAULT or slide_data.elapsed >= max_duration.value


def apply_slide_physics(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Force the slide's heading and speed onto the pawn, after the engine has had its say.

    PlayerMove recomputes velocity from input every frame, so anything written before it runs is
    thrown away - this must be called from a post hook to be the value the walking physics actually
    integrates. The pawn's speed cap is held clear of the forced speed, or the cap clamps the slide
    back down the moment anything lowers GroundSpeed (aiming down sights being the obvious one).
    Acceleration is zeroed only where we are the authority; see the note at that line.

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
    # cap (CrouchedPct * GroundSpeed) can't clamp us; Velocity carries the actual forced motion.
    pawn.CrouchedPct = max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    # Zeroing Acceleration stops PhysWalking fighting the velocity write - but only do it where we
    # are the authority. On a client, Acceleration is not scratch space: it is the one directional
    # value the ServerMove packet carries about us, and the host steers its copy of our pawn by it
    # and nothing else. Wiping it every frame left the host with no idea where we were going, so it
    # kept moving us along some fixed bearing of its own and corrected us onto it ~33 times a
    # second - which is why a client's slide ignored the direction they set off in while the exact
    # same code looked perfect for whoever was hosting. Measured, not guessed: the PATH probe read
    # accel=(none) on 90 of 90 client samples, and the host's log ran a client's whole slide without
    # a single frame of slide physics.
    if not is_client():
        pawn.Acceleration.X = 0.0
        pawn.Acceleration.Y = 0.0

    # Throttled deliberately. Logging this every frame burned 391 of the debug log's 400 lines on
    # two slides and silently dropped the whole of the next session - the rare lines are the ones
    # worth having, and per-frame detail crowds them out.
    _PostLog.frames += 1
    if _PostLog.frames % POST_LOG_EVERY == 0:
        dbg(f"POST {_who(pawn)} pct={slide_data.speed_pct:.2f} set={speed:.0f}")


__all__ = [
    "apply_slide_physics",
    "can_slide",
    "slide",
]
