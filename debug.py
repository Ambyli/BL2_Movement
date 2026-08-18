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
