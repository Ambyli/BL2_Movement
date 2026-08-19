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

    from common import WillowPlayerController

    SlideCallback = Callable[[WillowPlayerController], None]

slide_started: list[SlideCallback] = []
slide_ended: list[SlideCallback] = []


def fire(callbacks: Iterable[SlideCallback], pc: WillowPlayerController) -> None:
    """Run every subscriber, isolating failures.

    A listener that throws must not take the slide down with it - a broken HUD or a missing sound
    asset should cost you an icon, not leave movement wedged half way through a state change.

    Runs on: whichever machine fired the event - typically the one whose local player is
    sliding, since `slide_started` / `slide_ended` are fired from `lifecycle.enter_slide` /
    `exit_slide` which are per-machine.
    """
    # Iterate a snapshot copy so a subscriber that mutates the subscription list mid-fire (e.g.
    # unsubscribing itself) does not corrupt the loop.
    snapshot = list(callbacks)
    log.info(f"fire enter n_callbacks={len(snapshot)} pc={pc}")
    dispatched = 0
    for callback in snapshot:
        name = getattr(callback, "__qualname__", str(callback))
        log.debug(f"fire dispatch callback={name}")
        try:
            callback(pc)
            dispatched += 1
        except Exception as ex:  # noqa: BLE001 - isolation is the whole point
            # Log the failure with the callback name so we can tell which listener died. The rest
            # of the loop continues so one broken subscriber never blocks the others.
            log.warning(f"EVENT FAILED {name}: {type(ex).__name__}: {ex}")
    log.info(f"fire exit dispatched={dispatched}/{len(snapshot)}")
