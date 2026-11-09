"""GUI entry point: `espsync` / `python -m espsync`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPalette, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication

from ..config import APP_NAME, load_config

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def render_icon(size: int) -> QImage:
    """The app icon (a glow behind a screen) at one size."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
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
    return img


def make_icon() -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(render_icon(size)))
    return icon


def _setup_logging(verbose: bool) -> Path | None:
    """Console logging normally; a rotating log file for the packaged app,
    which has no console (windowed exe)."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if not getattr(sys, "frozen", False):
        logging.basicConfig(level=level, format=fmt)
        return None
    from logging.handlers import RotatingFileHandler

    from platformdirs import user_log_dir

    log_dir = Path(user_log_dir(APP_NAME, appauthor=False))
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "espsync.log"
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=level, handlers=[handler])

    def hook(exc_type, exc, tb) -> None:  # noqa: ANN001
        logging.getLogger("espsync").critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    return path


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


def _single_instance():  # noqa: ANN202 - returns QLocalServer | None
    """Make sure only one EspSync drives the lights per config file.

    Returns a listening QLocalServer for this instance, or None if another
    instance is already running (it has been told to bring its window up).
    """
    import getpass
    import hashlib

    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    from ..config import default_config_path

    digest = hashlib.sha1(str(default_config_path().resolve()).lower().encode()).hexdigest()[:10]
    name = f"{APP_NAME}-{getpass.getuser()}-{digest}"
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return None
    server = QLocalServer()
    QLocalServer.removeServer(name)  # stale socket from a crashed instance (Unix)
    server.listen(name)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="espsync", description="EspSync - screen sync for ESPHome lights")
    parser.add_argument("--config", help="config file path")
    parser.add_argument("--start", action="store_true", help="start syncing immediately")
    parser.add_argument("--minimized", action="store_true", help="start minimised (to the tray if available)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    log_path = _setup_logging(args.verbose)
    if log_path:
        logging.getLogger(__name__).info("EspSync starting; log file %s", log_path)
    if args.config:
        os.environ["ESPSYNC_CONFIG"] = str(Path(args.config).resolve())

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    icon = make_icon()
    app.setWindowIcon(icon)

    server = _single_instance()
    if server is None:
        return 0  # another EspSync with this config is running; it was asked to show itself

    from .main_window import MainWindow

    window = MainWindow(load_config(), icon)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), window._show_window()))
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
