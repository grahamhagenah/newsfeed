"""Draw the icons in static/ as PNGs, with no dependencies: the home-screen icons a white "n" and a green period on
black, the logo's "newsfeed." in short, and the tab icon the period alone. Run once after changing the design;
favicon.svg is the tab icon's same drawing."""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "static"
SAMPLES = 4  # Supersampling per axis, for smooth edges.
WHITE, GREEN = (255, 255, 255), (52, 211, 153)  # The page's text, and the articles' green (--article).


def inside_rounded(x, y, x0, y0, x1, y1, r):
    dx = max(x0 + r - x, 0, x - (x1 - r))
    dy = max(y0 + r - y, 0, y - (y1 - r))
    return x0 <= x <= x1 and y0 <= y <= y1 and dx * dx + dy * dy <= r * r


def mark(scale):
    """The "n." as (is this point inside it, its color) pairs, in 0..1 units, scaled about the middle: an "n" of a
    stem and an arch of the same weight, and the period on its baseline."""
    def at(v):
        return .5 + (v - .5) * scale

    t, x0, x1, top, base = .14 * scale, at(.165), at(.585), at(.3), at(.74)  # "n." centered across.
    radius = (x1 - x0) / 2
    cx, cy = x0 + radius, top + radius
    dot = .085 * scale
    dot_x, dot_y = x1 + .08 * scale + dot, base - dot

    def n(x, y):
        stem = x0 <= x <= x0 + t and top <= y <= base  # Full height, square at the top, as an "n" is.
        right = x1 - t <= x <= x1 and cy <= y <= base
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** .5
        arch = y <= cy and radius - t <= d <= radius
        return stem or right or arch

    return [(n, WHITE), (lambda x, y: (x - dot_x) ** 2 + (y - dot_y) ** 2 <= dot * dot, GREEN)]


def draw(size, corner, shapes):
    """corner: the black background's corner radius; shapes: (inside, color) pairs, all in 0..1 units."""
    rows = []
    n = SAMPLES * SAMPLES
    for py in range(size):
        row = bytearray()
        for px in range(size):
            bg, rgb = 0, [0, 0, 0]
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) / size
                    y = (py + (sy + 0.5) / SAMPLES) / size
                    if inside_rounded(x, y, 0, 0, 1, 1, corner):
                        bg += 1
                        color = next((color for inside, color in shapes if inside(x, y)), None)
                        if color:
                            rgb = [total + part for total, part in zip(rgb, color)]
            row += bytes(round(part / bg) if bg else 0 for part in rgb) + bytes((round(255 * bg / n),))
        rows.append(bytes(row))
    return rows


def png(size, rows):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def dot(size, radius):
    """The green period alone, a circle radius across (in 0..1 units) in the middle of a clear square, its edge
    smoothed by how much of each pixel it covers."""
    rows = []
    n = SAMPLES * SAMPLES
    for py in range(size):
        row = bytearray()
        for px in range(size):
            covered = sum(((px + (sx + .5) / SAMPLES) / size - .5) ** 2 + ((py + (sy + .5) / SAMPLES) / size - .5) ** 2 <= radius ** 2
                          for sy in range(SAMPLES) for sx in range(SAMPLES))
            row += bytes(GREEN) + bytes((round(255 * covered / n),))
        rows.append(bytes(row))
    return rows


# Tab icon: the green period alone, about two thirds of the tab's square across, with room around it, as a tab's
# other icons have; filling the square, it stood out larger than any of them.
DOT = .32  # Its radius, of the square's width. favicon.svg's circle is the same: r="32" in a 100 box.
(OUT / "favicon-32.png").write_bytes(png(32, dot(32, DOT)))
print("wrote favicon-32.png")

# Home-screen icons: square (the OS rounds the corners), the mark kept inside the central safe zone.
for name, size in [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)]:
    (OUT / name).write_bytes(png(size, draw(size, 0, mark(.72))))
    print("wrote", name)
