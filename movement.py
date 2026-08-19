"""The slide itself: how it decays, how it steers, and how it is forced onto the pawn."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from uemath import Vector

from .config import (
    CROUCHED_PCT_DEFAULT,
    SLIDE_BACK_CUTOFF,
    SLIDE_SPEED_DEFAULT,
    SLIDE_STEER_DEADZONE,
)
from .debug import every_n, log
from .state import PlayerSlideState

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


POST_LOG_EVERY: int = 30
"""One line per this many forced frames, so a slide costs a handful of lines rather than hundreds."""


def _who(pawn: WillowPlayerPawn) -> str:
    """Name the pawn a forced frame belongs to, as `Name#PlayerID`.

    The host drives every player's slide through here, so an untagged line cannot be attributed and
    two decay curves interleave into one that appears to jump backwards. The id is on the end
    because display names are not unique - two machines signed in to the same account report the
    same name, which is exactly the session this most needs to be readable in.
    """
    log.debug(f"_who enter pawn={pawn}")
    try:
        pri = pawn.Controller.PlayerReplicationInfo
        result = f"{pri.PlayerName}#{pri.PlayerID}"
    except Exception as ex:  # noqa: BLE001 - a label is not worth raising over
        log.debug(f"_who exit result=? reason={type(ex).__name__}")
        return "?"
    log.debug(f"_who exit result={result}")
    return result


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
    verbose = every_n("can_slide", POST_LOG_EVERY)
    if verbose:
        log.debug(
            f"can_slide enter is_sliding={slide_data.is_sliding} bDuck={bool(pc.bDuck)}"
            f" on_ground={pawn.IsOnGroundOrShortFall()}",
        )
    result = slide_data.is_sliding and bool(pc.bDuck) and pawn.IsOnGroundOrShortFall()
    if verbose:
        log.debug(f"can_slide exit result={result}")
    return result


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
    verbose = every_n("slide", POST_LOG_EVERY)
    if verbose:
        log.debug(
            f"slide enter speed_pct={slide_data.speed_pct:.3f} elapsed={slide_data.elapsed:.2f}"
            f" delta={delta_time:.4f} z={pawn.Location.Z:.2f} old_z={slide_data.old_z:.2f}"
            f" decay={slide_data.decay_rate:.3f} max_duration={slide_data.max_duration:.2f}",
        )
    # Height difference against last frame, in unreal units.
    z_diff: float = pawn.Location.Z - slide_data.old_z

    # Time decay applies on every frame; a slope only offsets it, and has to be genuinely steep to
    # offset it fully. Read the decay rate off the state (captured at slide open from the owning
    # player's settings) rather than live from config, so the host runs the client's curve.
    speed = slide_data.speed_pct - delta_time * slide_data.decay_rate
    if z_diff < 0:
        speed -= z_diff * 0.0005  # downhill, wins some speed back
        slope = "downhill"
    else:
        speed -= z_diff * 0.004  # uphill, sheds extra
        slope = "uphill"
    if verbose:
        log.debug(
            f"slide calc z_diff={z_diff:.2f} slope={slope} raw_speed={speed:.3f}"
            f" decay_rate={slide_data.decay_rate:.3f}",
        )

    # A slope may sustain a slide, but never push it past the speed it opened at.
    speed = min(speed, SLIDE_SPEED_DEFAULT)

    # Persist the frame's result. `old_z` is the next frame's slope baseline, `speed_pct` feeds
    # apply_slide_physics, and `elapsed` is checked against the hard duration cap below.
    slide_data.old_z = pawn.Location.Z
    slide_data.speed_pct = speed
    slide_data.elapsed += delta_time

    # Exit verdict: the decay bled below the walking-crouch floor, or the duration cap hit. Both
    # cutoffs come from state so a remote player's slide ends when their settings say it should,
    # not when the host's settings would.
    spent = speed < CROUCHED_PCT_DEFAULT or slide_data.elapsed >= slide_data.max_duration
    if verbose:
        log.debug(
            f"slide exit speed_pct={speed:.3f} elapsed={slide_data.elapsed:.2f}"
            f" spent={spent} cutoff={CROUCHED_PCT_DEFAULT:.3f}"
            f" max_duration={slide_data.max_duration:.2f}",
        )
    return spent


def apply_slide_physics(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Force the slide's heading and speed onto the pawn.

    The pawn's speed cap is held clear of the forced speed, or the cap clamps the slide back down
    the moment anything lowers GroundSpeed - aiming down sights being the obvious one.

    Steering input is read from `slide_data.input_x/y` rather than `pawn.Acceleration`. The driver
    on the machine that owns the slide samples the pawn's Acceleration into the state each frame,
    and streams that value to the host so its copy of a remote slide sees the same input. Reading
    the pawn directly here would let Unreal's Acceleration replication (which lags a move-window
    and can read zero across it) pull the two simulations apart.

    Runs on: BOTH, from the slide driver in `lifecycle`.
    """
    verbose = every_n("apply_slide_physics", POST_LOG_EVERY)
    if verbose:
        log.debug(
            f"apply_slide_physics enter who={_who(pawn)}"
            f" dir=({slide_data.dir_x:.3f},{slide_data.dir_y:.3f})"
            f" input=({slide_data.input_x:.2f},{slide_data.input_y:.2f})"
            f" speed_pct={slide_data.speed_pct:.3f} delta={delta_time:.4f}",
        )
    # Current slide heading, projected to the ground plane. Zero means the entry heading was never
    # locked (opened from a standstill) and there is nothing to force onto the pawn.
    direction = Vector((slide_data.dir_x, slide_data.dir_y, 0.0))
    if direction.magnitude == 0:
        if verbose:
            log.debug("apply_slide_physics exit reason=no_heading")
        return

    # Steering input for this frame. Zero-magnitude input (no movement keys held) skips the steering
    # branch entirely and the slide runs in a straight line at its current heading.
    accel = Vector((slide_data.input_x, slide_data.input_y, 0.0))
    if accel.magnitude > 0:
        accel.normalize()
        backwards = accel.dot(direction)
        if verbose:
            log.debug(f"apply_slide_physics calc backwards={backwards:.3f} cutoff={SLIDE_BACK_CUTOFF:.3f}")
        if backwards > SLIDE_BACK_CUTOFF:
            # Drop the part of the input running back down the slide, then steer on whatever
            # sideways component survives - weighted by how sideways it actually is. Normalising it
            # unweighted turns a hair of residue from a near-backwards input into a full strength
            # turn, which is how holding back spins a slide right around.
            if backwards < 0:
                accel = accel - direction * backwards
            strength = accel.magnitude
            if strength > SLIDE_STEER_DEADZONE:
                # Steer rate and turn cone come from state (snapshotted at slide open from the
                # owning player's settings), not live config, so the host steers the client's
                # slide at the client's rate.
                alpha = min(slide_data.steer_rate * delta_time * strength, 1.0)
                direction = direction.lerp(accel.normalize(), alpha).normalize()
                if verbose:
                    log.debug(
                        f"apply_slide_physics steer strength={strength:.3f} alpha={alpha:.3f}"
                        f" steer_rate={slide_data.steer_rate:.2f}"
                        f" new_dir=({direction.x:.3f},{direction.y:.3f})",
                    )

    # Backstop: never let steering accumulate far enough to reverse the slide, however the input is
    # fed in. Anything past the limit is pinned to the edge of the allowed cone.
    entry = Vector((slide_data.entry_x, slide_data.entry_y, 0.0))
    if entry.magnitude > 0:
        cos_limit = math.cos(math.radians(slide_data.max_turn_degrees))
        along = direction.dot(entry)
        if along < cos_limit:
            perp = direction - entry * along
            if perp.magnitude > 0:
                perp.normalize()
                sin_limit = math.sqrt(max(1.0 - cos_limit * cos_limit, 0.0))
                direction = (entry * cos_limit + perp * sin_limit).normalize()
                if verbose:
                    log.debug(
                        f"apply_slide_physics clamp along={along:.3f} cos_limit={cos_limit:.3f}"
                        f" clamped_dir=({direction.x:.3f},{direction.y:.3f})",
                    )

    # Write the (possibly steered, possibly clamped) heading back into the state so the next
    # frame's steering starts from where this one left off.
    slide_data.dir_x = direction.x
    slide_data.dir_y = direction.y

    # Derive absolute speed from the decay curve's speed_pct. SLIDE_SPEED_DEFAULT is where the
    # curve opens; dividing by it scales the raw tuning to the start_speed captured on the state
    # when the slide opened, which on the host is the value the client sent rather than the host's
    # own slider.
    speed = slide_data.start_speed * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)

    # CrouchedPct is held clear of the actual slide speed so the engine's cap
    # (CrouchedPct * GroundSpeed) can't clamp us; Velocity carries the actual forced motion.
    pawn.CrouchedPct = max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    if verbose:
        log.debug(
            f"apply_slide_physics exit who={_who(pawn)} pct={slide_data.speed_pct:.3f}"
            f" set={speed:.0f} dir=({direction.x:.3f},{direction.y:.3f})"
            f" crouched_pct={pawn.CrouchedPct:.3f}",
        )


__all__ = [
    "apply_slide_physics",
    "can_slide",
    "slide",
]
