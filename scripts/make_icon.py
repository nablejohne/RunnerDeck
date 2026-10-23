"""Generate the application icon (docs/m2-design.md §6).

    uv run python scripts/make_icon.py [--out-dir src/runnerdeck/ui/resources]

Draws a 256 px rounded square in the Fluent accent colour with a white "RD" using QPainter,
then writes `runnerdeck.png` (256 px) and `runnerdeck.ico` (16 / 32 / 48 / 256). Qt's ICO
plugin only writes one image per file, so the multi-size ICO container is assembled here in
pure Python: the three small sizes as 32-bit DIB entries, the 256 px one PNG-compressed (the
standard layout since Windows Vista). Runs offscreen; the generated files are committed.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # before any Qt import

from PyQt5.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PyQt5.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
)

PNG_NAME = "runnerdeck.png"
ICO_NAME = "runnerdeck.ico"
BASE_SIZE = 256
ICO_SIZES = (16, 32, 48, 256)
ACCENT = QColor("#0078D4")  # Fluent accent blue
TEXT = "RD"
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "src" / "runnerdeck" / "ui" / "resources"


def draw_icon(size: int = BASE_SIZE) -> QImage:
    """Rounded accent square with white bold "RD", on a transparent background."""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        radius = size * 0.19
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
        painter.fillPath(path, ACCENT)

        font = QFont("Segoe UI", -1, QFont.Bold)
        font.setPixelSize(int(size * 0.46))
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(QRectF(0, 0, size, size * 1.02), Qt.AlignCenter, TEXT)
    finally:
        painter.end()
    return image


def _scaled(image: QImage, size: int) -> QImage:
    if image.width() == size:
        return image
    return image.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("QImage.save(PNG) failed")
    return bytes(buffer.data())


def _dib_bytes(image: QImage) -> bytes:
    """ICO entry payload: BITMAPINFOHEADER + bottom-up 32-bit BGRA pixels + empty AND mask."""
    image = image.convertToFormat(QImage.Format_ARGB32)
    width, height = image.width(), image.height()
    mask_row = ((width + 31) // 32) * 4
    pixel_bytes = width * height * 4
    mask_bytes = mask_row * height
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        width,
        height * 2,  # XOR + AND mask
        1,  # planes
        32,  # bit count
        0,  # BI_RGB
        pixel_bytes + mask_bytes,
        0,
        0,
        0,
        0,
    )
    pixels = bytearray()
    for y in range(height - 1, -1, -1):  # bottom-up
        for x in range(width):
            argb = image.pixel(x, y)
            pixels += struct.pack(
                "<BBBB", argb & 0xFF, (argb >> 8) & 0xFF, (argb >> 16) & 0xFF, (argb >> 24) & 0xFF
            )
    return header + bytes(pixels) + bytes(mask_bytes)


def build_ico(base: QImage, sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """Multi-size ICO container (ICONDIR + ICONDIRENTRYs + payloads)."""
    entries: list[tuple[int, bytes]] = []
    for size in sizes:
        image = _scaled(base, size)
        payload = _png_bytes(image) if size >= 256 else _dib_bytes(image)
        entries.append((size, payload))

    header = struct.pack("<HHH", 0, 1, len(entries))  # reserved, type=icon, count
    directory = bytearray()
    payloads = bytearray()
    offset = len(header) + 16 * len(entries)
    for size, payload in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset)
        payloads += payload
        offset += len(payload)
    return header + bytes(directory) + bytes(payloads)


def write_icons(out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = draw_icon()
    png_path = out_dir / PNG_NAME
    if not base.save(str(png_path), "PNG"):
        raise RuntimeError(f"cannot write {png_path}")
    ico_path = out_dir / ICO_NAME
    ico_path.write_bytes(build_ico(base))
    return png_path, ico_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate runnerdeck.png / runnerdeck.ico")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)
    _app = QGuiApplication([sys.argv[0]])  # fonts need a QGuiApplication
    png_path, ico_path = write_icons(args.out_dir)
    print(png_path)
    print(ico_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
