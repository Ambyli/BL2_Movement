"""Feature-agnostic gates: predicates that open on trust and confirm a beat later.

Some state is established over a fast channel - an RPC, a local key press - before the signal that
*confirms* it arrives over a slower one, such as replication. A gate built here stays open through a
short grace window until its predicate is first seen true, then latches and defers to it. Nothing
here knows about sliding; any feature with the same open-early / confirm-late shape can reuse it.

Runs on: BOTH. The latch is a no-op on whichever machine already holds the confirming signal locally,
where the wrapped predicate passes on the very first frame.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable, Protocol


class Confirmable(Protocol):
    """The per-subject state an `optimistic` gate latches against.

    A feature reusing `optimistic` gives its own state object these two fields; the gate keeps its
    latch here rather than in a registry, so the subject's own lifetime is what cleans it up.
    """

    armed: bool
    """False until the wrapped predicate is first seen true for this subject; the gate then defers to
    the predicate rather than the grace window."""
    elapsed: float
    """Seconds since the subject opened. Bounds the pre-confirmation grace."""


def optimistic(
    grace: float,
    holder: Callable[..., Confirmable] = lambda *args: args[-1],
) -> Callable[[Callable[..., bool]], Callable[..., bool]]:
    """Turn a pure "is the confirming signal present?" predicate into an optimistic, latching gate.

    Until the predicate first returns true for a subject, a false result within `grace` seconds is
    read as "not arrived yet" and the gate stays open; after that first true it latches (`armed`) and
    the predicate is authoritative from then on. `holder(*args)` selects the `Confirmable` carrying
    the per-subject latch - the last positional argument by default, which is where per-subject state
    conventionally sits in this codebase.

    The latch lives on the subject the caller already owns, so there is nothing to register or clean
    up and two subjects never share one.
    """

    def decorate(pred: Callable[..., bool]) -> Callable[..., bool]:
        @wraps(pred)
        def gate(*args: object, **kwargs: object) -> bool:
            subject = holder(*args)
            if pred(*args, **kwargs):
                subject.armed = True
                return True
            # Predicate says no. Before the first confirmation that is only ever "the signal has not
            # landed yet", so stay open through the grace; after it, or once the window lapses, close.
            return not subject.armed and subject.elapsed < grace

        return gate

    return decorate


__all__ = ["Confirmable", "optimistic"]
