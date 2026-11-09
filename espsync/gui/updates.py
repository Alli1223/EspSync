"""Update checks, prompt and install flow for the GUI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import updater
from .tasks import run_async

if TYPE_CHECKING:
    from .main_window import MainWindow

log = logging.getLogger(__name__)

FIRST_CHECK_MS = 20_000
CHECK_EVERY_MS = 6 * 60 * 60 * 1000
AUTO_INSTALL_COUNTDOWN_S = 30


class _Progress(QObject):
    """Carries download progress from the worker thread to the GUI thread."""

    changed = Signal(int, int)


class UpdateDialog(QDialog):
    def __init__(self, info: updater.UpdateInfo, installable: bool, countdown: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.choice = "later"
        self.setWindowTitle("EspSync update")
        self.resize(560, 420)
        col = QVBoxLayout(self)
        head = QLabel(
            f"<b>EspSync {info.version} is available</b> — you have {updater.current_version()}."
        )
        head.setTextFormat(Qt.TextFormat.RichText)
        col.addWidget(head)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.notes or "_No release notes._")
        col.addWidget(notes, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        col.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        col.addWidget(self.progress)

        row = QHBoxLayout()
        self.skip_btn = QPushButton("Skip this version")
        self.later_btn = QPushButton("Later")
        if installable:
            self.go_btn = QPushButton("Install and restart")
            self.status.setText("EspSync will close, update and start again. Your lights are restored first.")
        else:
            self.go_btn = QPushButton("Open release page")
            self.status.setText("This copy wasn't installed with the EspSync installer, so it can't update itself.")
        self.go_btn.setDefault(True)
        row.addWidget(self.skip_btn)
        row.addStretch(1)
        row.addWidget(self.later_btn)
        row.addWidget(self.go_btn)
        col.addLayout(row)

        self.skip_btn.clicked.connect(lambda: self._finish("skip"))
        self.later_btn.clicked.connect(lambda: self._finish("later"))
        self.go_btn.clicked.connect(lambda: self._finish("install" if installable else "open"))

        self._remaining = countdown
        self._countdown = QTimer(self, interval=1000, timeout=self._tick)
        if countdown > 0:
            self._tick_label()
            self._countdown.start()

    def _tick_label(self) -> None:
        self.go_btn.setText(f"Install now ({self._remaining} s)")

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._countdown.stop()
            self._finish("install")
        else:
            self._tick_label()

    def _finish(self, choice: str) -> None:
        self._countdown.stop()
        self.choice = choice
        if choice == "install":
            # Stay open to show the download.
            self.go_btn.setText("Downloading…")
            for b in (self.go_btn, self.skip_btn, self.later_btn):
                b.setEnabled(False)
            self.progress.setVisible(True)
            self.status.setText("Downloading the installer…")
            self.accept_install()
        else:
            self.accept()

    def accept_install(self) -> None:  # replaced by the manager
        pass

    def set_progress(self, done: int, total: int) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.status.setText(f"Downloading… {done / 1e6:.1f} of {total / 1e6:.1f} MB")

    def show_error(self, text: str) -> None:
        self.status.setText(f"✘ {text}")
        self.progress.setVisible(False)
        self.go_btn.setText("Install and restart")
        for b in (self.go_btn, self.skip_btn, self.later_btn):
            b.setEnabled(True)


class UpdateManager(QObject):
    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.config = window.config
        self.pending: updater.UpdateInfo | None = None
        self._busy = False
        self._dialog: UpdateDialog | None = None
        self._timer = QTimer(self, interval=CHECK_EVERY_MS, timeout=self.check)
        QTimer.singleShot(5000, updater.cleanup_downloads)
        if self.config.updates.check_automatically:
            QTimer.singleShot(FIRST_CHECK_MS, self.check)
        self._timer.start()

    # ------------------------------------------------------------------ check
    def check(self, manual: bool = False) -> None:
        if self._busy or (not manual and not self.config.updates.check_automatically):
            return
        self._busy = True

        def done(info: updater.UpdateInfo | None) -> None:
            self._busy = False
            self._on_checked(info, manual)

        def failed(err: str) -> None:
            self._busy = False
            log.info("update check failed: %s", err)
            if manual:
                QMessageBox.warning(self.window, "Updates", f"Couldn't check for updates: {err}")

        run_async(lambda: asyncio.to_thread(updater.check), done, failed)

    def _on_checked(self, info: updater.UpdateInfo | None, manual: bool) -> None:
        if info is None:
            if manual:
                QMessageBox.information(
                    self.window, "Updates", f"You're on the latest version ({updater.current_version()})."
                )
            return
        if not manual and info.version == self.config.updates.skipped_version:
            return
        log.info("update available: %s", info.version)
        installable = updater.is_installed_build() and info.installable
        if installable and self.config.updates.install_automatically and not manual:
            self.pending = info
            self.try_auto_install()
        else:
            self.prompt(info, installable)

    # ---------------------------------------------------------------- install
    def sync_stopped(self) -> None:
        """Called when sync stops: a deferred automatic update can go ahead."""
        if self.pending is not None:
            QTimer.singleShot(2000, self.try_auto_install)

    def try_auto_install(self) -> None:
        info = self.pending
        if info is None or self._dialog is not None:
            return
        if self.window.engine.sending:
            log.info("update %s waits until sync stops", info.version)
            return  # never interrupt a sync session
        if self.window.isVisible() and not self.window.isMinimized():
            self.prompt(info, True, countdown=AUTO_INSTALL_COUNTDOWN_S)
        else:
            tray = self.window.tray
            if tray is not None:
                tray.showMessage("EspSync", f"Updating to {info.version}…",
                                 QSystemTrayIcon.MessageIcon.Information, 5000)
            self._download_and_install(info, None)

    def prompt(self, info: updater.UpdateInfo, installable: bool, countdown: int = 0) -> None:
        if self._dialog is not None:
            return
        dlg = self._dialog = UpdateDialog(info, installable, countdown, self.window)
        dlg.accept_install = lambda: self._download_and_install(info, dlg)  # type: ignore[method-assign]
        dlg.finished.connect(lambda _: self._dialog_closed(dlg))
        dlg.show()

    def _dialog_closed(self, dlg: UpdateDialog) -> None:
        self._dialog = None
        if dlg.choice == "skip":
            self.config.updates.skipped_version = dlg.info.version
            self.pending = None
            self.window._schedule_save()
        elif dlg.choice == "later":
            self.pending = None  # ask again at the next scheduled check
        elif dlg.choice == "open":
            QDesktopServices.openUrl(QUrl(dlg.info.page_url))

    def _download_and_install(self, info: updater.UpdateInfo, dlg: UpdateDialog | None) -> None:
        progress = _Progress()
        if dlg is not None:
            progress.changed.connect(dlg.set_progress)

        def done(path: Path) -> None:
            self.pending = None
            log.info("installing update %s", info.version)
            try:
                updater.launch_installer(path)
            except OSError as exc:
                fail(str(exc))
                return
            if dlg is not None:
                dlg.status.setText("Starting the installer…")
            self.window.quit_for_update()

        def fail(err: str) -> None:
            log.warning("update failed: %s", err)
            self.pending = None
            if dlg is not None:
                dlg.show_error(err)
            elif self.window.tray is not None:
                self.window.tray.showMessage("EspSync", f"Update failed: {err}",
                                             QSystemTrayIcon.MessageIcon.Warning, 8000)

        run_async(
            lambda: asyncio.to_thread(updater.download, info, None, progress.changed.emit),
            done,
            fail,
        )
        self._progress_ref = progress  # keep the emitter alive while downloading
