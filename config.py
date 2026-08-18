"""Every dial the slide exposes.

The feel dials are mod-menu sliders rather than constants, so they can be tuned in game without a
restart - recompiling this mod means quitting to desktop and reloading a save, which is by far the
slowest part of working on it. Read these as `option.value` at the point of use; never cache them,
or a slider change silently stops taking effect.
"""

from __future__ import annotations

from mods_base import BoolOption, GroupedOption, SliderOption

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
)

steer_rate = SliderOption(
    "Steer Rate",
    3.0,
    0.0,
    10.0,
    0.1,
    is_integer=False,
    description="How sharply the movement keys can curve a slide. 0 locks it dead straight.",
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
)

input_driven = BoolOption(
    "Input Driven Slides",
    True,
    description=(
        "Express a slide as movement input instead of forcing velocity onto the pawn. Unreal"
        " replicates input and derives velocity from it, so writing velocity directly makes a slide"
        " invisible to the server - which is what forced the host to re-simulate every remote slide"
        " by hand, and what the position corrections were fighting. Writing input instead lets the"
        " server see a slide as an ordinary move and reach the same answer on its own."
        " Turn off to return to forced velocity, for comparison or if this misbehaves."
    ),
)

smooth_coop_slides = BoolOption(
    "Smooth Co-op Slides",
    False,
    description=(
        "Ignore the server's position corrections for the length of your own slide, as a client."
        " This was needed when the slide was forced onto the pawn, because the server simulated it"
        " about a percent slower and corrected the difference around forty times a second. With"
        " input driven slides both machines should agree on their own, so this defaults off - it is"
        " kept as a fallback, and as a way to tell a remaining disagreement from a rendering one."
    ),
)

all_options = [
    GroupedOption(
        "Sliding",
        (start_speed, decay_rate, max_duration, steer_rate, max_turn_degrees),
    ),
    GroupedOption("Multiplayer", (input_driven, smooth_coop_slides)),
]
