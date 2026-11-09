"""GUI entry point: `espsync` / `python -m espsync`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication

from ..config import load_config


def make_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = float(size)
        grad = QRadialGradient(QPointF(s * 0.5, s * 0.5), s * 0.5)
        grad.setColorAt(0.0, QColor(255, 190, 90))
        grad.setColorAt(0.55, QColor(230, 70, 160))
        grad.setColorAt(1.0, QColor(60, 90, 230, 0))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(s / 2, s / 2), s / 2, s / 2)
        p.setBrush(QColor(20, 20, 26))
        p.drawRoundedRect(int(s * 0.22), int(s * 0.32), int(s * 0.56), int(s * 0.36), s * 0.05, s * 0.05)
        p.end()
        icon.addPixmap(pm)
    return icon


def dark_palette() -> QPalette:
    pal = QPalette()
    base, alt, text = QColor(30, 30, 35), QColor(40, 40, 46), QColor(225, 225, 230)
    pal.setColor(QPalette.ColorRole.Window, base)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, QColor(22, 22, 26))
    pal.setColor(QPalette.ColorRole.AlternateBase, alt)
    pal.setColor(QPalette.ColorRole.ToolTipBase, alt)
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, alt)
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, QColor(45, 108, 223))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(120, 120, 128))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(110, 110, 118))
    return pal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="espsync", description="EspSync - screen sync for ESPHome lights")
    parser.add_argument("--config", help="config file path")
    parser.add_argument("--start", action="store_true", help="start syncing immediately")
    parser.add_argument("--minimized", action="store_true", help="start minimised (to the tray if available)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.config:
        os.environ["ESPSYNC_CONFIG"] = str(Path(args.config).resolve())

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("EspSync")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    icon = make_icon()
    app.setWindowIcon(icon)

    from .main_window import MainWindow

    window = MainWindow(load_config(), icon)
    if args.minimized and window.tray is not None:
        pass  # stay in the tray
    elif args.minimized:
        window.showMinimized()
    else:
        window.show()
    if args.start:
        window.sync_btn.setChecked(True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
