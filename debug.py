"""Diagnostics.

Everything writes to a bounded file in the user's home directory via a RotatingFileHandler, so the
useful signal is a per-frame trace that survives the game being closed and rotates rather than
growing without bound.

Every line is prefixed with `[HH:MM:SS.mmm w=<world_seconds>] <L>` by a custom formatter:
- Wall clock is the only value two machines share, so it is what lets a host's log and a client's
  log be read as one timeline - but their clocks can be skewed against each other by an unknown
  offset.
- World time is exact and is the clock the netcode itself speaks in: `TimeStamp=` on every
  ServerMove and ClientAdjustPosition is this value, so it is the only way to line a log line up
  against the packet that caused it.
- `<L>` is the single-letter level marker (D/I/W/E), so a per-frame trace can be visually filtered
  from state transitions without scanning the whole line.
"""

from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_PATH: Path = Path.home() / "bl2_slide_debug.log"
"""Bounded log file. Rotates rather than truncates, so a session that generates more than one file's
worth still keeps the most recent lines."""

MAX_BYTES: int = 1_000_000
"""~1MB per file. A per-frame slide trace at ~60Hz is well within this for a single slide, and the
rotation makes overshoot recoverable rather than a hard cutoff."""

BACKUP_COUNT: int = 3
"""Kept alongside the live file, so a crash or a long session is not lost the moment we roll over."""


def _world_seconds() -> float | None:
    """Engine world time, or None when there is no world to read it from.

    Imported lazily on purpose. `state` does real SDK work at import time (`find_enum("ENetMode")`),
    and `debug` is the one module every other module imports - wiring them together at module level
    would mean a failure inside `state`'s import taking down the logging we would need to diagnose
    it. After the first call this is a `sys.modules` lookup, which is nothing next to the file open
    below.

    Deliberately does no logging of its own: the formatter calls this for every record, so a log
    line from here would recurse straight back into the formatter.
    """
    try:
        from .state import world_time  # noqa: PLC0415 - deliberately lazy, see docstring

        return world_time()
    except Exception:  # noqa: BLE001 - no world mid-load or mid-transition
        return None


class _StampFormatter(logging.Formatter):
    """Formatter that prepends the shared time stamp and a single-letter level marker.

    Never raises: this runs for every record, and an exception in a formatter takes the logger down
    for the rest of the session. Falls back to a `?` for either clock rather than dropping the line.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            wall = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        except Exception:  # noqa: BLE001 - a clock read must not cost us the line
            wall = "??:??:??.???"
        seconds = _world_seconds()
        world = "?" if seconds is None else f"{seconds:.3f}"
        return f"[{wall} w={world}] {record.levelname[0]} {record.getMessage()}"


def _make_logger() -> logging.Logger:
    """Configure and return the mod's logger.

    Idempotent: the mod menu reload path re-imports this module, and a second FileHandler on the
    same path would duplicate every line. The `if logger.handlers` guard reuses the handler already
    attached the first time we ran.
    """
    logger = logging.getLogger("bl2_slide")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    # `propagate=False` keeps our records off the root logger, which the SDK may have attached its
    # own handler to - without this, every line would also show up on the console.
    logger.propagate = False
    try:
        handler: logging.Handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(_StampFormatter())
    except OSError:
        # The log path may be unwritable (permission, disk, or a stale lock). Fall back to a null
        # handler so callers never raise from a `log.debug(...)`.
        handler = logging.NullHandler()
    logger.addHandler(handler)
    return logger


log: logging.Logger = _make_logger()
"""The mod's logger. Every module imports this and calls `log.debug` / `log.info` / etc. directly."""


_periodic: dict[str, int] = {}
"""Per-key call counters for `every_n`. Module-level so a function's throttle survives across
frames; keyed by a caller-chosen string so entry and exit logs of the same function share a counter
and stay aligned."""


def every_n(key: str, n: int = 30) -> bool:
    """True on 1 out of every `n` calls under `key`. Used to throttle per-frame log lines.

    A slide driver ticks at frame rate for every active slide, and logging entry and exit on every
    tick would drown the file in a few seconds. Callers capture the return value once per invocation
    and gate both their entry and exit logs on it, so a throttled function either logs the whole
    pair or logs nothing for that call.
    """
    count = _periodic.get(key, 0) + 1
    _periodic[key] = count
    return count % n == 0
