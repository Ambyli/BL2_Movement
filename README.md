# Sliding

A BL2 / TPS / AoDK movement mod. Crouch while sprinting to slide, jump out of a slide to keep
its momentum, hit slopes to gain or lose speed. Fork of juso40's original with the movement core
rewritten so a slide carries its own momentum instead of being driven by input, holds a speed
nothing else can scale, and cannot be steered into reverse.

Runs under [pyunrealsdk] and [willow2-mod-manager]. Multiplayer works on listen-server hosts;
`coop_support = RequiresAllPlayers` means every player in the party has to have the mod loaded.

[pyunrealsdk]: https://github.com/bl-sdk/pyunrealsdk
[willow2-mod-manager]: https://github.com/bl-sdk/willow2-mod-manager

## Controls

- **Crouch while sprinting** — starts a slide.
- **Jump during a slide** — jumps out, carrying the slide's horizontal velocity into the arc.
- **Movement keys** — steer the slide within a cone around the direction it opened on.
- **Release crouch, land off a ledge, or run out the duration cap** — ends the slide.

## What it does

A slide is not an input-driven state. When you crouch while sprinting, the mod:

- captures your current heading off the pawn's velocity,
- opens a decay curve at your `start_speed`,
- forces the pawn's `Acceleration` and speed cap every frame until the curve bleeds below the
  walking-crouch floor or the duration cap trips,
- lets slopes bend that decay (downhill adds speed back per unit of drop, uphill sheds it), and
- lets the movement keys rotate the heading within a hard cone around the entry direction.

While you're sliding the mod is *replacing* walking physics' idea of "what direction and how fast"
with its own. The engine still runs walking physics; the mod just writes the cap and heading that
walking physics reads.

## How it's configurable

Every knob is a mod-menu slider — no restart, no file edits. Sliders live in `config.py` and are
grouped as **Sliding** in the mod menu. Values are read live at the point of use, so a change
takes effect on your next slide.

| Slider           | Default | Range        | What it does                                                                 |
| ---------------- | ------- | ------------ | ---------------------------------------------------------------------------- |
| Slide Speed      | 970     | 400 – 2000   | Speed a slide opens at, in UU/s. Deliberately a flat number, not read off the pawn — ADS, weapon swaps and class mods can't scale a slide in progress. |
| Slide Decay Rate | 1.15    | 0.2 – 3.0    | Speed units per second the slide bleeds off on flat ground. Higher = shorter slide. |
| Max Slide Duration | 2.5   | 0.5 – 10.0   | Absolute cap in seconds. Downhill momentum can hold a slide at full speed indefinitely; this is the backstop. |
| Steer Rate       | 3.0     | 0.0 – 10.0   | How sharply movement keys can curve the slide. 0 locks it straight. |
| Max Turn         | 60°     | 0 – 180°     | Hard cone around the entry heading. Steering can never bring you round to travelling backwards. |
| Downhill Boost   | 0.0005  | 0.0 – 0.02   | Speed a slide wins back per UU of downhill drop each frame. 0 disables slope gain. |
| Uphill Drag      | 0.004   | 0.0 – 0.02   | Extra speed shed per UU of uphill rise each frame, on top of time decay. 0 disables slope drag. |

**Settings sync across the network.** Every client's slider values are announced to the host on
change and again at slide open, cached under the sender's `PlayerID`, and used when the host opens
its shadow copy of that player's slide. Your settings drive your slide on both machines, not the
host's.

Non-configurable structural constants live in `constants.py` and are values that would rescale the
whole curve if changed rather than tune one facet of it (`SLIDE_SPEED_DEFAULT`,
`CROUCHED_PCT_DEFAULT`, `SLIDE_BACK_CUTOFF`, `SLIDE_STEER_DEADZONE`, `POST_LOG_EVERY`).

## Architecture

The mod is deliberately split so each concern lives in one place.

```
__init__.py    Assembly. Builds the mod, wires events -> viewmodel, registers hooks and RPCs.
constants.py   Non-tunable structural values and the shared log-throttle constant.
config.py      SliderOptions only. Each fires _on_slider_change, which re-announces to the host.
state.py       PlayerSlideState / PlayerSlideSettings dataclasses, the live-slides dict, and pure
               helpers over them. No hooks, no RPCs.
movement.py    Pure per-frame math: decay curve, heading rotation, speed-cap derivation.
lifecycle.py   Entering, exiting, driving a slide with a coroutine, and the four host-side RPCs.
hooks.py       Where the mod attaches to the game (Jump, DuckPressed, PlayerMove, MoveAutonomous).
events.py      Observer lists so presentation stays out of movement code.
viewmodel.py   First-person weapon dip/roll animation. No game state; only reacts to events.
debug.py       Rotating-file logger and the every_n throttle helper.
```

### The slide runs on a coroutine, not a hook

A slide is driven by a per-frame viewport tick coroutine, one per active slide. The coroutine
doesn't care whose pawn a slide belongs to, whether that pawn's physics ran this frame, or which
machine is authoritative — one driver serves both our own slide and, on the host, every remote
one, and it advances at frame cadence on both machines. Two copies of the same slide therefore
run down the same curve rather than one of them stepping at packet rate.

### Client owns heading; host runs a shadow driver

BL2's netcode replays each `ServerMove` on the host to re-simulate an autonomous proxy's motion.
Anything we write directly to `pawn.Velocity` for a remote pawn on the host is discarded — the
host's own simulation, seeded from the replicated `Acceleration`, is what wins. So:

- **On the owning machine** — the `inject_slide_heading` PRE-hook reads the raw input, calls
  `steer_heading` to rotate the slide direction, then overwrites the outgoing move's input axes
  with that heading before `PlayerMove` reads them. The engine builds and replicates an
  `Acceleration` pointing along the slide direction, and stamps `CrouchedPct = cap` so the
  walking-physics speed limit equals the slide speed. Walking physics does the actual moving.
- **On the host, for a remote sliding player** — heading arrives already-steered in the
  replicated `Acceleration`; the `apply_remote_cap` PRE-hook on `MoveAutonomous` just holds the
  cap so the resim isn't clamped to crouch speed. The host's coroutine runs the same decay curve
  in parallel and produces the same `cap` value each frame.

### Two-frame slide-jump handoff

Pressing jump during a slide can't rely on the ordinary jump input path — crouching swallows it.
The handoff happens across two frames of `PlayerMove`:

1. The `Jump` hook stashes the pawn's horizontal velocity and sets a pending flag.
2. On the next `PlayerMove`, if we're already airborne, we RPC that stashed velocity to the host.
   If we're still grounded (rare, same-frame case), we force `DoJump(True)` and retry next frame.
3. The host's `server_set_slide_jump_velocity` calls `DoJump` on its copy if that copy is still
   grounded, then writes the velocity — so the host doesn't just get told the resulting velocity,
   it makes the same move.

### Settings are snapshotted per slide, not read live

`PlayerSlideState` stores the seven tuning values that shaped it at open time. `slide()`,
`steer_heading()` and `compute_cap()` all read from the state, not from config. A mid-slide slider
tweak therefore takes effect on your *next* slide instead of dragging the current one's math
apart between your machine and the host's.

### Presentation is decoupled

`lifecycle.enter_slide` and `_end_slide` fire `events.slide_started` / `events.slide_ended`, which
the view model subscribes to. Movement never imports presentation. Adding a new reaction (HUD,
audio, third-person pose) is one new module plus one line in `__init__.py`.

## Networking protocol

Four host-directed RPCs, all prefixed `sliding:` on the wire:

- `server_announce_settings(...)` — client → host. Cache seven floats under the sender's
  `PlayerID`. Fired on any slider change, and again at slide open in case the on-change hook
  never ran (fresh session, listen-server with the sliders untouched).
- `server_enter_slide(dir_x, dir_y)` — client → host. Open the host's shadow slide on the
  sent heading, using previously-announced settings (or `default_settings()` if none landed).
- `server_exit_slide()` — client → host. Tear the shadow slide down.
- `server_set_slide_jump_velocity(vel_x, vel_y)` — client → host. See the two-frame handoff above.

The protocol name (`sliding:`) is pinned in `lifecycle.py` rather than left to the SDK default of
`<module>:<qualname>`, which begins with the mod's *directory name*. Without pinning, the same
mod unzipped into `sliding` on one machine and `BL2_Movement-main` on another would produce
different identifiers and drop every message in both directions.

## Debugging

Every module logs through `debug.log` to `~/bl2_slide_debug.log` (1 MB per file, three rotations).
Every line is timestamped with wall clock **and** world time (`w=<world_seconds>`):

- Wall clock is the only value two machines share, so a host and client log can be read as one
  timeline — but their clocks can be skewed by an unknown offset.
- World time (`TimeSeconds`) is exact and is the clock the netcode itself speaks in: every
  `ServerMove` and `ClientAdjustPosition` carries a matching `TimeStamp=`.

Level marker is one letter (`D` / `I` / `W` / `E`) so a per-frame trace can be visually filtered
without scanning the whole line.

Per-frame log calls are throttled through `debug.every_n`, which returns True once every N calls
under a caller-chosen key. All modules use `POST_LOG_EVERY` (30) as the interval so a scan of the
log stays aligned across modules. Drop to 1 in `constants.py` for a per-frame trace when
debugging.

The consolidated per-slide summary line is `SLIDE_TICK` — one line per throttle interval with
everything a reader would ask about a slide in progress (position, facing, heading, side/turn
angles, live input, forced vs. actual speed, decay curve position, elapsed vs. cap, ground state).
State transitions log at `INFO` (`ENTER`, `EXIT`, `SLIDE_ON`, `SLIDE_OFF`, `SERVER_JUMP`,
announce activity).

## Development

Runtime dependencies (`uemath`, `coroutines`, `tweens`) are provided by the game's embedded
Python via pyunrealsdk. They are **not** on PyPI.

```
uv venv                        # do NOT uv sync — resolving the runtime deps will fail
uv pip install --group dev     # ruff + pytest for local checks
```

The virtualenv is dev-only. The mod itself runs inside the game.

- **Lint:** `uv run ruff check .`
- **Tests:** `uv run pytest`

Style notes:

- Comments explain *why*, not what. If removing the comment wouldn't confuse a future reader,
  don't write it.
- No numeric prefixes on inline comments; match the surrounding prose style.
- Module docstrings summarise the module's role and its runs-on machine (client / host / both).

## Credits

Fork of [juso40's original Sliding mod][juso40]. The view model animation is inherited unchanged;
the movement core, networking, and settings sync were rewritten.

[juso40]: https://github.com/juso40/bl-sdk-mods

## License

MIT — see `pyproject.toml`.
