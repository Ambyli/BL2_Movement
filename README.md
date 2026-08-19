# Sliding
*Adds a proper slide to Borderlands 2, TPS and AoDK.*

Crouch while sprinting and you drop into a slide that carries its own momentum instead of being driven by input. Jumping out of a slide preserves the horizontal speed you had, so a well-timed slide-hop launches you further than a plain sprint jump.

## How it feels
- **Slopes matter.** Sliding downhill sustains speed; uphill sheds it faster. A slope can hold a slide going, but it will never push you past the speed you opened at.
- **Steering, but bounded.** Movement keys can curve a slide, but there is a hard turn limit measured against the heading you started on — you can never steer round into travelling backwards. Input pointing back down the slide is ignored outright.
- **Locked speed.** The slide runs at its own speed, unaffected by aiming down sights, weapon swaps or class-mod movement modifiers.
- **Hard duration cap.** Downhill terrain can sustain a slide indefinitely; the max-duration setting is the backstop that ends it regardless.

## Tunables
All exposed as live sliders in the mod menu — changes take effect without a reload.

| Option | What it does |
| --- | --- |
| Slide Speed | Speed a slide opens at, in unreal units per second. |
| Slide Decay Rate | How fast the slide bleeds off on flat ground. |
| Max Slide Duration | Absolute cap on a single slide, in seconds. |
| Steer Rate | How sharply movement keys can curve a slide. `0` locks it dead straight. |
| Max Turn | How far, in degrees, a slide can rotate from its entry heading. |

## Co-op

Requires the mod on every player in the session. Both machines run the same slide on their copy of
the sliding pawn, from the same entry heading and down the same decay curve, so their simulations
track each other rather than one having to be corrected onto the other.

## How the sync works

A slide is driven by a coroutine, not by a game hook. `start_coroutine_tick` runs off the game
viewport's per-frame tick, which does not care whose pawn a slide belongs to, whether that pawn's
physics ran this frame, or which machine is authoritative over it. So one driver serves both cases:
the machine whose player pressed crouch starts one for its own pawn, and the host starts one for
every player who tells it they are sliding. Each tick it decays the speed curve against the slope,
applies the steering blend and turn-cone clamp, and writes the resulting velocity onto the pawn,
holding `CrouchedPct` clear of that speed so the engine's own walking cap cannot clamp it back down.

Driving both copies off a frame tick rather than off the move stream is what keeps them together:
the host advances a remote player's slide at its own frame rate rather than at the rate that
player's packets happen to arrive.

Entry and exit are signalled by `@host` messages. `server_enter_slide` carries the heading the slide
opened on, which is the part that has to travel — the host cannot recover it by sampling its own copy
of the pawn, because `ServerMove` replicates acceleration and a claimed position but never velocity.
Exit needs no payload: each machine ends its own copy from the same gate (crouch released, or no
longer on the ground), and the message only reconciles the host's copy if it is still running.

The slide-jump keeps its momentum through a two-frame handoff: the local `Jump` hook stashes the
current horizontal velocity, then the next `PlayerMove` calls `pawn.DoJump(True)` on the ground frame
and sends `server_set_slide_jump_velocity(vx, vy)` on the airborne frame, so the host makes the same
move rather than merely being told the result.

## Development

The mod runs inside Borderlands' embedded Python interpreter (shipped with pyunrealsdk), so the runtime dependencies — `unrealsdk`, `mods_base`, `networking`, `uemath`, `coroutines`, `tweens` — are game-provided rather than installable from PyPI. A local virtual environment is only for dev tooling.

### Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.14 on the local machine.

```
uv venv --python 3.14
uv pip install --group dev
```

Do **not** run `uv sync` — it will try to resolve the runtime dependencies against PyPI and fail. Use `uv pip install --group dev` to install only the dev tools (ruff and pytest).

### Tests

There is currently no test suite in the tree. The slide maths in `movement.py` and `state.py` is
pure and testable without the game; anything that reaches through hooks, coroutines or replication
has to be verified in a real session, and in co-op that means two machines.

The mod writes a bounded diagnostic log to `~/bl2_slide_debug.log`, and `discovery.py` writes a
fuller probe trace to `~/bl2_discovery.log` — including `SVMOVE` lines, which report the distance
between the host's simulation of a pawn and the position that pawn's own machine claims. That number
is the one worth watching after any change to the slide body: it should hold roughly steady across a
slide rather than climbing.

### Lint

```
uv run --no-sync ruff check .
```

Ruff rules and per-file exceptions live in `pyproject.toml` under `[tool.ruff]`.

## Credits

Fork of [juso40's original Sliding mod](https://github.com/juso40/bl2sdk-mods). The movement core has
been rewritten around a coroutine-driven slide that runs identical logic on client and host, with
steering, a turn-limit backstop, a locked slide speed, and a momentum-preserving slide-jump.
