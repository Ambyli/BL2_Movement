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
from .debug import adopted_stats, dbg, reset_suppressed, suppressed_count
from .state import (
    CLIENTS_SLIDE_STATES,
    OWN_SLIDE_STATE,
    PlayerSlideState,
    begin_slide_state,
    is_client,
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

    Runs on: HOST only. `broadcast` fires on every machine; the client guard turns it into a no-op
    everywhere except the host.
    """
    # Only the host owns the authoritative pawn state; every other machine ignores this body.
    if is_client():
        return
    # Identify which player sent the RPC via the decorator-injected `sender.Owner`.
    pc = cast("WillowPlayerController", server_set_slide_jump_velocity.sender.Owner)
    # Bail if that player has no pawn (mid-transition, respawn window, etc.).
    if (pawn := pc.Pawn) is None:
        return
    # If the RPC arrived on the same frame the client's DoJump ran, the server's copy of the pawn
    # is still grounded. Kick it into the falling state so the velocity write below survives
    # walking-physics clamps on the next tick.
    if pawn.IsOnGroundOrShortFall():
        pawn.DoJump(True)
    # Adopt the client's stashed horizontal velocity so the server integrates the same arc the
    # client is already predicting. This is what stops the server from correcting the jump away.
    pawn.Velocity.X = vel_x
    pawn.Velocity.Y = vel_y
    dbg(f"SERVER_JUMP from={pc.PlayerReplicationInfo.PlayerName} vel=({vel_x:.0f},{vel_y:.0f})")


def begin_server_slide(pc: WillowPlayerController, source: str) -> bool:
    """Start, or restart, the host's copy of a player's slide. Idempotent.

    Split out of the network message so the move-flag channel can drive it too. Both routes are
    live at once and both land here; whichever arrives first wins and the other is a no-op, which is
    what makes it safe to run them in parallel while we find out which is actually quicker.

    Runs on: HOST only. Callers are the broadcast RPC body (network route) and the move-flag read
    hook in moveflags.py (in-stream route).

    Returns True if this call was the one that started it.
    """
    # Defensive: refuse on client machines, and skip if the pawn has vanished.
    if is_client() or (pawn := pc.Pawn) is None:
        return False

    # Look for an existing state for this PC, sweeping dead weak-refs as we go.
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            # Weak pointer resolved to nothing - the controller was GC'd. Drop the entry.
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc:
            data = CLIENTS_SLIDE_STATES[player]
            # Live entry, already sliding -> this is a duplicate from the other route. No-op.
            if data.is_sliding:
                return False
            # Live entry, not currently sliding -> reactivate in place.
            data.is_sliding = True
            data.old_z = pawn.Location.Z
            begin_slide_state(cast("WillowPlayerPawn", pawn), data)
            break
    else:
        # No entry at all - first slide from this player this session. Allocate, initialize, store.
        data = PlayerSlideState(old_z=pawn.Location.Z, is_sliding=True)
        begin_slide_state(cast("WillowPlayerPawn", pawn), data)
        CLIENTS_SLIDE_STATES[unreal.WeakPointer(pc)] = data

    # Boost the replicated crouch multiplier so any observer sees slide-scale speed until the
    # PhysWalking hook takes over integration on this pawn's next frame.
    pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    loc = pawn.Location
    dbg(
        f"SLIDE_ON via={source} who={pc.PlayerReplicationInfo.PlayerName}"
        f" n={len(CLIENTS_SLIDE_STATES)} at=({loc.X:.0f},{loc.Y:.0f})",
    )
    # True: this call was the winner. False was returned above for a duplicate from the other route.
    return True


def end_server_slide(pc: WillowPlayerController, source: str) -> bool:
    """Stop the host's copy of a player's slide. Idempotent, for the same reason.

    Runs on: HOST only. Same two callers as begin_server_slide: the broadcast RPC body and the
    move-flag read hook (edge trigger when the slide bit clears).
    """
    # Bail on clients.
    if is_client():
        return False
    # Restore the pawn's normal crouch multiplier so walking physics goes back to real crouch speed
    # on the host and on remote observers via replication.
    if (pawn := pc.Pawn) is not None:
        pawn.CrouchedPct = CROUCHED_PCT_DEFAULT

    # Walk the dict, flip every matching sliding entry to not-sliding. `stopped` records whether we
    # actually changed anything - False here means the slide was already off (duplicate call from
    # the other route).
    stopped = False
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
        elif _pc == pc and CLIENTS_SLIDE_STATES[player].is_sliding:
            CLIENTS_SLIDE_STATES[player].is_sliding = False
            stopped = True
    if stopped:
        # Only log on the winning end call, not on duplicates.
        dbg(f"SLIDE_OFF via={source} who={pc.PlayerReplicationInfo.PlayerName}")
    # True: this call ended a live slide. False: it was already ended (or nothing to end).
    return stopped


@broadcast.message
def server_exit_slide() -> None:
    # Runs on: HOST only. Broadcast body; every other machine no-ops via the client guard.
    if is_client():
        return
    # Delegate to the shared end path, tagged so debug can distinguish RPC-driven ends from
    # move-flag-driven ends. If the move-flag route already ended this slide, end_server_slide
    # detects it via `stopped` and quietly returns False.
    end_server_slide(cast("WillowPlayerController", server_exit_slide.sender.Owner), "message")


@broadcast.message
def server_enter_slide() -> None:
    # Runs on: HOST only. Same shape as server_exit_slide - broadcast + client guard.
    if is_client():
        return
    # Delegate to the shared start path, tagged "message". A duplicate call (move-flag route got
    # there first) is detected inside begin_server_slide and returns False.
    begin_server_slide(cast("WillowPlayerController", server_enter_slide.sender.Owner), "message")


def enter_slide(pc: WillowPlayerController) -> None:
    """The client wants to slide; tells the host, but starts its own presentation immediately.

    Runs on: whichever machine's local player pressed duck-while-sprinting. Called from the
    `DuckPressed` hook in hooks.py. On a listen-server host, that's the host machine; on a client,
    that's the client machine. Both paths land here identically.
    """
    # Already sliding? Refuse - re-entering would re-lock the heading and reset decay.
    if OWN_SLIDE_STATE.is_sliding:
        return
    # Fire the broadcast RPC so every machine sees the start. On the host it populates the slide
    # dict; on other clients the body no-ops via the is_client guard. This runs BEFORE the local
    # prediction below so, in the common case, the host learns about our slide by the time our own
    # first-frame ServerMove arrives with the slide bit set.
    server_enter_slide()
    # Reset per-slide diagnostic counters (suppressed corrections, adopted moves, worst gap) so the
    # exit log describes just this slide.
    reset_suppressed()
    # Local prediction: mark ourselves as sliding, stamp starting Z for slope calc, initialize the
    # state dataclass (which locks entry heading from the pawn's current velocity), and boost
    # CrouchedPct so the very next frame walks at slide speed until the POST hook takes over the
    # velocity write.
    OWN_SLIDE_STATE.is_sliding = True
    OWN_SLIDE_STATE.old_z = pc.Pawn.Location.Z
    begin_slide_state(cast("WillowPlayerPawn", pc.Pawn), OWN_SLIDE_STATE)
    pc.Pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    dbg(
        f"ENTER_OWN client={is_client()} speed={start_speed.value:.0f} "
        f"dir=({OWN_SLIDE_STATE.dir_x:.2f},{OWN_SLIDE_STATE.dir_y:.2f}) "
        f"clients={len(CLIENTS_SLIDE_STATES)}",
    )
    # Notify subscribers - viewmodel dips the weapon, future audio/HUD hooks would fire here.
    events.fire(events.slide_started, pc)


def exit_slide(pc: WillowPlayerController) -> None:
    """End the local player's slide on whichever machine owns it.

    Runs on: same machine that called enter_slide (client or host, symmetric with the entry path).
    Reachable from three places: the PlayerMove exit checks in hooks.handle_move, the local
    speed/duration floor in movement.slide via client_exit_slide, and the client_exit_slide
    targeted RPC below when the host tells this client to stop.
    """
    # Not sliding? No-op. Idempotency matters: PlayerMove and client_exit_slide can fire on the
    # same frame, and moveflags' correction path can also reach exit_slide indirectly.
    if not OWN_SLIDE_STATE.is_sliding:
        return
    # Clear local flag and restore normal crouch multiplier so residual motion decays with ordinary
    # walking/crouching physics.
    OWN_SLIDE_STATE.is_sliding = False
    pc.Pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
    # Read final diagnostics for this slide - how many corrections the host suppressed on our
    # behalf, how many positions the host adopted, and the worst gap ever seen between our claim
    # and the server sim. All from the accumulators in debug.py.
    adopted, worst = adopted_stats()
    dbg(
        f"EXIT client={is_client()} suppressed={suppressed_count()}"
        f" adopted={adopted} worst_gap={worst:.0f}",
    )
    # Tell the host to stop tracking us. Once the host runs end_server_slide, the correction
    # suppression hook stops blocking and any real disagreement can flow through as a normal
    # ClientAdjustPosition. The move-flag route will independently reach the same conclusion on
    # the next move that lacks the slide bit.
    server_exit_slide()
    # Notify subscribers - viewmodel returns weapon to resting pose.
    events.fire(events.slide_ended, pc)


@targeted.message
def client_exit_slide() -> None:
    # Runs on: ONE specific client - the one the host addressed. `targeted.message` fires only on
    # the target machine, unlike `broadcast` which fires everywhere.
    # The host called this because its own slide simulation decided we should be done (speed
    # dropped below floor, duration cap hit, host-side end conditions). The host has already
    # dropped us from CLIENTS_SLIDE_STATES; this call syncs our local state to match.
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
