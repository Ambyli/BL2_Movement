"""Entering and leaving a slide, and the replication that keeps host and client agreeing.

The network messages live here rather than in a module of their own on purpose: `exit_slide` calls
`server_exit_slide`, and the targeted `client_exit_slide` calls `exit_slide` straight back. They are
two halves of one protocol, and splitting them would buy a tidier file listing at the cost of a
genuine import cycle.

The server bound messages are `broadcast` rather than `host`, which reads oddly for messages only
the host acts on. It is deliberate. `host` addresses a message by finding whichever player has
`bIsPartyLeader` set, re-finds them by player id a frame later when the queue flushes, and silently
drops the message if either step misses. Logs from a real session showed nine client slides produce
zero arrivals on the host, while the host's own slides - which never leave the machine - arrived
every time. `broadcast` does no addressing at all, so none of those steps can fail. The cost is that
every machine runs these bodies, hence the client guards: this state belongs to the host alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mods_base import get_pc
from networking.decorators import broadcast, targeted
from unrealsdk import unreal

from . import events
from .config import CROUCHED_PCT_DEFAULT, SLIDE_SPEED_DEFAULT, start_speed
from .debug import dbg, suppressed_count
from .state import (
    CLIENTS_SLIDE_STATES,
    OWN_SLIDE_STATE,
    PlayerSlideState,
    begin_slide_state,
    is_client,
    set_slide_heading,
)

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


@broadcast.json_message
def server_set_slide_jump_velocity(vel_x: float, vel_y: float) -> None:
    """Carry a slide's momentum into the jump that ended it, on the machine that owns movement.

    The client force-calls `DoJump` on its own pawn, because crouching swallows the ordinary jump
    input and a slide holds crouch throughout. That call is purely local - it sets no replicated
    flag - so without this the server never learns the player left the ground, keeps simulating them
    walking, and corrects the jump away as fast as it is predicted. Hence the `DoJump` here too:
    the server has to make the same move, not merely be told the resulting velocity.
    """
    if is_client():
        return
    pc = cast("WillowPlayerController", server_set_slide_jump_velocity.sender.Owner)
    if (pawn := pc.Pawn) is None:
        return
    if pawn.IsOnGroundOrShortFall():
        pawn.DoJump(True)
    pawn.Velocity.X = vel_x
    pawn.Velocity.Y = vel_y
    dbg(f"SERVER_JUMP from={pc.PlayerReplicationInfo.PlayerName} vel=({vel_x:.0f},{vel_y:.0f})")


def begin_server_slide(
    pc: WillowPlayerController,
    source: str,
    dir_x: float | None = None,
    dir_y: float | None = None,
) -> bool:
    """Start, or restart, the host's copy of a player's slide. Idempotent.

    Split out of the network message so the move-flag channel can drive it too. Both routes are
    live at once and both land here; whichever arrives first wins and the other is a no-op, which is
    what makes it safe to run them in parallel while we find out which is actually quicker.

    Returns True if this call was the one that started it.
    """
    if is_client() or (pawn := pc.Pawn) is None:
        return False

    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            data = CLIENTS_SLIDE_STATES[player]
            if data.is_sliding:
                # Already running, from the other route. Not a no-op if this call brought the
                # heading with it - the client's word on direction beats the host's guess, whichever
                # route happened to start the slide.
                if dir_x is not None and dir_y is not None:
                    set_slide_heading(data, dir_x, dir_y)
                return False
            data.is_sliding = True
            data.old_z = pawn.Location.Z
            begin_slide_state(cast("WillowPlayerPawn", pawn), data)
            break
    else:
        data = PlayerSlideState(old_z=pawn.Location.Z, is_sliding=True)
        begin_slide_state(cast("WillowPlayerPawn", pawn), data)
        CLIENTS_SLIDE_STATES[unreal.WeakPointer(pc)] = data

    # begin_slide_state guessed a heading from the pawn's velocity. Replace it if the client told us
    # what it actually is - the guess is only there for the flag route, which carries no direction.
    if dir_x is not None and dir_y is not None:
        set_slide_heading(data, dir_x, dir_y)

    pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    dbg(
        f"SLIDE_ON via={source} who={pc.PlayerReplicationInfo.PlayerName}"
        f" dir=({data.dir_x:.2f},{data.dir_y:.2f}) n={len(CLIENTS_SLIDE_STATES)}",
    )
    return True


def end_server_slide(pc: WillowPlayerController, source: str) -> bool:
    """Stop the host's copy of a player's slide. Idempotent, for the same reason."""
    if is_client():
        return False
    if (pawn := pc.Pawn) is not None:
        pawn.CrouchedPct = CROUCHED_PCT_DEFAULT

    stopped = False
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc and CLIENTS_SLIDE_STATES[player].is_sliding:
            CLIENTS_SLIDE_STATES[player].is_sliding = False
            stopped = True
    if stopped:
        dbg(f"SLIDE_OFF via={source} who={pc.PlayerReplicationInfo.PlayerName}")
    return stopped


@broadcast.message
def server_exit_slide() -> None:
    if is_client():
        return
    end_server_slide(cast("WillowPlayerController", server_exit_slide.sender.Owner), "message")


@broadcast.json_message
def server_enter_slide(dir_x: float, dir_y: float) -> None:
    if is_client():
        return
    begin_server_slide(
        cast("WillowPlayerController", server_enter_slide.sender.Owner),
        "message",
        dir_x,
        dir_y,
    )


def enter_slide(pc: WillowPlayerController) -> None:
    """The client wants to slide; tells the host, but starts its own presentation immediately."""
    if OWN_SLIDE_STATE.is_sliding:
        return
    OWN_SLIDE_STATE.is_sliding = True
    OWN_SLIDE_STATE.old_z = pc.Pawn.Location.Z
    begin_slide_state(cast("WillowPlayerPawn", pc.Pawn), OWN_SLIDE_STATE)
    pc.Pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    # Announced only after the heading exists. Sending first would transmit the *previous* slide's
    # direction, which is the same class of bug as the host guessing one.
    server_enter_slide(OWN_SLIDE_STATE.dir_x, OWN_SLIDE_STATE.dir_y)
    dbg(
        f"ENTER_OWN client={is_client()} speed={start_speed.value:.0f} "
        f"dir=({OWN_SLIDE_STATE.dir_x:.2f},{OWN_SLIDE_STATE.dir_y:.2f}) "
        f"clients={len(CLIENTS_SLIDE_STATES)}",
    )
    events.fire(events.slide_started, pc)


def exit_slide(pc: WillowPlayerController) -> None:
    if not OWN_SLIDE_STATE.is_sliding:
        return
    OWN_SLIDE_STATE.is_sliding = False
    pc.Pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
    dbg(f"EXIT client={is_client()} suppressed={suppressed_count()}")
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

# --- wire identifiers ------------------------------------------------------------------------------
# Pinned, rather than left to the library default of "<module>:<qualname>". That default begins with
# the mod's *directory name*, so the same mod unzipped into `sliding` on one machine and
# `BL2_Movement-main` on another produces different identifiers, and every message is discarded on
# arrival as unknown - in both directions, with nothing but a console warning to show for it. That is
# not hypothetical; it is what two session logs showed after the rest of the transport was proven
# working. A GitHub zip extracts as `<repo>-main`, so the mismatch recurs on every fresh download.
#
# Pinning the prefix makes the protocol depend on the mod, not on where somebody happened to put it.
# Both players still need matching builds, but they no longer need matching folder names.
PROTOCOL_PREFIX = "sliding"

for _func in network_functions:
    _func.network_identifier = f"{PROTOCOL_PREFIX}:{_func.__wrapped__.__qualname__}"
