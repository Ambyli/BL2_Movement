"""Diagnostics.

Everything here writes to a bounded file in the user's home directory rather than the console,
because the useful signal is a per-frame trace that has to survive the game being closed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

DEBUG_LOG: bool = True
LOG_PATH = Path.home() / "bl2_slide_debug.log"
MAX_LINES: int = 4000

_count: int = 0


def _world_seconds() -> float | None:
    """Engine world time, or None when there is no world to read it from.

    Imported lazily on purpose. `state` does real SDK work at import time (`find_enum("ENetMode")`),
    and `debug` is the one module every other module imports - wiring them together at module level
    would mean a failure inside `state`'s import taking down the logging we would need to diagnose
    it. After the first call this is a `sys.modules` lookup, which is nothing next to the file open
    below.
    """
    try:
        from .state import world_time  # noqa: PLC0415 - deliberately lazy, see docstring

        return world_time()
    except Exception:  # noqa: BLE001 - no world mid-load or mid-transition
        return None


def stamp() -> str:
    """The shared line prefix: `[21:46:03.412 w=1234.567] `.

    Both clocks are here because neither alone does the job. The wall clock is the only value two
    machines share, so it is what lets a host's log and a client's log be read as one timeline -
    but their clocks can be skewed against each other by an unknown offset. World time is exact and
    is the clock the netcode itself speaks in: `TimeStamp=` on every ServerMove and
    ClientAdjustPosition in the discovery log is this value, so it is the only way to line a log
    line up against the packet that caused it. Wall clock aligns the two files; world time aligns
    each file against its own netcode.

    Never raises - `dbg` is called from inside game hooks, where an exception in the logging would
    take movement down with it.
    """
    try:
        wall = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    except Exception:  # noqa: BLE001 - a clock read must not cost us the line
        wall = "??:??:??.???"
    seconds = _world_seconds()
    world = "?" if seconds is None else f"{seconds:.3f}"
    return f"[{wall} w={world}] "


def dbg(msg: str) -> None:
    """Append a bounded diagnostic line. Silently gives up rather than ever raising at a call site."""
    global _count  # noqa: PLW0603 - module-level counter is the point
    if not DEBUG_LOG or _count >= MAX_LINES:
        return
    _count += 1
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp()}{_count:04d} {msg}\n")
    except OSError:
        pass
