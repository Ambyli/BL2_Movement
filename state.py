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
from unrealsdk.unreal import WeakPointer

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


CLIENTS_SLIDE_STATES: dict[WeakPointer[WillowPlayerController], PlayerSlideState] = {}
OWN_SLIDE_STATE: PlayerSlideState = PlayerSlideState(old_z=0, is_sliding=False)

e_net_mode: WorldInfo.ENetMode = cast("WorldInfo.ENetMode", find_enum("ENetMode"))


def is_client() -> bool:
    """Whether this Python interpreter is running on a network client vs. the host.

    Runs on: BOTH. Every host-guarded function in the mod calls this to bail early on clients.
    NM_Client means "we are a client connected to a remote host"; NM_Standalone / NM_ListenServer
    both return False here, so single-player and listen-server-host both take the host branches.
    """
    return cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client


def world_time() -> float:
    """Current world time in seconds. Used to tell frames apart when deduplicating tick sources.

    Runs on: BOTH. Every machine has its own world time; comparisons only make sense against
    values captured earlier on the same machine.
    """
    return float(cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().TimeSeconds)


def begin_slide_state(pawn: WillowPlayerPawn, slide_data: PlayerSlideState) -> None:
    """Lock in the heading a slide was entered at.

    Runs on: BOTH. Called from `lifecycle.enter_slide` for the local slide and from
    `lifecycle.begin_server_slide` for the host's copy of every remote slide - so this function
    executes on every machine that needs to remember where a slide started.
    """
    # Reset the two curve state values. If this call is reactivating an existing entry (see
    # begin_server_slide), the previous slide's spent speed_pct and elapsed would otherwise leak
    # into the new one and either cut it short or run it past duration.
    slide_data.speed_pct = SLIDE_SPEED_DEFAULT
    slide_data.elapsed = 0.0

    # Read the pawn's current horizontal velocity - that's the direction we lock the slide to.
    # Z is dropped: slopes affect decay but never rotate the heading.
    vel = Vector(pawn.Velocity)
    vel.z = 0
    # Sub-1 uu/s means the player was effectively stationary when they opened the slide (opened
    # from a jump landing, for instance). No heading to lock; apply_slide_physics's magnitude
    # check will make the whole physics write a no-op until entry_x/y are populated another way.
    if vel.magnitude < 1.0:
        slide_data.dir_x = 0.0
        slide_data.dir_y = 0.0
        slide_data.entry_x = 0.0
        slide_data.entry_y = 0.0
        return
    # Normalise once, and store the unit vector as both the current heading (dir_x/y) and the
    # locked entry heading (entry_x/y). The turn-cone clamp in apply_slide_physics measures
    # against entry_x/y for the life of the slide.
    vel.normalize()
    slide_data.dir_x = vel.x
    slide_data.dir_y = vel.y
    slide_data.entry_x = vel.x
    slide_data.entry_y = vel.y
