"""Render the app icon into a multi-resolution Windows .ico (PNG-compressed entries).

    python packaging/make_icon.py [output.ico]
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from espsync.gui.app import ICON_SIZES, render_icon  # noqa: E402


def png_bytes(size: int) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    render_icon(size).save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path: Path) -> None:
    images = [(s, png_bytes(s)) for s in ICON_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    path.write_bytes(header + entries + blobs)


if __name__ == "__main__":
    app = QGuiApplication(sys.argv[:1])
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("espsync.ico")
    write_ico(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
