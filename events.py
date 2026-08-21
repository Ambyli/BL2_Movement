"""Observer lists connecting the slide lifecycle to everything that reacts to it.

This is what keeps presentation out of the movement code. `lifecycle` fires these; the view model,
HUD, audio and pose modules subscribe in `__init__`. Nothing on the firing side ever imports the
listening side, so features stay additive - a new one is a new module plus a line of wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .debug import log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from common import WillowPlayerController, WillowPlayerPawn

    SlideCallback = Callable[[WillowPlayerController], None]
    PoseCallback = Callable[[WillowPlayerPawn], None]

# First-person, owning-machine signals: fired for the local player, carrying the controller. The view
# model dips the arms off these.
slide_started: list[SlideCallback] = []
slide_ended: list[SlideCallback] = []

# Third-person, every-machine signals: the host broadcasts these for every slide it drives (see
# `lifecycle.net_slide_pose`), carrying the pawn, so a slide leans the body on every screen - not just
# the owning player's. The pose module leans the body mesh off these.
pose_started: list[PoseCallback] = []
pose_ended: list[PoseCallback] = []


def fire[Subject](callbacks: Iterable[Callable[[Subject], None]], subject: Subject) -> None:
    """Run every subscriber with `subject`, isolating failures.

    A listener that throws must not take the slide down with it - a broken HUD, view model or pose
    should cost you a cosmetic, not leave movement wedged half way through a state change.

    The subject is a controller for the `slide_started` / `slide_ended` (first-person) signals and a
    pawn for the `pose_started` / `pose_ended` (third-person) signals; `fire` only passes it through,
    so it does not care which. Runs on whichever machine fired the event - one machine for the slide
    signals, every machine for the pose ones.
    """
    # Iterate a snapshot copy so a subscriber that mutates the subscription list mid-fire (e.g.
    # unsubscribing itself) does not corrupt the loop.
    snapshot = list(callbacks)
    log.info(f"fire enter n_callbacks={len(snapshot)} subject={subject}")
    dispatched = 0
    for callback in snapshot:
        name = getattr(callback, "__qualname__", str(callback))
        log.debug(f"fire dispatch callback={name}")
        try:
            callback(subject)
            dispatched += 1
        except Exception as ex:  # noqa: BLE001 - isolation is the whole point
            # Log the failure with the callback name so we can tell which listener died. The rest
            # of the loop continues so one broken subscriber never blocks the others.
            log.warning(f"EVENT FAILED {name}: {type(ex).__name__}: {ex}")
    log.info(f"fire exit dispatched={dispatched}/{len(snapshot)}")
