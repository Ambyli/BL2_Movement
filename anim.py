"""Crash-safe tween helpers shared by the pose and viewmodel animations.

Both modules tween a native component transform while a slide runs - the body mesh's `Rotation` in
`pose`, the arms mesh's `RotOrigin`/`Origin` in `viewmodel`. Two hazards are shared, and both are the
kind that only bite when a pawn is torn down mid-animation (death, respawn, a level change, or a co-op
proxy leaving relevance). A zone load during a slide hits both at once, which is why it crashes:

- **A tween must never capture a native struct view.** The `tweens` library's `tween_property` stores
  the object you hand it and writes into it every frame. Hand it `mesh.Rotation` (or
  `SkeletalMesh.Origin`) and the tween keeps writing into that struct's native memory after the pawn
  that owned it is freed - an access violation in the SDK, on every machine, since these anims are
  driven for every slide. `field_sink` closes over a `WeakPointer` instead, re-resolves the live
  component every frame, and does nothing once it is gone.

- **The default pause gate faults during a loading screen.** Every `Tween` defaults `pause_while` to
  `get_pc().IsPaused()`, and the coroutine runner evaluates that gate each frame with no guard of its
  own. `get_pc()` returns None during a loading screen (and can hand back a dying controller in the
  teardown window just before it), so the gate itself faults on exactly the zone-load path these
  tweens are most likely to be alive on - before the sink above even runs. `SAFE_PAUSE` is the same
  gate, guarded, and every tween this mod starts sets it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from coroutines import WaitWhile
from mods_base import get_pc

if TYPE_CHECKING:
    from collections.abc import Callable

    from unrealsdk.unreal import WeakPointer


def _game_paused() -> bool:
    """Whether the game is paused, treating "no controller yet" as not paused.

    Mirrors the pause read the tween library does by default, but guarded: `get_pc()` returns None
    during a loading screen, and a read against a controller being torn down can throw, so both cases
    fall through to "not paused" rather than faulting the per-frame coroutine tick.
    """
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return False
    try:
        return bool(pc.IsPaused())
    except Exception:  # noqa: BLE001 - a controller mid-teardown; treat as not paused
        return False


SAFE_PAUSE = WaitWhile(_game_paused)
"""Drop-in replacement for the tween library's default `GameIsPaused` gate that never faults during a
level transition. Pass to `Tween.pause_while` on every tween the mod starts."""


def field_sink(
    ref: WeakPointer,
    component: Callable[[Any], Any],
    struct_name: str,
    field: str,
) -> Callable[[Any], None]:
    """Build a tween sink that writes one field of a component's struct, safely, each frame.

    `ref` weakly holds the pawn; `component` resolves the component that carries the struct from a
    live pawn (e.g. the body mesh, or the arms' skeletal mesh); `struct_name` is the struct property
    on that component (`Rotation`, `RotOrigin`, `Origin`) and `field` the axis within it.

    Every frame the sink re-resolves the pawn and its component and no-ops once either is gone, so the
    tween is safe to outlive the pawn. It reads the whole struct fresh, sets the one field, and writes
    the whole struct back - correct whether the SDK hands back a live view or a copy, and so parallel
    sinks over the same struct (each re-reading first) never clobber one another's field. The value is
    written as-is: rotator fields tween as ints, origin/vector fields as floats, matching the endpoint
    types the caller passes.
    """

    def sink(value: Any) -> None:
        pawn = ref()
        if pawn is None:
            return
        target = component(pawn)
        if target is None:
            return
        struct = getattr(target, struct_name, None)
        if struct is None:
            return
        setattr(struct, field, value)
        setattr(target, struct_name, struct)

    return sink
