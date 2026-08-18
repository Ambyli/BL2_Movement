"""The slide itself: how it decays, how it steers, and how it is handed to the engine.

Two drive models live here, chosen by the `input_driven` option.

The input driven one expresses a slide as movement input and lets the engine do the rest. Unreal
replicates *input* and derives velocity from it, so this is the only one of the two the server can
see without being told: a client's slide arrives in its ServerMove packet like any other move, and
both machines reach the same velocity through the same code.

The legacy one writes velocity straight onto the pawn. It is simpler and works fine single player,
but velocity is a derived value, so a slide written that way is invisible to replication - which is
what made co-op need the host to re-simulate every remote slide by hand, and a stream of position
corrections to paper over the difference. It is kept only so the two can be compared in game.
"""

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
    input_driven,
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
"""One line per this many driven frames. See the note in `_log_drive`."""


class _PostLog:
    frames: ClassVar[int] = 0


def _who(pawn: WillowPlayerPawn) -> str:
    """Name the pawn a driven frame belongs to.

    The drive functions run for our own slide and for every remote one, so an untagged line cannot
    be attributed - two players' decay curves interleave and read as one curve jumping backwards.
    """
    try:
        return str(pawn.Controller.PlayerReplicationInfo.PlayerName)
    except Exception:  # noqa: BLE001 - a label is not worth raising over
        return "?"


def _log_drive(
    tag: str,
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    speed: float,
    extra: str = "",
) -> None:
    """Throttled trace of a driven frame.

    Logging every frame burned 391 of the debug log's 400 lines on two slides and silently dropped
    the whole of the next session - the rare lines are the ones worth having, and per-frame detail
    crowds them out.
    """
    _PostLog.frames += 1
    if _PostLog.frames % POST_LOG_EVERY == 0:
        dbg(f"{tag} {_who(pawn)} pct={slide_data.speed_pct:.2f} set={speed:.0f}{extra}")


def _alignment(pawn: WillowPlayerPawn, direction: Vector) -> float:
    """How closely the pawn's current acceleration follows the heading we asked for. 1.0 is exact.

    Writing input and the engine *acting* on it are two different claims, and only the second one
    matters. This reads back what the previous frame actually produced, so the log distinguishes
    them instead of merely recording that we wrote something.
    """
    try:
        accel = Vector(pawn.Acceleration)
        accel.z = 0
        if accel.magnitude == 0:
            return 0.0
        return accel.normalize().dot(direction)
    except Exception:  # noqa: BLE001 - diagnostics only
        return float("nan")


def can_slide(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> bool:
    return OWN_SLIDE_STATE.is_sliding and bool(pc.bDuck) and pawn.IsOnGroundOrShortFall()


def slide(
    pc: WillowPlayerController,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Advance the decay curve and decide when the slide is spent. Called every frame.

    Owns nothing else: heading and speed are handed to the engine by the drive functions below.
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


def crouched_pct_for(pawn: WillowPlayerPawn, speed: float) -> float:
    """The CrouchedPct that holds the crouched speed cap at `speed`.

    Recomputed from GroundSpeed every frame rather than set once, which is what makes a slide immune
    to anything lowering GroundSpeed underneath it - aiming down sights being the one that started
    this. When GroundSpeed drops the multiplier rises to meet it, and the cap does not move.
    """
    return max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)


def steer(slide_data: PlayerSlideState, wish: Vector, delta_time: float) -> Vector:
    """Rotate the slide heading toward `wish`, within the back cutoff and the turn cone.

    `wish` is where the player is asking to go, in world space. Shared by both drive models so they
    steer identically; only how the result reaches the engine differs between them.
    """
    direction = Vector((slide_data.dir_x, slide_data.dir_y, 0.0))
    if direction.magnitude == 0:
        return direction

    if wish.magnitude > 0:
        wish.normalize()
        backwards = wish.dot(direction)
        if backwards > SLIDE_BACK_CUTOFF:
            # Drop the part of the input running back down the slide, then steer on whatever
            # sideways component survives - weighted by how sideways it actually is. Normalising it
            # unweighted turns a hair of residue from a near-backwards input into a full strength
            # turn, which is precisely how holding back used to spin the slide right around.
            if backwards < 0:
                wish = wish - direction * backwards
            strength = wish.magnitude
            if strength > SLIDE_STEER_DEADZONE:
                alpha = min(steer_rate.value * delta_time * strength, 1.0)
                direction = direction.lerp(wish.normalize(), alpha).normalize()

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
    return direction


def view_axes(pc: WillowPlayerController) -> tuple[Vector, Vector] | None:
    """The controller's horizontal forward and right vectors in world space, or None on failure.

    Asked of the engine rather than derived from the yaw with trigonometry. The sign convention on
    the right vector is exactly the sort of detail that is easy to get backwards and expensive to
    find out about in game, and GetAxes already knows the answer.
    """
    try:
        _ret, x_axis, y_axis, _z_axis = pc.GetAxes(pc.Rotation)
    except Exception as ex:  # noqa: BLE001 - the caller falls back to the legacy model
        dbg(f"AXES FAILED {type(ex).__name__}: {ex}")
        return None
    forward = Vector((x_axis.X, x_axis.Y, 0.0))
    right = Vector((y_axis.X, y_axis.Y, 0.0))
    if forward.magnitude == 0 or right.magnitude == 0:
        return None
    return forward.normalize(), right.normalize()


def apply_slide_input(
    pc: WillowPlayerController,
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> bool:
    """Express the slide as movement input, before the engine reads it. False if it could not.

    This is the whole point of the input driven model. PlayerMove builds its acceleration from
    PlayerInput's axes and hands that to ProcessMove *and* to ServerMove, so a slide written here
    reaches the server as ordinary input and is simulated there from the same numbers. Nothing needs
    forcing onto the pawn afterwards and nothing needs re-sending: the slide stops being a result
    the server cannot see, and goes back to being an input it already knows how to replicate.

    Must run before PlayerMove, never after - a post hook would be writing input the frame has
    already read and already sent.
    """
    if (axes := view_axes(pc)) is None:
        return False
    forward, right = axes

    player_input = pc.PlayerInput
    raw_forward = float(player_input.aForward)
    raw_strafe = float(player_input.aStrafe)

    # Steer from the raw input, read before it gets overwritten below.
    wish = forward * raw_forward + right * raw_strafe
    wish.z = 0
    direction = steer(slide_data, wish, delta_time)
    if direction.magnitude == 0:
        return False

    # Project the heading back onto the view axes. PlayerMove normalises whatever it builds, so only
    # the ratio between these two matters, never their magnitude.
    player_input.aForward = direction.dot(forward)
    player_input.aStrafe = direction.dot(right)

    speed = start_speed.value * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)
    pawn.CrouchedPct = crouched_pct_for(pawn, speed)

    # Read back before this frame overwrites it: `align` near 1.0 means the engine really is
    # accelerating along the heading we wrote last frame, which is the claim this model rests on.
    _log_drive("IN", pawn, slide_data, speed, f" align={_alignment(pawn, direction):+.2f}")
    return True


def hold_speed_cap(pawn: WillowPlayerPawn, slide_data: PlayerSlideState) -> None:
    """Host side, for a remote slider: raise their speed cap and nothing else.

    Their own input carries the heading, and carries it through replication, so there is nothing
    here to force. Under the input driven model this is the entire host duty.
    """
    speed = start_speed.value * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)
    pawn.CrouchedPct = crouched_pct_for(pawn, speed)


def apply_slide_physics(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Legacy drive model: force heading and speed onto the pawn after the engine has had its say.

    PlayerMove recomputes velocity from input every frame, so this has to run from a post hook to be
    the value the walking physics actually integrates. Acceleration is zeroed so the engine has
    nothing left to fight with. See the module docstring for why this is no longer the default.
    """
    accel = Vector(pawn.Acceleration)
    accel.z = 0
    direction = steer(slide_data, accel, delta_time)
    if direction.magnitude == 0:
        return

    speed = start_speed.value * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)

    pawn.CrouchedPct = crouched_pct_for(pawn, speed)
    pawn.Acceleration.X = 0.0
    pawn.Acceleration.Y = 0.0
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    _log_drive("POST", pawn, slide_data, speed)


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
    """Advance every slide the host is responsible for. Host only, every frame, unconditionally.

    CrouchedPct is not replicated, so the server has to decay a remote slider's cap itself or their
    slide never slows down there. Under the input driven model that is all this does - their heading
    arrives with their input. Under the legacy model it also has to force velocity, because nothing
    about that slide reaches the server on its own.

    Our own entry is only decayed here, never driven: the local pawn is handled from the hooks, in
    whichever way the current model needs.
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
            pawn = cast("WillowPlayerPawn", _pc.Pawn)
            try:
                if input_driven.value:
                    hold_speed_cap(pawn, state)
                else:
                    apply_slide_physics(pawn, state, delta_time)
            except Exception as ex:  # noqa: BLE001 - one bad pawn must not stall the others
                dbg(f"REMOTE SLIDE FAILED {type(ex).__name__}: {ex}")


__all__ = [
    "apply_slide_input",
    "apply_slide_physics",
    "can_slide",
    "crouched_pct_for",
    "drive_hosted_slides",
    "hold_speed_cap",
    "slide",
    "steer",
    "tick_hosted_slides",
    "view_axes",
]
