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
    # Where the host's copy of this pawn stood when the slide opened, to measure whether it followed.
    server_start_x: float = 0.0
    server_start_y: float = 0.0


CLIENTS_SLIDE_STATES: dict[WeakPointer[WillowPlayerController], PlayerSlideState] = {}
OWN_SLIDE_STATE: PlayerSlideState = PlayerSlideState(old_z=0, is_sliding=False)

e_net_mode: WorldInfo.ENetMode = cast("WorldInfo.ENetMode", find_enum("ENetMode"))


def is_client() -> bool:
    return cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client


def world_time() -> float:
    """Current world time in seconds. Used to tell frames apart when deduplicating tick sources."""
    return float(cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().TimeSeconds)


def begin_slide_state(pawn: WillowPlayerPawn, slide_data: PlayerSlideState) -> None:
    """Lock in the heading a slide was entered at."""
    slide_data.speed_pct = SLIDE_SPEED_DEFAULT
    slide_data.elapsed = 0.0

    vel = Vector(pawn.Velocity)
    vel.z = 0
    if vel.magnitude < 1.0:
        slide_data.dir_x = 0.0
        slide_data.dir_y = 0.0
        slide_data.entry_x = 0.0
        slide_data.entry_y = 0.0
        return
    vel.normalize()
    slide_data.dir_x = vel.x
    slide_data.dir_y = vel.y
    slide_data.entry_x = vel.x
    slide_data.entry_y = vel.y
