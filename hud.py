"""HUD slide indicator - hide the native crouch icon while sliding and show a slide icon in its place.

BL2's crouch indicator is a Scaleform clip, `_level0.p1.crouch`, inside the WillowHUDGFxMovie. It is
frame-driven (frame 1 blank / 6 shown) with `_visible` always true; the game's `UpdateCrouched()` sets
the frame on a crouch transition. We hide it with the GFxObject `SetVisible(False)` lever on slide
start and restore it on end. Because `UpdateCrouched` only runs on a *transition* - and a slide holds
the crouch state - a one-shot hide sticks for the whole slide without a per-frame fight. A slide is
itself a crouch, so hiding the crouch icon is what "replaces" it; the slide icon is drawn in its spot.

The slide icon is a Canvas overlay drawn in `WillowGameViewportClient.PostRender` while sliding - BL2's
HUD is fully Scaleform, so the classic `HUD.PostRender`/`DrawHUD` script path never fires; the viewport
client's PostRender is the render entry that does, and it carries the `Canvas` as an argument. As a POST
hook it runs after the HUD movie has rendered, so the draw lands on top. The art is a placeholder for
now - swapped for the supplied icon texture once it lands.

No game state; only reacts to slide events, plus the render hook for the overlay - like viewmodel /
pose / effects. Runs on: LOCAL MACHINE only. The HUD is per-player, and the slide events it hangs off
(`events.slide_started` / `slide_ended`) are the first-person, owning-machine signals.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mods_base import hook
from unrealsdk import make_struct, unreal
from unrealsdk.hooks import Type

from .debug import log

CROUCH_CLIP_PATH = "_level0.p1.crouch"
"""ActionScript path of the native crouch indicator clip inside WillowHUDGFxMovie (found via the
gfx_enum probe: a direct child of the player-1 HUD root `p1`)."""

# The slide icon, baked from assets/slide.png into same-colour rectangles by tools/build_icon.py. BL2
# can't load a custom Texture2D at runtime (mip pixels are unwritable) and GFx external image loading
# crashes - but the Canvas can draw solid-colour rects, so we paint the icon as its ~700 rects. Loaded
# once here; the Color structs are built lazily on first draw (make_struct needs the SDK up).
_ICON_PATH = Path(__file__).resolve().parent / "assets" / "slide_icon.json"


def _load_icon() -> dict[str, Any] | None:
    try:
        return json.loads(_ICON_PATH.read_text(encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001 - a missing/broken icon just means no overlay art
        log.warning(f"hud icon load failed {type(ex).__name__}: {ex}")
        return None


_ICON = _load_icon()

# The HUD movie's authored stage, and how it maps to the screen. `scaleMode = exactFit` stretches the
# 1280x720 stage across the whole viewport (aspect-distorted), so a stage coordinate maps to a pixel by
# `screen = stage / STAGE_DIM * canvas_dim` at any resolution. Confirmed by the gfx_stage probe: the
# crosshairs clip's anchor lands at stage (640, 360) = dead centre, where the reticle draws.
STAGE_W = 1280.0
STAGE_H = 720.0

# Fallback position (fraction of canvas) if the crouch clip's live coords can't be read - the measured
# stage anchor (p1 + crouch = 570.3, 407.55) over the stage, i.e. where the crouch icon sits.
ICON_FRAC_X = 570.3 / STAGE_W
ICON_FRAC_Y = 407.55 / STAGE_H

# Overall size of the drawn icon relative to the crouch clip's on-screen box. 1.0 matches the native
# crouch icon's size; bump it up to make the slide icon bigger. Position/size are still being tuned.
ICON_SCALE = 1.0


class _State:
    """Slide flag + the crouch icon's cached stage geometry. The render hook reads them; events set them."""

    is_sliding: bool = False
    logged_render = False
    # (stage_x, stage_y, stage_w, stage_h) of the crouch clip, cached at slide start (the HUD layout is
    # static, so we read it once rather than every frame). None until computed / if the clip is absent.
    icon_stage: tuple[float, float, float, float] | None = None
    # The icon's rects as (x, y, w, h, Color) with the Color struct pre-built, so the per-frame draw
    # loop does no allocation. None until built on the first draw (make_struct needs the SDK loaded).
    icon_rects: list[tuple[int, int, int, int, Any]] | None = None


def _icon_rects() -> list[tuple[int, int, int, int, Any]]:
    """Icon rects with their Color structs, built once and cached."""
    if _State.icon_rects is None:
        rects: list[tuple[int, int, int, int, Any]] = []
        for x, y, w, h, r, g, b, a in (_ICON["rects"] if _ICON else []):
            rects.append((x, y, w, h, make_struct("Color", R=r, G=g, B=b, A=a)))
        _State.icon_rects = rects
    return _State.icon_rects


def _hud_movie(pc: Any) -> Any:
    """The live WillowHUDGFxMovie for this controller, or None mid-load / in a menu."""
    hud = getattr(pc, "myHUD", None)
    return getattr(hud, "HUDMovie", None) if hud is not None else None


def _crouch_clip(movie: Any) -> Any:
    """A GFxObject handle to the crouch clip, or None if the movie or clip is not there yet."""
    if movie is None:
        return None
    try:
        return movie.GetVariableObject(CROUCH_CLIP_PATH)
    except Exception:  # noqa: BLE001 - no clip mid-load; caller treats None as "nothing to do"
        return None


def _read_icon_stage(movie: Any) -> tuple[float, float, float, float] | None:
    """The crouch clip's anchor + size in stage coords, or None if it can't be read.

    Global stage position is `p1` + the clip's local `_x/_y` (both `p1` and `_level0` sit at 100% scale
    and no rotation, so the offsets just add). `_width/_height` already include the clip's own 90% scale.
    Read once at slide start and cached - the HUD layout does not move.
    """
    if movie is None:
        return None
    try:
        # GFxMoviePlayer exposes GetVariableNumber (float), not GetVariableFloat.
        p1x = float(movie.GetVariableNumber("_level0.p1._x"))
        p1y = float(movie.GetVariableNumber("_level0.p1._y"))
        cx = float(movie.GetVariableNumber(CROUCH_CLIP_PATH + "._x"))
        cy = float(movie.GetVariableNumber(CROUCH_CLIP_PATH + "._y"))
        cw = float(movie.GetVariableNumber(CROUCH_CLIP_PATH + "._width"))
        ch = float(movie.GetVariableNumber(CROUCH_CLIP_PATH + "._height"))
    except Exception as ex:  # noqa: BLE001 - fall back to the measured default if the reads fail
        log.info(f"hud _read_icon_stage failed {type(ex).__name__}: {ex}")
        return None
    return (p1x + cx, p1y + cy, cw, ch)


def _set_crouch_visible(pc: Any, visible: bool) -> None:
    """Show or hide the native crouch clip, tolerating a HUD that is not up yet.

    A missing movie/clip (loading screen, menu, HUD toggled off) is a no-op: there is nothing on
    screen to hide, and the next natural `UpdateCrouched` will paint the correct state anyway.
    """
    clip = _crouch_clip(_hud_movie(pc))
    if clip is None:
        log.info(f"hud _set_crouch_visible skip reason=no_clip visible={visible}")
        return
    try:
        clip.SetVisible(visible)
        log.info(f"hud crouch clip SetVisible({visible})")
    except Exception as ex:  # noqa: BLE001 - a cosmetic HUD failure must never break the slide
        log.warning(f"hud SetVisible failed {type(ex).__name__}: {ex}")


def on_start(pc: Any) -> None:
    """Hide the native crouch icon and start drawing the slide icon. Subscribed to `slide_started`."""
    log.info("hud.on_start enter")
    _State.is_sliding = True
    _State.logged_render = False  # re-arm the per-slide "did the draw hook fire" log
    movie = _hud_movie(pc)
    _State.icon_stage = _read_icon_stage(movie)
    _set_crouch_visible(pc, False)
    log.info(f"hud.on_start exit icon_stage={_State.icon_stage}")


def on_end(pc: Any) -> None:
    """Restore the native crouch icon and stop drawing. Subscribed to `slide_ended`.

    Restore must always run - a stuck-hidden crouch icon is the most visible failure - so it mirrors
    `_end_slide`'s discipline: unconditional, and idempotent (SetVisible(True) is harmless if already
    shown). The frame is whatever the game last set it to, which is correct for the post-slide state.
    """
    log.info("hud.on_end enter")
    _State.is_sliding = False
    _set_crouch_visible(pc, True)
    log.info("hud.on_end exit")


@hook("WillowGame.WillowGameViewportClient:PostRender", Type.POST)
def draw_slide_icon(
    _obj: unreal.UObject,
    args: unreal.WrappedStruct,
    _ret: Any,
    _func: unreal.BoundFunction,
) -> None:
    """While sliding, paint the slide icon over the (now hidden) crouch spot. Runs on: LOCAL only.

    Hooked on the viewport client's PostRender (the one render entry BL2 actually fires - the HUD's own
    PostRender/DrawHUD never run under its Scaleform HUD), with the `Canvas` taken from the call args.
    POST so it draws after the HUD movie. The icon is painted as solid-colour rectangles (see
    `_icon_rects`) since BL2 can't load a custom texture at runtime.
    """
    if not _State.is_sliding:
        return
    canvas = getattr(args, "Canvas", None)
    if canvas is None:
        return
    try:
        w = float(canvas.SizeX)
        h = float(canvas.SizeY)
    except Exception:  # noqa: BLE001 - no valid canvas this frame
        return

    # Map the crouch clip's stage anchor + size to screen pixels (exactFit -> linear). The anchor is the
    # clip's on-screen centre (verified against the crosshairs clip), so centre the tile on it and match
    # its size. Fall back to the measured fraction + a size in canvas units if the live read was absent.
    stage = _State.icon_stage
    if stage is not None:
        gx, gy, gw, gh = stage
        cx = gx / STAGE_W * w
        cy = gy / STAGE_H * h
        tw = gw / STAGE_W * w
        th = gh / STAGE_H * h
    else:
        cx = w * ICON_FRAC_X
        cy = h * ICON_FRAC_Y
        tw = th = h * 0.06

    tex = getattr(canvas, "DefaultTexture", None)
    rects = _icon_rects()
    if not _State.logged_render:
        _State.logged_render = True
        log.info(
            f"hud draw_slide_icon fired canvas=({w:.0f}x{h:.0f}) box=({tw:.0f}x{th:.0f})"
            f" at ({cx:.0f},{cy:.0f}) rects={len(rects)} tex={tex!r}"
        )
    if tex is None or not rects or _ICON is None:
        return

    # Paint the icon: map its pixel grid into a box the size of the crouch clip (scaled by ICON_SCALE),
    # centred on the crouch anchor. Each baked rect becomes one solid-colour DrawRect. Colour must go
    # through SetDrawColorStruct with a real Color struct - SetDrawColor()/the DrawColor property both
    # silently no-op on this Canvas (they drew black).
    box_w = tw * ICON_SCALE
    box_h = th * ICON_SCALE
    px_w = box_w / float(_ICON["w"])
    px_h = box_h / float(_ICON["h"])
    ox = cx - box_w / 2.0
    oy = cy - box_h / 2.0
    try:
        for rx, ry, rw, rh, color in rects:
            canvas.SetDrawColorStruct(color)
            canvas.SetPos(ox + rx * px_w, oy + ry * px_h)
            canvas.DrawRect(rw * px_w, rh * px_h, tex)
    except Exception as ex:  # noqa: BLE001 - a failed HUD draw must never break the frame
        log.warning(f"hud draw_slide_icon failed {type(ex).__name__}: {ex}")


# Passed explicitly to build_mod alongside the movement hooks: build_mod only auto-gathers hooks from
# the __init__ scope, so this render hook has to be handed over by name.
hud_hooks = [draw_slide_icon]
