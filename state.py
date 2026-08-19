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

from .config import SLIDE_SPEED_DEFAULT
from .debug import log

if TYPE_CHECKING:
    from common import WillowGameEngine, WillowPlayerController, WillowPlayerPawn, WorldInfo


class State:
    """Cross-frame scratch for the slide jump, which spans two frames by nature."""

    do_slide_jump: ClassVar[bool] = False
    horizontal_velocity: ClassVar[Vector] = Vector()


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
    # Steering input this frame. On the machine that owns the slide the driver samples the pawn's
    # Acceleration into these; on the host's copy of a remote slide they arrive from the owning
    # client via `server_slide_input`. Read this rather than pawn.Acceleration in the physics so the
    # two machines always agree on what the player is pressing, without depending on Unreal's
    # Acceleration replication being fresh across a movement frame.
    input_x: float = 0.0
    input_y: float = 0.0
    # Absolute speed, in unreal units, that the slide opened at. Captured at slide start rather than
    # read live from `start_speed.value` in the physics, so the host uses the client's slider value
    # for the client's slide instead of its own.
    start_speed: float = 0.0


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
    start_speed: float,
) -> None:
    """Open a slide on a known heading and start speed, resetting the curve it runs down.

    Runs on: BOTH. The machine that owns the slide passes a heading sampled from its own pawn and
    its own slider value; the host passes what arrived with the enter message, so the two agree by
    construction rather than by each sampling its own copy of the pawn at a different moment.

    speed_pct, elapsed and input are reset because this state object outlives any one slide - left
    alone, the previous slide's spent speed, elapsed time and last input would carry across.
    """
    log.info(
        f"begin_slide_state enter dir=({dir_x:.3f},{dir_y:.3f}) start_speed={start_speed:.0f}"
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
    slide_data.start_speed = start_speed
    log.info(
        f"begin_slide_state exit speed_pct={slide_data.speed_pct:.3f} entry=({slide_data.entry_x:.3f},{slide_data.entry_y:.3f})"
        f" start_speed={slide_data.start_speed:.0f}",
    )
