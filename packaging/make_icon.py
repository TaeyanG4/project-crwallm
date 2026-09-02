"""Draw the application icon, with no image library.

Pillow is a 3 MB dependency for one file that changes about never, and the
build already has enough moving parts. ``zlib`` and ``struct`` are in the
standard library and a PNG is not complicated: a header, a stream of
scanlines, a CRC per chunk.

The design has to survive 16x16 in a taskbar, which rules out anything with
detail. Three bars narrowing downward - a list being funnelled into a smaller
one - is the whole idea of the program and stays legible at any size.

Run: ``python packaging/make_icon.py``
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "crwallm.ico"
SIZES = (256, 128, 64, 48, 32, 16)

BLUE = (29, 78, 216)
WHITE = (255, 255, 255)

Pixel = tuple[int, int, int, int]


def rounded(x: float, y: float, size: float, radius: float) -> bool:
    """Inside a rounded square inset from the edges."""
    pad = size * 0.06
    lo, hi = pad, size - pad
    if not (lo <= x <= hi and lo <= y <= hi):
        return False
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def bar(x: float, y: float, size: float, index: int) -> bool:
    """One of three white bars, each narrower and shorter than the last."""
    widths = (0.62, 0.44, 0.26)
    tops = (0.28, 0.47, 0.66)
    height = size * 0.11
    width = size * widths[index]
    top = size * tops[index]
    return abs(x - size / 2) <= width / 2 and top <= y <= top + height


def pixel(px: int, py: int, size: int) -> Pixel:
    """Colour one pixel, sampled 3x3 so the curves are not staircases."""
    hits_shape = 0
    hits_bar = 0
    samples = 0
    radius = size * 0.22
    for sy in range(3):
        for sx in range(3):
            x = px + (sx + 0.5) / 3
            y = py + (sy + 0.5) / 3
            samples += 1
            if rounded(x, y, size, radius):
                hits_shape += 1
                if any(bar(x, y, size, i) for i in range(3)):
                    hits_bar += 1

    if hits_shape == 0:
        return (0, 0, 0, 0)

    alpha = round(255 * hits_shape / samples)
    mix = hits_bar / hits_shape
    rgb = tuple(round(BLUE[i] + (WHITE[i] - BLUE[i]) * mix) for i in range(3))
    return (rgb[0], rgb[1], rgb[2], alpha)


def png(size: int) -> bytes:
    raw = bytearray()
    for py in range(size):
        raw.append(0)  # filter type 0, per scanline
        for px in range(size):
            raw.extend(pixel(px, py, size))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def ico(images: dict[int, bytes]) -> bytes:
    """An ICO is a directory of images; PNG entries are allowed and smaller."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = bytearray()
    body = bytearray()
    for size, data in images.items():
        # 256 is written as 0 - the field is one byte.
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(body)
        )
        body += data
    return header + bytes(entries) + bytes(body)


def main() -> None:
    OUT.write_bytes(ico({size: png(size) for size in SIZES}))
    print(f"{OUT}  {OUT.stat().st_size / 1024:.1f} KB  sizes={list(SIZES)}")


if __name__ == "__main__":
    main()
