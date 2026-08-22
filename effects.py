"""Material-aware slide audio - the game's own footstep sound, on a cadence, matched to the surface.

BL2 resolves the floor material inside `WillowPawn.PlayFootStepSound(FootDown, bFirstPerson)` and
plays the right sound, so we just call it repeatedly while a pawn slides and the slide scuffs along
the ground in the surface's own voice - no manual material trace (which `pawn.Trace` wouldn't collide
with anyway). Driven by the pose events, which fire on every machine for every sliding pawn (see
`lifecycle.net_slide_pose`), so each machine plays its own copy positioned at the pawn - exactly how
BL2 plays footsteps normally, which the slide's anim-freeze otherwise suppresses.

Sound only for now; the matching particle needs a resolved `ImpactDefinition` (deferred). No game
state, only reacts to events, like `viewmodel` / `pose`. Runs on: BOTH (every machine).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coroutines import Time, WaitWhile, start_coroutine_tick
from unrealsdk.unreal import WeakPointer

from .constants import SLIDE_STEP_INTERVAL
from .debug import log

if TYPE_CHECKING:
    from common import WillowPlayerPawn
    from coroutines import TickCoroutine

_active: dict[int, WeakPointer] = {}
"""PlayerID -> weak pointer to the sliding pawn whose footstep cadence is running.

Membership is the run flag: the cadence coroutine loops while its key is present and exits once
`on_end` removes it. Weak so a pawn that disconnects mid-slide takes its cadence with it. Keyed by
PlayerID (not the object) so the host can run several at once and start/stop find the same entry.
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


def _step_loop(key: int, ref: WeakPointer) -> TickCoroutine:
    """Play a footstep on the sliding pawn every `SLIDE_STEP_INTERVAL`, until `on_end` clears the key."""
    foot = 0
    while key in _active:
        pawn = ref()
        if pawn is None:
            break
        try:
            # bFirstPerson=False: the positional world footstep, so every player hears the slide at
            # the pawn. BL2 resolves the surface material inside this call.
            pawn.PlayFootStepSound(foot, False)
        except Exception as ex:  # noqa: BLE001 - a failed sound must never break the cadence
            log.warning(f"effects PlayFootStepSound failed {type(ex).__name__}: {ex}")
        foot ^= 1  # alternate feet for a little variety
        yield _wait(SLIDE_STEP_INTERVAL)
    _active.pop(key, None)
    log.info(f"effects._step_loop exit key={key}")


def on_start(pawn: WillowPlayerPawn) -> None:
    """Begin the footstep cadence for a sliding pawn. Subscribed to `events.pose_started`."""
    key = _key(pawn)
    log.info(f"effects.on_start enter key={key}")
    if key is None or key in _active:
        log.info(f"effects.on_start exit reason={'no_player_id' if key is None else 'already_running'}")
        return
    ref = WeakPointer(pawn)
    _active[key] = ref
    start_coroutine_tick(_step_loop(key, ref))
    log.info("effects.on_start exit reason=cadence_started")


def on_end(pawn: WillowPlayerPawn) -> None:
    """Stop the footstep cadence. Subscribed to `events.pose_ended`."""
    key = _key(pawn)
    log.info(f"effects.on_end enter key={key}")
    if key is not None:
        # Drop the key; the coroutine sees it gone and exits on its next tick.
        _active.pop(key, None)
    log.info("effects.on_end exit")
