"""Feature-agnostic gates: predicates that open on trust and confirm a beat later.

Some state is established over a fast channel - an RPC, a local key press - before the signal that
*confirms* it arrives over a slower one, such as replication. A gate built here stays open through a
short grace window until its predicate is first seen true, then latches and defers to it. Nothing
here knows about sliding; any feature with the same open-early / confirm-late shape can reuse it.

Runs on: BOTH. The latch is a no-op on whichever machine already holds the confirming signal locally,
where the wrapped predicate passes on the very first frame.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Protocol


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
    *,
    confirm: Callable[..., bool] | None = None,
    holder: Callable[..., Confirmable] = lambda *args: args[-1],
) -> Callable[[Callable[..., bool]], Callable[..., bool]]:
    """Turn a validity predicate into an optimistic, latching gate that tolerates a late signal.

    Two roles, deliberately kept apart. The wrapped predicate answers "is this valid right now?" and
    is authoritative once the gate has latched. `confirm` answers the narrower "has the late signal
    arrived?" - the one thing established over a fast channel and confirmed over a slow one - and is
    what latches `armed`. Until the gate latches, a failing predicate within `grace` seconds is read
    as "not arrived yet" and the gate stays open; after it, the predicate rules.

    Keeping the two apart matters when only *part* of the validity check is what replicates late:
    arming on the whole predicate would let an unrelated, always-local condition (being on the ground,
    say) gate the confirmation. Point `confirm` at just the late signal instead. It defaults to the
    wrapped predicate - arm on the first fully-valid frame - for features whose entire gate is the
    late signal.

    `holder(*args)` selects the `Confirmable` carrying the per-subject latch - the last positional
    argument by default. The latch lives on the subject the caller already owns, so there is nothing
    to register or clean up and two subjects never share one.
    """

    def decorate(pred: Callable[..., bool]) -> Callable[..., bool]:
        @wraps(pred)
        def gate(*args: object, **kwargs: object) -> bool:
            subject = holder(*args)
            if pred(*args, **kwargs):
                # Fully valid this frame - the strongest confirmation there is, so latch regardless of
                # what `confirm` would say.
                subject.armed = True
                return True
            # Predicate fails. Latch if the late signal has landed; with no `confirm` given, only a
            # valid frame ever arms. Until armed, a failure inside the grace is just "not arrived yet",
            # so hold the gate open.
            if confirm is not None and confirm(*args, **kwargs):
                subject.armed = True
            return not subject.armed and subject.elapsed < grace

        return gate

    return decorate


__all__ = ["Confirmable", "optimistic"]
