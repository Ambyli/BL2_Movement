"""Entering and leaving a slide, and the replication that keeps host and client agreeing.

The network messages live here rather than in a module of their own on purpose: `exit_slide` calls
`server_exit_slide`, and the targeted `client_exit_slide` calls `exit_slide` straight back. They are
two halves of one protocol, and splitting them would buy a tidier file listing at the cost of a
genuine import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mods_base import get_pc
from networking.decorators import host, targeted
from unrealsdk import unreal

from . import events
from .config import CROUCHED_PCT_DEFAULT, SLIDE_SPEED_DEFAULT, start_speed
from .debug import dbg
from .state import CLIENTS_SLIDE_STATES, OWN_SLIDE_STATE, PlayerSlideState, begin_slide_state

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


@host.json_message
def server_set_slide_jump_velocity(vel_x: float, vel_y: float) -> None:
    pc = cast("WillowPlayerController", server_set_slide_jump_velocity.sender.Owner)
    pc.Pawn.Velocity.X = vel_x
    pc.Pawn.Velocity.Y = vel_y


@host.message
def server_exit_slide() -> None:
    pc = cast("WillowPlayerController", server_exit_slide.sender.Owner)
    pc.Pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            CLIENTS_SLIDE_STATES[player].is_sliding = False


@host.message
def server_enter_slide() -> None:
    pc = cast("WillowPlayerController", server_enter_slide.sender.Owner)

    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            data = CLIENTS_SLIDE_STATES[player]
            data.is_sliding = True
            data.old_z = pc.Pawn.Location.Z
            begin_slide_state(cast("WillowPlayerPawn", pc.Pawn), data)
            break
    else:
        data = PlayerSlideState(old_z=pc.Pawn.Location.Z, is_sliding=True)
        begin_slide_state(cast("WillowPlayerPawn", pc.Pawn), data)
        CLIENTS_SLIDE_STATES[unreal.WeakPointer(pc)] = data

    pc.Pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    dbg(f"SERVER_ENTER ran, clients={len(CLIENTS_SLIDE_STATES)}")


def enter_slide(pc: WillowPlayerController) -> None:
    """The client wants to slide; tells the host, but starts its own presentation immediately."""
    if OWN_SLIDE_STATE.is_sliding:
        return
    server_enter_slide()
    OWN_SLIDE_STATE.is_sliding = True
    OWN_SLIDE_STATE.old_z = pc.Pawn.Location.Z
    begin_slide_state(cast("WillowPlayerPawn", pc.Pawn), OWN_SLIDE_STATE)
    pc.Pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    dbg(
        f"ENTER_OWN speed={start_speed.value:.0f} "
        f"dir=({OWN_SLIDE_STATE.dir_x:.2f},{OWN_SLIDE_STATE.dir_y:.2f}) "
        f"clients={len(CLIENTS_SLIDE_STATES)}",
    )
    events.fire(events.slide_started, pc)


def exit_slide(pc: WillowPlayerController) -> None:
    if not OWN_SLIDE_STATE.is_sliding:
        return
    OWN_SLIDE_STATE.is_sliding = False
    pc.Pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
    dbg("EXIT")
    server_exit_slide()
    events.fire(events.slide_ended, pc)


@targeted.message
def client_exit_slide() -> None:
    exit_slide(cast("WillowPlayerController", get_pc()))


# Passed explicitly to add_network_functions: it only scans the scope of the module that calls it,
# which is __init__, so nothing here would be picked up automatically.
network_functions = [
    server_set_slide_jump_velocity,
    server_exit_slide,
    server_enter_slide,
    client_exit_slide,
]
