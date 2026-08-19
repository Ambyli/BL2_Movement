"""Sliding - crouch while sprinting to slide, jump out of it to keep the momentum.

Fork of juso40's original. The movement core was rewritten so a slide carries its own momentum
rather than being driven by input, holds a speed nothing else can scale, and cannot be steered into
reverse. See the individual modules; this one only assembles them.
"""

from __future__ import annotations

from mods_base import CoopSupport, build_mod
from networking import add_network_functions

from . import discovery, events, sync, viewmodel
from .config import all_options
from .debug import log
from .hooks import all_hooks
from .lifecycle import network_functions as lifecycle_network_functions

# Wiring. Presentation subscribes here rather than being called from the slide logic, so movement
# never needs to know what is watching it. Each future feature (HUD, audio, third-person pose) adds
# one more pair of lines and touches nothing else.
events.slide_started.append(viewmodel.on_start)
events.slide_ended.append(viewmodel.on_end)

# Diagnostics ride the same event rather than hooking anything of their own, which is the point of
# the event list.
events.slide_started.append(discovery.on_start)
events.slide_ended.append(discovery.on_end)


def _on_enable() -> None:
    log.info("_on_enable enter")
    discovery.enable()
    log.info("_on_enable exit")


def _on_disable() -> None:
    log.info("_on_disable enter")
    discovery.disable()
    log.info("_on_disable exit")


# Both of these only scan the calling module's scope, so the lists have to be handed over
# explicitly - relying on auto-discovery here would silently register nothing at all.
mod = build_mod(
    hooks=all_hooks,
    options=all_options,
    on_enable=_on_enable,
    on_disable=_on_disable,
    coop_support=CoopSupport.RequiresAllPlayers,
)
# Every module that ships its own RPCs contributes to one list here and every entry gets a
# stable identifier: `<PROTOCOL_PREFIX>:<function qualname>`. Pinned rather than left to the
# library default of `<module>:<qualname>`, which begins with the mod's directory name - so the
# same mod unzipped into `sliding` on one machine and `BL2_Movement-main` on another would
# produce different identifiers, and every message would be discarded on arrival as unknown, in
# both directions, with nothing but a console warning to show for it. Both players still need
# matching builds; they no longer need matching folder names.
PROTOCOL_PREFIX = "sliding"

_all_network_functions = [*lifecycle_network_functions, *sync.network_functions]
for _func in _all_network_functions:
    _func.network_identifier = f"{PROTOCOL_PREFIX}:{_func.__wrapped__.__qualname__}"

add_network_functions(mod, _all_network_functions)
