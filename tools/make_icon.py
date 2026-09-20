#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    make_icon.py
# Description: Draw the SigenEnergyManager Plugin Store icon in the Highsteads house
#              style — rounded navy square, name above, glyph, subtitle below.
# Author:      CliveS & Claude Opus 5
# Date:        20-09-2026
# Version:     1.0
#
# Official requirement (indigo-reference/official/official-plugin-dev.md:391):
# Contents/Resources/icon.png, PNG, 256x256 optimal, never under 128px high.
# Drawn at 4x and downsampled, which is the cheapest way to get clean edges.

from PIL import Image, ImageDraw, ImageFont

S      = 1024                     # working size; final is S // SCALE
SCALE  = 4
NAVY_T = (26, 42, 68)             # top of the background gradient
NAVY_B = (12, 22, 40)
CYAN   = (94, 214, 236)
AMBER  = (255, 186, 62)
WHITE  = (247, 250, 252)
BORDER = (58, 84, 118)

BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def font(px):
    return ImageFont.truetype(BOLD, px)


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=255)
    return m


def background():
    bg = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(bg)
    for y in range(S):
        t = y / (S - 1)
        d.line([(0, y), (S, y)],
               fill=tuple(int(a + (b - a) * t) for a, b in zip(NAVY_T, NAVY_B)))
    return bg


def centred(d, text, y, px, fill, spacing=0):
    f = font(px)
    if not spacing:
        w = d.textbbox((0, 0), text, font=f)[2]
        d.text(((S - w) / 2, y), text, font=f, fill=fill)
        return
    widths = [d.textbbox((0, 0), c, font=f)[2] for c in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = (S - total) / 2
    for c, w in zip(text, widths):
        d.text((x, y), c, font=f, fill=fill)
        x += w + spacing


def draw():
    img = background()
    d = ImageDraw.Draw(img)

    # ── name, and the rule under it (the Zigbee2MQTT pattern) ────────────
    centred(d, "SIGENERGY", 96, 132, WHITE, spacing=2)
    d.rounded_rectangle([250, 258, S - 250, 268], 5, fill=CYAN)

    # ── the sun, behind and above the battery's right shoulder ───────────
    cx, cy, r = 690, 430, 74
    for i in range(8):                                   # eight rays
        import math
        a = math.radians(i * 45)
        x0, y0 = cx + math.cos(a) * (r + 26), cy + math.sin(a) * (r + 26)
        x1, y1 = cx + math.cos(a) * (r + 62), cy + math.sin(a) * (r + 62)
        d.line([(x0, y0), (x1, y1)], fill=AMBER, width=17)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=AMBER)

    # ── the battery, upright, charged to about seven tenths ──────────────
    bx0, bx1 = 300, 560
    by0, by1 = 380, 760
    d.rounded_rectangle([bx0 + 74, by0 - 44, bx1 - 74, by0 + 14], 14, fill=WHITE)  # terminal
    d.rounded_rectangle([bx0, by0, bx1, by1], 34, fill=NAVY_B, outline=WHITE, width=20)
    inner = 44
    fill_top = by0 + inner + int((by1 - by0 - 2 * inner) * 0.30)
    d.rounded_rectangle([bx0 + inner, fill_top, bx1 - inner, by1 - inner], 12, fill=CYAN)

    # ── subtitle ─────────────────────────────────────────────────────────
    centred(d, "MANAGER", 838, 96, CYAN, spacing=16)

    # ── rounded corners + a hairline edge, as the others have ────────────
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), rounded_mask(S, 196))
    ImageDraw.Draw(out).rounded_rectangle([6, 6, S - 7, S - 7], 190,
                                          outline=BORDER + (255,), width=12)
    return out.resize((S // SCALE, S // SCALE), Image.LANCZOS)


if __name__ == "__main__":
    import sys
    draw().save(sys.argv[1])
    print("written:", sys.argv[1])
