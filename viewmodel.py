"""First-person view model - the gun dipping and rolling as you drop into a slide.

BL2 has no slide animation, so this is faked entirely by tweening the arms mesh origin and
rotation. Inherited from upstream unchanged; only the entry points are new.

The tweens write through weak-pointer-guarded sinks (`anim.field_sink`) rather than being handed the
arms mesh's `RotOrigin`/`Origin` structs directly: the slide-in tween runs for over a second, so a
zone load (or death/respawn) part-way through would otherwise leave it writing into freed native
memory - an access violation. Same fix, and same reason, as the body-lean tweens in `pose`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from tweens import Tween, circ_out, cubic_in_out, cubic_out, elastic_out, quad_out
from unrealsdk.unreal import WeakPointer

from .anim import SAFE_PAUSE, field_sink
from .debug import log

if TYPE_CHECKING:
    from common import WillowPlayerController, WillowPlayerPawn


class _View:
    tweener: ClassVar[Tween] = Tween()


def _arms_mesh(pawn: WillowPlayerPawn) -> Any:
    """The arms' skeletal mesh from a live pawn, or None if it (or the pawn) is gone.

    The sinks call this every frame off a weak pointer, so it must tolerate any link in the chain
    having been torn down since the tween started.
    """
    arms = getattr(pawn, "Arms", None)
    return None if arms is None else getattr(arms, "SkeletalMesh", None)


def on_start(pc: WillowPlayerController) -> None:
    """Dip and roll the weapon into the slide pose.

    Runs on: LOCAL MACHINE only (the one whose player is sliding). The Arms mesh is first-person
    and lives only on the owning client; on other machines this pawn has no Arms attachment and
    the early-return below catches that. Subscribed to `events.slide_started` in __init__.
    """
    log.info(f"viewmodel.on_start enter pc={pc} tween_running={_View.tweener.is_running()}")
    # Kill any tween still running from an interrupted previous slide - a slide that ends before
    # the pose animation finishes would otherwise leave overlapping tweens fighting each other.
    if _View.tweener.is_running():
        _View.tweener.kill()
        log.info("viewmodel.on_start killed prior tween")
    # Third-person pawns have no Arms mesh; on those, this callback is a no-op. Only the owning
    # client sees its own first-person arms. Guard the pawn too: a slide can end on the same frame
    # the pawn is torn down, and `pc.Pawn` is None then.
    pawn = getattr(pc, "Pawn", None)
    arms = None if pawn is None else getattr(pawn, "Arms", None)
    if arms is None or not arms.Attachments:
        log.info("viewmodel.on_start exit reason=no_arms_attachments")
        return
    sm = arms.SkeletalMesh
    ref = WeakPointer(pawn)
    # Build a fresh parallel tween. Each tween_callable call adds one channel through a guarded sink;
    # set_parallel(True) below makes them all animate concurrently rather than sequentially. `sm` is
    # only read here and now for the starting angles; the sinks re-resolve the live mesh every frame.
    _View.tweener = Tween()
    t = _View.tweener
    t.tween_callable(
        field_sink(ref, _arms_mesh, "RotOrigin", "Pitch"),
        start_value=int(sm.RotOrigin.Pitch),
        final_value=500,
        duration=0.2,
    ).transition(cubic_in_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "RotOrigin", "Yaw"),
        start_value=int(sm.RotOrigin.Yaw),
        final_value=-200,
        duration=0.4,
    ).transition(quad_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "RotOrigin", "Roll"),
        start_value=int(sm.RotOrigin.Roll),
        final_value=-6300,
        duration=0.5,
    ).transition(cubic_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "Origin", "X"),
        start_value=float(sm.Origin.X),
        final_value=30.0,
        duration=1.2,
    ).transition(elastic_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "Origin", "Y"),
        start_value=float(sm.Origin.Y),
        final_value=-14.5,
        duration=0.5,
    ).transition(circ_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "Origin", "Z"),
        start_value=float(sm.Origin.Z),
        final_value=-175.0,
        duration=0.5,
    ).transition(circ_out)
    t.set_parallel(True)
    t.pause_while(SAFE_PAUSE)
    t.start()
    log.info("viewmodel.on_start exit reason=started_slide_in_tween")


def on_end(pc: WillowPlayerController) -> None:
    """Return the weapon to its resting pose.

    Runs on: LOCAL MACHINE only. Subscribed to `events.slide_ended` in __init__.
    """
    log.info(f"viewmodel.on_end enter pc={pc} tween_running={_View.tweener.is_running()}")
    # Kill the slide-in tween if it's still running - we want to hand off cleanly to the
    # settle-out animation rather than have both interpolators writing to the same channels.
    if _View.tweener.is_running():
        _View.tweener.kill()
        log.info("viewmodel.on_end killed prior tween")
    # Same third-person / torn-down-pawn guard as on_start: nothing to animate if this pawn has no
    # Arms mesh, or no pawn at all (a slide ending as the pawn is destroyed).
    pawn = getattr(pc, "Pawn", None)
    arms = None if pawn is None else getattr(pawn, "Arms", None)
    if arms is None or not arms.Attachments:
        log.info("viewmodel.on_end exit reason=no_arms_attachments")
        return
    sm = arms.SkeletalMesh
    ref = WeakPointer(pawn)
    # New parallel tween, this time interpolating everything back to its resting pose.
    _View.tweener = Tween()
    t = _View.tweener
    t.tween_callable(
        field_sink(ref, _arms_mesh, "RotOrigin", "Pitch"),
        start_value=int(sm.RotOrigin.Pitch),
        final_value=0,
        duration=0.5,
    ).transition(cubic_in_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "RotOrigin", "Yaw"),
        start_value=int(sm.RotOrigin.Yaw),
        final_value=0,
        duration=0.4,
    ).transition(quad_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "RotOrigin", "Roll"),
        start_value=int(sm.RotOrigin.Roll),
        final_value=0,
        duration=0.3,
    ).transition(cubic_in_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "Origin", "X"),
        start_value=float(sm.Origin.X),
        final_value=40.0,
        duration=0.4,
    ).transition(circ_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "Origin", "Y"),
        start_value=float(sm.Origin.Y),
        final_value=0.0,
        duration=0.6,
    ).transition(circ_out)
    t.tween_callable(
        field_sink(ref, _arms_mesh, "Origin", "Z"),
        start_value=float(sm.Origin.Z),
        final_value=-167.0,
        duration=0.5,
    ).transition(circ_out)
    t.set_parallel(True)
    t.pause_while(SAFE_PAUSE)
    t.start()
    log.info("viewmodel.on_end exit reason=started_settle_out_tween")
