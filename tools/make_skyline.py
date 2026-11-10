"""Writes assets/skyline.svg: the night skyline of Aksum stelae.

Shapes only: the page's stylesheet moves them (classes sk-r rise, sk-t twinkle,
sk-z the zigzag). Run: python3 tools/make_skyline.py assets/skyline.svg
"""
import random
import sys

GROUND = 372
TILE = 26
BACK, MID, IVORY = "#12295A", "#263D75", "#F6EEDC"

# (x, width, top, colour, delay seconds)
STELAE = [
    # the far row
    (10, 70, 262, BACK, .00), (130, 92, 196, BACK, .12), (300, 66, 238, BACK, .06),
    (440, 96, 176, BACK, .20), (880, 90, 150, BACK, .26), (1050, 70, 222, BACK, .16),
    (1200, 100, 136, BACK, .32), (1370, 64, 262, BACK, .10),
    # the near row
    (84, 60, 302, MID, .34), (236, 76, 274, MID, .44), (372, 70, 296, MID, .40),
    (540, 70, 288, MID, .52), (838, 66, 296, MID, .50), (980, 84, 250, MID, .60),
    (1130, 78, 282, MID, .56), (1300, 72, 300, MID, .66),
    # the coin's three, in ivory
    (612, 72, 276, IVORY, .95), (694, 78, 196, IVORY, 1.20), (782, 84, 112, IVORY, 1.45),
]


def stela(i, x, w, top, fill, delay, rnd):
    r = w / 2
    tiles = 1 if w < 64 else 2
    x0 = x + (w - tiles * TILE) / 2
    first = -(-(top + r * 0.9) // TILE) * TILE          # the first whole row of windows
    rows = int((364 - first) // TILE)
    out = [f'<g class="sk-r" style="--d:{delay:.2f}s">']
    out.append(f'<path d="M{x} {GROUND}V{top + r:g}a{r:g} {r:g} 0 0 1 {w} 0V{GROUND}Z" fill="{fill}"/>')
    if rows > 0:
        pat = "skWinI" if fill == IVORY else "skWinD"
        out.append(f'<g transform="translate({x0:g} 0)"><rect y="{first:g}" width="{tiles * TILE}" height="{rows * TILE}" fill="url(#{pat})"/></g>')
        lit = "#86662A" if fill == IVORY else "#E6CF9C"
        cells = [(c, k) for c in range(tiles * 2) for k in range(rows)]
        rnd.shuffle(cells)
        for c, k in cells[: max(1, round(len(cells) * 0.3))]:
            wx = x0 + (c // 2) * TILE + (5 if c % 2 == 0 else 15)
            wy = first + k * TILE + 8
            out.append(f'<rect class="sk-t" x="{wx:g}" y="{wy:g}" width="6" height="10" fill="{lit}" '
                       f'style="--d:{rnd.uniform(0, 3.4):.2f}s;--t:{rnd.uniform(2.6, 5.2):.2f}s"/>')
    out.append('</g>')
    return "".join(out)


def main(path):
    rnd = random.Random(1906)   # a fixed seed, so the same windows are lit every time
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 400" preserveAspectRatio="xMidYMax slice" aria-hidden="true" focusable="false">',
        '<defs>',
        '<pattern id="skWinD" width="26" height="26" patternUnits="userSpaceOnUse"><rect x="5" y="8" width="6" height="10" fill="#0A1733" opacity=".6"/><rect x="15" y="8" width="6" height="10" fill="#0A1733" opacity=".6"/></pattern>',
        '<pattern id="skWinI" width="26" height="26" patternUnits="userSpaceOnUse"><rect x="5" y="8" width="6" height="10" fill="#12295A"/><rect x="15" y="8" width="6" height="10" fill="#12295A"/></pattern>',
        '<pattern id="skZig" width="24" height="14" patternUnits="userSpaceOnUse"><path d="M0 2l12 10 12-10" fill="none" stroke="#E6CF9C" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></pattern>',
        '</defs>',
    ]
    for i, s in enumerate(STELAE):
        parts.append(stela(i, *s, rnd))
    parts.append(f'<rect x="-2000" y="{GROUND}" width="5440" height="40" fill="#071027"/>')
    parts.append(f'<path d="M-2000 {GROUND}H3440" fill="none" stroke="#F6EEDC" stroke-width="3"/>')
    parts.append('<rect class="sk-z" x="-2016" y="380" width="5472" height="14" fill="url(#skZig)"/>')
    parts.append('</svg>')
    open(path, "w").write("\n".join(parts) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
