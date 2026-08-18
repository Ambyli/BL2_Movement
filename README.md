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
Requires the mod on every player in the session. Both the client and the host run the same slide-physics code on their copy of the sliding pawn, so their simulations agree by construction. No position corrections fire during a slide, and no teleport lands when it ends.

## How the sync works

The mod hooks BL2's per-frame walking-physics function (`Engine.Pawn:PhysWalking`) with a PRE hook that returns the `Block` sentinel when the pawn is sliding. That skips the engine's normal walking body entirely and runs a custom slide step inline: decay the speed curve against the slope, apply the steering blend + turn-cone clamp, then call `pawn.MoveSmooth(delta)` so BL2's own collision code handles wall-slide and step-up.

Because the same hook runs on both machines, driven by the same inputs replicated through the normal move stream, the two simulations produce identical positions. This is functionally equivalent to what a native UE3 developer would do by adding a new `PHYS_Sliding` physics mode — we can't literally add an enum value from Python, but a PRE hook that blocks the native call and runs its own body is the same dispatch.

Enter/exit is signalled two ways in parallel, and whichever arrives first wins (the other is a no-op via idempotency):
- A `broadcast` RPC (`server_enter_slide` / `server_exit_slide`) — reliable but arrives tens of ms late.
- A bit in `SavedMove.CompressedFlags` — travels with the move packet itself, applied on the exact move the client made the transition on.

The slide-jump keeps its momentum via a two-frame handoff: the local Jump hook stashes the current horizontal velocity, and the next PlayerMove PRE hook calls `pawn.DoJump(True)` on the ground frame and fires `server_set_slide_jump_velocity(vx, vy)` on the airborne frame.

## Development

The mod runs inside Borderlands' embedded Python interpreter (shipped with pyunrealsdk), so the runtime dependencies — `unrealsdk`, `mods_base`, `networking`, `uemath`, `coroutines`, `tweens` — are game-provided rather than installable from PyPI. A local virtual environment is only for dev tooling: linting and unit tests.

### Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.14 on the local machine.

```
uv venv --python 3.14
uv pip install --group dev
```

Do **not** run `uv sync` — it will try to resolve the runtime dependencies against PyPI and fail. Use `uv pip install --group dev` to install only the dev tools (ruff and pytest).

### Tests

```
uv run --no-sync pytest
```

The `--no-sync` flag skips uv's implicit dependency resolution — the mod's runtime dependencies aren't on PyPI, so bare `uv run` would fail before pytest even started.

The suite covers the pure slide math: entry-heading lock in `state.begin_slide_state`, the decay curve and exit triggers in `movement.slide`, and the steering blend / back-input filter / turn-cone clamp / pawn writes in `movement.apply_slide_physics`. Tests never load the game — `tests/conftest.py` installs stand-in modules into `sys.modules` before any test imports the mod, with a working `Vector` implementation for the vector math and minimal fakes for everything else.

`tests/simulate_slide.py` is a diagnostic that runs the actual `_phys_sliding` hook against two fake pawns (client and host) frame-by-frame and prints their positions. Useful for confirming the sync stays stable after any change to the slide-physics body.

When adding tests for new pure-math paths, extend `conftest.py` if the code touches a game API not already stubbed. Anything that reaches through hooks or replication is out of scope here and needs to be verified in-game.

### Lint

```
uv run --no-sync ruff check .
```

Ruff rules and per-file exceptions live in `pyproject.toml` under `[tool.ruff]`.

## Credits
Fork of [juso40's original Sliding mod](https://github.com/juso40/bl2sdk-mods). The movement core has been rewritten around a custom physics-mode hook (`PhysWalking` PRE + `Block`) that runs identical slide logic on client and host, with steering, a turn-limit backstop, a locked slide speed, and a momentum-preserving slide-jump.
