"""Slide state and the pure helpers that operate on it.

Deliberately free of game hooks and of any presentation concern, so both the local and the host
paths can share it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from mods_base import ENGINE
from uemath import Vector
from unrealsdk import find_enum

from .constants import SLIDE_SPEED_DEFAULT
from .debug import log

if TYPE_CHECKING:
    from common import WillowGameEngine, WillowPlayerController, WillowPlayerPawn, WorldInfo


class State:
    """Cross-frame scratch for the slide jump, which spans two frames by nature."""

    do_slide_jump: ClassVar[bool] = False
    horizontal_velocity: ClassVar[Vector] = Vector()


@dataclass
class PlayerSlideSettings:
    """The dials that shape a slide: start speed, decay curve, duration cap, steering rate, turn
    cone, and the two slope gains (downhill boost, uphill drag).

    Announced from every client to the host on slider change and again at slide open, so the host
    can drive a remote player's slide with the numbers that player tuned to rather than its own.
    Read from `PlayerSlideState` (where these values are snapshotted at slide open) rather than
    live from config, so a mid-slide slider tweak takes effect on the next slide instead of
    dragging the current one's math apart between the two machines.
    """

    start_speed: float
    decay_rate: float
    max_duration: float
    steer_rate: float
    max_turn_degrees: float
    downhill_boost: float
    uphill_drag: float


@dataclass
class PlayerSlideState:
    old_z: float
    is_sliding: bool
    # Current heading, which steering rotates.
    dir_x: float = 0.0
    dir_y: float = 0.0
    # Heading the slide opened on. Never rotates; the turn limit is measured against it.
    entry_x: float = 0.0
    entry_y: float = 0.0
    speed_pct: float = SLIDE_SPEED_DEFAULT
    # Seconds this slide has been running, for the hard duration cap.
    elapsed: float = 0.0
    # World-space steering input for this frame, on the machine that owns the slide: the injection
    # hook writes the player's live input here before overwriting the pawn's axes, and steer_heading
    # reads it. Stays zero on the host's copy of a remote slide - the host does not steer it; that
    # heading arrives already-steered in the replicated Acceleration.
    input_x: float = 0.0
    input_y: float = 0.0
    # Slider-driven physics dials, snapshotted at slide open from the owning machine's
    # `PlayerSlideSettings`. Read from state during slide() and apply_slide_physics rather than
    # live from config so a remote player's slide runs with their values, not the host's.
    start_speed: float = 0.0
    decay_rate: float = 0.0
    max_duration: float = 0.0
    steer_rate: float = 0.0
    max_turn_degrees: float = 0.0
    downhill_boost: float = 0.0
    uphill_drag: float = 0.0
    # The CrouchedPct the pawn should hold this frame - GroundSpeed * cap is the walking-physics speed
    # limit, so `cap` IS the slide's speed governor now that velocity is no longer forced directly.
    # The driver recomputes it each tick from the decay curve; the injection hook (own pawn) and the
    # MoveAutonomous hook (remote pawn on the host) read it and stamp it onto the pawn so the engine's
    # own movement drives the slide at slide speed on both machines.
    cap: float = 0.0
    # Latch for the optimistic slide gate (`movement.slide_gate`, built on `gating.optimistic`): the
    # host opens its shadow from the enter RPC, which outruns the replicated crouch flag, so the gate
    # must not end the slide on a not-yet-arrived flag. Set true once the crouch flag (`bDuck`, the
    # confirm signal) is seen - immediate on the owning machine, where the local press is already
    # applied - after which the gate defers to the plain crouch/ground check. Reset in
    # begin_slide_state. Satisfies gating.Confirmable together with `elapsed` above.
    armed: bool = False


SLIDE_STATES: dict[int, PlayerSlideState] = {}
"""Every slide running on this machine, keyed by PlayerID.

Keyed by id rather than by a controller reference because a level change replaces every controller
object while the id survives. Membership is liveness: an entry exists only while its slide runs.

On a client this only ever holds our own slide - remote pawns are simulated proxies whose position
arrives through BL2's normal replication, so there is nothing for us to drive. On the host it holds
every sliding player, our own included.
"""

OWN_SLIDE_STATE: PlayerSlideState = PlayerSlideState(old_z=0, is_sliding=False)
"""The local player's slide, registered into SLIDE_STATES under our own id while it runs.

One stable object for the life of the session rather than a fresh one per slide: `begin_slide_state`
resets every field in place, and object identity is how the driver tells our own slide from a
remote one without a second lookup.
"""

PLAYER_SETTINGS: dict[int, PlayerSlideSettings] = {}
"""Host-side cache of announced slider values, keyed by PlayerID.

Populated by `server_announce_settings` and read by `server_enter_slide` when it opens the host's
copy of a remote player's slide. Absent means that player has not announced yet (mod-load race, or
they are the local player themselves) - the host falls back to `default_settings()` in that case
so the slide still opens with reasonable numbers.

Never cleaned up when a player leaves: leaves a stale entry per departed session, negligible next
to a running slide's memory footprint, and avoids adding a leave hook that would need to run on
the exact tick a player's controller becomes invalid to key by.
"""


def default_settings() -> PlayerSlideSettings:
    """A fresh `PlayerSlideSettings` from this machine's current config sliders.

    Used as both what the client announces to the host (its own current values) and as the host's
    fallback when a remote player has not yet announced. Read `option.value` at call time rather
    than caching, so a slider change is picked up on the next call without invalidation.

    Lazy import for the config module: `state` is imported by `config`'s `on_change` path in the
    other direction, so binding at module scope would cycle.
    """
    log.debug("default_settings enter")
    from .config import (  # noqa: PLC0415 - deliberately lazy, see docstring
        decay_rate,
        downhill_boost,
        max_duration,
        max_turn_degrees,
        start_speed,
        steer_rate,
        uphill_drag,
    )

    result = PlayerSlideSettings(
        start_speed=start_speed.value,
        decay_rate=decay_rate.value,
        max_duration=max_duration.value,
        steer_rate=steer_rate.value,
        max_turn_degrees=max_turn_degrees.value,
        downhill_boost=downhill_boost.value,
        uphill_drag=uphill_drag.value,
    )
    log.debug(
        f"default_settings exit start_speed={result.start_speed:.0f} decay={result.decay_rate:.3f}"
        f" max_duration={result.max_duration:.2f} steer={result.steer_rate:.2f}"
        f" max_turn={result.max_turn_degrees:.1f}"
        f" downhill={result.downhill_boost:.4f} uphill={result.uphill_drag:.4f}",
    )
    return result

e_net_mode: WorldInfo.ENetMode = cast("WorldInfo.ENetMode", find_enum("ENetMode"))


def is_client() -> bool:
    """Whether this Python interpreter is running on a network client vs. the host.

    NM_Client means "we are a client connected to a remote host"; NM_Standalone / NM_ListenServer
    both return False here, so single-player and listen-server-host both count as the host.
    """
    log.debug("is_client enter")
    result = cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client
    log.debug(f"is_client exit result={result}")
    return result


def world_time() -> float:
    """Current world time in seconds.

    Every machine has its own; comparisons only mean anything against values captured earlier on
    the same machine.

    Deliberately unlogged: the log formatter reads world time on every record, so any log call
    here would recurse into the formatter.
    """
    return float(cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().TimeSeconds)


def player_id(pc: WillowPlayerController) -> int | None:
    """This controller's stable network id, or None if it has no replication info.

    `getattr` rather than attribute access because callers hand this whatever controller they have,
    including AI ones that carry no PlayerReplicationInfo at all.
    """
    log.debug(f"player_id enter pc={pc}")
    if (pri := getattr(pc, "PlayerReplicationInfo", None)) is None:
        log.debug("player_id exit result=None reason=no_pri")
        return None
    result = int(pri.PlayerID)
    log.debug(f"player_id exit result={result}")
    return result


def state_for(pc: WillowPlayerController) -> PlayerSlideState | None:
    """The live slide governing this controller, or None if it is not sliding."""
    log.debug(f"state_for enter pc={pc}")
    if (player := player_id(pc)) is None:
        log.debug("state_for exit result=None reason=no_player_id")
        return None
    state = SLIDE_STATES.get(player)
    result = state if state is not None and state.is_sliding else None
    log.debug(
        f"state_for exit player={player} has_entry={state is not None}"
        f" is_sliding={state.is_sliding if state is not None else False} result={result}",
    )
    return result


def heading_from(pawn: WillowPlayerPawn) -> tuple[float, float]:
    """The unit heading to lock a slide to, read off the pawn's current horizontal velocity.

    Z is dropped: slopes affect decay but never rotate the heading. Returns (0, 0) when the pawn was
    effectively stationary - opened from a jump landing, say - which leaves the slide with no
    heading to force and makes `apply_slide_physics` a no-op until one arrives.
    """
    log.debug(f"heading_from enter pawn.Velocity=({pawn.Velocity.X:.2f},{pawn.Velocity.Y:.2f},{pawn.Velocity.Z:.2f})")
    vel = Vector(pawn.Velocity)
    vel.z = 0
    if vel.magnitude < 1.0:
        log.debug(f"heading_from exit result=(0.00,0.00) reason=magnitude={vel.magnitude:.3f}<1.0")
        return 0.0, 0.0
    log.debug(f"heading_from calc pre_norm_magnitude={vel.magnitude:.2f}")
    vel.normalize()
    log.debug(f"heading_from exit result=({vel.x:.3f},{vel.y:.3f})")
    return vel.x, vel.y


def begin_slide_state(
    slide_data: PlayerSlideState,
    dir_x: float,
    dir_y: float,
    settings: PlayerSlideSettings,
) -> None:
    """Open a slide on a known heading and slider settings, resetting the curve it runs down.

    Runs on: BOTH. The machine that owns the slide passes a heading sampled from its own pawn and
    its own settings; the host passes what arrived with the enter path (heading in the enter
    message, dials from the announced `PLAYER_SETTINGS` cache), so the two agree by construction
    rather than by each reading its own live config a frame apart.

    speed_pct, elapsed and input are reset because this state object outlives any one slide - left
    alone, the previous slide's spent speed, elapsed time and last input would carry across. The
    five slider values are snapshotted onto the state so mid-slide slider changes never drag the
    running curve apart between the two machines - they take effect at the next slide, when
    `begin_slide_state` is called again with fresh settings.
    """
    log.info(
        f"begin_slide_state enter dir=({dir_x:.3f},{dir_y:.3f}) start_speed={settings.start_speed:.0f}"
        f" decay={settings.decay_rate:.3f} max_duration={settings.max_duration:.2f}"
        f" steer={settings.steer_rate:.2f} max_turn={settings.max_turn_degrees:.1f}"
        f" downhill={settings.downhill_boost:.4f} uphill={settings.uphill_drag:.4f}"
        f" prior_speed_pct={slide_data.speed_pct:.3f} prior_elapsed={slide_data.elapsed:.2f}",
    )
    slide_data.speed_pct = SLIDE_SPEED_DEFAULT
    slide_data.elapsed = 0.0
    slide_data.dir_x = dir_x
    slide_data.dir_y = dir_y
    slide_data.entry_x = dir_x
    slide_data.entry_y = dir_y
    slide_data.input_x = 0.0
    slide_data.input_y = 0.0
    slide_data.start_speed = settings.start_speed
    slide_data.decay_rate = settings.decay_rate
    slide_data.max_duration = settings.max_duration
    slide_data.steer_rate = settings.steer_rate
    slide_data.max_turn_degrees = settings.max_turn_degrees
    slide_data.downhill_boost = settings.downhill_boost
    slide_data.uphill_drag = settings.uphill_drag
    # Open the cap at the curve's top so the very first frame - before the driver recomputes it - is
    # already clear of a clamp rather than zero (which would freeze the pawn for one frame).
    slide_data.cap = SLIDE_SPEED_DEFAULT
    # Disarmed until this machine sees the crouch flag: on the host that is a few frames after the
    # enter RPC opens the shadow, on the owning machine it is the first frame.
    slide_data.armed = False
    log.info(
        f"begin_slide_state exit speed_pct={slide_data.speed_pct:.3f}"
        f" entry=({slide_data.entry_x:.3f},{slide_data.entry_y:.3f})"
        f" start_speed={slide_data.start_speed:.0f} decay={slide_data.decay_rate:.3f}"
        f" max_duration={slide_data.max_duration:.2f} steer={slide_data.steer_rate:.2f}"
        f" max_turn={slide_data.max_turn_degrees:.1f}"
        f" downhill={slide_data.downhill_boost:.4f} uphill={slide_data.uphill_drag:.4f}",
    )
