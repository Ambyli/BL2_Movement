"""Bake a PNG icon into a compact rectangle list the HUD can paint with Canvas.DrawRect.

BL2 can't load a custom Texture2D at runtime (mip bulk data is unwritable; loadMovie crashes), but the
Canvas *can* draw arbitrary solid-colour rectangles (SetDrawColorStruct + DrawRect). So instead of a
texture we decompose the icon into a small set of same-colour rectangles (greedy 2D merge, light colour
quantisation, transparent pixels skipped) and paint those. hud.py loads the result and draws it.

Run after editing the art:  python tools/build_icon.py
Reads  assets/slide.png  ->  writes  assets/slide_icon.json  ({w, h, rects: [[x,y,w,h,r,g,b,a], ...]}).

Dev-time only (needs Pillow, a dev dependency). Not run in-game.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

QUANT = 8          # colour quantisation step (per channel) - merges anti-aliasing noise for fewer rects
ALPHA_SKIP = 24    # pixels with alpha below this are treated as fully transparent (not drawn)
ALPHA_SOLID = 200  # at/above this, snap alpha to 255 (helps merging); between, keep for edge blending

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "slide.png"
OUT = ROOT / "assets" / "slide_icon.json"


def main() -> None:  # noqa: PLR0912 - one linear greedy-decomposition pass, clearer left inline
    im = Image.open(SRC).convert("RGBA")
    w, h = im.size
    px = im.load()

    def q(v: int) -> int:
        return (v // QUANT) * QUANT

    grid: list[list[tuple[int, int, int, int] | None]] = [[None] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < ALPHA_SKIP:
                continue
            grid[y][x] = (q(r), q(g), q(b), 255 if a >= ALPHA_SOLID else a)

    used = [[False] * w for _ in range(h)]
    rects: list[list[int]] = []
    for y in range(h):
        for x in range(w):
            if used[y][x] or grid[y][x] is None:
                continue
            c = grid[y][x]
            # grow width along the row
            x2 = x
            while x2 + 1 < w and not used[y][x2 + 1] and grid[y][x2 + 1] == c:
                x2 += 1
            # grow height while every cell of the candidate row matches
            y2 = y
            growing = True
            while growing and y2 + 1 < h:
                for xx in range(x, x2 + 1):
                    if used[y2 + 1][xx] or grid[y2 + 1][xx] != c:
                        growing = False
                        break
                if growing:
                    y2 += 1
            for yy in range(y, y2 + 1):
                for xx in range(x, x2 + 1):
                    used[yy][xx] = True
            r, g, b, a = c
            rects.append([x, y, x2 - x + 1, y2 - y + 1, r, g, b, a])

    OUT.write_text(json.dumps({"w": w, "h": h, "rects": rects}), encoding="utf-8")
    print(f"{SRC.name} {w}x{h} -> {OUT.name}: {len(rects)} rects")


if __name__ == "__main__":
    main()
