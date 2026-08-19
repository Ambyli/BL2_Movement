"""First-person view model - the gun dipping and rolling as you drop into a slide.

BL2 has no slide animation, so this is faked entirely by tweening the arms mesh origin and
rotation. Inherited from upstream unchanged; only the entry points are new.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from tweens import Tween, circ_out, cubic_in_out, cubic_out, elastic_out, quad_out

from .debug import log

if TYPE_CHECKING:
    from common import WillowPlayerController


class _View:
    tweener: ClassVar[Tween] = Tween()


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
    # client sees its own first-person arms.
    arms = pc.Pawn.Arms
    if not arms.Attachments:
        log.info("viewmodel.on_start exit reason=no_arms_attachments")
        return
    # Build a fresh parallel tween. Each tween_property call adds one channel; set_parallel(True)
    # below makes them all animate concurrently rather than sequentially.
    _View.tweener = Tween()
    t = _View.tweener
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
    # Same third-person guard as on_start: nothing to animate if this pawn has no Arms mesh.
    arms = pc.Pawn.Arms
    if not arms.Attachments:
        log.info("viewmodel.on_end exit reason=no_arms_attachments")
        return
    # New parallel tween, this time interpolating everything back to its resting pose.
    _View.tweener = Tween()
    t = _View.tweener
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
    log.info("viewmodel.on_end exit reason=started_settle_out_tween")
