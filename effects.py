"""Material-aware slide audio, assembled from a phase "recipe": an entry impact, then an ongoing voice.

BL2's own footstep/impact calls resolve the floor material themselves, and its Wwise `AkEvent`s carry
ready-made scrape sounds, so we just fire them at the right moments - no manual trace (which
`pawn.Trace` wouldn't collide with anyway). A `Recipe` says what plays when:

- **enter** - a one-shot `WillowImpactDefinition` fired through `PlayFootImpactEffect` as the player
  drops into the slide (a landing thud - `Footsteps_Land_Common`). Material-correct dust + sound.
- **loop** - the ongoing voice while the slide runs. Preferably an `AkEvent` (a real slide scrape/
  whoosh) re-posted on `loop_interval` to sustain; otherwise an impact def or the plain
  `PlayFootStepSound` on the mod-menu cadence.

Add phases (exit, etc.) by extending `Recipe` and the driver below. Driven by the pose events, which
fire on every machine for every sliding pawn (see `lifecycle.net_slide_pose`), so each machine plays
its own copy positioned at the pawn - exactly how BL2 plays footsteps, which the anim-freeze
otherwise suppresses. Non-replicated throughout, since every machine already fires its own.

No game state, only reacts to events, like `viewmodel` / `pose`. Runs on: BOTH (every machine).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from coroutines import Time, WaitWhile, start_coroutine_tick
from unrealsdk import find_object
from unrealsdk.unreal import WeakPointer

from . import config
from .debug import log

if TYPE_CHECKING:
    from common import WillowPlayerPawn
    from coroutines import TickCoroutine


@dataclass(frozen=True)
class Recipe:
    """What a slide sounds like, phase by phase.

    `enter_impact` / `loop_impact` are `WillowImpactDefinition` object paths (fired via
    `PlayFootImpactEffect`, material-correct dust + sound). `loop_akevent` is an `AkEvent` object path
    (a Wwise scrape/whoosh, posted via `PostAkEvent`), re-posted every `loop_interval` seconds so a
    short clip sustains across the slide - this is the preferred ongoing voice. Falls back to
    `loop_impact`, then to the plain `PlayFootStepSound` cadence when `loop_footstep` is True. None
    skips a phase. Define alternates and swap `CURRENT` to change the slide's voice.
    """

    enter_impact: str | None = None
    loop_akevent: str | None = None
    loop_interval: float = 0.6
    loop_impact: str | None = None
    loop_footstep: bool = False


CURRENT = Recipe(
    enter_impact="GD_Impacts.Footsteps_Land.Footsteps_Land_Common",
    loop_akevent="Ake_Mon_Loader.Loader_Shared.Ak_Play_Mon_Loader_Slides",
    loop_interval=0.6,
)

_active: dict[int, WeakPointer] = {}
"""PlayerID -> weak pointer to the sliding pawn whose loop is running.

Membership is the run flag: the loop coroutine runs while its key is present and exits once `on_end`
removes it. Weak so a pawn that disconnects mid-slide takes its loop with it. Keyed by PlayerID so the
host can run several at once and start/stop find the same entry.
"""

_objects: dict[str, object] = {}
"""Cache of resolved game objects (impact defs, AkEvents) by object path. Only successful lookups are
cached, so an asset not loaded yet is retried rather than stuck at None."""


def _resolve(cls_name: str, path: str) -> object | None:
    """Resolve (and cache) a game object by class + object path, or None if it is not loaded."""
    cached = _objects.get(path)
    if cached is not None:
        return cached
    try:
        obj = find_object(cls_name, path)
    except Exception as ex:  # noqa: BLE001 - a missing asset is not fatal, just no sound
        log.warning(f"effects asset not found {cls_name} {path}: {type(ex).__name__}: {ex}")
        return None
    if obj is not None:
        _objects[path] = obj
    return obj


def _play_impact(pawn: WillowPlayerPawn, path: str, foot: int) -> None:
    """Fire one material impact (dust + sound). bReplicate=False - each machine fires its own."""
    definition = _resolve("WillowImpactDefinition", path)
    if definition is None:
        return
    try:
        pawn.PlayFootImpactEffect(definition, foot, True, False)
    except Exception as ex:  # noqa: BLE001 - a failed effect must never break the slide
        log.warning(f"effects PlayFootImpactEffect failed {type(ex).__name__}: {ex}")


def _post_akevent(pawn: WillowPlayerPawn, path: str) -> None:
    """Post a Wwise AkEvent at the pawn. bStopWhenOwnerDestroyed=True so it dies with the pawn."""
    event = _resolve("AkEvent", path)
    if event is None:
        return
    try:
        pawn.PostAkEvent(event, True, False)
    except Exception as ex:  # noqa: BLE001 - a failed sound must never break the slide
        log.warning(f"effects PostAkEvent failed {type(ex).__name__}: {ex}")


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


def _step_loop(key: int, ref: WeakPointer) -> TickCoroutine:
    """Play the recipe's ongoing voice on its cadence, until `on_end` clears the key."""
    foot = 0
    while key in _active:
        pawn = ref()
        if pawn is None:
            break
        interval = config.step_interval.value
        if CURRENT.loop_akevent is not None:
            _post_akevent(pawn, CURRENT.loop_akevent)
            interval = CURRENT.loop_interval
        elif CURRENT.loop_impact is not None:
            _play_impact(pawn, CURRENT.loop_impact, foot)
            foot ^= 1
        elif CURRENT.loop_footstep:
            # Plain footstep scuff, stacked for a little volume (the call takes no volume). bFirstPerson
            # False is the positional world footstep; BL2 resolves the surface material inside it.
            for _ in range(int(config.step_stack.value)):
                try:
                    pawn.PlayFootStepSound(foot, False)
                except Exception as ex:  # noqa: BLE001 - a failed sound must never break the cadence
                    log.warning(f"effects PlayFootStepSound failed {type(ex).__name__}: {ex}")
                foot ^= 1
        yield _wait(interval)
    _active.pop(key, None)
    log.info(f"effects._step_loop exit key={key}")


def on_start(pawn: WillowPlayerPawn) -> None:
    """Fire the recipe's entry impact, then start the ongoing voice. Subscribed to `events.pose_started`."""
    key = _key(pawn)
    log.info(f"effects.on_start enter key={key}")
    if key is None or key in _active:
        log.info(f"effects.on_start exit reason={'no_player_id' if key is None else 'already_running'}")
        return
    if CURRENT.enter_impact is not None:
        _play_impact(pawn, CURRENT.enter_impact, 1)
    ref = WeakPointer(pawn)
    _active[key] = ref
    start_coroutine_tick(_step_loop(key, ref))
    log.info("effects.on_start exit reason=started")


def on_end(pawn: WillowPlayerPawn) -> None:
    """Stop the ongoing voice. Subscribed to `events.pose_ended`."""
    key = _key(pawn)
    log.info(f"effects.on_end enter key={key}")
    if key is not None:
        # Drop the key; the loop coroutine sees it gone and exits on its next tick.
        _active.pop(key, None)
    log.info("effects.on_end exit")
