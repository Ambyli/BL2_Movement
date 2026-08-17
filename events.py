"""Observer lists connecting the slide lifecycle to everything that reacts to it.

This is what keeps presentation out of the movement code. `lifecycle` fires these; the view model,
HUD, audio and pose modules subscribe in `__init__`. Nothing on the firing side ever imports the
listening side, so features stay additive - a new one is a new module plus a line of wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .debug import dbg

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
    """
    for callback in list(callbacks):
        try:
            callback(pc)
        except Exception as ex:  # noqa: BLE001 - isolation is the whole point
            dbg(f"EVENT FAILED {getattr(callback, '__qualname__', callback)}: {type(ex).__name__}: {ex}")
