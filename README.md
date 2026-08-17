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
Requires the mod on every player in the session. Movement is host-authoritative, so remote players slide correctly whatever the host is doing — jumping, driving, or sitting in a menu.

## Credits
Fork of [juso40's original Sliding mod](https://github.com/juso40/bl2sdk-mods). The movement core has been rewritten around a host-driven state model with steering, a turn-limit backstop, and a locked slide speed.
