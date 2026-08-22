"""Every dial the slide exposes.

The feel dials are mod-menu options rather than constants, so they can be tuned in game without a
restart - recompiling this mod means quitting to desktop and reloading a save, which is by far the
slowest part of working on it. Read these as `option.value` at the point of use; never cache them,
or a change silently stops taking effect.

Two control kinds: a `SliderOption` where a real number matters (speed, turn angle, sound stack), and
a `Preset` - a `SpinnerOption` of named choices mapped to values - where fiddling a decimal on a bar
is just annoying. A `Preset` still reads as `.value` (the mapped number), so nothing downstream cares
which kind a dial is.

Each physics dial fires `_on_slider_change` when the user changes it in the mod menu; that callback
re-announces the full set to the host so a remote player's slide runs with their own tuned values
rather than the host's defaults. The announce is idempotent and best-effort - a raise from the RPC
path is swallowed rather than leaked back into the mod menu. The sound dials are local presentation
and skip the announce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mods_base import GroupedOption, SliderOption, SpinnerOption

if TYPE_CHECKING:
    from collections.abc import Callable


def _on_slider_change(*_: object) -> None:
    """Announce this machine's current physics values to the host.

    Fires from every physics dial's `on_change`, so any dial the user tweaks triggers one announce
    with the whole set - simpler than tracking which changed and sending only that field. The
    catchall `except` covers every case where an announce cannot succeed right now (no world, not a
    client, network layer not initialised at mod load, dedicated-server host with no host to address,
    etc.); in all of them the announce will be resent from the top of `enter_slide` the next time the
    local player opens a slide, so a dropped one here is not lost, just deferred.

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


class Preset:
    """A named-preset dial: a `SpinnerOption` the player cycles, each label mapped to a value.

    Where a `SliderOption` would make the player nudge a decimal along a bar, this offers a short list
    of recognisable choices instead. The menu shows `.option` (the spinner); the rest of the mod reads
    `.value` and gets the mapped number, exactly as it reads a slider's `.value` - so a dial being a
    preset or a slider is invisible past this module.
    """

    def __init__(
        self,
        name: str,
        presets: dict[str, float],
        default: str,
        description: str,
        on_change: Callable[..., None] | None = None,
    ) -> None:
        self._presets = dict(presets)
        self.option = SpinnerOption(
            name,
            default,
            list(self._presets),
            description=description,
            on_change=on_change,
        )

    @property
    def value(self) -> float:
        """The number the current choice maps to; the first preset if the choice is somehow unknown."""
        return self._presets.get(self.option.value, next(iter(self._presets.values())))


# --- physics dials (announced to the host) --------------------------------------------------------

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

decay_rate = Preset(
    "Slide Length",
    {"Very short": 2.4, "Short": 1.7, "Normal": 1.15, "Long": 0.8, "Very long": 0.5},
    "Normal",
    "How far a slide carries before it runs out. Longer slides bleed their speed off more slowly.",
    on_change=_on_slider_change,
)

max_duration = Preset(
    "Max Slide Time",
    {"Short (1.5s)": 1.5, "Normal (2.5s)": 2.5, "Long (4s)": 4.0, "Very long (6s)": 6.0},
    "Normal (2.5s)",
    "Hard cap on how long a single slide can last, regardless of terrain.",
    on_change=_on_slider_change,
)

steer_rate = Preset(
    "Steering",
    {"Locked": 0.0, "Gentle": 1.5, "Normal": 3.0, "Sharp": 5.0, "Very sharp": 8.0},
    "Normal",
    "How sharply the movement keys can curve a slide. Locked keeps it dead straight.",
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

downhill_boost = Preset(
    "Downhill Boost",
    {"Off": 0.0, "Low": 0.0005, "Medium": 0.002, "High": 0.005, "Very high": 0.01},
    "Low",
    "Speed a slide wins back running downhill. Off keeps downhill at the flat-ground decay rate.",
    on_change=_on_slider_change,
)

uphill_drag = Preset(
    "Uphill Drag",
    {"Off": 0.0, "Low": 0.002, "Normal": 0.004, "High": 0.008, "Very high": 0.015},
    "Normal",
    "Extra speed a slide sheds running uphill, on top of the normal time decay. Off disables it.",
    on_change=_on_slider_change,
)

# --- sound dials (local presentation, read live, no announce) --------------------------------------

step_interval = Preset(
    "Slide Sound Density",
    {"Sparse": 0.2, "Normal": 0.1, "Tight": 0.07, "Very tight": 0.05},
    "Normal",
    "How closely the slide's scuff sounds repeat. Tighter is a busier scuff.",
)

step_stack = SliderOption(
    "Slide Sound Volume",
    2,
    1,
    5,
    1,
    is_integer=True,
    description=(
        "How many footstep sounds stack per burst. The footstep call has no volume control, so more"
        " stacked instances make the slide louder."
    ),
)

all_options = [
    GroupedOption(
        "Sliding",
        (
            start_speed,
            decay_rate.option,
            max_duration.option,
            steer_rate.option,
            max_turn_degrees,
            downhill_boost.option,
            uphill_drag.option,
        ),
    ),
    GroupedOption(
        "Slide Effects",
        (
            step_interval.option,
            step_stack,
        ),
    ),
]
