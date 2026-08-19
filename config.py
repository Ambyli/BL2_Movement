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

# Structural rather than feel dials, so they stay constants. SLIDE_SPEED_DEFAULT is where speed_pct
# opens and the top of the decay range; CROUCHED_PCT_DEFAULT is both normal crouch speed and the
# cutoff the decay ends at. Moving either rescales the whole curve instead of tuning anything.
SLIDE_SPEED_DEFAULT: float = 2.2
CROUCHED_PCT_DEFAULT: float = 0.5

# Input pointing further than this back down the slide is ignored outright. -0.5 is 120 degrees off
# the heading, so back, back-left and back-right are all inert.
SLIDE_BACK_CUTOFF: float = -0.5
# Sideways input weaker than this does not steer at all. Without it, an input that is very nearly
# straight backwards leaves a sliver of a sideways component that still creeps the heading round.
SLIDE_STEER_DEADZONE: float = 0.1

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

all_options = [
    GroupedOption(
        "Sliding",
        (start_speed, decay_rate, max_duration, steer_rate, max_turn_degrees),
    ),
]
