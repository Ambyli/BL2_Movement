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
    return cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client


def world_time() -> float:
    """Current world time in seconds.

    Every machine has its own; comparisons only mean anything against values captured earlier on
    the same machine.
    """
    return float(cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().TimeSeconds)


def player_id(pc: WillowPlayerController) -> int | None:
    """This controller's stable network id, or None if it has no replication info.

    `getattr` rather than attribute access because callers hand this whatever controller they have,
    including AI ones that carry no PlayerReplicationInfo at all.
    """
    if (pri := getattr(pc, "PlayerReplicationInfo", None)) is None:
        return None
    return int(pri.PlayerID)


def state_for(pc: WillowPlayerController) -> PlayerSlideState | None:
    """The live slide governing this controller, or None if it is not sliding."""
    if (player := player_id(pc)) is None:
        return None
    state = SLIDE_STATES.get(player)
    return state if state is not None and state.is_sliding else None


def heading_from(pawn: WillowPlayerPawn) -> tuple[float, float]:
    """The unit heading to lock a slide to, read off the pawn's current horizontal velocity.

    Z is dropped: slopes affect decay but never rotate the heading. Returns (0, 0) when the pawn was
    effectively stationary - opened from a jump landing, say - which leaves the slide with no
    heading to force and makes `apply_slide_physics` a no-op until one arrives.
    """
    vel = Vector(pawn.Velocity)
    vel.z = 0
    if vel.magnitude < 1.0:
        return 0.0, 0.0
    vel.normalize()
    return vel.x, vel.y


def begin_slide_state(slide_data: PlayerSlideState, dir_x: float, dir_y: float) -> None:
    """Open a slide on a known heading, resetting the curve it runs down.

    Runs on: BOTH. The machine that owns the slide passes a heading sampled from its own pawn; the
    host passes the one that arrived with the enter message, so the two agree by construction
    rather than by each sampling its own copy of the pawn at a different moment.

    speed_pct and elapsed are reset because this state object outlives any one slide - left alone,
    the previous slide's spent speed and elapsed time would cut the new one short.
    """
    slide_data.speed_pct = SLIDE_SPEED_DEFAULT
    slide_data.elapsed = 0.0
    slide_data.dir_x = dir_x
    slide_data.dir_y = dir_y
    slide_data.entry_x = dir_x
    slide_data.entry_y = dir_y
