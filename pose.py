"""Third-person body pose - leaning a sliding pawn's body into the slide, on every machine.

Where `viewmodel` dips the first-person arms for the local player only, this leans the third-person
body mesh so *other* players see the slide. It is driven by the pose events, which the host
broadcasts for every slide (see `lifecycle.net_slide_pose`), so one player's slide leans their body
on every screen - the owning player included, though they are in first person and will not see it.

Rotation only. Phase 0 found the body mesh's component `Rotation` persists frame to frame, but its
`Translation` is engine-managed (crouch height) and gets undone. The slide already forces crouch on
all machines, so the legs read as a low slide; this leans the torso on top of that. No game state,
only reacts to events - like `viewmodel`.

Runs on: BOTH (every machine), once per slide per machine, for whichever pawn the event names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tweens import Tween, cubic_in_out, cubic_out

from .debug import log

if TYPE_CHECKING:
    from common import WillowPlayerPawn

# The slide lean, in Unreal rotation units (65536 == 360 degrees). Roll tips the body sideways into
# the slide; Pitch leans it back a touch. Starting values - tune in game, then promote to constants
# or mod-menu sliders once the look is settled.
_LEAN_ROLL: int = 6000
_LEAN_PITCH: int = -1500

_tweens: dict[int, Tween] = {}
"""One tween per sliding pawn, keyed by PlayerID.

Unlike the single first-person view, the host can have several bodies leaning at once, so a lone
module-level tween like `viewmodel`'s would not do. Keyed by PlayerID rather than the pawn object so
the entry survives the pawn being swapped under us (a level change replaces the object; the id
survives), and so start and end find the same entry.
"""


def _key(pawn: WillowPlayerPawn) -> int | None:
    """The PlayerID to track this pawn's lean under, or None if it carries no replication info."""
    pri = getattr(pawn, "PlayerReplicationInfo", None)
    return None if pri is None else int(pri.PlayerID)


def _kill(key: int) -> None:
    """Stop and drop any lean still animating for this player, so a new one starts clean."""
    tween = _tweens.pop(key, None)
    if tween is not None and tween.is_running():
        tween.kill()


def on_pose_start(pawn: WillowPlayerPawn) -> None:
    """Lean the body into the slide. Subscribed to `events.pose_started` in `__init__`."""
    key = _key(pawn)
    log.info(f"pose.on_pose_start enter key={key}")
    if key is None:
        log.info("pose.on_pose_start exit reason=no_player_id")
        return
    mesh = getattr(pawn, "Mesh", None)
    if mesh is None:
        log.info("pose.on_pose_start exit reason=no_mesh")
        return
    # Kill a lean still running from an interrupted previous slide before starting a fresh one, or the
    # two interpolators fight over the same Rotation channels.
    _kill(key)
    tween = Tween()
    tween.tween_property(mesh.Rotation, "Roll", final_value=_LEAN_ROLL, duration=0.25).from_current().transition(
        cubic_out,
    )
    tween.tween_property(mesh.Rotation, "Pitch", final_value=_LEAN_PITCH, duration=0.25).from_current().transition(
        cubic_out,
    )
    tween.set_parallel(True)
    tween.start()
    _tweens[key] = tween
    log.info("pose.on_pose_start exit reason=leaning")


def on_pose_end(pawn: WillowPlayerPawn) -> None:
    """Return the body to upright. Subscribed to `events.pose_ended` in `__init__`."""
    key = _key(pawn)
    log.info(f"pose.on_pose_end enter key={key}")
    if key is None:
        log.info("pose.on_pose_end exit reason=no_player_id")
        return
    mesh = getattr(pawn, "Mesh", None)
    if mesh is None:
        # No mesh left to settle (respawn, level change), but still drop the tracked tween so the dict
        # does not hold a dead key.
        _kill(key)
        log.info("pose.on_pose_end exit reason=no_mesh")
        return
    _kill(key)
    tween = Tween()
    tween.tween_property(mesh.Rotation, "Roll", final_value=0, duration=0.3).from_current().transition(
        cubic_in_out,
    )
    tween.tween_property(mesh.Rotation, "Pitch", final_value=0, duration=0.3).from_current().transition(
        cubic_in_out,
    )
    tween.set_parallel(True)
    tween.start()
    _tweens[key] = tween
    log.info("pose.on_pose_end exit reason=settling")
