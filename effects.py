"""Slide effects, assembled from a phase "recipe": an entry hit, then an ongoing sound and dust trail.

Each effect is a small callable that plays one instance on a pawn; the factories below - `akevent`,
`impact`, `particle` - build them, each wrapping the one engine call it needs. A `Recipe` just wires
which effect fires when, so the loop is mechanism-agnostic: swapping a slide's sound or dust is a
one-line change in `CURRENT`, not a change to the driver.

Driven by the pose events, which fire on every machine for every sliding pawn (see
`lifecycle.net_slide_pose`), so each machine plays its own copy positioned at the pawn - the effects
the slide's anim-freeze otherwise suppresses. Non-replicated throughout, since every machine already
fires its own.

No game state, only reacts to events, like `viewmodel` / `pose`. Runs on: BOTH (every machine).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from coroutines import Time, WaitWhile, start_coroutine_tick
from mods_base import ENGINE
from unrealsdk import find_object, make_struct
from unrealsdk.unreal import WeakPointer

from .constants import SLIDE_DUST_DROP, SLIDE_DUST_FORWARD, SLIDE_FX_TICK
from .debug import log

if TYPE_CHECKING:
    from collections.abc import Callable

    from common import WillowPlayerPawn
    from coroutines import TickCoroutine

    Effect = Callable[[WillowPlayerPawn], None]

_objects: dict[str, object] = {}
"""Cache of resolved game objects (impact defs, AkEvents, particle systems) by object path. Only
successful lookups are cached, so an asset not loaded yet is retried rather than stuck at None."""


def _resolve(cls_name: str, path: str) -> object | None:
    """Resolve (and cache) a game object by class + object path, or None if it is not loaded."""
    cached = _objects.get(path)
    if cached is not None:
        return cached
    try:
        obj = find_object(cls_name, path)
    except Exception as ex:  # noqa: BLE001 - a missing asset is not fatal, just no effect
        log.warning(
            f"effects asset not found {cls_name} {path}: {type(ex).__name__}: {ex}"
        )
        return None
    if obj is not None:
        _objects[path] = obj
    return obj


def _emitter_pool() -> object | None:
    """The world's pooled particle spawner, or None mid-load/transition."""
    try:
        return ENGINE.GetCurrentWorldInfo().MyEmitterPool
    except Exception:  # noqa: BLE001 - no world mid-transition
        return None


# --- effect factories: each returns a callable that plays one instance on a pawn --------------------


def akevent(path: str) -> Effect:
    """A Wwise event (e.g. a scrape / whoosh), posted at the pawn."""

    def play(pawn: WillowPlayerPawn) -> None:
        event = _resolve("AkEvent", path)
        if event is None:
            return
        try:
            pawn.PostAkEvent(event, True, False)
        except Exception as ex:  # noqa: BLE001 - a failed effect must never break the slide
            log.warning(f"effects akevent failed {type(ex).__name__}: {ex}")

    return play


def impact(path: str) -> Effect:
    """A material impact (dust + sound), resolved to the surface by the engine."""

    def play(pawn: WillowPlayerPawn) -> None:
        definition = _resolve("WillowImpactDefinition", path)
        if definition is None:
            return
        try:
            pawn.PlayFootImpactEffect(definition, 1, True, False)
        except Exception as ex:  # noqa: BLE001 - a failed effect must never break the slide
            log.warning(f"effects impact failed {type(ex).__name__}: {ex}")

    return play


def particle(path: str) -> Effect:
    """A particle system spawned just ahead of the pawn, at foot height, through the emitter pool.

    Direct `SpawnEmitter`, because the impact call's own particle does not render here.
    """

    def play(pawn: WillowPlayerPawn) -> None:
        template = _resolve("ParticleSystem", path)
        if template is None:
            return
        pool = _emitter_pool()
        if pool is None:
            return
        # Forward is the travel direction, falling back to facing when nearly stopped, so the effect
        # kicks up just ahead of the pawn.
        origin = pawn.Location
        vel = pawn.Velocity
        speed = math.hypot(vel.X, vel.Y)
        if speed > 1.0:
            fwd_x, fwd_y = vel.X / speed, vel.Y / speed
        else:
            yaw = float(pawn.Rotation.Yaw) * (math.tau / 65536.0)
            fwd_x, fwd_y = math.cos(yaw), math.sin(yaw)
        where = make_struct(
            "Vector",
            X=origin.X + fwd_x * SLIDE_DUST_FORWARD,
            Y=origin.Y + fwd_y * SLIDE_DUST_FORWARD,
            Z=origin.Z - SLIDE_DUST_DROP,
        )
        try:
            pool.SpawnEmitter(template, where)
        except Exception as ex:  # noqa: BLE001 - a failed effect must never break the slide
            log.warning(f"effects particle failed {type(ex).__name__}: {ex}")

    return play


@dataclass(frozen=True)
class Recipe:
    """What a slide's effects are: a one-shot on entry, then a looping sound and dust, each paced.

    `enter`, `loop_sound` and `loop_dust` are effect callables from the factories above (or None to
    skip). `loop_sound` fires every `loop_sound_interval` seconds; `loop_dust` every
    `loop_dust_interval`. Swap `CURRENT` to change the slide.
    """

    enter: Effect | None = None
    loop_sound: Effect | None = None
    loop_sound_interval: float = 0.6
    loop_dust: Effect | None = None
    loop_dust_interval: float = 0.15


CURRENT = Recipe(
    enter=impact("GD_Impacts.Footsteps_Land.Footsteps_Land_Common"),
    loop_sound=akevent("Ake_Mon_Loader.Loader_Shared.Ak_Play_Mon_Loader_Slides"),
    loop_sound_interval=1.2,
    loop_dust=particle("FX_BulletImpacts_Dirt.Particles.Part_Impact_Dirt_LargeCal"),
    loop_dust_interval=0.15,
)

_active: dict[int, WeakPointer] = {}
"""PlayerID -> weak pointer to the sliding pawn whose loop is running.

Membership is the run flag: the loop coroutine runs while its key is present and exits once `on_end`
removes it. Weak so a pawn that disconnects mid-slide takes its loop with it. Keyed by PlayerID so the
host can run several at once and start/stop find the same entry.
"""


def _key(pawn: WillowPlayerPawn) -> int | None:
    pri = getattr(pawn, "PlayerReplicationInfo", None)
    return None if pri is None else int(pri.PlayerID)


def _wait(seconds: float) -> WaitWhile:
    """A WaitWhile that yields until `seconds` of game time have elapsed."""
    acc = [0.0]

    def waiting() -> bool:
        acc[0] += Time.delta_time
        return acc[0] < seconds

    return WaitWhile(waiting)


def _effects_loop(key: int, ref: WeakPointer) -> TickCoroutine:
    """Fire the recipe's ongoing dust and sound, each on its own interval, until `on_end` clears key."""
    # Accumulators so the dust and the sound each fire on their own interval off one fine base tick.
    # Seeded high so both fire on the first tick.
    since_sound = 1.0e9
    since_dust = 1.0e9
    while key in _active:
        pawn = ref()
        if pawn is None:
            break
        since_sound += SLIDE_FX_TICK
        since_dust += SLIDE_FX_TICK

        if CURRENT.loop_dust is not None and since_dust >= CURRENT.loop_dust_interval:
            CURRENT.loop_dust(pawn)
            since_dust = 0.0

        if (
            CURRENT.loop_sound is not None
            and since_sound >= CURRENT.loop_sound_interval
        ):
            CURRENT.loop_sound(pawn)
            since_sound = 0.0

        yield _wait(SLIDE_FX_TICK)
    _active.pop(key, None)
    log.info(f"effects._effects_loop exit key={key}")


def on_start(pawn: WillowPlayerPawn) -> None:
    """Fire the recipe's entry effect, then start the ongoing loop. Subscribed to `events.pose_started`."""
    key = _key(pawn)
    log.info(f"effects.on_start enter key={key}")
    if key is None or key in _active:
        log.info(
            f"effects.on_start exit reason={'no_player_id' if key is None else 'already_running'}"
        )
        return
    if CURRENT.enter is not None:
        CURRENT.enter(pawn)
    ref = WeakPointer(pawn)
    _active[key] = ref
    start_coroutine_tick(_effects_loop(key, ref))
    log.info("effects.on_start exit reason=started")


def on_end(pawn: WillowPlayerPawn) -> None:
    """Stop the ongoing loop. Subscribed to `events.pose_ended`."""
    key = _key(pawn)
    log.info(f"effects.on_end enter key={key}")
    if key is not None:
        # Drop the key; the loop coroutine sees it gone and exits on its next tick.
        _active.pop(key, None)
    log.info("effects.on_end exit")
