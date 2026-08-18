"""Temporary diagnostics.

Deleted wholesale in the cleanup phase. Everything here writes to a bounded file in the user's
home directory rather than the console, because the useful signal is a per-frame trace that has to
survive the game being closed.
"""

from __future__ import annotations

from pathlib import Path

DEBUG_LOG: bool = True
LOG_PATH = Path.home() / "bl2_slide_debug.log"
MAX_LINES: int = 4000

_count: int = 0
_suppressed: int = 0


def note_suppressed() -> None:
    """Count one dropped server correction."""
    global _suppressed  # noqa: PLW0603 - module-level counter is the point
    _suppressed += 1


def suppressed_count() -> int:
    """How many corrections have been dropped since the count was last reset."""
    return _suppressed


_adopted: int = 0
_worst_gap: float = 0.0


def note_adopted(gap: float) -> None:
    """Count one position adopted from a client, and remember the largest gap closed."""
    global _adopted, _worst_gap  # noqa: PLW0603 - module-level counters are the point
    _adopted += 1
    _worst_gap = max(_worst_gap, gap)


def adopted_stats() -> tuple[int, float]:
    """Positions adopted and the worst gap, since the last reset."""
    return _adopted, _worst_gap


def reset_suppressed() -> None:
    """Zero the count, so each slide reports its own figure rather than a running total."""
    # Only the suppression count is per-slide. Adoption is about *other* players' slides, so
    # zeroing it when our own slide starts reported 0 every time while it was actually working.
    global _suppressed  # noqa: PLW0603 - module-level counter is the point
    _suppressed = 0


def dbg(msg: str) -> None:
    """Append a bounded diagnostic line. Silently gives up rather than ever raising at a call site."""
    global _count  # noqa: PLW0603 - module-level counter is the point
    if not DEBUG_LOG or _count >= MAX_LINES:
        return
    _count += 1
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{_count:04d} {msg}\n")
    except OSError:
        pass
