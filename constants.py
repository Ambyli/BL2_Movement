"""Structural constants: values the mod is built around rather than tuned with.

Anything the user can tune from the mod menu lives in `config.py` as a `SliderOption`; anything
here is fixed by the shape of the slide itself and moving it would rescale the whole curve rather
than tuning one facet of it. Kept in its own module so it can be imported by both `config` and the
runtime modules without circling.
"""

from __future__ import annotations

SLIDE_SPEED_DEFAULT: float = 2.2
"""Top of the decay range and the CrouchedPct a slide opens at. Moving this rescales the whole
curve - the sliders that tune slide feel (start_speed, decay_rate) are all measured against it."""

CROUCHED_PCT_DEFAULT: float = 0.5
"""Both the ordinary walking-crouch speed and the floor the decay curve ends at. Moving this
changes what the pawn holds after a slide ends, not the shape of the slide itself."""

SLIDE_BACK_CUTOFF: float = -0.5
"""Input pointing further than this back down the slide is ignored outright. -0.5 is 120 degrees off
the heading, so back, back-left and back-right are all inert - the slide cannot be turned round to
travel backwards."""

SLIDE_STEER_DEADZONE: float = 0.1
"""Sideways input weaker than this does not steer at all. Without it, input that is very nearly
straight backwards leaves a sliver of a sideways component that still creeps the heading round."""

POST_LOG_EVERY: int = 30
"""One line per this many forced frames, so a slide costs a handful of lines rather than hundreds.
Every per-frame `every_n` gate throughout the mod uses this so a scan of the log stays aligned across
modules."""
