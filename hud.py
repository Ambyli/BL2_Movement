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
hook it runs after the HUD movie has rendered, so the draw lands on top. The art is a real Texture2D
loaded from a bundled package and drawn with a single DrawTile (see `_ensure_texture`).

No game state; only reacts to slide events, plus the render hook for the overlay - like viewmodel /
pose / effects. Runs on: LOCAL MACHINE only. The HUD is per-player, and the slide events it hangs off
(`events.slide_started` / `slide_ended`) are the first-person, owning-machine signals.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import get_pc, hook
from unrealsdk import make_struct, unreal
from unrealsdk.hooks import Type

from . import config
from .constants import (
    BLEND_TRANSLUCENT,
    CROUCH_CLIP_PATH,
    CROUCH_SHOWN_FRAME,
    HUD_STAGE_H,
    HUD_STAGE_W,
    RF_STANDALONE,
    SLIDE_ICON_FRAC_X,
    SLIDE_ICON_FRAC_Y,
    SLIDE_ICON_OBJECT,
    SLIDE_ICON_PACKAGE,
    SLIDE_ICON_SCALE,
)
from .debug import log

# The slide icon ships as a real Texture2D in a BL2 package (assets/sliding_slideicon.upk, built from
# assets/slide.png by tools/build_icon_upk.py). Runtime Canvas rect-painting was too slow (~7000
# rects/frame); a single DrawTile of a texture is one call. The package is copied into the game's
# CookedPCConsole on first use so BL2 finds it by name (see SLIDE_ICON_PACKAGE) and manages its
# lifetime like any cooked asset; the texture is then GC-rooted with RF_Standalone so it is never
# collected. Only the __file__-derived on-disk locations live here; the value constants are in constants.
_ASSET_DIR = Path(__file__).resolve().parent / "assets"
_ASSET_UPK = _ASSET_DIR / f"{SLIDE_ICON_PACKAGE}.upk"
_META_PATH = _ASSET_DIR / "slide_icon_meta.json"
# CookedPCConsole sits two levels up from sdk_mods/sliding, under WillowGame.
_COOKED_DIR = Path(__file__).resolve().parents[2] / "WillowGame" / "CookedPCConsole"


def _load_meta() -> dict[str, Any]:
    try:
        return json.loads(_META_PATH.read_text(encoding="utf-8"))
    except Exception as ex:  # noqa: BLE001 - fall back to whole-texture UVs if the meta is missing
        log.warning(f"hud icon meta load failed {type(ex).__name__}: {ex}")
        return {}


_META = _load_meta()


class _State:
    """Slide flag + the crouch icon's cached stage geometry. The render hook reads them; events set them."""

    is_sliding: bool = False
    logged_render = False
    # (stage_x, stage_y, stage_w, stage_h) of the crouch clip, cached at slide start (the HUD layout is
    # static, so we read it once rather than every frame). None until computed / if the clip is absent.
    icon_stage: tuple[float, float, float, float] | None = None
    # The loaded slide Texture2D (GC-rooted), plus the modulation structs the per-frame DrawTile needs,
    # pre-built so the draw allocates nothing. All None until loaded / built on first use.
    tex: Any = None
    tint: Any = None        # LinearColor(1,1,1,1) passed to DrawTile
    draw_color: Any = None  # opaque white Color for SetDrawColorStruct


def _colors() -> tuple[Any, Any]:
    """The DrawTile tint (LinearColor) and the SetDrawColorStruct value (Color), built once and cached
    (make_struct needs the SDK up, so this is lazy rather than module-level)."""
    if _State.tint is None:
        _State.tint = make_struct("LinearColor", R=1.0, G=1.0, B=1.0, A=1.0)
        _State.draw_color = make_struct("Color", R=255, G=255, B=255, A=255)
    return _State.tint, _State.draw_color


def _ensure_texture() -> Any:
    """Load the slide Texture2D once and return it, or None on failure.

    Copies the packaged .upk into CookedPCConsole if it isn't there yet (so BL2 finds it by name and
    owns its lifetime), loads it, then sets RF_Standalone so the engine GC never collects it. Every
    failure is swallowed to None - a missing icon must never break the slide, only skip the overlay.
    """
    if _State.tex is not None:
        return _State.tex
    try:
        dest = _COOKED_DIR / _ASSET_UPK.name
        # Copy when absent OR when the bundled .upk is newer than the deployed copy (so a rebuilt icon
        # is picked up on the next launch). copy2 preserves mtime, so this won't re-fire once in sync.
        # Wrapped on its own: if the deployed file is locked (busy) or the copy fails, fall through and
        # load whatever is already there rather than aborting the whole load - a stale icon beats none.
        try:
            if _ASSET_UPK.exists() and _COOKED_DIR.is_dir() and (
                not dest.exists() or _ASSET_UPK.stat().st_mtime > dest.stat().st_mtime
            ):
                shutil.copy2(_ASSET_UPK, dest)
                log.info(f"hud copied slide icon package -> {dest}")
        except OSError as ex:
            log.info(f"hud slide icon copy skipped ({type(ex).__name__}: {ex}) - using deployed copy")
        try:
            unrealsdk.load_package(SLIDE_ICON_PACKAGE)
        except Exception:  # noqa: BLE001 - by-name can miss a just-copied file; load it by path
            unrealsdk.load_package(str(dest if dest.exists() else _ASSET_UPK))
        tex = unrealsdk.find_object("Texture2D", SLIDE_ICON_OBJECT)
        tex.ObjectFlags |= RF_STANDALONE
        _State.tex = tex
        log.info(f"hud slide texture loaded {tex!r} flags=0x{int(tex.ObjectFlags):016X}")
    except Exception as ex:  # noqa: BLE001 - no texture just means no overlay art
        log.warning(f"hud slide texture load failed {type(ex).__name__}: {ex}")
        _State.tex = None
    return _State.tex


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


def _paused() -> bool:
    """Whether the game is paused or in a menu, where the slide icon should not draw. Mirrors the pause
    read the mod uses elsewhere (lifecycle); a missing/unreadable controller counts as paused so the
    icon errs toward hidden.
    """
    try:
        pc = get_pc(possibly_loading=True)
        return pc is None or bool(pc.IsPaused())
    except Exception:  # noqa: BLE001 - an unreadable state is treated as paused (hide the icon)
        return True


def _apply_crouch_override() -> None:
    """Force the native crouch icon visible + on its shown frame every frame, when the user has enabled
    "Always Show Crouch Icon". No-op otherwise, so the normal case keeps its cheap one-shot hide/show
    in on_start/on_end. Runs from the per-frame render hook; all failures are cosmetic and swallowed.
    """
    if not config.always_show_crouch_icon.value:
        return
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return
    clip = _crouch_clip(_hud_movie(pc))
    if clip is None:
        return
    try:
        clip.SetVisible(True)
        clip.GotoAndStopI(CROUCH_SHOWN_FRAME)
    except Exception as ex:  # noqa: BLE001 - a cosmetic HUD failure must never break the frame
        log.info(f"hud crouch override failed {type(ex).__name__}: {ex}")


def on_start(pc: Any) -> None:
    """Hide the native crouch icon and start drawing the slide icon. Subscribed to `slide_started`."""
    log.info("hud.on_start enter")
    _State.is_sliding = True
    _State.logged_render = False  # re-arm the per-slide "did the draw hook fire" log
    movie = _hud_movie(pc)
    _State.icon_stage = _read_icon_stage(movie)
    # Leave the crouch icon alone when the user wants it kept on screen; _apply_crouch_override drives
    # it every frame in that mode.
    if not config.always_show_crouch_icon.value:
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
    POST so it draws after the HUD movie. The icon is one DrawTile of the bundled slide Texture2D
    (see `_ensure_texture`), centred on the crouch anchor.

    Two options steer it: "Always Show Slide Icon" draws it even when not sliding (for positioning),
    and "Always Show Crouch Icon" keeps the native crouch icon on screen (see `_apply_crouch_override`).
    """
    # Crouch-icon override runs every frame, independent of whether the slide icon draws.
    _apply_crouch_override()
    # Draw while sliding, but not over menus / a paused game. "Always Show Slide Icon" forces it on and
    # ignores both the slide state and the pause state.
    if not config.always_show_slide_icon.value and (not _State.is_sliding or _paused()):
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
        cx = gx / HUD_STAGE_W * w
        cy = gy / HUD_STAGE_H * h
        tw = gw / HUD_STAGE_W * w
        th = gh / HUD_STAGE_H * h
    else:
        cx = w * SLIDE_ICON_FRAC_X
        cy = h * SLIDE_ICON_FRAC_Y
        tw = th = h * 0.06

    # Nudge onto the native crouch icon: its clip anchor sits up-left of the visible icon. The offset is
    # user-tunable (Slide Icon X/Y Offset), in stage units so it stays resolution-independent (exactFit).
    cx += float(config.icon_offset_x.value) / HUD_STAGE_W * w
    cy += float(config.icon_offset_y.value) / HUD_STAGE_H * h

    # Load the texture lazily HERE, in the PostRender context - load_package hard-faults the game if
    # called from the slide-event dispatch (game-thread mid-tick); PostRender is the verified-safe entry
    # (the dev bridge loads/draws this exact texture from the same hook). Guarded to load only once.
    was_loaded = _State.tex is not None
    tex = _ensure_texture()
    if tex is not None and not was_loaded:
        # Loaded on THIS frame - give the GPU resource a frame to finish initialising before DrawTile
        # touches it, so we never draw a not-yet-ready (null) resource. Drawing resumes next frame.
        return
    # Source region: the icon occupies the top-left content_w x content_h of the padded (power-of-two)
    # texture, so sample just that and leave the transparent pad out.
    cw = float(_META.get("content_w", 0)) or (float(tex.SizeX) if tex is not None else 0.0)
    ch = float(_META.get("content_h", 0)) or (float(tex.SizeY) if tex is not None else 0.0)
    if not _State.logged_render:
        _State.logged_render = True
        log.info(
            f"hud draw_slide_icon fired canvas=({w:.0f}x{h:.0f}) box=({tw:.0f}x{th:.0f})"
            f" at ({cx:.0f},{cy:.0f}) tex={tex!r} content=({cw:.0f}x{ch:.0f})"
        )
    if tex is None or ch <= 0.0:
        return

    # Size relative to the crouch box (resolution-independent, centred on its anchor) so it matches the
    # crouch icon it replaces. Height sets the scale; width follows the icon's own aspect (the crouch
    # box's per-axis screen scale differs, so deriving both from it would stretch the art). Crispness
    # comes from a supersampled source + clean mips, not from native-size drawing. Snap origin to whole
    # pixels so the sampler lands cleanly.
    scale = (th * SLIDE_ICON_SCALE) / ch
    box_w = cw * scale
    box_h = ch * scale
    ox = float(round(cx - box_w / 2.0))
    oy = float(round(cy - box_h / 2.0))
    tint, draw_color = _colors()
    try:
        canvas.SetDrawColorStruct(draw_color)
        canvas.SetPos(ox, oy)
        # DrawTile(Tex, XL, YL, U, V, UL, VL, LColor, ClipTile, Blend): the cw x ch source region scaled
        # to box_w x box_h. BLEND_Translucent honours the texture's alpha (default blend draws opaque).
        canvas.DrawTile(tex, box_w, box_h, 0.0, 0.0, cw, ch, tint, False, BLEND_TRANSLUCENT)
    except Exception as ex:  # noqa: BLE001 - a failed HUD draw must never break the frame
        log.warning(f"hud draw_slide_icon failed {type(ex).__name__}: {ex}")


# Passed explicitly to build_mod alongside the movement hooks: build_mod only auto-gathers hooks from
# the __init__ scope, so this render hook has to be handed over by name.
hud_hooks = [draw_slide_icon]
