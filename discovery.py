"""Phase 2 discovery: answers, in one game session, every runtime question the rest of the mod needs.

Deleted alongside `debug.py` in the cleanup phase. Nothing imports this except `__init__`.

Each in-game round trip costs a full quit, relaunch and save reload, which has been by far the
slowest part of building this mod. So rather than one restart per unknown, this gathers all four at
once:

  * which AkEvents exist, to find something that sounds like scraping   -> bl2_akevents.txt
  * which function plays footsteps, and whether it names the surface    -> bl2_discovery.log
  * what the third person AnimTree and skeleton actually contain        -> bl2_discovery.log
  * which PostRender fires, and whether it hands us a Canvas            -> bl2_discovery.log

Two rules shape everything below. Every probe is wrapped, because a discovery build that crashes
gathers nothing - a failed probe must cost one missing answer and not the slide. And every writer is
bounded, because some of what is hooked here runs on every rendered frame.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from mods_base import ENGINE
from unrealsdk import construct_object, find_all
from unrealsdk.hooks import Type, add_hook, remove_hook

from .debug import dbg
from .state import OWN_SLIDE_STATE, is_client, world_time

if TYPE_CHECKING:
    from unrealsdk import unreal

    from common import WillowPlayerController

DISCOVERY: bool = True
"""Master switch. Turn off to silence every probe here without unpicking the wiring."""

DISCOVERY_LOG = Path.home() / "bl2_discovery.log"
AKEVENT_DUMP = Path.home() / "bl2_akevents.txt"

MAX_NOTES: int = 3000
"""Safety net. Some of what is hooked here is per-frame; a stuck probe must not fill the disk."""

AKEVENT_RESCAN_SECONDS: float = 30.0
"""Assets stream in as you travel, so the AkEvent scan repeats - but not on every single slide."""

MAX_FOOTSTEP_HOOKS: int = 12
MAX_FIRES_PER_FUNC: int = 6
MAX_BONES: int = 200

FOOTSTEP_WORDS = ("footstep", "foot_step", "footfall")
POSTRENDER_WORDS = ("postrender",)
SURFACE_WORDS = ("material", "footstep", "surface", "physmat", "impact")

# The rubberband probe. `adjustposition` and `setlocation` are the server correcting a client and
# run on the client; `servermove` and `moveautonomous` are the server simulating that client and run
# on the host. Hooking both means one build gathers evidence from whichever end it is installed on.
NETCODE_WORDS = (
    "adjustposition",
    "setlocation",
    "servermove",
    "moveautonomous",
    "ackgoodmove",
    "autonomousphysics",
    # Added for the replay probe. Listing every match tells us the real name of the replay entry
    # point instead of guessing it, which is a whole restart per wrong guess.
    "updateposition",
    "savedmove",
)
NETCODE_HOOK_WORDS = ("adjustposition", "setlocation", "moveautonomous")
# Every variant the scan turned up. Hooking only the base one produced zero samples across a whole
# co-op session - BL2 routes through one of the others, and a hook on a name that is never called
# looks exactly like a function that never disagrees.
SERVERMOVE_FUNCS = (
    "Engine.PlayerController:ServerMove",
    "Engine.PlayerController:PCServerMoveInner",
    "Engine.PlayerController:DualServerMove",
    "Engine.PlayerController:OldServerMove",
    "WillowGame.WillowPlayerController:ShortServerMove",
)

# The move-internals probe. UE3 carries per-move ability state in a SavedMove subclass and a byte of
# CompressedFlags, and carries directional special moves in DoubleClickMove. None of that can be
# subclassed from Python, but if the bits and the field are readable - and DoubleClickMove settable -
# then the slide's start can ride the move stream instead of a side-channel message, which is what
# gives it correct timing under replay. This dump decides whether that route is open.
MOVE_WORDS = ("compressedflags", "savedmove", "doubleclick", "dodge", "acknowledgedmove")
MOVE_PROP_WORDS = (
    "savedmove",
    "doubleclick",
    "timestamp",
    "pendingmove",
    "currentmove",
    "ackedmove",
    "compressed",
)
INPUT_PROP_WORDS = ("double", "click", "duck", "sprint", "crouch")
# The flag-layout decoder. A SavedMove carries both the individual booleans and the byte they are
# packed into, so hooking the packer and logging the two side by side decodes the layout exactly -
# and, just as importantly, shows which bits are never set by anything, which is where a slide flag
# could live.
SAVEDMOVE_ID = "SlidingDiscoverySavedMove"
SAVEDMOVE_FUNCS = (
    "WillowGame.WillowSavedMove:CompressedFlags",
    "Engine.SavedMove:CompressedFlags",
)
SAVEDMOVE_BOOLS = ("bSprint", "bRun", "bDuck", "bPressedJump", "bDoubleJump")
MAX_SAVEDMOVE_FIRES: int = 40

# The teleport watcher. Four fixes have each addressed a real defect without removing the jump the
# player actually feels, so this stops reasoning about causes and records the event: sample the
# pawn's position every frame across the end of a slide and report any step too large to be running.
TELEPORT_ID = "SlidingDiscoveryTeleport"
TELEPORT_FUNCS = ("Engine.PlayerController:PlayerTick",)
TELEPORT_WATCH_FRAMES: int = 45
TELEPORT_MIN_JUMP: float = 50.0
MAX_TELEPORT_REPORTS: int = 12

# The path tracer. The teleport watcher above answers "did we jump"; this answers "where were we
# actually going". It samples the pawn's world position across the whole slide and reports the
# bearing the player really travelled, next to the three headings that were supposed to determine
# it: the heading the mod locked at entry, the direction the player was facing, and the acceleration
# the host is being told about. If a slide always runs off along one fixed world bearing no matter
# which way the player set off, these four columns say which of them the motion is actually
# following - and if it follows none of them, that it is coming from somewhere else entirely.
PATH_ID = "SlidingDiscoveryPath"
PATH_FUNCS = ("Engine.PlayerController:PlayerTick",)
PATH_SAMPLE_EVERY: int = 8
"""Frames between samples. At ~140fps this is ~17 per slide - a readable curve, not a wall."""
MAX_PATH_REPORTS: int = 90
"""Roughly five slides' worth of samples. Bounded like everything else here."""
PATH_MIN_TRAVEL: float = 5.0
"""Below this much movement from entry, the bearing is noise rather than a direction."""

MOVEFLAG_ID = "SlidingDiscoveryMoveFlags"
MOVEFLAG_FUNCS = ("Engine.PlayerController:MoveAutonomous",)
MAX_MOVEFLAG_FIRES: int = 40
SERVERMOVE_ID = "SlidingDiscoveryServerMove"
MAX_SERVERMOVE_FIRES: int = 40
MAX_NETCODE_HOOKS: int = 12
MAX_NETCODE_FIRES: int = 40

# The transport probe. The networking library moves every message over these two engine functions,
# so watching them on both machines says exactly where a message stops: never sent, sent but not
# received, or received under an identifier nobody is listening for.
TRANSPORT_ID = "SlidingDiscoveryTransport"
TRANSPORT_FUNCS = (
    "Engine.PlayerController:ServerSpeech",
    "WillowGame.WillowPlayerController:ClientMessage",
    "Engine.PlayerController:ClientMessage",
)
CUSTOM_MESSAGE_PREFIX = "!willow_nw:"
MAX_TRANSPORT_FIRES: int = 30

HUD_ID = "SlidingDiscoveryHud"
FOOTSTEP_ID = "SlidingDiscoveryFootstep"
NETCODE_ID = "SlidingDiscoveryNetcode"

# The replay probe. When the server corrects a client, UE3 does not merely snap it - PlayerTick sees
# bUpdatePosition and calls ClientUpdatePosition, which re-runs every unacknowledged SavedMove
# through MoveAutonomous. That path never goes through PlayerWalking.PlayerMove, which is where
# `hooks.enforce_slide` lives, so the replayed frames would be re-simulated as ordinary crouch
# walking with the locked slide heading discarded. This brackets the call and measures exactly that:
# whether `_Phys.applied` advances across it, and what the pawn's heading looks like on either side.
#
# ClientUpdatePosition is script in stock UE3, but so was PhysWalking by that reasoning and it has
# never once dispatched - so the name is verified by the function scan rather than assumed, and the
# probe logs a one-shot line the moment it actually fires. No line means it never ran.
REPLAY_ID = "SlidingDiscoveryReplay"
REPLAY_FUNCS = (
    "Engine.PlayerController:ClientUpdatePosition",
    "WillowGame.WillowPlayerController:ClientUpdatePosition",
)
MAX_REPLAY_REPORTS: int = 24
MAX_SAVEDMOVE_WALK: int = 64
"""Cap on how far down the SavedMoves linked list to walk. A corrupt NextMove must not hang a frame."""

HUD_FUNCS = (
    "WillowGame.WillowHUD:PostRender",
    "Engine.HUD:PostRender",
)


class _Progress:
    """What has already been gathered, so one-shot probes stay one-shot."""

    notes: ClassVar[int] = 0
    dumped_animtree: ClassVar[bool] = False
    scanned_functions: ClassVar[bool] = False
    last_akevent_scan: ClassVar[float] = -1e9
    seen_akevents: ClassVar[set[str]] = set()
    fires: ClassVar[dict[str, int]] = {}
    netcode_fires: ClassVar[dict[str, int]] = {}
    transport_fires: ClassVar[int] = 0
    servermove_fires: ClassVar[int] = 0
    dumped_identity: ClassVar[bool] = False
    dumped_move_internals: ClassVar[bool] = False
    moveflag_fires: ClassVar[int] = 0
    last_moveflag: ClassVar[str] = ""
    savedmove_fires: ClassVar[int] = 0
    last_savedmove: ClassVar[str] = ""
    seen_flag_bits: ClassVar[int] = 0
    probed_flag_layout: ClassVar[bool] = False
    watch_frames: ClassVar[int] = 0
    watch_last: ClassVar[tuple[float, float, float] | None] = None
    teleport_reports: ClassVar[int] = 0
    path_entry: ClassVar[tuple[float, float, float] | None] = None
    path_entry_facing: ClassVar[tuple[float, float] | None] = None
    path_entry_dir: ClassVar[tuple[float, float] | None] = None
    path_frames: ClassVar[int] = 0
    path_reports: ClassVar[int] = 0
    replay_fires: ClassVar[int] = 0
    replay_reported: ClassVar[bool] = False
    replay_pending: ClassVar[tuple[int, int, str] | None] = None
    last_fire_args: ClassVar[dict[str, str]] = {}
    dumped_surface_props: ClassVar[bool] = False
    hud_seen: ClassVar[set[str]] = set()
    pre_hooks: ClassVar[list[str]] = []
    post_hooks: ClassVar[list[str]] = []


def note(msg: str) -> None:
    """Append one line to the discovery log. Never raises at a call site."""
    if not DISCOVERY or _Progress.notes >= MAX_NOTES:
        return
    _Progress.notes += 1
    try:
        with DISCOVERY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{msg}\n")
    except OSError:
        pass


# --- small helpers ---------------------------------------------------------------------------------


def _short(value: object, limit: int = 140) -> str:
    """Render any unreal value in one readable line, whatever it turns out to be."""
    try:
        path_name = getattr(value, "_path_name", None)
        if callable(path_name):
            try:
                cls = str(value.Class.Name)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - best effort labelling only
                cls = "?"
            text = f"{cls}[{path_name()}]"
        else:
            text = repr(value)
    except Exception as ex:  # noqa: BLE001 - a probe must never raise
        text = f"<unreadable {type(ex).__name__}>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_args(args: unreal.WrappedStruct) -> str:
    """List an argument struct's properties and values.

    This is the question that decides Phase 4's whole shape: if the footstep function is handed the
    surface it is standing on, the slide audio can read it straight off. If it is not, we have to
    trace for the physical material ourselves.
    """
    parts: list[str] = []
    try:
        for prop in args._type._properties():  # noqa: SLF001 - the only way to enumerate these
            name = str(prop.Name)
            try:
                value = getattr(args, name)
            except Exception as ex:  # noqa: BLE001 - report and keep going
                value = f"<unreadable {type(ex).__name__}>"
            parts.append(f"{name}={_short(value)}")
    except Exception as ex:  # noqa: BLE001
        return f"<args unreadable {type(ex).__name__}: {ex}>"
    return ", ".join(parts) if parts else "<no args>"


def _hook_name(func: unreal.UObject) -> str | None:
    """Build the `Package.Class:Maybe.State.Function` string `add_hook` wants, from the object.

    The colon goes after the owning class, not before the last dotted segment - state scoped
    functions like `WillowPlayerController:PlayerWalking.PlayerMove` have two segments after it. So
    walk outwards until the class is reached, rather than splitting the path text.
    """
    parts: list[str] = []
    node: Any = func
    for _ in range(8):
        if node is None:
            return None
        try:
            if str(node.Class.Name) == "Class":
                return f"{node._path_name()}:{'.'.join(reversed(parts))}"  # noqa: SLF001
            parts.append(str(node.Name))
            node = node.Outer
        except Exception:  # noqa: BLE001 - unnameable, give up on this one
            return None
    return None


# --- probes ----------------------------------------------------------------------------------------


def _dump_akevents() -> None:
    """Append every AkEvent path not already seen. Grepped offline for a scraping sound."""
    now = world_time()
    if now - _Progress.last_akevent_scan < AKEVENT_RESCAN_SECONDS:
        return
    _Progress.last_akevent_scan = now

    try:
        found = list(find_all("AkEvent"))
    except Exception as ex:  # noqa: BLE001
        note(f"AKEVENT scan failed: {type(ex).__name__}: {ex}")
        return

    fresh: list[str] = []
    for event in found:
        try:
            path = event._path_name()  # noqa: SLF001
        except Exception:  # noqa: BLE001 - skip the odd one out
            continue
        if path not in _Progress.seen_akevents:
            _Progress.seen_akevents.add(path)
            fresh.append(path)

    if not fresh:
        note(f"AKEVENT no new events (total {len(_Progress.seen_akevents)})")
        return

    try:
        with AKEVENT_DUMP.open("a", encoding="utf-8") as handle:
            for path in sorted(fresh):
                handle.write(f"{path}\n")
    except OSError as ex:
        note(f"AKEVENT write failed: {ex}")
        return
    note(f"AKEVENT +{len(fresh)} new, {len(_Progress.seen_akevents)} total")


def _dump_animtree(pc: WillowPlayerController) -> None:
    """Dump the third person mesh, its AnimTree and its skeleton. Decides the Phase 5 approach.

    If the tree already exposes SkelControlSingleBone nodes we can drive them by name; if it exposes
    nothing, the pose has to build and splice its own controls in, which is a substantially harder
    piece of work. This is the dump that tells us which of those we are in for.
    """
    if _Progress.dumped_animtree:
        return
    _Progress.dumped_animtree = True

    try:
        mesh = pc.Pawn.Mesh
    except Exception as ex:  # noqa: BLE001
        note(f"ANIM no mesh: {type(ex).__name__}: {ex}")
        return

    note(f"ANIM mesh={_short(mesh)}")

    try:
        note(f"ANIM skeletalmesh={_short(mesh.SkeletalMesh)}")
    except Exception as ex:  # noqa: BLE001
        note(f"ANIM skeletalmesh unreadable: {type(ex).__name__}: {ex}")

    try:
        tree = mesh.AnimTree
    except Exception as ex:  # noqa: BLE001
        note(f"ANIM animtree unreadable: {type(ex).__name__}: {ex}")
        tree = None
    note(f"ANIM animtree={_short(tree)}")

    # The SkelControls, which is the part that matters most. They can hang off either the component
    # or the template it was built from, so look in both rather than assuming.
    for source_name, source in (("mesh", mesh), ("animtree", tree)):
        if source is None:
            continue
        try:
            entries = list(source.SkelControlLists)
        except Exception:  # noqa: BLE001 - normally only one of the two has these
            continue
        note(f"ANIM {source_name}.SkelControlLists: {len(entries)} entries")
        for entry in entries:
            try:
                bone = str(entry.BoneName)
            except Exception:  # noqa: BLE001
                bone = "?"
            chain: list[str] = []
            try:
                control = entry.Control
                while control is not None and len(chain) < 8:
                    chain.append(f"{control.Class.Name}({control.Name})")
                    control = control.NextControl
            except Exception as ex:  # noqa: BLE001
                chain.append(f"<{type(ex).__name__}>")
            note(f"ANIM   bone={bone} -> {' -> '.join(chain) or '<none>'}")

    # Bone names, so a hand authored pose can name the joints it moves.
    try:
        bones = list(mesh.SkeletalMesh.RefSkeleton)
    except Exception as ex:  # noqa: BLE001
        note(f"ANIM RefSkeleton unreadable: {type(ex).__name__}: {ex}")
        return
    note(f"ANIM bones: {len(bones)}")
    for index, bone in enumerate(bones[:MAX_BONES]):
        try:
            note(f"ANIM   [{index}] {bone.Name} parent={bone.ParentIndex}")
        except Exception as ex:  # noqa: BLE001
            note(f"ANIM   [{index}] unreadable: {type(ex).__name__}")
            break


def _dump_surface_props(pc: WillowPlayerController) -> None:
    """List every pawn property whose name suggests it already knows the surface underfoot.

    The second angle on the Phase 4 question. If the game is already tracking what it is standing on,
    the slide audio reads it straight off - which is far cheaper than writing a downward trace and
    only then finding out whether it was needed. Checking costs a few lines; assuming costs a
    restart.
    """
    if _Progress.dumped_surface_props:
        return
    _Progress.dumped_surface_props = True

    try:
        pawn = pc.Pawn
        properties = pawn.Class._properties()  # noqa: SLF001 - the only way to enumerate these
    except Exception as ex:  # noqa: BLE001
        note(f"SURFACE no pawn: {type(ex).__name__}: {ex}")
        return

    note(f"SURFACE pawn={_short(pawn)}")
    try:
        note(f"SURFACE Base={_short(pawn.Base)}")
    except Exception as ex:  # noqa: BLE001
        note(f"SURFACE Base unreadable: {type(ex).__name__}: {ex}")

    seen: set[str] = set()
    try:
        for prop in properties:
            name = str(prop.Name)
            if name in seen or not any(word in name.lower() for word in SURFACE_WORDS):
                continue
            seen.add(name)
            try:
                value = getattr(pawn, name)
            except Exception as ex:  # noqa: BLE001
                value = f"<unreadable {type(ex).__name__}>"
            note(f"SURFACE   {name}={_short(value)}")
    except Exception as ex:  # noqa: BLE001
        note(f"SURFACE property walk failed: {type(ex).__name__}: {ex}")
    note(f"SURFACE {len(seen)} matching properties")


def _footstep_probe(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> None:
    """Log the first few calls of anything footstep shaped, with its arguments.

    Repeats of an identical call are skipped rather than counted. Walking produces a steady stream of
    them, and the interesting event is the one where the arguments *change* - that is what would show
    the surface being named. Spending the budget on six identical lines would hide exactly that.
    """
    try:
        name = str(func.func.Name)
    except Exception:  # noqa: BLE001
        name = "?"
    if _Progress.fires.get(name, 0) >= MAX_FIRES_PER_FUNC:
        return
    described = _describe_args(args)
    if _Progress.last_fire_args.get(name) == described:
        return
    _Progress.last_fire_args[name] = described
    count = _Progress.fires.get(name, 0) + 1
    _Progress.fires[name] = count
    note(f"FOOTSTEP #{count} {name} on={_short(obj)} args=({described})")


def _props_matching(cls: Any, words: tuple[str, ...]) -> list[str]:
    """Names of a class's properties whose name contains any of the given words."""
    found: list[str] = []
    try:
        for prop in cls._properties():  # noqa: SLF001 - the only way to enumerate these
            name = str(prop.Name)
            if any(word in name.lower() for word in words):
                found.append(name)
    except Exception:  # noqa: BLE001
        return found
    return found


def _dump_move_internals(pc: WillowPlayerController) -> None:
    """Dump everything about how this build carries per-move state.

    Three questions, all of which decide whether the slide can ride the move stream:
      * does a SavedMove subclass exist, and what does it carry
      * which CompressedFlags bit is which, so slide start can be read from the flags
      * is DoubleClickMove - UE3's per-move directional special move channel - writable

    A UE3 developer would subclass SavedMove and claim a spare flag bit. We cannot compile script,
    so the question is what of that machinery is reachable read-only, and whether anything already
    replicated can carry the signal for us.
    """
    if _Progress.dumped_move_internals:
        return
    _Progress.dumped_move_internals = True

    try:
        pc_class = pc.Class
    except Exception as ex:  # noqa: BLE001
        note(f"MOVE no controller class: {type(ex).__name__}: {ex}")
        return
    note(f"MOVE pc_class={_short(pc_class)}")

    for name in _props_matching(pc_class, MOVE_PROP_WORDS):
        try:
            value = getattr(pc, name)
        except Exception as ex:  # noqa: BLE001
            value = f"<unreadable {type(ex).__name__}>"
        note(f"MOVE   pc.{name} = {_short(value)}")

    # The SavedMove class itself, and every field it carries.
    saved_class: Any = None
    try:
        saved_class = pc.SavedMoveClass
    except Exception as ex:  # noqa: BLE001
        note(f"MOVE SavedMoveClass unreadable: {type(ex).__name__}: {ex}")
    note(f"MOVE SavedMoveClass={_short(saved_class)}")
    if saved_class is not None:
        fields: list[str] = []
        try:
            fields = [str(prop.Name) for prop in saved_class._properties()]  # noqa: SLF001
        except Exception as ex:  # noqa: BLE001
            note(f"MOVE SavedMove fields unreadable: {type(ex).__name__}: {ex}")
        note(f"MOVE SavedMove has {len(fields)} fields: {fields}")

    # PlayerInput is where a double click direction would be authored, if anywhere.
    player_input: Any = None
    try:
        player_input = pc.PlayerInput
    except Exception as ex:  # noqa: BLE001
        note(f"MOVE PlayerInput unreadable: {type(ex).__name__}: {ex}")
    note(f"MOVE PlayerInput={_short(player_input)}")
    if player_input is not None:
        for name in _props_matching(player_input.Class, INPUT_PROP_WORDS):
            try:
                value = getattr(player_input, name)
            except Exception as ex:  # noqa: BLE001
                value = f"<unreadable {type(ex).__name__}>"
            note(f"MOVE   input.{name} = {_short(value)}")

        # Writability matters as much as existence: a field we can read but not set is no use as a
        # channel. Written back with its own current value, so the probe changes nothing.
        for name in _props_matching(player_input.Class, ("double",)):
            try:
                current = getattr(player_input, name)
                setattr(player_input, name, current)
            except Exception as ex:  # noqa: BLE001
                note(f"MOVE   input.{name} NOT writable: {type(ex).__name__}: {ex}")
            else:
                note(f"MOVE   input.{name} writable")


def _probe_flag_layout(pc: WillowPlayerController) -> None:
    """Derive the CompressedFlags bit layout by asking the packer, instead of waiting to see it.

    `CompressedFlags()` is only called from `ReplicateMove`, which only runs on a client - so
    observing it needs a co-op session. But it is a pure function of the move's own booleans, so we
    can build a SavedMove ourselves, flip one flag at a time, and read which bit moves. Same answer,
    solo, deterministically, in one run.

    The point of it is the free mask at the end: any bit no flag claims is a bit a slide marker can
    occupy, which is what lets slide state ride the move stream rather than a side channel.
    """
    if _Progress.probed_flag_layout:
        return
    _Progress.probed_flag_layout = True

    try:
        saved_class = pc.SavedMoveClass
        move = construct_object(saved_class, pc)
    except Exception as ex:  # noqa: BLE001
        note(f"FLAGBITS could not construct a SavedMove: {type(ex).__name__}: {ex}")
        return
    note(f"FLAGBITS probing {_short(saved_class)}")

    def packed() -> int | None:
        try:
            return int(move.CompressedFlags())
        except Exception as ex:  # noqa: BLE001
            note(f"FLAGBITS CompressedFlags() failed: {type(ex).__name__}: {ex}")
            return None

    # Clear everything first, so the baseline is genuinely "no flags set".
    for name in SAVEDMOVE_BOOLS:
        try:
            setattr(move, name, False)
        except Exception:  # noqa: BLE001, S110 - absent on this class, nothing to clear
            pass

    if (base := packed()) is None:
        return
    note(f"FLAGBITS baseline=0b{base:08b} ({base})")

    used = base
    for name in SAVEDMOVE_BOOLS:
        try:
            setattr(move, name, True)
        except Exception as ex:  # noqa: BLE001
            note(f"FLAGBITS {name}: not settable ({type(ex).__name__})")
            continue
        value = packed()
        try:
            setattr(move, name, False)
        except Exception:  # noqa: BLE001, S110 - restore is best effort
            pass
        if value is None:
            continue
        bit = value ^ base
        used |= bit
        note(f"FLAGBITS {name} -> bit 0b{bit:08b} ({bit})")

    # DoubleClickMove is an enum rather than a bool, and may also fold into the byte.
    for probe_value in (1, 2, 3, 4):
        try:
            move.DoubleClickMove = probe_value
            value = packed()
            move.DoubleClickMove = 0
        except Exception:  # noqa: BLE001 - not present, or not settable
            break
        if value is not None and (bit := value ^ base):
            used |= bit
            note(f"FLAGBITS DoubleClickMove={probe_value} -> bit 0b{bit:08b} ({bit})")

    free = (~used) & 0xFF
    note(f"FLAGBITS used=0b{used:08b} free=0b{free:08b} ({free})")
    if free:
        lowest = free & -free
        note(f"FLAGBITS lowest free bit = {lowest} (0b{lowest:08b}) - candidate slide marker")
    else:
        note("FLAGBITS no free bit in the byte - fall back to ForcedDoubleClick")


def _savedmove_flags_probe(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Decode the CompressedFlags bit layout from the move that packed it.

    Post hook, so `ret` is the byte the game actually produced, while `obj` is the SavedMove holding
    the booleans that went into it. Reading both off the same object removes the guesswork entirely -
    no correlating across frames, no assuming stock UE3 bit order, which BL2 does not use.

    The running OR of every byte seen is the useful by-product: any bit never set by anything the
    game does is a bit a slide flag can occupy.
    """
    if _Progress.savedmove_fires >= MAX_SAVEDMOVE_FIRES:
        return
    try:
        packed = int(ret)
    except Exception:  # noqa: BLE001 - not the byte we expected
        return

    def flag(name: str) -> str:
        try:
            return "1" if getattr(obj, name) else "0"
        except Exception:  # noqa: BLE001
            return "?"

    bools = " ".join(f"{name[1:].lower()}={flag(name)}" for name in SAVEDMOVE_BOOLS)
    try:
        dclick = str(obj.DoubleClickMove)
    except Exception:  # noqa: BLE001
        dclick = "?"

    key = f"{packed}|{bools}|{dclick}"
    if key == _Progress.last_savedmove:
        return
    _Progress.last_savedmove = key
    _Progress.savedmove_fires += 1
    _Progress.seen_flag_bits |= packed
    note(
        f"SMOVE #{_Progress.savedmove_fires} packed={packed:>3} 0b{packed:08b} {bools}"
        f" dclick={dclick} sliding={int(OWN_SLIDE_STATE.is_sliding)}"
        f" everseen=0b{_Progress.seen_flag_bits:08b}",
    )


def _moveflags_probe(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Decode the CompressedFlags bit layout by watching it change against known booleans.

    Logged only when the combination changes, rather than every move. A bitfield is decoded from the
    transitions, not from the volume of samples - and every move logged unchanged is a line not
    spent on the one where a bit flipped.
    """
    if _Progress.moveflag_fires >= MAX_MOVEFLAG_FIRES:
        return
    try:
        flags = int(args.CompressedFlags)
        dclick = str(args.DoubleClickMove)
    except Exception:  # noqa: BLE001 - variant without these args
        return

    def flag(name: str) -> str:
        try:
            return "1" if getattr(obj, name) else "0"
        except Exception:  # noqa: BLE001
            return "?"

    duck, run, jump = flag("bDuck"), flag("bRun"), flag("bPressedJump")
    sprint = flag("bInSprintState")
    sliding = "1" if OWN_SLIDE_STATE.is_sliding else "0"

    key = f"{flags}|{dclick}|{duck}{run}{jump}{sprint}{sliding}"
    if key == _Progress.last_moveflag:
        return
    _Progress.last_moveflag = key
    _Progress.moveflag_fires += 1
    note(
        f"MFLAG #{_Progress.moveflag_fires} flags={flags:>3} 0b{flags:08b} dclick={dclick}"
        f" duck={duck} run={run} jump={jump} sprint={sprint} sliding={sliding}",
    )


def _servermove_probe(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Measure the exact quantity the engine decides corrections on.

    `ServerMove` carries the client's own claimed position; the server compares it against where its
    simulation put that pawn, and corrects past a threshold. Every other probe here shows one side
    or the other - this is the only one that shows the gap between them, which is what says whether
    the jitter is a small constant offset, a drift that grows across the slide, or an oscillation.
    """
    if _Progress.servermove_fires >= MAX_SERVERMOVE_FIRES:
        return
    try:
        pawn = obj.Pawn
        if pawn is None:
            return
        here = pawn.Location
        claimed = None
        for field in ("ClientLoc", "ClientLocation", "InClientLoc"):
            try:
                claimed = getattr(args, field)
            except Exception:  # noqa: BLE001 - variant without this name
                continue
            break
        if claimed is None:
            return
        dx, dy, dz = claimed.X - here.X, claimed.Y - here.Y, claimed.Z - here.Z
        error = (dx * dx + dy * dy + dz * dz) ** 0.5
    except Exception:  # noqa: BLE001 - shape differs between ServerMove variants
        return
    if error < 1.0:
        return
    _Progress.servermove_fires += 1
    try:
        who = str(obj.PlayerReplicationInfo.PlayerName)
    except Exception:  # noqa: BLE001
        who = "?"
    note(f"SVMOVE #{_Progress.servermove_fires} {who} error={error:.1f} d=({dx:.1f},{dy:.1f},{dz:.1f})")


def _transport_probe(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> None:
    """Log every mod network message crossing the wire, on whichever machine sees it.

    Unfiltered by slide state on purpose - the question is whether the message moves at all, and a
    message that is never sent would be invisible to a slide-filtered probe.
    """
    if _Progress.transport_fires >= MAX_TRANSPORT_FIRES:
        return
    try:
        # ServerSpeech names it Type; ClientMessage does too, but its payload field differs.
        msg_type = str(args.Type)
    except Exception:  # noqa: BLE001 - not one of ours, or no such field
        return
    if not msg_type.startswith(CUSTOM_MESSAGE_PREFIX):
        return

    _Progress.transport_fires += 1
    try:
        name = str(func.func.Name)
    except Exception:  # noqa: BLE001
        name = "?"
    try:
        who = str(obj.PlayerReplicationInfo.PlayerName)
    except Exception:  # noqa: BLE001
        who = "?"
    note(f"XPORT #{_Progress.transport_fires} {name} client={is_client()} on={who} type={msg_type}")


def _dump_network_identity(pc: WillowPlayerController) -> None:
    """Record who this machine thinks everyone is, and what identifiers it is listening for.

    Three things can silently swallow a message: the identifiers not matching between the two
    machines, `get_host_pri` picking the wrong player, or it picking *this* player - in which case
    the message is handled locally and never sent at all. All three are visible here.
    """
    if _Progress.dumped_identity:
        return
    _Progress.dumped_identity = True

    try:
        from networking.registration import registered_callbacks  # noqa: PLC0415

        listening = sorted(k for k in registered_callbacks if "slid" in k.lower())
        note(f"NETID listening_for={listening}")
    except Exception as ex:  # noqa: BLE001
        note(f"NETID registered_callbacks unreadable: {type(ex).__name__}: {ex}")

    try:
        local_pri = pc.PlayerReplicationInfo
        note(f"NETID local={local_pri.PlayerName} id={local_pri.PlayerID} client={is_client()}")
    except Exception as ex:  # noqa: BLE001
        note(f"NETID local pri unreadable: {type(ex).__name__}: {ex}")
        return

    try:
        from networking.transmission import get_host_pri  # noqa: PLC0415

        host_pri = get_host_pri()
        note(
            f"NETID host={host_pri.PlayerName} id={host_pri.PlayerID}"
            f" is_self={host_pri == local_pri}",
        )
    except Exception as ex:  # noqa: BLE001 - StopIteration here means no party leader is flagged
        note(f"NETID get_host_pri FAILED: {type(ex).__name__}: {ex}")

    try:
        roster = [
            (str(pri.PlayerName), int(pri.PlayerID), bool(pri.bIsPartyLeader))
            for pri in ENGINE.GetCurrentWorldInfo().GRI.PRIArray
        ]
        note(f"NETID roster(name,id,isPartyLeader)={roster}")
    except Exception as ex:  # noqa: BLE001
        note(f"NETID roster unreadable: {type(ex).__name__}: {ex}")


def _netcode_probe(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> None:
    """Catch the server correcting a sliding client, or simulating one, in the act.

    Filtered to slides only. These run at packet rate all game long, so logging them unconditionally
    would bury everything else - and it is specifically the calls landing mid-slide that explain the
    rubberband. The args carry the position the server is asserting, which is the number that says
    how far the two simulations have drifted apart.
    """
    # Also log across the teleport watch window, not only during the slide. The correction that
    # actually moves the player lands several frames *after* the slide ends, so a slide-only filter
    # hid the single most important packet in every log gathered so far.
    if not OWN_SLIDE_STATE.is_sliding and _Progress.watch_frames <= 0:
        return
    try:
        name = str(func.func.Name)
    except Exception:  # noqa: BLE001
        name = "?"
    count = _Progress.netcode_fires.get(name, 0) + 1
    _Progress.netcode_fires[name] = count
    if count > MAX_NETCODE_FIRES:
        return

    try:
        location = obj.Pawn.Location
        here = f"({location.X:.0f},{location.Y:.0f},{location.Z:.0f})"
    except Exception:  # noqa: BLE001 - some of these are called on the pawn, not the controller
        here = "?"
    when = "during" if OWN_SLIDE_STATE.is_sliding else f"after+{TELEPORT_WATCH_FRAMES - _Progress.watch_frames}"
    moved = "?"
    try:
        location = obj.Pawn.Location
        dx = float(args.NewLocX) - float(location.X)
        dy = float(args.NewLocY) - float(location.Y)
        dz = float(args.NewLocZ) - float(location.Z)
        moved = f"{(dx * dx + dy * dy + dz * dz) ** 0.5:.0f}"
    except Exception:  # noqa: BLE001 - variant without these args
        pass
    note(
        f"NET #{count} {name} {when} client={is_client()} at={here} asserts_move={moved}"
        f" args=({_describe_args(args)})",
    )


def _arg_field_names(args: Any) -> list[str]:
    """Every parameter name on a hook's args struct. Mirrors `hooks._arg_names`, kept local so the
    diagnostics module stays deletable in one piece."""
    try:
        return [str(field.Name) for field in args._type._fields()]
    except Exception:  # noqa: BLE001 - a probe must never raise at its call site
        try:
            return [name for name in dir(args) if not name.startswith("_")]
        except Exception:  # noqa: BLE001
            return []


def _velocity_summary(pc: Any) -> str:
    """Speed and unit heading of the pawn, as `spd=891 hdg=(0.98,-0.21)`."""
    try:
        velocity = pc.Pawn.Velocity
        vx, vy = float(velocity.X), float(velocity.Y)
    except Exception:  # noqa: BLE001 - no pawn mid-transition
        return "spd=? hdg=?"
    speed = (vx * vx + vy * vy) ** 0.5
    if speed < 1.0:
        return f"spd={speed:.0f} hdg=(none)"
    return f"spd={speed:.0f} hdg=({vx / speed:.2f},{vy / speed:.2f})"


def _saved_move_count(pc: Any) -> str:
    """How many unacknowledged moves are queued to be replayed.

    UE3 keeps these as a linked list off `SavedMoves`, walked via `NextMove`. Capped, because a
    diagnostic that walks a corrupt list forever costs the frame it was meant to describe.
    """
    try:
        move = pc.SavedMoves
    except Exception:  # noqa: BLE001 - variant without the property
        return "?"
    count = 0
    try:
        while move is not None and count < MAX_SAVEDMOVE_WALK:
            count += 1
            move = move.NextMove
    except Exception:  # noqa: BLE001, S110 - end of list; the count so far is the answer
        pass
    return f"{count}{'+' if count >= MAX_SAVEDMOVE_WALK else ''}"


def _applied_count() -> int:
    """`hooks._Phys.applied` - how many times slide physics has been forced onto the local pawn.

    Imported lazily. `discovery` is imported before `hooks` in `__init__`, and a module-level import
    here would invert that order for no benefit.
    """
    try:
        from .hooks import _Phys  # noqa: PLC0415 - deliberately lazy, see docstring

        return int(_Phys.applied)
    except Exception:  # noqa: BLE001
        return -1


def _replay_probe_pre(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """PRE ClientUpdatePosition: snapshot the state the replay is about to overwrite.

    Runs on: the corrected machine, which is whichever one is a client.
    """
    # One-shot proof of life, before any filter can suppress it. If this line never appears, the
    # replay hypothesis is dead on its face and the corrections are doing something else entirely.
    if not _Progress.replay_reported:
        _Progress.replay_reported = True
        note(f"REPLAY fired args={_arg_field_names(args)} client={is_client()}")

    if not OWN_SLIDE_STATE.is_sliding and _Progress.watch_frames <= 0:
        return
    _Progress.replay_fires += 1
    if _Progress.replay_fires > MAX_REPLAY_REPORTS:
        return
    try:
        # Stash the pre-replay reading for the POST half to diff against. The applied counter is the
        # measurement that matters; the rest is context for reading the line.
        _Progress.replay_pending = (
            _Progress.replay_fires,
            _applied_count(),
            _velocity_summary(obj),
        )
        note(
            f"REPLAY #{_Progress.replay_fires} pre sliding={OWN_SLIDE_STATE.is_sliding}"
            f" moves={_saved_move_count(obj)} applied={_applied_count()}"
            f" {_velocity_summary(obj)}"
            f" slide_hdg=({OWN_SLIDE_STATE.dir_x:.2f},{OWN_SLIDE_STATE.dir_y:.2f})"
            f" pct={OWN_SLIDE_STATE.speed_pct:.2f}",
        )
    except Exception as ex:  # noqa: BLE001 - never break the correction path
        note(f"REPLAY pre failed: {type(ex).__name__}: {ex}")


def _replay_probe_post(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """POST ClientUpdatePosition: report what the replay did, and whether our forcing ran inside it.

    `applied=N->N` is the finding: the replay re-simulated those moves without the slide, so the
    locked heading was discarded and the pawn came out following raw input. `applied=N->N+k` would
    kill the hypothesis - our physics would be running inside the replay after all.
    """
    pending = _Progress.replay_pending
    if pending is None:
        return
    _Progress.replay_pending = None
    index, applied_before, before = pending
    try:
        applied_after = _applied_count()
        note(
            f"REPLAY #{index} post applied={applied_before}->{applied_after}"
            f" (forced {applied_after - applied_before}x during replay)"
            f" before[{before}] after[{_velocity_summary(obj)}]"
            f" slide_hdg=({OWN_SLIDE_STATE.dir_x:.2f},{OWN_SLIDE_STATE.dir_y:.2f})",
        )
    except Exception as ex:  # noqa: BLE001 - never break the correction path
        note(f"REPLAY post failed: {type(ex).__name__}: {ex}")


def _scan_functions() -> None:
    """Find the footstep and PostRender functions by searching, rather than by guessing names.

    Guessing costs a whole restart per wrong guess - a hook on a name that does not exist just never
    fires, which looks exactly like a hook on a function that is never called. So enumerate every
    UFunction, match on the name, and hook whatever actually turns up.
    """
    if _Progress.scanned_functions:
        return
    _Progress.scanned_functions = True

    try:
        functions = list(find_all("Function"))
    except Exception as ex:  # noqa: BLE001
        note(f"SCAN failed: {type(ex).__name__}: {ex}")
        return
    note(f"SCAN {len(functions)} functions in memory")

    footstep: list[str] = []
    postrender: list[str] = []
    netcode: list[str] = []
    move: list[str] = []
    for func in functions:
        try:
            lowered = str(func.Name).lower()
        except Exception:  # noqa: BLE001
            continue
        if any(word in lowered for word in FOOTSTEP_WORDS):
            bucket = footstep
        elif any(word in lowered for word in POSTRENDER_WORDS):
            bucket = postrender
        elif any(word in lowered for word in NETCODE_WORDS):
            bucket = netcode
        elif any(word in lowered for word in MOVE_WORDS):
            bucket = move
        else:
            continue
        if (hook_name := _hook_name(func)) is not None:
            bucket.append(hook_name)

    for label, names in (
        ("FOOTSTEP-CANDIDATE", footstep),
        ("POSTRENDER-CANDIDATE", postrender),
        ("NETCODE-CANDIDATE", netcode),
        ("MOVE-CANDIDATE", move),
    ):
        for name in sorted(set(names)):
            note(f"SCAN {label} {name}")

    # Hook only the correction and simulation calls, not every netcode function that matched. The
    # listing above is for reading; this is for measuring.
    netcode_installed = 0
    for name in sorted(set(netcode)):
        if netcode_installed >= MAX_NETCODE_HOOKS:
            note(f"SCAN netcode hook cap reached at {MAX_NETCODE_HOOKS}")
            break
        if not any(word in name.lower() for word in NETCODE_HOOK_WORDS):
            continue
        try:
            added = add_hook(name, Type.PRE, NETCODE_ID, _netcode_probe)
        except Exception as ex:  # noqa: BLE001
            note(f"SCAN could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.pre_hooks.append(name)
            netcode_installed += 1
        note(f"SCAN hook {name}: {'added' if added else 'refused'}")
    note(f"SCAN {netcode_installed} netcode hooks live")

    # Hook the footstep candidates now. They only start reporting on the *next* slide, which is why
    # the log says so - one slide is not enough to gather this.
    installed = 0
    for name in sorted(set(footstep)):
        if installed >= MAX_FOOTSTEP_HOOKS:
            note(f"SCAN footstep hook cap reached at {MAX_FOOTSTEP_HOOKS}")
            break
        try:
            added = add_hook(name, Type.PRE, FOOTSTEP_ID, _footstep_probe)
        except Exception as ex:  # noqa: BLE001
            note(f"SCAN could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.pre_hooks.append(name)
            installed += 1
        note(f"SCAN hook {name}: {'added' if added else 'refused'}")
    note(f"SCAN {installed} footstep hooks live - SLIDE AGAIN to capture their calls")

    # A Canvas may be reachable even if no PostRender hook fires, so record whether any exist.
    try:
        note(f"SCAN {len(list(find_all('Canvas')))} Canvas objects in memory")
    except Exception as ex:  # noqa: BLE001
        note(f"SCAN Canvas scan failed: {type(ex).__name__}: {ex}")


def _hud_probe(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    func: unreal.BoundFunction,
) -> None:
    """Record, once per PostRender function, whether it is usable for drawing.

    This runs on every rendered frame, so it must do nothing at all after the first hit. It stays
    registered rather than removing itself, because unhooking from inside a running hook is not
    obviously safe and a set lookup per frame is cheap.
    """
    try:
        key = f"{obj.Class.Name}.{func.func.Name}"
    except Exception:  # noqa: BLE001
        key = "?"
    if key in _Progress.hud_seen:
        return
    _Progress.hud_seen.add(key)

    try:
        canvas = obj.Canvas
    except Exception as ex:  # noqa: BLE001
        note(f"HUD {key} fired, Canvas unreadable: {type(ex).__name__}: {ex}")
        return
    if canvas is None:
        note(f"HUD {key} fired, Canvas=None")
        return
    try:
        note(f"HUD {key} fired, Canvas={_short(canvas)} size={canvas.SizeX}x{canvas.SizeY}")
    except Exception as ex:  # noqa: BLE001
        note(f"HUD {key} fired, Canvas set but size unreadable: {type(ex).__name__}: {ex}")


# --- wiring ----------------------------------------------------------------------------------------


def _unit(x: float, y: float) -> tuple[float, float] | None:
    """Normalise a ground-plane vector, or None if it is too short to have a direction."""
    magnitude = (x * x + y * y) ** 0.5
    if magnitude < 1e-6:
        return None
    return (x / magnitude, y / magnitude)


def _fmt(vec: tuple[float, float] | None) -> str:
    """A unit vector as `(0.98,-0.21)`, or `(none)` when there was no direction to report."""
    return "(none)" if vec is None else f"({vec[0]:.2f},{vec[1]:.2f})"


def _facing_of(pc: Any) -> tuple[float, float] | None:
    """The player's view direction as a ground-plane unit vector.

    UE3 rotators are 16-bit: 65536 units to a full turn, not 360 degrees. Getting that conversion
    wrong yields a plausible-looking vector that is silently wrong, which is worse here than no
    reading at all - the whole question is whether the slide follows this vector or ignores it.
    """
    try:
        yaw = float(pc.Rotation.Yaw)
    except Exception:  # noqa: BLE001 - no controller rotation this frame
        return None
    radians = yaw * math.tau / 65536.0
    return (math.cos(radians), math.sin(radians))


def _accel_of(pc: Any) -> tuple[float, float] | None:
    """The pawn's current acceleration as a unit vector - the only directional input the host gets.

    Sampled at the end of the tick, which is after `apply_slide_physics` has zeroed it. That is
    deliberate: if this reads `(none)` for the whole slide, the host is being handed a zero
    acceleration and has nothing left to steer its copy of the pawn with, which would explain a
    remote slide that goes somewhere unrelated to the player's input.
    """
    try:
        accel = pc.Pawn.Acceleration
        return _unit(float(accel.X), float(accel.Y))
    except Exception:  # noqa: BLE001 - no pawn this frame
        return None


def _bearing_from_entry(here: tuple[float, float, float]) -> tuple[tuple[float, float] | None, float]:
    """Net travel from where the slide opened: its bearing, and how far. (None, d) below the floor."""
    entry = _Progress.path_entry
    if entry is None:
        return (None, 0.0)
    dx, dy, dz = here[0] - entry[0], here[1] - entry[1], here[2] - entry[2]
    travelled = (dx * dx + dy * dy + dz * dz) ** 0.5
    if travelled < PATH_MIN_TRAVEL:
        return (None, travelled)
    return (_unit(dx, dy), travelled)


def _path_trace(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """POST PlayerTick: sample where we actually are, mid-slide.

    Throttled and bounded. Reports the bearing actually travelled since entry alongside the mod's
    locked heading, the player's facing, and the replicated acceleration, so the four can be read
    off against each other on one line.
    """
    if not OWN_SLIDE_STATE.is_sliding or _Progress.path_reports >= MAX_PATH_REPORTS:
        return
    _Progress.path_frames += 1
    if _Progress.path_frames % PATH_SAMPLE_EVERY != 0:
        return
    try:
        location = obj.Pawn.Location
        here = (float(location.X), float(location.Y), float(location.Z))
    except Exception:  # noqa: BLE001 - no pawn this frame
        return

    bearing, travelled = _bearing_from_entry(here)
    _Progress.path_reports += 1
    note(
        f"PATH #{_Progress.path_reports} f+{_Progress.path_frames}"
        f" at=({here[0]:.0f},{here[1]:.0f},{here[2]:.0f})"
        f" moved={travelled:.0f} went={_fmt(bearing)}"
        f" locked={_fmt(_Progress.path_entry_dir)}"
        f" facing={_fmt(_facing_of(obj))}"
        f" accel={_fmt(_accel_of(obj))}",
    )


def _angle_between(a: tuple[float, float] | None, b: tuple[float, float] | None) -> str:
    """Angle between two unit vectors in degrees, as a string. `?` when either is missing."""
    if a is None or b is None:
        return "?"
    dot = max(-1.0, min(1.0, a[0] * b[0] + a[1] * b[1]))
    return f"{math.degrees(math.acos(dot)):.0f}deg"


def _teleport_watch(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Sample our own pawn's position across the end of a slide and report any jump.

    Armed by `slide_ended`, so it costs nothing outside the window that matters. Reports the size and
    direction of the step, which distinguishes a server correction pulling us back along the slide
    from something else moving us entirely.
    """
    if _Progress.watch_frames <= 0:
        return
    _Progress.watch_frames -= 1
    try:
        location = obj.Pawn.Location
        here = (float(location.X), float(location.Y), float(location.Z))
    except Exception:  # noqa: BLE001 - no pawn this frame
        return

    previous = _Progress.watch_last
    _Progress.watch_last = here
    if previous is None:
        return

    dx, dy, dz = here[0] - previous[0], here[1] - previous[1], here[2] - previous[2]
    step = (dx * dx + dy * dy + dz * dz) ** 0.5
    if step < TELEPORT_MIN_JUMP or _Progress.teleport_reports >= MAX_TELEPORT_REPORTS:
        return
    _Progress.teleport_reports += 1
    frame = TELEPORT_WATCH_FRAMES - _Progress.watch_frames
    note(
        f"TPORT #{_Progress.teleport_reports} frame+{frame} jump={step:.0f}"
        f" d=({dx:.0f},{dy:.0f},{dz:.0f}) client={is_client()}",
    )


def _begin_path(pc: WillowPlayerController) -> None:
    """Stamp the reference frame every PATH line is measured against: where we were, which way we
    were facing, and the heading the mod just locked.
    """
    _Progress.path_frames = 0
    _Progress.path_entry_facing = _facing_of(pc)
    _Progress.path_entry_dir = _unit(OWN_SLIDE_STATE.dir_x, OWN_SLIDE_STATE.dir_y)
    try:
        location = pc.Pawn.Location
        _Progress.path_entry = (float(location.X), float(location.Y), float(location.Z))
    except Exception:  # noqa: BLE001 - no pawn; the samples will report (none) and say so
        _Progress.path_entry = None
    note(
        f"PATH open at={_Progress.path_entry} locked={_fmt(_Progress.path_entry_dir)}"
        f" facing={_fmt(_Progress.path_entry_facing)}"
        f" offset={_angle_between(_Progress.path_entry_dir, _Progress.path_entry_facing)}"
        f" client={is_client()}",
    )


def _end_path(pc: WillowPlayerController) -> None:
    """The line that answers the question: where did this slide actually go, and how far off was it
    from both the heading it locked and the way the player was facing when they started it.
    """
    try:
        location = pc.Pawn.Location
        here = (float(location.X), float(location.Y), float(location.Z))
    except Exception:  # noqa: BLE001 - no pawn
        return
    bearing, travelled = _bearing_from_entry(here)
    note(
        f"PATH close moved={travelled:.0f} went={_fmt(bearing)}"
        f" locked={_fmt(_Progress.path_entry_dir)}"
        f" facing={_fmt(_Progress.path_entry_facing)}"
        f" went_vs_locked={_angle_between(bearing, _Progress.path_entry_dir)}"
        f" went_vs_facing={_angle_between(bearing, _Progress.path_entry_facing)}",
    )


def on_end(pc: WillowPlayerController) -> None:
    """Subscribed to `slide_ended`. Reports the path, then arms the teleport watcher."""
    if not DISCOVERY:
        return
    _end_path(pc)
    _Progress.watch_frames = TELEPORT_WATCH_FRAMES
    try:
        location = pc.Pawn.Location
        _Progress.watch_last = (float(location.X), float(location.Y), float(location.Z))
    except Exception:  # noqa: BLE001
        _Progress.watch_last = None


def on_start(pc: WillowPlayerController) -> None:
    """Subscribed to `slide_started`. Everything expensive here is one-shot or throttled."""
    if not DISCOVERY:
        return
    _begin_path(pc)
    _dump_network_identity(pc)
    _dump_move_internals(pc)
    _probe_flag_layout(pc)
    _scan_functions()
    _dump_animtree(pc)
    _dump_surface_props(pc)
    _dump_akevents()


def enable() -> None:
    """Start a clean pair of dumps and register the per-frame probes."""
    if not DISCOVERY:
        return
    for path in (DISCOVERY_LOG, AKEVENT_DUMP):
        try:
            path.write_text("", encoding="utf-8")
        except OSError as ex:
            dbg(f"DISCOVERY could not clear {path.name}: {ex}")
    _Progress.notes = 0
    _Progress.seen_akevents = set()
    _Progress.hud_seen = set()
    _Progress.fires = {}
    note("=== sliding discovery ===")

    for name in SAVEDMOVE_FUNCS:
        try:
            # Unconditional: the flag injector blocks this function on sliding moves, and a
            # plain POST hook would therefore only ever see the moves we did not touch.
            added = add_hook(name, Type.POST_UNCONDITIONAL, SAVEDMOVE_ID, _savedmove_flags_probe)
        except Exception as ex:  # noqa: BLE001
            note(f"SMOVE could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.post_hooks.append(name)
        note(f"SMOVE hook {name}: {'added' if added else 'refused'}")

    for name in PATH_FUNCS:
        try:
            added = add_hook(name, Type.POST, PATH_ID, _path_trace)
        except Exception as ex:  # noqa: BLE001
            note(f"PATH could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.post_hooks.append(name)
        note(f"PATH hook {name}: {'added' if added else 'refused'}")

    for name in TELEPORT_FUNCS:
        try:
            added = add_hook(name, Type.POST, TELEPORT_ID, _teleport_watch)
        except Exception as ex:  # noqa: BLE001
            note(f"TPORT could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.post_hooks.append(name)
        note(f"TPORT hook {name}: {'added' if added else 'refused'}")

    for name in MOVEFLAG_FUNCS:
        try:
            added = add_hook(name, Type.PRE, MOVEFLAG_ID, _moveflags_probe)
        except Exception as ex:  # noqa: BLE001
            note(f"MFLAG could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.pre_hooks.append(name)
        note(f"MFLAG hook {name}: {'added' if added else 'refused'}")

    for name in SERVERMOVE_FUNCS:
        try:
            added = add_hook(name, Type.POST, SERVERMOVE_ID, _servermove_probe)
        except Exception as ex:  # noqa: BLE001
            note(f"SVMOVE could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.post_hooks.append(name)
        note(f"SVMOVE hook {name}: {'added' if added else 'refused'}")

    for name in TRANSPORT_FUNCS:
        try:
            added = add_hook(name, Type.PRE, TRANSPORT_ID, _transport_probe)
        except Exception as ex:  # noqa: BLE001 - a missing candidate is expected, not fatal
            note(f"XPORT could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.pre_hooks.append(name)
        note(f"XPORT hook {name}: {'added' if added else 'refused'}")

    # Both halves of the replay bracket. PRE snapshots what the replay is about to overwrite, POST
    # reports whether our forcing ran inside it. Registered by hand like everything else here -
    # build_mod only gathers from __init__'s own scope, so a decorator would register nothing.
    for name in REPLAY_FUNCS:
        try:
            added_pre = add_hook(name, Type.PRE, REPLAY_ID, _replay_probe_pre)
            added_post = add_hook(name, Type.POST, REPLAY_ID, _replay_probe_post)
        except Exception as ex:  # noqa: BLE001 - a missing variant is expected, not fatal
            note(f"REPLAY could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added_pre:
            _Progress.pre_hooks.append(name)
        if added_post:
            _Progress.post_hooks.append(name)
        note(
            f"REPLAY hook {name}: pre={'added' if added_pre else 'refused'}"
            f" post={'added' if added_post else 'refused'}",
        )

    for name in HUD_FUNCS:
        try:
            added = add_hook(name, Type.POST, HUD_ID, _hud_probe)
        except Exception as ex:  # noqa: BLE001 - a missing candidate is expected, not fatal
            note(f"HUD could not hook {name}: {type(ex).__name__}: {ex}")
            continue
        if added:
            _Progress.post_hooks.append(name)
        note(f"HUD hook {name}: {'added' if added else 'refused'}")

    dbg("DISCOVERY enabled")


def disable() -> None:
    """Unhook everything this module registered, whenever and however it was registered."""
    for name in _Progress.post_hooks:
        for identifier in (HUD_ID, SERVERMOVE_ID, TELEPORT_ID, REPLAY_ID, PATH_ID):
            try:
                remove_hook(name, Type.POST, identifier)
            except Exception:  # noqa: BLE001, S110 - nothing to do if it was never added
                pass
    for name in _Progress.pre_hooks:
        # One list holds both kinds, and removing an identifier that was never added is harmless, so
        # try both rather than tracking which probe claimed which name.
        for identifier in (FOOTSTEP_ID, NETCODE_ID, TRANSPORT_ID, MOVEFLAG_ID, REPLAY_ID):
            try:
                remove_hook(name, Type.PRE, identifier)
            except Exception:  # noqa: BLE001, S110
                pass
    for name in SAVEDMOVE_FUNCS:
        try:
            remove_hook(name, Type.POST_UNCONDITIONAL, SAVEDMOVE_ID)
        except Exception:  # noqa: BLE001, S110
            pass
    _Progress.post_hooks.clear()
    _Progress.pre_hooks.clear()
    _Progress.scanned_functions = False
    _Progress.dumped_animtree = False
    _Progress.replay_reported = False
    _Progress.replay_pending = None


__all__ = ["disable", "enable", "note", "on_start"]
