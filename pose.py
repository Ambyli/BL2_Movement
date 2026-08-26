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
from unrealsdk.unreal import WeakPointer

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


def _rotation_sink(ref: WeakPointer, field: str) -> Callable[[float], None]:
    """A tween sink that writes one field (Roll/Pitch) of the pawn's mesh Rotation each frame.

    This is the crash fix. The old code handed the tween `mesh.Rotation` directly - a struct view
    backed by the component's native memory - and the tween then wrote into that captured view every
    frame for the whole 0.25-0.3s lean. A pawn destroyed mid-lean (death, respawn, level change, or a
    remote proxy leaving relevance in co-op) freed that memory while the tween kept writing to it: an
    access violation in the SDK, on every machine, since the pose is broadcast.

    So the tween never holds the struct now. This sink re-resolves the pawn from a weak pointer every
    frame and does nothing once it is gone, then reads a fresh Rotation off a still-live mesh for each
    write. Same weak-pointer re-resolve `_drive_slide` and the effects loop already use for their own
    per-frame pawn access.
    """

    def sink(value: float) -> None:
        pawn = ref()
        if pawn is None:
            return
        mesh = getattr(pawn, "Mesh", None)
        if mesh is None:
            return
        rotation = mesh.Rotation
        setattr(rotation, field, int(value))
        # Assign the whole struct back rather than trusting the read to be a live view - correct
        # whether the SDK handed back a view or a copy, and the two parallel sinks each re-read first
        # so they never clobber each other's field.
        mesh.Rotation = rotation

    return sink


def _lean(
    pawn: WillowPlayerPawn,
    mesh: object,
    key: int,
    roll: int,
    pitch: int,
    duration: float,
    ease: Callable[[float], float],
) -> None:
    """Start a parallel Roll+Pitch lean toward (roll, pitch) over `duration`, tracked under `key`.

    Writes go through weak-pointer-guarded sinks (see `_rotation_sink`), so the tween is safe to
    outlive the pawn. `mesh` is the caller's already-resolved, currently-live mesh, used only to read
    the starting angles here and now; the tween itself never captures it.
    """
    ref = WeakPointer(pawn)
    current = mesh.Rotation
    tween = Tween()
    tween.tween_callable(
        _rotation_sink(ref, "Roll"),
        start_value=int(current.Roll),
        final_value=roll,
        duration=duration,
    ).transition(ease)
    tween.tween_callable(
        _rotation_sink(ref, "Pitch"),
        start_value=int(current.Pitch),
        final_value=pitch,
        duration=duration,
    ).transition(ease)
    tween.set_parallel(True)
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
