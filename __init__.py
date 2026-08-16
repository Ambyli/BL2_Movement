from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from mods_base import ENGINE, build_mod, get_pc, hook
from networking import add_network_functions
from networking.decorators import host, targeted
from unrealsdk import find_enum, unreal
from unrealsdk.hooks import Type
from unrealsdk.unreal import WeakPointer

from tweens import (
    Tween,
    circ_out,
    cubic_in_out,
    cubic_out,
    elastic_out,
    quad_out,
)
from uemath import Vector

if TYPE_CHECKING:
    from common import WillowGameEngine, WillowPlayerController, WillowPlayerPawn, WorldInfo

SLIDE_SPEED_DEFAULT: float = 2.2
CROUCHED_PCT_DEFAULT: float = 0.5
# Absolute speed a slide opens at, in unreal units per second. Deliberately a flat number rather
# than something read off the pawn: GroundSpeed alone swings from 594 to 440 the instant sprint
# drops, and weapons, skills and class mods all move it too. This is the one dial for slide speed.
SLIDE_START_SPEED: float = 970.0
# How fast the slide can be curved with the movement keys, in fraction of the way turned per
# second. 0 locks the slide to the direction you entered it in.
SLIDE_STEER_RATE: float = 3.0
# Input pointing further than this back down the slide is ignored outright. -0.5 is 120 degrees
# off the heading, so back, back-left and back-right are all inert.
SLIDE_BACK_CUTOFF: float = -0.5
# Sideways input weaker than this does not steer at all. Without it, an input that is very nearly
# straight backwards leaves a sliver of a sideways component that still creeps the heading round.
SLIDE_STEER_DEADZONE: float = 0.1
# Hard limit on how far a slide can be turned from the heading it started on. Steering may curve a
# slide, but can never bring you round to travelling backwards.
SLIDE_MAX_TURN_DEGREES: float = 60.0

# --- temporary diagnostics, delete once the slide behaves ---------------------------------------
DEBUG_LOG: bool = True
_DBG_PATH = Path.home() / "bl2_slide_debug.log"
_DBG_COUNT: int = 0
_DBG_MAX: int = 400


def _dbg(msg: str) -> None:
    """Append a bounded diagnostic line to the user's home directory."""
    global _DBG_COUNT
    if not DEBUG_LOG or _DBG_COUNT >= _DBG_MAX:
        return
    _DBG_COUNT += 1
    try:
        with _DBG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{_DBG_COUNT:04d} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------------------------


class State:
    do_slide_jump: ClassVar[bool] = False
    tweener: ClassVar[Tween] = Tween()
    horizontal_velocity: ClassVar[Vector] = Vector()


@dataclass
class PlayerSlideState:
    old_z: float
    is_sliding: bool
    dir_x: float = 0.0
    dir_y: float = 0.0
    entry_x: float = 0.0
    entry_y: float = 0.0
    speed_pct: float = SLIDE_SPEED_DEFAULT


CLIENTS_SLIDE_STATES: dict[WeakPointer[WillowPlayerController], PlayerSlideState] = {}
OWN_SLIDE_STATE: PlayerSlideState = PlayerSlideState(old_z=0, is_sliding=False)

e_net_mode: WorldInfo.ENetMode = cast("WorldInfo.ENetMode", find_enum("ENetMode"))


def is_client() -> bool:
    return cast("WillowGameEngine", ENGINE).GetCurrentWorldInfo().NetMode == e_net_mode.NM_Client


def tween_slide(pc: WillowPlayerController) -> None:
    if State.tweener.is_running():
        State.tweener.kill()
    arms = pc.Pawn.Arms
    if not arms.Attachments:
        return
    State.tweener = Tween()
    t = State.tweener
    t.tween_property(
        arms.SkeletalMesh.RotOrigin,
        "Pitch",
        final_value=500,
        duration=0.2,
    ).from_current().transition(cubic_in_out)
    t.tween_property(
        arms.SkeletalMesh.RotOrigin,
        "Yaw",
        final_value=-200,
        duration=0.4,
    ).from_current().transition(quad_out)
    t.tween_property(
        arms.SkeletalMesh.RotOrigin,
        "Roll",
        final_value=-6300,
        duration=0.5,
    ).from_current().transition(cubic_out)
    t.tween_property(
        arms.SkeletalMesh.Origin,
        "X",
        final_value=30,
        duration=1.2,
    ).from_current().transition(elastic_out)
    t.tween_property(
        arms.SkeletalMesh.Origin,
        "Y",
        final_value=-14.5,
        duration=0.5,
    ).from_current().transition(circ_out)
    t.tween_property(
        arms.SkeletalMesh.Origin,
        "Z",
        final_value=-175,
        duration=0.5,
    ).from_current().transition(circ_out)
    t.set_parallel(True)
    t.start()


def tween_reset(pc: WillowPlayerController) -> None:
    if State.tweener.is_running():
        State.tweener.kill()
    arms = pc.Pawn.Arms
    if not arms.Attachments:
        return
    State.tweener = Tween()
    t = State.tweener
    State.tweener = Tween()
    t = State.tweener
    t.tween_property(
        arms.SkeletalMesh.RotOrigin,
        "Pitch",
        final_value=0,
        duration=0.5,
    ).from_current().transition(cubic_in_out)
    t.tween_property(
        arms.SkeletalMesh.RotOrigin,
        "Yaw",
        final_value=0,
        duration=0.4,
    ).from_current().transition(quad_out)
    t.tween_property(
        arms.SkeletalMesh.RotOrigin,
        "Roll",
        final_value=0,
        duration=0.3,
    ).from_current().transition(cubic_in_out)
    t.tween_property(
        arms.SkeletalMesh.Origin,
        "X",
        final_value=40,
        duration=0.4,
    ).from_current().transition(circ_out)
    t.tween_property(
        arms.SkeletalMesh.Origin,
        "Y",
        final_value=0,
        duration=0.6,
    ).from_current().transition(circ_out)
    t.tween_property(
        arms.SkeletalMesh.Origin,
        "Z",
        final_value=-167,
        duration=0.5,
    ).from_current().transition(circ_out)
    t.set_parallel(True)
    t.start()


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


def exit_slide(pc: WillowPlayerController) -> None:
    if not OWN_SLIDE_STATE.is_sliding:
        return
    OWN_SLIDE_STATE.is_sliding = False
    pc.Pawn.CrouchedPct = CROUCHED_PCT_DEFAULT
    _dbg("EXIT")
    server_exit_slide()
    tween_reset(pc)


@targeted.message
def client_exit_slide() -> None:
    exit_slide(cast("WillowPlayerController", get_pc()))


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
    _dbg(f"SERVER_ENTER ran, clients={len(CLIENTS_SLIDE_STATES)}")


def enter_slide(pc: WillowPlayerController) -> None:
    """The client wants to slide, sends the request to the server but can already start the vfx"""
    if OWN_SLIDE_STATE.is_sliding:
        return
    server_enter_slide()
    OWN_SLIDE_STATE.is_sliding = True
    OWN_SLIDE_STATE.old_z = pc.Pawn.Location.Z
    begin_slide_state(cast("WillowPlayerPawn", pc.Pawn), OWN_SLIDE_STATE)
    pc.Pawn.CrouchedPct = SLIDE_SPEED_DEFAULT
    _dbg(
        f"ENTER_OWN speed={SLIDE_START_SPEED:.0f} "
        f"dir=({OWN_SLIDE_STATE.dir_x:.2f},{OWN_SLIDE_STATE.dir_y:.2f}) "
        f"clients={len(CLIENTS_SLIDE_STATES)}",
    )
    tween_slide(pc)


def begin_slide_state(pawn: WillowPlayerPawn, slide_data: PlayerSlideState) -> None:
    """Lock in the heading a slide was entered at."""
    slide_data.speed_pct = SLIDE_SPEED_DEFAULT

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


def apply_slide_physics(
    pawn: WillowPlayerPawn,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Force the slide's heading and speed onto the pawn, after the engine has had its say.

    PlayerMove recomputes velocity from input every frame, so anything written before it runs gets
    thrown away. Running this as a post hook instead means these are the values the walking physics
    actually integrates. Acceleration and friction are zeroed so the engine has nothing left to
    fight with, and the pawn's own speed cap is held clear of the speed being forced - otherwise
    the cap clamps the slide back down the moment anything lowers GroundSpeed.
    """
    direction = Vector((slide_data.dir_x, slide_data.dir_y, 0.0))
    if direction.magnitude == 0:
        return

    # What the physics tick actually produced from last frame's forced velocity. If this points
    # away from the slide heading, something downstream is still overriding us.
    prev = Vector(pawn.Velocity)
    prev.z = 0
    prev_dot = prev.normalized.dot(direction) if prev.magnitude > 1.0 else 0.0

    raw = Vector(pawn.Acceleration)
    raw.z = 0
    raw_dot = raw.normalized.dot(direction) if raw.magnitude > 0 else 0.0

    accel = Vector(pawn.Acceleration)
    accel.z = 0
    if accel.magnitude > 0:
        accel.normalize()
        backwards = accel.dot(direction)
        if backwards > SLIDE_BACK_CUTOFF:
            # Drop the part of the input running back down the slide, then steer on whatever
            # sideways component survives - weighted by how sideways it actually is. Normalising
            # it unweighted would turn a hair of residue from a near-backwards input into a
            # full strength turn, which is precisely how holding back used to spin the slide.
            if backwards < 0:
                accel = accel - direction * backwards
            strength = accel.magnitude
            if strength > SLIDE_STEER_DEADZONE:
                alpha = min(SLIDE_STEER_RATE * delta_time * strength, 1.0)
                direction = direction.lerp(accel.normalize(), alpha).normalize()

    # Backstop: never let steering accumulate far enough to reverse the slide, however the input
    # is fed in. Anything past the limit is pinned to the edge of the allowed cone.
    entry = Vector((slide_data.entry_x, slide_data.entry_y, 0.0))
    if entry.magnitude > 0:
        cos_limit = math.cos(math.radians(SLIDE_MAX_TURN_DEGREES))
        along = direction.dot(entry)
        if along < cos_limit:
            perp = direction - entry * along
            if perp.magnitude > 0:
                perp.normalize()
                sin_limit = math.sqrt(max(1.0 - cos_limit * cos_limit, 0.0))
                direction = (entry * cos_limit + perp * sin_limit).normalize()

    slide_data.dir_x = direction.x
    slide_data.dir_y = direction.y

    speed = SLIDE_START_SPEED * (slide_data.speed_pct / SLIDE_SPEED_DEFAULT)

    pawn.CrouchedPct = max(SLIDE_SPEED_DEFAULT, (speed / max(pawn.GroundSpeed, 1.0)) * 2.0)
    pawn.Acceleration.X = 0.0
    pawn.Acceleration.Y = 0.0
    pawn.Velocity.X = direction.x * speed
    pawn.Velocity.Y = direction.y * speed

    _dbg(
        f"POST prev_spd={prev.magnitude:.0f} prev_dot={prev_dot:+.2f} "
        f"in_mag={raw.magnitude:.0f} in_dot={raw_dot:+.2f} "
        f"turn={math.degrees(math.acos(max(-1.0, min(1.0, direction.dot(entry))))) if entry.magnitude else 0.0:5.1f} "
        f"pct={slide_data.speed_pct:.2f} set={speed:.0f}",
    )


def slide(
    pc: WillowPlayerController,
    slide_data: PlayerSlideState,
    delta_time: float,
) -> None:
    """Calculate the new speed of the player, has to be called every frame. Server Side only!"""
    # z_diff is the height difference between the current frame and the last frame in cm (Unreal units)
    z_diff: float = pc.Pawn.Location.Z - slide_data.old_z
    speed = slide_data.speed_pct
    # We generally want to slow down over time, but if we are going up a slope, we want to slow down even more
    # Slididng down a slope should slightly increase the speed
    if z_diff < 0:  # We are going down a slope
        speed -= z_diff * 0.0005
    else:  # We are going up a slope or on flat ground
        speed -= delta_time * 0.7 + z_diff * 0.004

    slide_data.old_z = pc.Pawn.Location.Z
    slide_data.speed_pct = speed

    # Heading and velocity are applied in the post hook, once PlayerMove can no longer overwrite
    # them. This function only owns the decay curve and deciding when the slide is spent.
    if speed < CROUCHED_PCT_DEFAULT:
        client_exit_slide(pc.PlayerReplicationInfo)


def can_slide(pc: WillowPlayerController, pawn: WillowPlayerPawn) -> bool:
    return OWN_SLIDE_STATE.is_sliding and bool(pc.bDuck) and pawn.IsOnGroundOrShortFall()


@hook("WillowGame.WillowPlayerInput:Jump")
def jump(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """If the client is sliding and wants to jump he stores his horizontal velocity and passes it later to the server"""
    if OWN_SLIDE_STATE.is_sliding:
        pc = cast("WillowPlayerController", obj.Outer)
        vel: Vector = Vector(pc.Pawn.Velocity)
        vel.z = 0
        State.horizontal_velocity = vel
        State.do_slide_jump = True


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove")
def handle_move(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)

    # We most likely wont pass our slide exit conditions after pressing jump,
    # as that will cause the player to stand up
    # So we need to do the slide jump as one of the first things.
    if State.do_slide_jump:
        if pawn.IsOnGroundOrShortFall():
            pawn.DoJump(True)
        else:
            server_set_slide_jump_velocity(State.horizontal_velocity.x, State.horizontal_velocity.y)
            State.do_slide_jump = False
            return

    # Check our exit conditions
    if not can_slide(pc, pawn):
        exit_slide(pc)
        return

    if not is_client():
        # We are sliding
        for player in CLIENTS_SLIDE_STATES.copy():
            if (_pc := player()) is None:
                CLIENTS_SLIDE_STATES.pop(player)
            else:
                state = CLIENTS_SLIDE_STATES[player]
                slide(_pc, state, args.DeltaTime)
                if _pc == pc:
                    # Mirror our own speed back out, so the exit check below still sees it when
                    # we are the host and our state lives in the clients dict rather than ours.
                    OWN_SLIDE_STATE.speed_pct = state.speed_pct
    else:
        slide(pc, OWN_SLIDE_STATE, args.DeltaTime)

    # After actually sliding, check if we are still fast enough to slide
    if OWN_SLIDE_STATE.speed_pct < CROUCHED_PCT_DEFAULT:
        exit_slide(pc)


@hook("WillowGame.WillowPlayerController:PlayerWalking.PlayerMove", Type.POST)
def enforce_slide(
    obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """Reassert the slide once PlayerMove has finished recomputing movement from input."""
    if not OWN_SLIDE_STATE.is_sliding:
        return
    pc = cast("WillowPlayerController", obj)
    pawn = cast("WillowPlayerPawn", pc.Pawn)
    if pawn is None:
        return
    try:
        apply_slide_physics(pawn, OWN_SLIDE_STATE, args.DeltaTime)
    except Exception as ex:  # noqa: BLE001 - temporary diagnostics
        _dbg(f"POST FAILED {type(ex).__name__}: {ex}")


@hook("WillowGame.WillowPlayerInput:DuckPressed")
def handle_duck(
    obj: unreal.UObject,
    _args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """The Client should check itself if he wants to slide"""
    pc = cast("WillowPlayerController", obj.Outer)
    _dbg(f"DUCK sprinting={bool(pc.bInSprintState)}")
    if pc.bInSprintState:
        try:
            enter_slide(pc)
        except Exception as ex:  # noqa: BLE001 - temporary diagnostics
            _dbg(f"ENTER FAILED {type(ex).__name__}: {ex}")


mod = build_mod(
    hooks=[
        handle_move,
        enforce_slide,
        handle_duck,
        jump,
    ],
)

add_network_functions(mod)
