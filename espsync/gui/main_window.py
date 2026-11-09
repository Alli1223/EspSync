"""Main application window."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, LightConfig, save_config
from ..engine import SyncEngine
from ..lights.esphome import identify_light, probe_device
from .devices_dialog import DevicesDialog
from .light_panel import EntityChoice, LightPanel
from .room_view import RoomView
from .screen_view import ScreenView
from .settings_panel import SettingsPanel
from .tasks import run_async

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, icon: QIcon) -> None:
        super().__init__()
        self.config = config
        self.engine = SyncEngine(config)
        self.setWindowTitle("EspSync")
        self.setWindowIcon(icon)
        self.resize(1360, 820)

        # ------------------------------------------------------------ toolbar
        tb = QToolBar("Sync")
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(tb)
        self.sync_btn = QPushButton("▶  Start sync")
        self.sync_btn.setCheckable(True)
        self.sync_btn.setMinimumWidth(150)
        self.sync_btn.setStyleSheet(
            "QPushButton { padding: 6px 14px; font-weight: 600; border-radius: 5px; background: #2d6cdf; color: white; }"
            "QPushButton:checked { background: #c0392b; }"
        )
        self.sync_btn.toggled.connect(self.set_syncing)
        tb.addWidget(self.sync_btn)
        tb.addSeparator()
        devices = QAction("Devices…", self)
        devices.triggered.connect(self.open_devices)
        tb.addAction(devices)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #aaa; padding-left: 12px;")
        tb.addWidget(self.status_label)

        # ---------------------------------------------------------- left: list
        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(6, 6, 6, 6)
        lcol.addWidget(QLabel("<b>Lights</b>"))
        self.light_list = QListWidget()
        self.light_list.currentItemChanged.connect(self._on_list_selection)
        lcol.addWidget(self.light_list, 1)
        btns = QHBoxLayout()
        add = QPushButton("Add")
        add_menu = QMenu(add)
        add_menu.addAction("Lamp / bulb", lambda: self.add_light("lamp"))
        add_menu.addAction("Addressable strip", lambda: self.add_light("strip"))
        add_menu.addSeparator()
        add_menu.addAction("Lights from a device…", self.open_devices)
        add.setMenu(add_menu)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_light)
        dup = QPushButton("Duplicate")
        dup.clicked.connect(self.duplicate_light)
        for b in (add, dup, remove):
            btns.addWidget(b)
        lcol.addLayout(btns)
        left.setMinimumWidth(200)

        # -------------------------------------------------------- centre/right
        self.room = RoomView(config)
        self.room.lightSelected.connect(self.select_light)
        self.room.lightChanged.connect(self._on_room_moved)

        self.screen = ScreenView(config)
        self.screen.lightSelected.connect(self.select_light)
        self.screen.regionEdited.connect(self._on_region_edited)

        self.light_panel = LightPanel(config)
        self.light_panel.changed.connect(self._on_light_edited)
        self.light_panel.identifyRequested.connect(self.identify)
        self.light_panel.refreshEntitiesRequested.connect(self.fetch_entities)
        self.settings = SettingsPanel(config)
        self.settings.changed.connect(self._on_settings)
        tabs = QTabWidget()
        tabs.addTab(self.light_panel, "Light")
        tabs.addTab(self.settings, "Settings")

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.screen)
        right.addWidget(tabs)
        right.setSizes([330, 470])

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.room)
        split.addWidget(right)
        split.setSizes([220, 600, 540])
        self.setCentralWidget(split)

        # ----------------------------------------------------------- timers
        self._save_timer = QTimer(self, singleShot=True, interval=600, timeout=self.save)
        self._saved_portal_token = config.sync.portal_restore_token
        self._restart_timer = QTimer(self, singleShot=True, interval=700, timeout=self._restart_engine)
        self._ui_timer = QTimer(self, interval=33, timeout=self._tick)
        self._preview_timer = QTimer(self, interval=120, timeout=self._tick_preview)
        self._stats_timer = QTimer(self, interval=1000, timeout=self._tick_stats)

        self.tray = self._make_tray(icon)
        self.refresh_list()
        if config.lights:
            self.select_light(config.lights[0].id)
        for dev in config.devices:
            self.fetch_entities(dev.id, quiet=True)

        self.engine.start(send=False)  # live preview while arranging lights
        self._ui_timer.start()
        self._preview_timer.start()
        self._stats_timer.start()

    # ----------------------------------------------------------------- tray
    def _make_tray(self, icon: QIcon) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        self.tray_toggle = menu.addAction("Start sync", lambda: self.sync_btn.toggle())
        menu.addAction("Show window", self._show_window)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)
        tray.setContextMenu(menu)
        tray.setToolTip("EspSync")
        tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        tray.show()
        return tray

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ----------------------------------------------------------------- sync
    def set_syncing(self, on: bool) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.engine.stop()
            self.engine.start(send=on)
        finally:
            QApplication.restoreOverrideCursor()
        self.sync_btn.setText("■  Stop sync" if on else "▶  Start sync")
        if self.tray is not None:
            self.tray_toggle.setText("Stop sync" if on else "Start sync")
            self.tray.setToolTip("EspSync — syncing" if on else "EspSync")
        self._tick_stats()

    def _restart_engine(self) -> None:
        if self.engine.running and self.engine.sending:
            self.engine.restart()

    # ---------------------------------------------------------------- lights
    def refresh_list(self) -> None:
        current = self.current_light_id()
        self.light_list.blockSignals(True)
        self.light_list.clear()
        for light in self.config.lights:
            kind = "▬" if light.kind == "strip" else "●"
            item = QListWidgetItem(f"{kind}  {light.name}")
            item.setData(Qt.ItemDataRole.UserRole, light.id)
            if not light.enabled:
                item.setForeground(Qt.GlobalColor.gray)
            self.light_list.addItem(item)
        self.light_list.blockSignals(False)
        if current:
            self._select_in_list(current)

    def current_light_id(self) -> str | None:
        item = self.light_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _select_in_list(self, light_id: str) -> None:
        for i in range(self.light_list.count()):
            if self.light_list.item(i).data(Qt.ItemDataRole.UserRole) == light_id:
                self.light_list.blockSignals(True)
                self.light_list.setCurrentRow(i)
                self.light_list.blockSignals(False)
                return

    def select_light(self, light_id: str) -> None:
        light = self.config.light(light_id)
        self._select_in_list(light_id)
        self.room.select(light_id)
        self.screen.select(light_id)
        self.light_panel.set_light(light)

    def _on_list_selection(self, item: QListWidgetItem | None) -> None:
        if item is not None:
            self.select_light(item.data(Qt.ItemDataRole.UserRole))

    def add_light(self, kind: str, **fields: object) -> LightConfig:
        n = sum(1 for l in self.config.lights if l.kind == kind) + 1
        light = LightConfig(
            name=f"New {'strip' if kind == 'strip' else 'lamp'}" if n == 1 else f"{'Strip' if kind == 'strip' else 'Lamp'} {n}",
            kind=kind,  # type: ignore[arg-type]
            transport="wled" if kind == "strip" else "api",
        )
        if kind == "strip":
            light.region_size = 0.12
            light.x, light.y, light.x2, light.y2 = -0.3, -0.85, 0.3, -0.85
        for k, v in fields.items():
            setattr(light, k, v)
        self.config.lights.append(light)
        self._structure_changed()
        self.select_light(light.id)
        return light

    def duplicate_light(self) -> None:
        light = self.config.light(self.current_light_id() or "")
        if light is None:
            return
        from dataclasses import asdict

        data = asdict(light)
        data.pop("id")
        data["name"] = light.name + " copy"
        data["x"] = max(-1.0, min(1.0, light.x + 0.1))
        copy = LightConfig(**data)
        self.config.lights.append(copy)
        self._structure_changed()
        self.select_light(copy.id)

    def remove_light(self) -> None:
        light = self.config.light(self.current_light_id() or "")
        if light is None:
            return
        self.config.lights.remove(light)
        self.light_panel.set_light(None)
        self._structure_changed()

    def _structure_changed(self) -> None:
        self.refresh_list()
        self.room.rebuild()
        self.engine.mark_dirty()
        self._schedule_restart()
        self._schedule_save()

    # ------------------------------------------------------------- editing
    def _on_room_moved(self, light_id: str) -> None:
        self.engine.mark_dirty()
        if light_id == self.current_light_id():
            self.light_panel.set_light(self.config.light(light_id))
        self.screen.update()
        self._schedule_save()

    def _on_region_edited(self, light_id: str) -> None:
        self.engine.mark_dirty()
        if light_id == self.current_light_id():
            self.light_panel.update_region_label()
        self._schedule_save()

    def _on_light_edited(self, kind: str) -> None:
        lid = self.current_light_id()
        if kind in ("output", "rebuild"):
            self.refresh_list()
            self.room.rebuild()
            self.engine.mark_dirty()
            if kind == "output":
                self._schedule_restart()
        elif kind == "appearance":
            self.refresh_list()
            if lid:
                self.room.refresh_light(lid)
        else:
            if lid:
                self.room.refresh_light(lid)
            self.engine.mark_dirty()
        self.screen.update()
        self._schedule_save()

    def _on_settings(self, kind: str) -> None:
        if kind == "room":
            self.room.rebuild()
            self.engine.mark_dirty()
        elif kind == "output":
            self._schedule_restart()
        elif kind == "capture":
            self.engine.restart()
        self._schedule_save()

    def _schedule_restart(self) -> None:
        if self.engine.sending:
            self._restart_timer.start()

    def _schedule_save(self) -> None:
        self._save_timer.start()

    def save(self) -> None:
        try:
            save_config(self.config)
        except OSError as exc:
            self.statusBar().showMessage(f"Could not save config: {exc}", 5000)

    # ------------------------------------------------------------- devices
    def open_devices(self) -> None:
        dlg = DevicesDialog(self.config, self)
        dlg.devicesChanged.connect(self._on_devices_changed)
        dlg.importLights.connect(self.import_lights)
        dlg.exec()

    def _on_devices_changed(self) -> None:
        self.light_panel.refresh_devices()
        for dev in self.config.devices:
            if dev.id not in self.light_panel.entities:
                self.fetch_entities(dev.id, quiet=True)
        self._schedule_restart()
        self._schedule_save()

    def fetch_entities(self, device_id: str, quiet: bool = False, then=None) -> None:  # noqa: ANN001
        dev = self.config.device(device_id)
        if dev is None:
            return

        def done(result: tuple) -> None:
            _, lights = result
            choices = [EntityChoice(l.object_id or str(l.key), l.name, list(l.effects)) for l in lights]
            self.light_panel.set_entities(device_id, choices)
            if then is not None:
                then(choices)

        def failed(err: str) -> None:
            if not quiet:
                QMessageBox.warning(self, "Device", f"Could not reach {dev.name}: {err}")
            log.info("fetching lights from %s failed: %s", dev.name, err)

        run_async(lambda: probe_device(dev), done, failed)

    def import_lights(self, device_id: str) -> None:
        dev = self.config.device(device_id)
        if dev is None:
            return

        def add_all(choices: list[EntityChoice]) -> None:
            existing = {(l.device_id, l.entity) for l in self.config.lights}
            new = [c for c in choices if (device_id, c.object_id) not in existing]
            if not new:
                QMessageBox.information(self, "Devices", f"No new lights on {dev.name}.")
                return
            for i, c in enumerate(new):
                side = -1 if (len(self.config.lights) + i) % 2 == 0 else 1
                strip = any(e.lower() in ("wled", "e1.31") for e in c.effects)
                self.add_light(
                    "strip" if strip else "lamp",
                    name=c.name,
                    device_id=device_id,
                    entity=c.object_id,
                    transport="wled" if strip else "api",
                    udp_effect=next((e for e in c.effects if e.lower() in ("wled", "e1.31")), "") if strip else "",
                    x=0.75 * side,
                    y=-0.6 + 0.3 * (i // 2),
                )
            QMessageBox.information(
                self, "Devices",
                f"Added {len(new)} light(s) from {dev.name}. Drag them to where they are in your room.",
            )

        self.fetch_entities(device_id, then=add_all)

    def identify(self, light_id: str) -> None:
        light = self.config.light(light_id)
        dev = self.config.device(light.device_id) if light else None
        if light is None or dev is None or not light.entity:
            QMessageBox.information(self, "Identify", "Choose a device and light first.")
            return
        run_async(
            lambda: identify_light(dev, light.entity),
            on_error=lambda e: QMessageBox.warning(self, "Identify", f"Could not flash {light.name}: {e}"),
        )

    # ---------------------------------------------------------------- ticks
    def _tick(self) -> None:
        colors = self.engine.colors()
        self.room.set_colors(colors)
        self.screen.set_colors(colors)

    def _tick_preview(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.screen.set_frame(self.engine.preview())
            self.screen.bars = self.engine.stats.bars
            self.screen.hdr = self.engine.stats.hdr

    def _tick_stats(self) -> None:
        # The preview capture only matters while the window is on screen;
        # don't spend CPU on it from the tray.
        if not self.engine.sending:
            visible = self.isVisible() and not self.isMinimized()
            if visible and not self.engine.running:
                self.engine.start(send=False)
            elif not visible and self.engine.running:
                self.engine.stop()
        st = self.engine.stats
        parts = []
        if st.backend:
            parts.append(f"{st.backend.upper()}{' · HDR' if st.hdr else ''}")
        parts.append(f"screen {st.capture_fps:.0f} fps")
        if self.engine.sending:
            parts.append(f"capture→send {st.latency_ms:.1f} ms")
            errors = [f"{self.config.light(k).name}: {v}" for k, v in st.lights.items()
                      if v != "ready" and self.config.light(k)]
            ready = sum(1 for v in st.lights.values() if v == "ready")
            parts.append(f"{ready}/{len(st.lights)} lights ready")
            if errors:
                parts.append(" · ".join(errors[:3]))
        else:
            parts.append("preview (not sending)")
        if st.error:
            parts.append(st.error)
        self.status_label.setText("   ·   ".join(parts))
        # The Wayland portal issues a new single-use restore token on every
        # capture start; save it now or the next launch asks for the screen again.
        if self.config.sync.portal_restore_token != self._saved_portal_token:
            self._saved_portal_token = self.config.sync.portal_restore_token
            self.save()

    # -------------------------------------------------------------- closing
    def closeEvent(self, event: QCloseEvent) -> None:
        self._ui_timer.stop()
        self._preview_timer.stop()
        self._stats_timer.stop()
        self.save()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.engine.shutdown()  # restores lights if configured
        finally:
            QApplication.restoreOverrideCursor()
        if self.tray is not None:
            self.tray.hide()
        event.accept()
        QApplication.quit()
