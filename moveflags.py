"""Carrying slide state inside the engine's own move stream, rather than beside it.

A UE3 developer adds an ability like this by subclassing `SavedMove`, claiming a spare bit in
`CompressedFlags`, and unpacking it on the server in `MoveAutonomous`. We cannot compile script, so
we cannot subclass anything - but `WillowSavedMove.CompressedFlags` is an ordinary function, pre
hooks can override a return value, and the bound function is callable. That is enough to do the same
thing from outside: let Willow pack its own bits, then add ours.

Why this matters more than it sounds. The side-channel message route works, but a message is queued,
sent at one per player tick, and applied whenever it lands - so the host starts its copy of a slide
some tens of milliseconds after the client did, and the two run permanently offset. A move flag
travels *with* the move it belongs to: the server applies it on the same move, with the same
timestamp, and it lives in the SavedMove list, so it survives the replay that follows a correction.
That offset is the thing generating a steady position error, and this is the channel that removes it.

The bit layout was measured rather than assumed - BL2 does not use stock UE3's:

    bits 0-2  DoubleClickMove (a three bit field, not flags)
    bit  3    bRun            (8)
    bit  4    bDuck           (16)
    bit  5    bPressedJump    (32)
    bit  6    bSprint         (64)   <- Gearbox's own addition
    bit  7    unused          (128)  <- ours

`bDoubleJump` packs to nothing, since BL2 has no double jump. Stock UE3 would have put bRun at 1,
bDuck at 2 and bPressedJump at 4; everything is shifted up three bits to fit DoubleClickMove
underneath. Guessing would have been wrong, and a wrong guess here is silent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from unrealsdk.hooks import Block, Type, add_hook, prevent_hooking_direct_calls, remove_hook

from .debug import dbg
from .lifecycle import begin_server_slide, end_server_slide
from .state import OWN_SLIDE_STATE, is_client

if TYPE_CHECKING:
    from common import WillowPlayerController
    from unrealsdk import unreal

SLIDE_FLAG_BIT: int = 0x80
"""The one bit nothing else claims. See the layout in the module docstring."""

INJECT_ID = "SlidingFlagInject"
READ_ID = "SlidingFlagRead"

INJECT_FUNCS = (
    "WillowGame.WillowSavedMove:CompressedFlags",
    "Engine.SavedMove:CompressedFlags",
)
READ_FUNCS = ("Engine.PlayerController:MoveAutonomous",)


class _Flags:
    """Whether each player was sliding on the previous move we saw from them.

    Keyed by player id rather than by controller, because ids are stable across level transitions
    and the controller objects are not.
    """

    last_seen: ClassVar[dict[int, bool]] = {}
    injected: ClassVar[int] = 0
    reported_inject: ClassVar[bool] = False
    reported_read: ClassVar[bool] = False


def _inject_slide_flag(
    _obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> tuple[type[Block], int] | None:
    """Add our bit to the flags this move is about to send.

    The original is called rather than reimplemented, because the packing is Willow's and we have no
    business duplicating it - we only want to add to whatever it decided. `prevent_hooking_direct_calls`
    stops that inner call re-entering this hook.

    Runs on: whichever machine is packing an outgoing SavedMove - normally a client sending to the
    host. Fires on the host too when the host is a listen server building its own SavedMoves, but
    there is no reader on that side, so the injected bit goes nowhere.
    """
    # No slide, no bit - return None so the engine's own return value stands.
    if not OWN_SLIDE_STATE.is_sliding:
        return None
    try:
        with prevent_hooking_direct_calls():
            original = int(func())
    except Exception as ex:  # noqa: BLE001 - never break move replication over this
        dbg(f"FLAG INJECT FAILED {type(ex).__name__}: {ex}")
        return None

    # Block the native return and substitute our OR'd value; the counter and one-shot log let us
    # tell from the debug output whether the client half of the protocol is actually running.
    _Flags.injected += 1
    if not _Flags.reported_inject:
        _Flags.reported_inject = True
        dbg(f"FLAG inject live: 0b{original:08b} -> 0b{original | SLIDE_FLAG_BIT:08b}")
    return (Block, original | SLIDE_FLAG_BIT)


def _read_slide_flag(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Start or stop the host's copy of a slide from the flag on the move.

    Edge triggered: a slide begins on the first move carrying the bit and ends on the first move
    without it. That places both transitions on exactly the move the client made them on, which a
    queued message cannot do.

    Runs on: HOST only. Fires as a PRE hook on MoveAutonomous every time an incoming ServerMove
    from any client is applied.
    """
    if is_client():
        return
    try:
        # Read the bit, and identify the player by their replication id (survives level loads).
        sliding = bool(int(args.CompressedFlags) & SLIDE_FLAG_BIT)
        pc = cast("WillowPlayerController", obj)
        player_id = int(pc.PlayerReplicationInfo.PlayerID)
    except Exception:  # noqa: BLE001 - a move we cannot read is a move we ignore
        return

    if not _Flags.reported_read:
        _Flags.reported_read = True
        dbg("FLAG read live")

    # Only act on edges. If the same bit state as last frame, nothing to do - most frames are
    # steady-state and shouldn't churn the slide dict.
    was = _Flags.last_seen.get(player_id, False)
    if sliding == was:
        return
    _Flags.last_seen[player_id] = sliding

    # Delegate to the shared start/end paths, tagged "flag" so debug can distinguish this route
    # from the broadcast route. Both routes are idempotent, so whichever gets here first wins and
    # the other becomes a no-op.
    try:
        if sliding:
            begin_server_slide(pc, "flag")
        else:
            end_server_slide(pc, "flag")
    except Exception as ex:  # noqa: BLE001 - must never break the move path
        dbg(f"FLAG EDGE FAILED {type(ex).__name__}: {ex}")


def enable_move_flags() -> None:
    """Wire the two move-flag hooks: inject on the client, read on the host. Runs on: BOTH.

    Position sync used to require two more hooks (a trust hook that adopted the client's claimed
    position, a drive hook that forced velocity POST MoveAutonomous). Both are gone: the PhysWalking
    PRE+Block hook in `hooks._phys_sliding` runs identical slide physics on both machines, so the
    host's simulation and the client's are the same by construction and there is nothing to adopt.
    """
    # Inject hooks: PRE CompressedFlags on both the Willow-specific and generic SavedMove classes.
    # BL2 uses one or the other depending on subclassing; try both, expect one to raise.
    for name in INJECT_FUNCS:
        try:
            added = add_hook(name, Type.PRE, INJECT_ID, _inject_slide_flag)
        except Exception as ex:  # noqa: BLE001 - a missing variant is expected, not fatal
            dbg(f"FLAG could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"FLAG inject hook on {name}: {'added' if added else 'refused'}")

    # Read hook: PRE MoveAutonomous on the host, edge-triggers begin/end_server_slide.
    for name in READ_FUNCS:
        try:
            added = add_hook(name, Type.PRE, READ_ID, _read_slide_flag)
        except Exception as ex:  # noqa: BLE001
            dbg(f"FLAG could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"FLAG read hook on {name}: {'added' if added else 'refused'}")


def disable_move_flags() -> None:
    """Unwire the two move-flag hooks and clear per-player state. Runs on: BOTH."""
    for name in INJECT_FUNCS:
        try:
            remove_hook(name, Type.PRE, INJECT_ID)
        except Exception:  # noqa: BLE001, S110 - nothing to do if it was never added
            pass
    for name in READ_FUNCS:
        try:
            remove_hook(name, Type.PRE, READ_ID)
        except Exception:  # noqa: BLE001, S110
            pass
    # Clear the per-player "were they sliding last frame" cache so a subsequent enable starts
    # from a clean slate; a stale True here would suppress the first begin_server_slide.
    _Flags.last_seen.clear()


def injected_count() -> int:
    """How many moves have carried the slide bit. Proves the client half is working."""
    return _Flags.injected


__all__ = [
    "SLIDE_FLAG_BIT",
    "disable_move_flags",
    "enable_move_flags",
    "injected_count",
]
