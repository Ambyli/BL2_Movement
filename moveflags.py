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

from .config import trust_client_slides
from .debug import dbg, note_adopted
from .lifecycle import begin_server_slide, end_server_slide
from .movement import apply_slide_physics, slide
from .state import CLIENTS_SLIDE_STATES, OWN_SLIDE_STATE, is_client

if TYPE_CHECKING:
    from unrealsdk import unreal

    from common import WillowPlayerController

SLIDE_FLAG_BIT: int = 0x80
"""The one bit nothing else claims. See the layout in the module docstring."""

INJECT_ID = "SlidingFlagInject"
READ_ID = "SlidingFlagRead"

INJECT_FUNCS = (
    "WillowGame.WillowSavedMove:CompressedFlags",
    "Engine.SavedMove:CompressedFlags",
)
READ_FUNCS = ("Engine.PlayerController:MoveAutonomous",)
DRIVE_ID = "SlidingFlagDrive"
DRIVE_FUNCS = ("Engine.PlayerController:MoveAutonomous",)

TRUST_ID = "SlidingTrustClient"
TRUST_FUNCS = (
    "Engine.PlayerController:ServerMove",
    "Engine.PlayerController:PCServerMoveInner",
    "WillowGame.WillowPlayerController:ShortServerMove",
)


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
    """Add our bit to the flags this move is about to send. Client side.

    The original is called rather than reimplemented, because the packing is Willow's and we have no
    business duplicating it - we only want to add to whatever it decided. `prevent_hooking_direct_calls`
    stops that inner call re-entering this hook.
    """
    if not OWN_SLIDE_STATE.is_sliding:
        return None
    try:
        with prevent_hooking_direct_calls():
            original = int(func())
    except Exception as ex:  # noqa: BLE001 - never break move replication over this
        dbg(f"FLAG INJECT FAILED {type(ex).__name__}: {ex}")
        return None

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
    """Start or stop the host's copy of a slide from the flag on the move. Host side.

    Edge triggered: a slide begins on the first move carrying the bit and ends on the first move
    without it. That places both transitions on exactly the move the client made them on, which a
    queued message cannot do.
    """
    if is_client():
        return
    try:
        sliding = bool(int(args.CompressedFlags) & SLIDE_FLAG_BIT)
        pc = cast("WillowPlayerController", obj)
        player_id = int(pc.PlayerReplicationInfo.PlayerID)
    except Exception:  # noqa: BLE001 - a move we cannot read is a move we ignore
        return

    if not _Flags.reported_read:
        _Flags.reported_read = True
        dbg("FLAG read live")

    was = _Flags.last_seen.get(player_id, False)
    if sliding == was:
        return
    _Flags.last_seen[player_id] = sliding

    try:
        if sliding:
            begin_server_slide(pc, "flag")
        else:
            end_server_slide(pc, "flag")
    except Exception as ex:  # noqa: BLE001 - must never break the move path
        dbg(f"FLAG EDGE FAILED {type(ex).__name__}: {ex}")


def _drive_remote_slide(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Force a remote player's slide velocity where the server actually integrates their movement.

    This is the server-side twin of `enforce_slide`, and it exists for the identical reason. The host
    was driving remote pawns from `PlayerTick`, but a remote pawn does not move on the host's frame -
    it moves inside `MoveAutonomous`, which runs at packet rate and calls `AutonomousPhysics` ->
    `PhysWalking` -> `CalcVelocity`. `CalcVelocity` recomputes velocity from acceleration before
    integrating, so anything written on the host's tick was overwritten before it was ever used, and
    the server simulated the player as walking no matter what it had been told about the slide.

    A post hook puts our write after that physics, in the same relative position as the client's own
    post hook - which is what gives the two simulations a chance to track each other.
    """
    if is_client():
        return
    try:
        pc = cast("WillowPlayerController", obj)
        pawn = pc.Pawn
        if pawn is None:
            return
        delta_time = float(args.DeltaTime)
    except Exception:  # noqa: BLE001
        return
    if delta_time <= 0.0:
        return

    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
            continue
        if _pc != pc:
            continue
        state = CLIENTS_SLIDE_STATES[player]
        if not state.is_sliding:
            return
        try:
            # Decay here too, not on the host's tick. There is one MoveAutonomous per client frame,
            # so decaying on this clock advances the server's copy in step with the client's own
            # frames rather than with the host's - which is a different frame rate entirely.
            slide(pc, state, delta_time)
            apply_slide_physics(pawn, state, delta_time)
        except Exception as ex:  # noqa: BLE001 - must never break the move path
            dbg(f"FLAG DRIVE FAILED {type(ex).__name__}: {ex}")
        return


class _Trust:
    reported: ClassVar[bool] = False
    reported_miss: ClassVar[bool] = False


class _ArgName:
    """The argument each ServerMove variant carries the client position in, once discovered."""

    resolved: ClassVar[dict[str, str | None]] = {}


def _claimed_location(args: unreal.WrappedStruct, variant: str) -> Any:
    """The position the client says it reached, whatever this ServerMove variant calls it.

    Found by inspecting the argument struct rather than by guessing names. BL2 routes moves through
    several ServerMove variants and they do not agree on what the field is called - guessing wrong
    returns None, which is indistinguishable from "this player is not sliding", and cost a full test
    cycle exactly that way. The resolved name is cached per variant and logged once.
    """
    if variant in _ArgName.resolved:
        name = _ArgName.resolved[variant]
        if name is None:
            return None
        try:
            return getattr(args, name)
        except Exception:  # noqa: BLE001
            return None

    found: str | None = None
    names: list[str] = []
    try:
        for prop in args._type._properties():  # noqa: SLF001 - the only way to enumerate these
            field = str(prop.Name)
            names.append(field)
            lowered = field.lower()
            if "loc" not in lowered or "rot" in lowered:
                continue
            try:
                value = getattr(args, field)
                # A position, not a flag or an index: it has to have vector components.
                _ = (value.X, value.Y, value.Z)
            except Exception:  # noqa: BLE001 - not a vector, keep looking
                continue
            found = field
            break
    except Exception as ex:  # noqa: BLE001
        dbg(f"TRUST arg scan failed on {variant}: {type(ex).__name__}: {ex}")

    _ArgName.resolved[variant] = found
    dbg(f"TRUST {variant} position arg={found} (args: {names})")
    if found is None:
        return None
    try:
        return getattr(args, found)
    except Exception:  # noqa: BLE001
        return None


def _trust_client_position(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Adopt a sliding client's own position on the host, rather than simulating against it.

    Three attempts were made to have the server reproduce the slide from its own physics - forcing
    velocity on the host tick, then in a post hook where the movement actually integrates, with the
    slide state delivered on the move stream so the timing was right. All three drifted, because two
    independent simulations running on different clocks from different inputs do not converge, and
    the hooks that would make them converge - a SavedMove subclass, a custom physics mode - need
    script we cannot compile.

    Measured drift was 293 units growing to 549 across a single slide, along a fixed heading: the
    client sliding away with the server standing still. That gap is what arrives as a teleport when
    corrections resume.

    So this stops trying to agree and removes the disagreement instead. The client is authoritative
    over its own position for the length of a slide - about a second and a half - and the server
    follows. The position adopted is one the client's own collision already accepted, so it is a
    legal place to stand rather than an arbitrary point.

    This must be a *pre* hook. `ServerMove` simulates the move, compares the result against the
    position the client claimed, and sends a correction if they differ - all before it returns. A
    post hook adopted the position faithfully and still left the correction already on the wire,
    which measured as a 303 unit gap being closed one step after it had been acted on. Adopting
    first means the comparison the server makes is against a position it already agrees with.
    """
    if is_client() or not trust_client_slides.value:
        return
    try:
        pc = cast("WillowPlayerController", obj)
        pawn = pc.Pawn
        if pawn is None:
            return
    except Exception:  # noqa: BLE001
        return

    matched = False
    for player in CLIENTS_SLIDE_STATES.copy():
        if (_pc := player()) is None:
            CLIENTS_SLIDE_STATES.pop(player)
            continue
        if _pc != pc or not CLIENTS_SLIDE_STATES[player].is_sliding:
            continue
        matched = True

        try:
            variant = str(_func.func.Name)
        except Exception:  # noqa: BLE001
            variant = "?"
        if (claimed := _claimed_location(args, variant)) is None:
            return
        try:
            here = pawn.Location
            gap = (
                (claimed.X - here.X) ** 2 + (claimed.Y - here.Y) ** 2 + (claimed.Z - here.Z) ** 2
            ) ** 0.5
            pawn.SetLocation(claimed)
        except Exception as ex:  # noqa: BLE001 - must never break the move path
            dbg(f"TRUST FAILED {type(ex).__name__}: {ex}")
            return

        note_adopted(gap)
        if not _Trust.reported:
            _Trust.reported = True
            dbg(f"TRUST live: closed a {gap:.0f} unit gap on the first adopted move")
        return

    # Not matched. Both this and the host side correction blocking depend on finding the caller in
    # the slide dict, and both have been silently finding nothing - so say who was actually looked
    # for and who was on file, rather than leaving a miss indistinguishable from an idle frame.
    if not matched and not _Trust.reported_miss:
        _Trust.reported_miss = True
        try:
            who = str(pc.PlayerReplicationInfo.PlayerName)
        except Exception:  # noqa: BLE001
            who = "?"
        on_file = []
        for player in CLIENTS_SLIDE_STATES.copy():
            if (_pc := player()) is None:
                continue
            try:
                on_file.append(
                    f"{_pc.PlayerReplicationInfo.PlayerName}"
                    f"/sliding={CLIENTS_SLIDE_STATES[player].is_sliding}"
                    f"/same={_pc == pc}",
                )
            except Exception:  # noqa: BLE001
                on_file.append("?")
        dbg(f"TRUST MISS servermove_for={who} on_file={on_file}")


def enable_move_flags() -> None:
    for name in INJECT_FUNCS:
        try:
            added = add_hook(name, Type.PRE, INJECT_ID, _inject_slide_flag)
        except Exception as ex:  # noqa: BLE001 - a missing variant is expected, not fatal
            dbg(f"FLAG could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"FLAG inject hook on {name}: {'added' if added else 'refused'}")

    for name in READ_FUNCS:
        try:
            added = add_hook(name, Type.PRE, READ_ID, _read_slide_flag)
        except Exception as ex:  # noqa: BLE001
            dbg(f"FLAG could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"FLAG read hook on {name}: {'added' if added else 'refused'}")

    for name in DRIVE_FUNCS:
        try:
            added = add_hook(name, Type.POST, DRIVE_ID, _drive_remote_slide)
        except Exception as ex:  # noqa: BLE001
            dbg(f"FLAG could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"FLAG drive hook on {name}: {'added' if added else 'refused'}")

    for name in TRUST_FUNCS:
        try:
            added = add_hook(name, Type.PRE, TRUST_ID, _trust_client_position)
        except Exception as ex:  # noqa: BLE001 - a missing variant is expected, not fatal
            dbg(f"TRUST could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        dbg(f"TRUST hook on {name}: {'added' if added else 'refused'}")


def disable_move_flags() -> None:
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
    for name in DRIVE_FUNCS:
        try:
            remove_hook(name, Type.POST, DRIVE_ID)
        except Exception:  # noqa: BLE001, S110
            pass
    for name in TRUST_FUNCS:
        try:
            remove_hook(name, Type.PRE, TRUST_ID)
        except Exception:  # noqa: BLE001, S110
            pass
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
