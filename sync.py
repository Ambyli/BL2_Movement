"""Latency-compensated per-tick input streaming.

Independent of slide logic. Handles the general problem of "one machine samples a value each
tick, ships it to another, receiver wants the value it should be applying *now* rather than the
one that was sampled a few network ticks ago" - the receiver catches up by applying an extra
number of ticks' worth of effect on the frame where the incoming value first changes.

## How it fits together

The owning machine calls `owner_send(input_x, input_y, tick)` after each of its driver ticks. The
`tick` is a per-slide counter that starts at 0 when the slide opens and increments once per
sample. The RPC ships that triple to the host.

On the host, the driver calls `host_read(player_id)` before physics. That returns the freshest
known `(input_x, input_y, catchup_ticks)` for that sender. `catchup_ticks` is 0 when the
incoming value has not changed since it was last consumed - "you have already caught up, apply
plain physics." It is `min(latest_tick - applied_tick, MAX_CATCHUP_TICKS)` on the frame where
the value transitions - "the value has moved on and you have to make up ground this frame."

Physics is expected to consume `catchup_ticks` by scaling the effective delta for whichever
input-driven step it applies (in this codebase: the steering lerp in `apply_slide_physics`).

## Why "new value only" and not "every tick"

Continuous catch-up (every frame extrapolates by the current latency) compounds. A stable
one-tick network offset would cause the receiver to overshoot the sender's position every
frame, then correct back, then overshoot again - a fixed drift traded for a jitter.

One-shot catch-up on value transitions closes the gap on the transition itself and leaves
steady-state input holding to run at plain per-tick cadence. Release is treated as a transition
too (`held` -> `released`), so the receiver's "stop steering" event fires with the same catch-up
that the "start steering" event did, and the residual error at slide end is bounded.

## Why a self-contained module

The catch-up rule is not slide-specific: any per-tick client-authoritative value the host has to
replay under latency would use the same shape. Keeping the RPC, the per-sender cache, and the
tick bookkeeping in one file lets the next feature that needs this (aim direction, jump
velocity, etc.) instantiate its own copy of the pattern without touching the slide code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from networking.decorators import host

from .debug import every_n, log
from .state import player_id, state_for

if TYPE_CHECKING:
    from common import WillowPlayerController


MAX_CATCHUP_TICKS: int = 3
"""Hard cap on how many ticks of extrapolation the host will apply in one frame.

A jitter spike or a run of dropped packets can push the receipt-vs-sample gap much higher than
the average latency; without a cap, the first packet after the gap would produce a visible
flick as the receiver "catches up" all at once. Capping at 3 ticks means the receiver may
undercompensate on a bad packet run rather than overcorrect, which reads as a small steady
drift rather than a sharp snap.
"""


@dataclass
class _SenderState:
    """Per-sender bookkeeping on the host.

    Not exported: consumers interact via `host_read`, which fully encapsulates the "have I seen
    this value before" logic. Kept as a dataclass rather than a tuple so it can be mutated in
    place by the RPC handler and by `host_read` on consume.
    """

    input_x: float = 0.0
    input_y: float = 0.0
    # The tick number attached to the value currently held above. Bumped by the RPC when a
    # fresher sample arrives; never reset once set, so out-of-order packets are ignored.
    latest_tick: int = -1
    # The tick number the host has already applied physics for. Advanced by `host_read` after
    # each consume; the difference `latest_tick - applied_tick` at read time is how far behind
    # the receiver was, which becomes the (capped) catch-up count.
    applied_tick: int = -1
    # The value from the previous consumed frame, so `host_read` can tell whether the current
    # sample is a "new value" (transition -> catch up) or a repeat (steady -> no catch up).
    prev_input_x: float = 0.0
    prev_input_y: float = 0.0
    # Whether any consume has happened yet. Without this, the very first consume (where
    # prev_input_x/y are still their dataclass defaults of 0.0) would look like "value has not
    # changed from zero" even when the sender's first sample WAS zero - correct outcome, but
    # would also swallow the initial catch-up for a non-zero first sample. Explicit flag keeps
    # the transition logic honest.
    consumed_once: bool = False


_states: dict[int, _SenderState] = {}
"""Per-sender bookkeeping, keyed by PlayerID.

Populated by the RPC on first sample from a given sender and cleared by `forget` on slide end.
A stale entry that survives a leave (edge case: player disconnects mid-slide, no exit RPC
lands) is one dataclass worth of memory and gets overwritten on the same PlayerID's next
session; not worth adding a leave hook to clean up.
"""


def owner_send(input_x: float, input_y: float, tick: int) -> None:
    """Owner-side: ship one tick-tagged sample to the host.

    Wraps the RPC call so callers see a plain function rather than the decorated symbol - keeps
    the driver's send site tidy and lets this module change the wire format later without
    touching every caller.
    """
    log.debug(f"sync.owner_send enter input=({input_x:.2f},{input_y:.2f}) tick={tick}")
    server_input_ticked(input_x, input_y, tick)
    log.debug("sync.owner_send exit")


@host.json_message
def server_input_ticked(input_x: float, input_y: float, tick: int) -> None:
    """Cache one tick-tagged input sample on the host, keyed by sender.

    Runs on: HOST only. Fires once per driver tick on the sender's machine.

    Only accepts strictly-newer ticks. Same or older gets dropped: in a run where three
    packets arrived out of order the middle one would otherwise pollute the cache with a stale
    value, and losing an occasional in-order packet is exactly what `MAX_CATCHUP_TICKS`
    covers.
    """
    verbose = every_n("sync.server_input_ticked", 30)
    if verbose:
        log.debug(
            f"sync.server_input_ticked enter input=({input_x:.2f},{input_y:.2f}) tick={tick}",
        )
    pc = cast("WillowPlayerController", server_input_ticked.sender.Owner)
    if pc is None or (player := player_id(pc)) is None:
        if verbose:
            log.debug(
                f"sync.server_input_ticked exit reason=no_player has_pc={pc is not None}",
            )
        return
    if state_for(pc) is None:
        # Sender is not currently sliding on the host's model. The enter RPC has not landed yet
        # or the slide already ended; either way there is nothing to steer, so we drop the
        # sample. `host_read` will see the default `_SenderState()` when the slide does open.
        if verbose:
            log.debug(f"sync.server_input_ticked exit reason=not_sliding player={player}")
        return
    entry = _states.setdefault(player, _SenderState())
    if tick <= entry.latest_tick:
        if verbose:
            log.debug(
                f"sync.server_input_ticked exit reason=stale tick={tick}"
                f" latest={entry.latest_tick}",
            )
        return
    entry.input_x = input_x
    entry.input_y = input_y
    entry.latest_tick = tick
    if verbose:
        log.debug(
            f"sync.server_input_ticked exit stored player={player}"
            f" latest_tick={entry.latest_tick}",
        )


def host_read(player: int) -> tuple[float, float, int]:
    """Host-side: return the freshest known input plus the catch-up count for this frame.

    Semantics:
    - Return value is `(input_x, input_y, catchup_ticks)`.
    - `catchup_ticks` is `0` unless the incoming value has moved on since the last consume, in
      which case it is `min(latest_tick - applied_tick, MAX_CATCHUP_TICKS)`.
    - "The value has moved on" is defined as `(input_x, input_y)` differing from the values
      the host last consumed. So holding a key steady for many frames returns `catchup=0` on
      every frame after the first, and pressing / releasing produces one non-zero return.
    - The comparison uses `consumed_once` to distinguish "first consume ever" (which should
      trigger catch-up if the first sample is not `(0, 0)`) from "we already saw this exact
      value" (which should not).

    The consumer is expected to fold `catchup_ticks` into whatever input-driven physics step
    it applies this frame, typically as an extra `catchup * expected_delta` term on the
    stepping alpha.
    """
    entry = _states.get(player)
    if entry is None:
        log.debug(f"sync.host_read exit player={player} result=(0.0,0.0,0) reason=no_entry")
        return 0.0, 0.0, 0

    is_new_value = (
        not entry.consumed_once
        or entry.input_x != entry.prev_input_x
        or entry.input_y != entry.prev_input_y
    )
    catchup = 0
    if is_new_value and entry.applied_tick < entry.latest_tick:
        catchup = min(entry.latest_tick - entry.applied_tick, MAX_CATCHUP_TICKS)

    log.debug(
        f"sync.host_read player={player} input=({entry.input_x:.2f},{entry.input_y:.2f})"
        f" latest={entry.latest_tick} applied={entry.applied_tick} new_value={is_new_value}"
        f" catchup={catchup}",
    )

    entry.applied_tick = entry.latest_tick
    entry.prev_input_x = entry.input_x
    entry.prev_input_y = entry.input_y
    entry.consumed_once = True
    return entry.input_x, entry.input_y, catchup


def forget(player: int) -> None:
    """Drop cached state for a player. Called by the slide-end path on the host.

    Not strictly required for correctness (the next slide's samples overwrite the value fields
    on arrival and `latest_tick` grows past whatever was there before), but a fresh slide
    starting with `applied_tick = -1` and `latest_tick = -1` avoids a spurious "new value"
    catch-up burst on the first frame of the new slide when the previous slide's last sample
    happened to differ from the new slide's first sample.
    """
    removed = _states.pop(player, None)
    log.info(f"sync.forget player={player} had_entry={removed is not None}")


network_functions = [server_input_ticked]
"""Exported for the mod's `add_network_functions` call, so the RPC identifier here gets the same
`PROTOCOL_PREFIX` treatment lifecycle's do and both machines resolve it consistently."""
