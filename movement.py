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
from .state import PlayerSlideState

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


POST_LOG_EVERY: int = 30
"""One line per this many forced frames, so a slide costs a handful of lines rather than hundreds."""


class _PostLog:
    frames: ClassVar[int] = 0


def _who(pawn: WillowPlayerPawn) -> str:
    """Name the pawn a forced frame belongs to, as `Name#PlayerID`.

    The host drives every player's slide through here, so an untagged line cannot be attributed and
    two decay curves interleave into one that appears to jump backwards. The id is on the end
    because display names are not unique - two machines signed in to the same account report the
    same name, which is exactly the session this most needs to be readable in.
    """
    try:
        pri = pawn.Controller.PlayerReplicationInfo
        return f"{pri.PlayerName}#{pri.PlayerID}"
    except Exception:  # noqa: BLE001 - a label is not worth raising over
        return "?"


def can_slide(
    pc: WillowPlayerController,
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
) -> bool:
    """Whether this slide should still be running.

    Runs on: BOTH, against whichever player's state it is handed - our own on the machine that owns
    it, and every sliding player on the host. `bDuck` reaches the host for a remote player through
    the move stream's compressed flags, so the same gate reads true on both machines.
    """
    return slide_data.is_sliding and bool(pc.bDuck) and pawn.IsOnGroundOrShortFall()


def slide(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> bool:
    """Advance the decay curve and report whether the slide is spent. Called every frame.

    Owns nothing else: heading and velocity are applied in `apply_slide_physics`, and ending the
    slide is the caller's job.

    Runs on: BOTH, from the slide driver in `lifecycle`.

    Returns True when the slide is spent and the caller should end it.
    """
    # Height difference against last frame, in unreal units.
    z_diff: float = pawn.Location.Z - slide_data.old_z

    # Time decay applies on every frame; a slope only offsets it, and has to be genuinely steep to
    # offset it fully.
    speed = slide_data.speed_pct - delta_time * decay_rate.value
    if z_diff < 0:
        speed -= z_diff * 0.0005  # downhill, wins some speed back
    else:
        speed -= z_diff * 0.004  # uphill, sheds extra

    # A slope may sustain a slide, but never push it past the speed it opened at.
    speed = min(speed, SLIDE_SPEED_DEFAULT)

    # Persist the frame's result. `old_z` is the next frame's slope baseline, `speed_pct` feeds
    # apply_slide_physics, and `elapsed` is checked against the hard duration cap below.
    slide_data.old_z = pawn.Location.Z
    slide_data.speed_pct = speed
    slide_data.elapsed += delta_time

    # Exit verdict: the decay bled below the walking-crouch floor, or the duration cap hit.
    return speed < CROUCHED_PCT_DEFAULT or slide_data.elapsed >= max_duration.value


def apply_slide_physics(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Force the slide's heading and speed onto the pawn.

    The pawn's speed cap is held clear of the forced speed, or the cap clamps the slide back down
    the moment anything lowers GroundSpeed - aiming down sights being the obvious one.

    Acceleration is deliberately left exactly as the engine set it. On the machine that owns the
    slide that is the player's live input, which is what steering reads below; on the host it is the
    same vector, unpacked from that player's move. Writing to it would make the two machines'
    walking physics blend differently and pull their simulations apart.

    Runs on: BOTH, from the slide driver in `lifecycle`.
    """
    # Current slide heading, projected to the ground plane. Zero means the entry heading was never
    # locked (opened from a standstill) and there is nothing to force onto the pawn.
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
            # turn, which is how holding back spins a slide right around.
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

    # CrouchedPct is held clear of the actual slide speed so the engine's cap
    # (CrouchedPct * GroundSpeed) can't clamp us; Velocity carries the actual forced motion.
    pawn.CrouchedPct = max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    _PostLog.frames += 1
    if _PostLog.frames % POST_LOG_EVERY == 0:
        dbg(f"POST {_who(pawn)} pct={slide_data.speed_pct:.2f} set={speed:.0f}")


__all__ = [
    "apply_slide_physics",
    "can_slide",
    "slide",
]
