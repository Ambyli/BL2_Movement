"""The slide itself: how it decays, how it steers, and how its speed cap is derived.

B* note: this module no longer writes `pawn.Velocity`. The slide is driven by the engine's own
walking physics - `GroundSpeed * CrouchedPct` is the speed limit and the pawn's Acceleration is the
heading - because that is the only motion a listen-server host reproduces for a remote client's pawn
(it re-simulates each move from the replicated Acceleration, and ignores any Velocity we write on the
proxy). So the per-frame job here is pure computation: advance the decay curve (`slide`), rotate the
heading from steering input (`steer_heading`), and turn the curve into a CrouchedPct cap
(`compute_cap`). The pawn writes live in `hooks` (own pawn) and the host's MoveAutonomous hook.
"""

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
    """Name the pawn a forced frame belongs to, as `Name#PlayerID`."""
    try:
        pri = pawn.Controller.PlayerReplicationInfo
        return f"{pri.PlayerName}#{pri.PlayerID}"
    except Exception as ex:  # noqa: BLE001 - a label is not worth raising over
        log.debug(f"_who exit result=? reason={type(ex).__name__}")
        return "?"


def can_slide(
    pc: WillowPlayerController,
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
) -> bool:
    """Whether this slide should still be running.

    Runs on: BOTH. `bDuck` reaches the host for a remote player through the move stream's compressed
    flags, so the same gate reads true on both machines.
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

    Owns nothing else: heading is rotated in `steer_heading`, the speed cap is derived in
    `compute_cap`, and ending the slide is the caller's job.

    Runs on: BOTH, from the slide driver in `lifecycle`.
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

    # Time decay applies every frame; a slope only offsets it. Read the decay rate off the state
    # (captured at slide open from the owning player's settings) so the host runs the client's curve.
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

    slide_data.old_z = pawn.Location.Z
    slide_data.speed_pct = speed
    slide_data.elapsed += delta_time

    # Exit verdict: the decay bled below the walking-crouch floor, or the duration cap hit. Both
    # cutoffs come from state so a remote player's slide ends when their settings say it should.
    spent = speed < CROUCHED_PCT_DEFAULT or slide_data.elapsed >= slide_data.max_duration
    if verbose:
        log.debug(
            f"slide exit speed_pct={speed:.3f} elapsed={slide_data.elapsed:.2f}"
            f" spent={spent} cutoff={CROUCHED_PCT_DEFAULT:.3f}"
            f" max_duration={slide_data.max_duration:.2f}",
        )
    return spent


def steer_heading(slide_data: PlayerSlideState, delta_time: float) -> None:
    """Rotate the slide heading from this frame's steering input, clamped to the turn cone.

    Reads `slide_data.input_x/y` (a world-space input vector - on the owning machine the injection
    hook writes the player's live input there before overwriting the pawn's axes) and writes the
    result to `slide_data.dir_x/y`. No pawn writes: the heading reaches our own pawn later as
    input-axis injection, and reaches a remote pawn on the host through Unreal's replication of the
    owner's forced Acceleration.

    Runs on: the machine that OWNS the slide (the host does not steer a remote slide - that heading
    arrives already-steered in the replicated Acceleration).
    """
    verbose = every_n("steer_heading", POST_LOG_EVERY)
    direction = Vector((slide_data.dir_x, slide_data.dir_y, 0.0))
    if direction.magnitude == 0:
        return

    accel = Vector((slide_data.input_x, slide_data.input_y, 0.0))
    if accel.magnitude > 0:
        accel.normalize()
        backwards = accel.dot(direction)
        if backwards > SLIDE_BACK_CUTOFF:
            if backwards < 0:
                accel = accel - direction * backwards
            strength = accel.magnitude
            if strength > SLIDE_STEER_DEADZONE:
                alpha = min(slide_data.steer_rate * delta_time * strength, 1.0)
                direction = direction.lerp(accel.normalize(), alpha).normalize()
                if verbose:
                    log.debug(
                        f"steer_heading strength={strength:.3f} alpha={alpha:.3f}"
                        f" steer_rate={slide_data.steer_rate:.2f}"
                        f" new_dir=({direction.x:.3f},{direction.y:.3f})",
                    )

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
                        f"steer_heading clamp along={along:.3f} cos_limit={cos_limit:.3f}"
                        f" clamped_dir=({direction.x:.3f},{direction.y:.3f})",
                    )

    slide_data.dir_x = direction.x
    slide_data.dir_y = direction.y


def compute_cap(slide_data: PlayerSlideState, ground_speed: float) -> float:
    """Turn the decay curve into the CrouchedPct the pawn should hold this frame.

    `GroundSpeed * CrouchedPct` is walking physics' speed limit, so returning `slide_speed /
    GroundSpeed` makes that limit equal the slide speed exactly - the engine accelerates the pawn up
    to it and holds it there. slide_speed is the decay curve scaled to the owning player's
    `start_speed` (captured at slide open), so a remote player's slide runs at their top speed.

    Runs on: BOTH, from the slide driver. Stamped onto the pawn elsewhere - the injection hook for
    our own pawn, the MoveAutonomous hook for a remote pawn on the host.
    """
    slide_speed = slide_data.start_speed * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)
    return slide_speed / max(ground_speed, 1.0)


__all__ = [
    "can_slide",
    "compute_cap",
    "slide",
    "steer_heading",
]
