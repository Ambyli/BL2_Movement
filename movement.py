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


def can_slide(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> bool:
    return OWN_SLIDE_STATE.is_sliding and bool(pc.bDuck) and pawn.IsOnGroundOrShortFall()


def slide(
    pc: WillowPlayerController,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Advance the decay curve and decide when the slide is spent. Called every frame.

    Owns nothing else: heading and velocity are applied in `apply_slide_physics`, from a post hook,
    where PlayerMove can no longer overwrite them.
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

    slide_data.old_z = pc.Pawn.Location.Z
    slide_data.speed_pct = speed
    slide_data.elapsed += delta_time

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
    """
    direction = Vector((slide_data.dir_x, slide_data.dir_y, 0.0))
    if direction.magnitude == 0:
        return

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

    slide_data.dir_x = direction.x
    slide_data.dir_y = direction.y

    speed = start_speed.value * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)

    pawn.CrouchedPct = max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)
    pawn.Acceleration.X = 0.0
    pawn.Acceleration.Y = 0.0
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    dbg(f"POST pct={slide_data.speed_pct:.2f} set={speed:.0f}")


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
    """
    now = world_time()
    if now == _HostTick.last_world_time:
        return
    _HostTick.last_world_time = now

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
    """
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
            continue

        state = CLIENTS_SLIDE_STATES[player]
        if not state.is_sliding:
            continue

        slide(_pc, state, delta_time)

        if _pc == local_pc:
            # Mirror our own progress out, so the local exit checks still see it when we are the
            # host and our state lives in this dict rather than in OWN_SLIDE_STATE.
            OWN_SLIDE_STATE.speed_pct = state.speed_pct
            OWN_SLIDE_STATE.elapsed = state.elapsed
        elif _pc.Pawn is not None:
            try:
                apply_slide_physics(cast("WillowPlayerPawn", _pc.Pawn), state, delta_time)
            except Exception as ex:  # noqa: BLE001 - one bad pawn must not stall the others
                dbg(f"REMOTE SLIDE FAILED {type(ex).__name__}: {ex}")


__all__ = [
    "apply_slide_physics",
    "can_slide",
    "drive_hosted_slides",
    "slide",
    "tick_hosted_slides",
]
