"""Every dial the slide exposes.

The feel dials are mod-menu sliders rather than constants, so they can be tuned in game without a
restart - recompiling this mod means quitting to desktop and reloading a save, which is by far the
slowest part of working on it. Read these as `option.value` at the point of use; never cache them,
or a slider change silently stops taking effect.

Each slider fires `_on_slider_change` when the user drags it in the mod menu; that callback
re-announces the full set to the host so a remote player's slide runs with their own tuned
values rather than the host's defaults. The announce is idempotent and best-effort - a raise from
the RPC path is swallowed rather than leaked back into the mod menu.
"""

from __future__ import annotations

from mods_base import GroupedOption, SliderOption


def _on_slider_change(*_: object) -> None:
    """Announce this machine's current slider values to the host.

    Fires from every slider's `on_change`, so any dial the user tweaks triggers one announce with
    the whole set - simpler than tracking which slider changed and sending only that field, and
    the payload is five floats. The catchall `except` covers every case where an announce cannot
    succeed right now (no world, not a client, network layer not initialised at mod load,
    dedicated-server host with no host to address, etc.); in all of them the announce will be
    resent from the top of `enter_slide` the next time the local player opens a slide, so a
    dropped one here is not lost, just deferred.

    Lazy imports to break the module cycle: `lifecycle` and `state` both import `config` at module
    scope, so binding them here at module scope would import-cycle.
    """
    from .debug import log  # noqa: PLC0415 - deliberately lazy, see docstring
    from .lifecycle import _announce_settings_to_host  # noqa: PLC0415
    from .state import default_settings  # noqa: PLC0415

    log.info("_on_slider_change enter")
    try:
        _announce_settings_to_host(default_settings())
    except Exception as ex:  # noqa: BLE001 - a failed announce must never break the mod menu
        log.warning(f"SLIDER ANNOUNCE FAILED {type(ex).__name__}: {ex}")
    log.info("_on_slider_change exit")

start_speed = SliderOption(
    "Slide Speed",
    970.0,
    400.0,
    2000.0,
    10.0,
    is_integer=False,
    description=(
        "Speed a slide opens at, in unreal units per second. Deliberately a flat number rather than"
        " something read off the pawn, so aiming down sights, weapon swaps and class mods cannot"
        " change a slide already in progress."
    ),
    on_change=_on_slider_change,
)

decay_rate = SliderOption(
    "Slide Decay Rate",
    1.15,
    0.2,
    3.0,
    0.05,
    is_integer=False,
    description=(
        "How fast a slide bleeds off, in speed units per second. Higher is a shorter slide. The"
        " slide runs from 2.2 down to 0.5, so 1.15 gives roughly a 1.5 second slide on the flat."
    ),
    on_change=_on_slider_change,
)

max_duration = SliderOption(
    "Max Slide Duration",
    2.5,
    0.5,
    10.0,
    0.1,
    is_integer=False,
    description=(
        "Absolute cap on a single slide, in seconds. Downhill momentum can hold a slide at full"
        " speed indefinitely; this is the backstop that ends it regardless of terrain."
    ),
    on_change=_on_slider_change,
)

steer_rate = SliderOption(
    "Steer Rate",
    3.0,
    0.0,
    10.0,
    0.1,
    is_integer=False,
    description="How sharply the movement keys can curve a slide. 0 locks it dead straight.",
    on_change=_on_slider_change,
)

max_turn_degrees = SliderOption(
    "Max Turn",
    60.0,
    0.0,
    180.0,
    5.0,
    is_integer=False,
    description=(
        "Hard limit on how far a slide can turn from the heading it started on, so steering can"
        " never bring you round to travelling backwards."
    ),
    on_change=_on_slider_change,
)

downhill_boost = SliderOption(
    "Downhill Boost",
    0.0005,
    0.0,
    0.02,
    0.0005,
    is_integer=False,
    description=(
        "Speed a slide wins back per unreal unit of downhill drop each frame. 0 disables slope"
        " gain, so downhill runs decay at the flat-ground rate."
    ),
    on_change=_on_slider_change,
)

uphill_drag = SliderOption(
    "Uphill Drag",
    0.004,
    0.0,
    0.02,
    0.0005,
    is_integer=False,
    description=(
        "Extra speed a slide sheds per unreal unit of uphill rise each frame, on top of the"
        " normal time decay. 0 disables slope drag."
    ),
    on_change=_on_slider_change,
)

all_options = [
    GroupedOption(
        "Sliding",
        (
            start_speed,
            decay_rate,
            max_duration,
            steer_rate,
            max_turn_degrees,
            downhill_boost,
            uphill_drag,
        ),
    ),
]
