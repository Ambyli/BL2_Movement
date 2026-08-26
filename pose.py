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

from typing import TYPE_CHECKING, Any

from tweens import Tween, cubic_in_out, cubic_out
from unrealsdk.unreal import WeakPointer

from .anim import SAFE_PAUSE, field_sink
from .constants import SLIDE_ANIM_RATE, SLIDE_LEAN_PITCH, SLIDE_LEAN_ROLL
from .debug import log

if TYPE_CHECKING:
    from collections.abc import Callable

    from common import WillowPlayerPawn

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


def _body_mesh(pawn: WillowPlayerPawn) -> Any:
    """The body's skeletal mesh from a live pawn, or None if it (or the pawn) is gone.

    The sinks call this every frame off a weak pointer, so it must tolerate the pawn having been torn
    down since the tween started. Mirrors `viewmodel._arms_mesh`.
    """
    return getattr(pawn, "Mesh", None)


def _lean(
    pawn: WillowPlayerPawn,
    mesh: Any,
    key: int,
    roll: int,
    pitch: int,
    duration: float,
    ease: Callable[[float], float],
) -> None:
    """Start a parallel Roll+Pitch lean toward (roll, pitch) over `duration`, tracked under `key`.

    Writes go through weak-pointer-guarded sinks (`anim.field_sink`), so the tween is safe to outlive
    the pawn. `mesh` is the caller's already-resolved, currently-live mesh, used only to read the
    starting angles here and now; the sinks re-resolve the live mesh every frame. Same helpers, and
    same reasoning, as the arm tweens in `viewmodel`.
    """
    ref = WeakPointer(pawn)
    current = mesh.Rotation
    tween = Tween()
    tween.tween_callable(
        field_sink(ref, _body_mesh, "Rotation", "Roll"),
        start_value=int(current.Roll),
        final_value=roll,
        duration=duration,
    ).transition(ease)
    tween.tween_callable(
        field_sink(ref, _body_mesh, "Rotation", "Pitch"),
        start_value=int(current.Pitch),
        final_value=pitch,
        duration=duration,
    ).transition(ease)
    tween.set_parallel(True)
    # Guard the per-frame pause read so a lean still animating into a zone load does not fault on the
    # tween library's default `get_pc().IsPaused()` gate (None mid-load). See `anim.SAFE_PAUSE`.
    tween.pause_while(SAFE_PAUSE)
    tween.start()
    _tweens[key] = tween


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
    _lean(pawn, mesh, key, roll=SLIDE_LEAN_ROLL, pitch=SLIDE_LEAN_PITCH, duration=0.25, ease=cubic_out)
    # Freeze the engine's skeletal animation so the legs stop the crouch-walk shuffle while sliding.
    # The lean above is a Python tween on the component transform, not engine anim, so it still plays.
    # Restored in on_pose_end. Note this only *stops* the legs in their entry pose - it does not splay
    # them into a slide; a real legs-out pose needs an animation BL2 does not have (see the plan doc).
    mesh.GlobalAnimRateScale = SLIDE_ANIM_RATE
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
    # Un-freeze the skeletal animation (1.0 is the engine default) so the legs move normally again.
    mesh.GlobalAnimRateScale = 1.0
    _kill(key)
    _lean(pawn, mesh, key, roll=0, pitch=0, duration=0.3, ease=cubic_in_out)
    log.info("pose.on_pose_end exit reason=settling")
