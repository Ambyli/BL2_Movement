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

HOST_ARM_GRACE: float = 0.25
"""Seconds the host holds a just-opened shadow slide open before enforcing the crouch/ground gate.
The `server_enter_slide` RPC that starts the shadow travels a faster channel than the replicated
`bDuck` flag and beats it to the host, so the first driver tick reads `bDuck` false purely because
the crouch state has not arrived yet - not because the player let go. Ending the slide there kills
the shadow before `apply_remote_cap` ever lifts the cap. This window keeps it alive until the flag
lands; the decay curve and duration cap still bound a shadow whose flag never arrives, and it is far
shorter than a real slide (an exit RPC ends one early regardless)."""

SLIDE_LEAN_PITCH: int = 12000
"""Backward recline of the third-person body while sliding, in Unreal rotation units (65536 = 360
degrees). Positive pitches the whole body back into the slide; 12000 is ~66 degrees."""

SLIDE_LEAN_ROLL: int = 0
"""Sideways tilt of the third-person body while sliding, in Unreal rotation units. 0 keeps the lean a
pure backward recline; a small roll would angle the body into the turn."""

SLIDE_ANIM_RATE: float = 0.0
"""Skeletal-animation rate scale held on the body while sliding. 0 freezes the walk/crouch shuffle so
the legs stop cycling under the lean; restored to the engine default (1.0) when the slide ends."""

SLIDE_MIN_SPEED_FRACTION: float = 0.9
"""Fraction of max sprint speed the player must be moving at (horizontally) before a slide will start,
so a slide only comes off a committed sprint, not a walk or a sprint still winding up. While sprinting
the pawn's `GroundSpeed` already reads as its (class-mod-adjusted) sprint speed, so that is the
reference. 0 disables the speed gate. Enforced in the duck hook."""

SLIDE_DUST_DROP: float = 78.0
"""How far below the pawn's Location (capsule centre) to spawn the slide dust, in unreal units - about
foot height, so the dust kicks up from the surface rather than the pawn's waist. The player capsule
half-height is ~80."""

SLIDE_DUST_FORWARD: float = 40.0
"""How far ahead of the pawn (along its travel direction) to spawn the slide dust, in unreal units, so
it kicks up just in front rather than under the body."""

SLIDE_FX_TICK: float = 0.03
"""Base tick of the slide-effects loop, in seconds. Fine enough that the dust and the sound can each
be paced off their own interval; not a per-effect rate itself."""

POST_LOG_EVERY: int = 30
"""One line per this many forced frames, so a slide costs a handful of lines rather than hundreds.
Every per-frame `every_n` gate throughout the mod uses this so a scan of the log stays aligned across
modules."""

PHYS_WALKING: int = 1
"""UE3 `EPhysics.PHYS_Walking`, the engine's ground-movement physics mode. Stable across every
Borderlands build - the enum is engine-level. Read off `pawn.Physics` by `state.on_ground` as a
UFunction-free stand-in for `IsOnGroundOrShortFall()`, whose method call faults on a pawn mid-level-
transition (see `state.on_ground`)."""
