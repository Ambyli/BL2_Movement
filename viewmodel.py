"""First-person view model - the gun dipping and rolling as you drop into a slide.

BL2 has no slide animation, so this is faked entirely by tweening the arms mesh origin and
rotation. Inherited from upstream unchanged; only the entry points are new.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from tweens import Tween, circ_out, cubic_in_out, cubic_out, elastic_out, quad_out

if TYPE_CHECKING:
    from common import WillowPlayerController


class _View:
    tweener: ClassVar[Tween] = Tween()


def on_start(pc: WillowPlayerController) -> None:
    """Dip and roll the weapon into the slide pose."""
    if _View.tweener.is_running():
        _View.tweener.kill()
    arms = pc.Pawn.Arms
    if not arms.Attachments:
        return
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


def on_end(pc: WillowPlayerController) -> None:
    """Return the weapon to its resting pose."""
    if _View.tweener.is_running():
        _View.tweener.kill()
    arms = pc.Pawn.Arms
    if not arms.Attachments:
        return
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
