"""Sliding - crouch while sprinting to slide, jump out of it to keep the momentum.

Fork of juso40's original. The movement core was rewritten so a slide carries its own momentum
rather than being driven by input, holds a speed nothing else can scale, and cannot be steered into
reverse. See the individual modules; this one only assembles them.
"""

from __future__ import annotations

from mods_base import build_mod
from networking import add_network_functions

from . import events, viewmodel
from .config import all_options
from .hooks import all_hooks, disable_host_tick, enable_host_tick
from .lifecycle import network_functions

# Wiring. Presentation subscribes here rather than being called from the slide logic, so movement
# never needs to know what is watching it. Each future feature (HUD, audio, third-person pose) adds
# one more pair of lines and touches nothing else.
events.slide_started.append(viewmodel.on_start)
events.slide_ended.append(viewmodel.on_end)

# Both of these only scan the calling module's scope, so the lists have to be handed over
# explicitly - relying on auto-discovery here would silently register nothing at all.
mod = build_mod(
    hooks=all_hooks,
    options=all_options,
    on_enable=enable_host_tick,
    on_disable=disable_host_tick,
)
add_network_functions(mod, network_functions)
