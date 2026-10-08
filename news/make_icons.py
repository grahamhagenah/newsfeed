"""Draw the icons in static/ (three white bars on black) as PNGs, with no dependencies. Run once after changing the design."""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).parent / "static"
SAMPLES = 4  # Supersampling per axis, for smooth edges.


def inside_rounded(x, y, x0, y0, x1, y1, r):
    dx = max(x0 + r - x, 0, x - (x1 - r))
    dy = max(y0 + r - y, 0, y - (y1 - r))
    return x0 <= x <= x1 and y0 <= y <= y1 and dx * dx + dy * dy <= r * r


def draw(size, corner, bars):
    """corner: background corner radius; bars: (x0, y_center, width, height), all in 0..1 units."""
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            bg = fg = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) / size
                    y = (py + (sy + 0.5) / SAMPLES) / size
                    if inside_rounded(x, y, 0, 0, 1, 1, corner):
                        bg += 1
                        if any(inside_rounded(x, y, bx, cy - h / 2, bx + w, cy + h / 2, h / 2) for bx, cy, w, h in bars):
                            fg += 1
            n = SAMPLES * SAMPLES
            alpha = bg / n
            white = fg / bg if bg else 0
            v = round(255 * white)
            row += bytes((v, v, v, round(255 * alpha)))
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


# Home-screen icons: square (the OS rounds the corners), bars kept inside the central safe zone.
APP_BARS = [(0.27, 0.38, 0.46, 0.075), (0.27, 0.50, 0.33, 0.075), (0.27, 0.62, 0.40, 0.075)]
# Tab icon: rounded square, bars larger so they read at 16–32px. Matches favicon.svg.
TAB_BARS = [(0.20, 0.30, 0.60, 0.12), (0.20, 0.50, 0.42, 0.12), (0.20, 0.70, 0.52, 0.12)]

for name, size, corner, bars in [
    ("favicon-32.png", 32, 0.22, TAB_BARS),
    ("apple-touch-icon.png", 180, 0, APP_BARS),
    ("icon-192.png", 192, 0, APP_BARS),
    ("icon-512.png", 512, 0, APP_BARS),
]:
    (OUT / name).write_bytes(png(size, draw(size, corner, bars)))
    print("wrote", name)
