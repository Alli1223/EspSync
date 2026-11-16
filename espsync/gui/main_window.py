"""Main application window."""

from __future__ import annotations

import logging
import math
import time

import numpy as np
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, LightConfig, save_config
from ..engine import SyncEngine
from ..hue import HueApi, HueArea
from ..lights.esphome import identify_light, probe_device
from ..scenes import SCENES, Scene, SceneResult, apply_scene, scene_by_id
from .areas_dialog import AreasDialog
from .devices_dialog import DevicesDialog
from .light_panel import EntityChoice, LightPanel
from .room_view import RoomView
from .screen_view import ScreenView
from .settings_panel import SettingsDialog, SettingsPanel
from .tasks import run_async
from .theme import C, glow, restyle, set_kind, set_role, swatch
from .updates import UpdateManager
from .widgets import COLOUR_ROLE, DETAIL_ROLE, TAG_ROLE, AmbientBar, Card, LightDelegate, Segmented

log = logging.getLogger(__name__)

TRANSPORT_NAMES = {"api": "ESPHome", "wled": "WLED", "e131": "E1.31", "hue": "Hue", "virtual": "Preview only"}


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, icon: QIcon) -> None:
        super().__init__()
        self.config = config
        self.engine = SyncEngine(config)
        self.setWindowTitle("EspSync")
        self.setWindowIcon(icon)
        self.resize(1360, 820)

        self._stop_restore: bool | None = None  # one-off override for the next engine.stop()

        self._restart_full = False  # the next engine restart also puts back lights that dropped out

        # ------------------------------------------------------------- header
        header = QWidget()
        header.setObjectName("header")
        bar = QHBoxLayout(header)
        bar.setContentsMargins(16, 10, 16, 10)
        bar.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icon.pixmap(26, 26))
        bar.addWidget(logo)
        bar.addWidget(set_role(QLabel("EspSync"), "brand"))
        bar.addSpacing(6)
        self.sync_btn = QPushButton("▶  Start sync")
        self.sync_btn.setCheckable(True)
        self.sync_btn.setMinimumWidth(150)
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        set_kind(self.sync_btn, "primary")
        self.sync_glow = glow(self.sync_btn, C.accent_deep)
        self.sync_btn.toggled.connect(self.set_syncing)
        bar.addWidget(self.sync_btn)
        bar.addWidget(self._divider())

        # Areas: which lights sync (and get scenes).
        bar.addWidget(set_role(QLabel("AREA"), "heading"))
        self.area_bar = Segmented()
        self.area_bar.chosen.connect(self.set_area)
        bar.addWidget(self.area_bar)
        self.area_edit = QPushButton("Edit…")
        set_kind(self.area_edit, "quiet")
        self.area_edit.setToolTip("Make areas: sets of lights to sync on their own, like the lights on your desk")
        self.area_edit.clicked.connect(self.edit_areas)
        bar.addWidget(self.area_edit)
        bar.addStretch(1)

        # Master brightness for every light (same value as Picture → Colour → Brightness).
        sun = QLabel("☀")
        sun.setToolTip("Sync brightness")
        set_role(sun, "muted")
        bar.addWidget(sun)
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(0, 200)
        self.brightness.setFixedWidth(150)
        self.brightness.setToolTip("Brightness of all lights while syncing. Above 100% lifts dark scenes.")
        self.brightness.setValue(round(config.sync.brightness * 100))
        self.brightness.valueChanged.connect(self._on_brightness)
        bar.addWidget(self.brightness)
        self.brightness_label = QLabel()
        set_role(self.brightness_label, "value")
        self._show_brightness()
        bar.addWidget(self.brightness_label)
        bar.addWidget(self._divider())
        devices = QPushButton("Devices…")
        devices.setToolTip("ESPHome devices and Philips Hue bridges")
        devices.clicked.connect(lambda: self.open_devices())
        bar.addWidget(devices)
        settings_btn = QPushButton("Settings…")
        settings_btn.setToolTip("Update rates and network load for all lights, app updates")
        settings_btn.clicked.connect(self.open_settings)
        bar.addWidget(settings_btn)
        self.ambient = AmbientBar()  # under the header: what the lights show right now

        # ---------------------------------------------------------- status bar
        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.state_pill = set_role(QLabel("PREVIEW"), "pill")
        self.status_label = set_role(QLabel(), "muted")
        status.addWidget(self.state_pill)
        status.addWidget(self.status_label, 1)

        # -------------------------------------------------- left: lights, scenes
        lights_card = Card("Lights")
        self.light_count = lights_card.add_to_header(set_role(QLabel("0"), "count"))
        self.light_list = QListWidget()
        self.light_list.setProperty("flat", True)
        self.light_list.setItemDelegate(LightDelegate(self.light_list))
        self.light_list.setMouseTracking(True)
        self.light_list.currentItemChanged.connect(self._on_list_selection)
        lights_card.add(self.light_list, 1)
        btns = QHBoxLayout()
        btns.setSpacing(4)
        add = QPushButton("+  Add")
        set_kind(add, "accent")
        add_menu = QMenu(add)
        add_menu.addAction("Lamp / bulb", lambda: self.add_light("lamp"))
        add_menu.addAction("Addressable strip", lambda: self.add_light("strip"))
        add_menu.addSeparator()
        add_menu.addAction("Lights from an ESPHome device…", lambda: self.open_devices())
        add_menu.addAction("Lights from a Philips Hue bridge…", lambda: self.open_devices("hue"))
        add.setMenu(add_menu)
        remove = QPushButton("Remove")
        set_kind(remove, "danger")
        remove.clicked.connect(self.remove_light)
        dup = QPushButton("Duplicate")
        set_kind(dup, "quiet")
        dup.clicked.connect(self.duplicate_light)
        btns.addWidget(add, 1)
        btns.addWidget(dup)
        btns.addWidget(remove)
        lights_card.col.addLayout(btns)

        # Scenes: one click puts every light of the area on a plain white (and stops sync).
        scenes_card = Card("Scenes")
        grid = QGridLayout()
        grid.setSpacing(6)
        self.scene_buttons: dict[str, QPushButton] = {}
        for i, scene in enumerate(SCENES):
            btn = QPushButton(scene.name)
            set_kind(btn, "tile")
            btn.setIcon(QIcon(self._scene_swatch(scene)))
            btn.setIconSize(QSize(16, 16))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{scene.hint}. Stops sync and sets every light in the area.")
            btn.clicked.connect(lambda _=False, sid=scene.id: self.apply_scene(sid))
            self.scene_buttons[scene.id] = btn
            grid.addWidget(btn, i // 2, i % 2)
        scenes_card.col.addLayout(grid)

        left = QWidget()
        lcol = QVBoxLayout(left)
        lcol.setContentsMargins(0, 0, 0, 0)
        lcol.setSpacing(10)
        lcol.addWidget(lights_card, 1)
        lcol.addWidget(scenes_card)
        left.setMinimumWidth(250)

        # -------------------------------------------------------- centre/right
        self.room = RoomView(config)
        self.room.lightSelected.connect(self.select_light)
        self.room.lightChanged.connect(self._on_room_moved)
        centre = Card("Room", tone="canvas")
        centre.add_to_header(set_role(QLabel("Drag a light to where it is · scroll over a lamp to set its height"),
                                      "hint"))
        centre.add(self.room, 1)

        self.screen = ScreenView(config)
        self.screen.lightSelected.connect(self.select_light)
        self.screen.regionEdited.connect(self._on_region_edited)
        preview = Card("Screen")
        preview.add_to_header(set_role(QLabel("Drag a lamp's box to move where it samples"), "hint"))
        preview.add(self.screen, 1)

        self.light_panel = LightPanel(config)
        self.light_panel.changed.connect(self._on_light_edited)
        self.light_panel.identifyRequested.connect(self.identify)
        self.light_panel.refreshEntitiesRequested.connect(self.fetch_entities)
        self.light_panel.refreshHueRequested.connect(self.fetch_hue_area)
        self.settings = SettingsPanel(config, self)
        self.settings.changed.connect(self._on_settings)
        self.settings_dialog = SettingsDialog(self.settings, self)
        tabs = self.tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self.light_panel, "Light")
        for title, page in self.settings.pages:
            tabs.addTab(page, title)
        tabs_card = Card()
        tabs_card.col.setContentsMargins(10, 8, 6, 8)
        tabs_card.add(tabs, 1)

        right = QSplitter(Qt.Orientation.Vertical)
        right.setChildrenCollapsible(False)
        right.addWidget(preview)
        right.addWidget(tabs_card)
        right.setSizes([320, 480])

        split = QSplitter()
        split.setChildrenCollapsible(False)
        split.addWidget(left)
        split.addWidget(centre)
        split.addWidget(right)
        split.setSizes([260, 570, 530])

        central = QWidget()
        col = QVBoxLayout(central)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        col.addWidget(header)
        col.addWidget(self.ambient)
        body = QWidget()
        bcol = QVBoxLayout(body)
        bcol.setContentsMargins(12, 12, 12, 10)
        bcol.addWidget(split)
        col.addWidget(body, 1)
        self.setCentralWidget(central)

        # ----------------------------------------------------------- timers
        self._save_timer = QTimer(self, singleShot=True, interval=600, timeout=self.save)
        self._restart_timer = QTimer(self, singleShot=True, interval=700, timeout=self._restart_engine)
        self._ui_timer = QTimer(self, interval=33, timeout=self._tick)
        self._preview_timer = QTimer(self, interval=120, timeout=self._tick_preview)
        self._stats_timer = QTimer(self, interval=1000, timeout=self._tick_stats)

        self.tray = self._make_tray(icon)
        self.updates = UpdateManager(self)
        self.settings.checkUpdatesRequested.connect(lambda: self.updates.check(manual=True))
        self.settings.testEffectRequested.connect(self._test_effect)
        self._refresh_areas()
        self.refresh_list()
        if config.lights:
            self.select_light(config.lights[0].id)
        for dev in config.devices:
            self.fetch_entities(dev.id, quiet=True)
        for bridge in config.hue_bridges:
            self.fetch_hue_area(bridge.id, quiet=True)

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
        self.tray_area_menu = menu.addMenu("Area")
        self.add_scene_actions(menu.addMenu("Scene"))
        menu.addAction("Show window", self._show_window)
        menu.addAction("Settings…", self.open_settings)
        menu.addAction("Check for updates", lambda: self.updates.check(manual=True))
        menu.addSeparator()
        # close() (not QApplication.quit) so closeEvent restores the lights and saves.
        menu.addAction("Quit", self.close)
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
            self.engine.stop(restore=self._stop_restore)
            self.engine.start(send=on)
        finally:
            QApplication.restoreOverrideCursor()
        self.sync_btn.setText("■  Stop sync" if on else "▶  Start sync")
        halo = QColor(C.danger_deep if on else C.accent_deep)
        halo.setAlpha(170 if on else 150)
        self.sync_glow.setColor(halo)
        self.sync_glow.setBlurRadius(26)
        if self.tray is not None:
            self.tray_toggle.setText("Stop sync" if on else "Start sync")
            self.tray.setToolTip("EspSync — syncing" if on else "EspSync")
        self._tick_stats()
        if not on:
            self.updates.sync_stopped()  # an update may have been waiting for this

    # --------------------------------------------------------------- scenes
    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(24)
        return line

    @staticmethod
    def _scene_swatch(scene: Scene):  # noqa: ANN205 - QPixmap
        if not scene.on:
            return swatch((0.16, 0.17, 0.2), 16)
        level = 0.45 + 0.55 * scene.brightness  # dim scenes still read as their colour
        return swatch(tuple(c * level for c in scene.rgb), 16)

    def add_scene_actions(self, menu: QMenu) -> None:
        for scene in SCENES:
            # The action passes its "checked" flag to the first parameter.
            menu.addAction(QIcon(self._scene_swatch(scene)), scene.name,
                           lambda _=False, sid=scene.id: self.apply_scene(sid))

    def apply_scene(self, scene_id: str) -> None:
        """Stop syncing (if running) and set every light of the active area to a preset scene."""
        scene = scene_by_id(scene_id)
        if scene is None:
            return
        if self.sync_btn.isChecked():
            # The scene replaces "restore the lights": restoring first would make
            # them jump to their old state and then to the scene.
            self._stop_restore = False
            try:
                self.sync_btn.setChecked(False)
            finally:
                self._stop_restore = None
        self.statusBar().showMessage(f"Setting {scene.name}…")

        def done(result: SceneResult) -> None:
            self.statusBar().showMessage(result.summary(scene), 8000 if result.errors else 4000)

        def failed(err: str) -> None:
            self.statusBar().showMessage(f"{scene.name} failed: {err}", 8000)

        run_async(lambda: apply_scene(self.config, scene), done, failed)

    def quit_for_update(self) -> None:
        """The installer is running: shut down (restoring the lights) so it can replace us."""
        self.close()

    def _restart_engine(self) -> None:
        full, self._restart_full = self._restart_full, False
        if not (self.engine.running and self.engine.sending):
            return
        if full:
            # Which lights sync has changed: stop properly, so that the ones that
            # dropped out are put back rather than left on their last colour.
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self.engine.stop()
                self.engine.start(send=True)
            finally:
                QApplication.restoreOverrideCursor()
        else:
            self.engine.restart()

    # ---------------------------------------------------------------- areas
    def _refresh_areas(self) -> None:
        """Rebuild the area selector (and the tray's Area menu) from the config."""
        cfg = self.config
        if cfg.area() is None:
            cfg.active_area = ""
        options = [("", "Full room", f"Sync every light ({len(cfg.lights)})")]
        for area in cfg.areas:
            n = len(area.light_ids)
            options.append((area.id, area.name or "Area", f"Sync only this area's {n} light{'' if n == 1 else 's'}"))
        self.area_bar.set_options(options, cfg.active_area)
        if self.tray is not None:
            self.tray_area_menu.clear()
            for key, label, _tip in options:
                # The action passes its "checked" flag to the first parameter.
                action = self.tray_area_menu.addAction(label, lambda _=False, k=key: self.set_area(k))
                action.setCheckable(True)
                action.setChecked(key == cfg.active_area)

    def set_area(self, area_id: str) -> None:
        """Choose which lights sync: one area's, or every light ("" = the full room)."""
        area = self.config.area(area_id) if area_id else None
        new = area.id if area else ""
        if new != self.config.active_area:
            self.config.active_area = new
            self._active_lights_changed()
            n = len(self.config.active_lights())
            self.statusBar().showMessage(
                f"{area.name if area else 'Full room'}: {n} light{'' if n == 1 else 's'} to sync", 4000
            )
        self._refresh_areas()

    def edit_areas(self) -> None:
        dlg = AreasDialog(self.config, self)
        dlg.changed.connect(self._on_areas_edited)
        dlg.exec()

    def _on_areas_edited(self) -> None:
        self._refresh_areas()
        self._active_lights_changed()

    def _active_lights_changed(self) -> None:
        """The active area or its lights changed: redraw, and re-target a running sync."""
        self.refresh_list()
        self.room.scene().update()
        self.screen.update()
        self.light_panel.update_area()
        self.engine.mark_dirty()
        self._restart_full = True
        self._schedule_restart()
        self._schedule_save()

    def _join_area(self, light: LightConfig) -> None:
        """A light added while an area is active belongs to that area."""
        area = self.config.area()
        if area is not None and light.id not in area.light_ids:
            area.light_ids.append(light.id)

    # ---------------------------------------------------------------- lights
    @staticmethod
    def _light_detail(light: LightConfig) -> str:
        what = f"Strip · {light.led_count} LEDs" if light.kind == "strip" else "Lamp"
        return f"{what} · {TRANSPORT_NAMES.get(light.transport, light.transport)}"

    def refresh_list(self) -> None:
        current = self.current_light_id()
        self.light_list.blockSignals(True)
        self.light_list.clear()
        in_area = self.config.area() is not None
        for light in self.config.lights:
            item = QListWidgetItem(light.name)
            item.setData(Qt.ItemDataRole.UserRole, light.id)
            item.setData(DETAIL_ROLE, self._light_detail(light))
            item.setData(TAG_ROLE, "" if self.config.is_active(light) else "off" if not light.enabled else "not in area")
            item.setToolTip("Addressable strip" if light.kind == "strip" else "Lamp / bulb")
            self.light_list.addItem(item)
        self._dots = {}
        self.light_list.blockSignals(False)
        total, active = len(self.config.lights), len(self.config.active_lights())
        self.light_count.setText(f"{active} of {total}" if in_area or active != total else str(total))
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
        self._join_area(light)
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
        if light.id in getattr(self.config.area(), "light_ids", ()):
            self._join_area(copy)
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
        self.config.prune_areas()
        self._refresh_areas()
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
        if kind == "membership":  # "Sync this light" or its place in the area was switched
            self._refresh_areas()
            self._active_lights_changed()
            return
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
        elif kind == "sync":
            # The Picture tab brightness slider moved: keep the header one in step.
            value = round(self.config.sync.brightness * 100)
            if self.brightness.value() != value:
                self.brightness.blockSignals(True)
                self.brightness.setValue(value)
                self.brightness.blockSignals(False)
                self._show_brightness()
        self._schedule_save()

    def _test_effect(self, kind: str) -> None:
        if not self.engine.running:
            self.engine.start(send=False)  # at least show it in the preview
        if kind.startswith("ow:"):
            self.engine.test_overwatch(kind[3:])
        elif kind == "levelup":
            self.engine.test_levelup()
        else:
            self.engine.test_low_health()

    def _on_brightness(self, value: int) -> None:
        # The capture loop reads this every frame, so it applies to the next
        # frame on every light (ESPHome and Hue) without restarting anything.
        self.config.sync.brightness = value / 100
        self._show_brightness()
        self.settings.show_brightness()
        self._schedule_save()

    def _show_brightness(self) -> None:
        self.brightness_label.setText(f"{self.brightness.value()}%")

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

    # ------------------------------------------------------------ settings
    def open_settings(self) -> None:
        """The Settings window: network load for all lights, updates.  It stays open beside the main window."""
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    # ------------------------------------------------------------- devices
    def open_devices(self, tab: str = "esphome") -> None:
        dlg = DevicesDialog(self.config, self, tab)
        dlg.devicesChanged.connect(self._on_devices_changed)
        dlg.importLights.connect(self.import_lights)
        dlg.hueLightsImported.connect(self.add_hue_lights)
        dlg.exec()

    def _on_devices_changed(self) -> None:
        self.light_panel.refresh_devices()
        for dev in self.config.devices:
            if dev.id not in self.light_panel.entities:
                self.fetch_entities(dev.id, quiet=True)
        for bridge in self.config.hue_bridges:
            cached = self.light_panel.hue_areas.get(bridge.id)
            if cached is None or cached.id != bridge.entertainment_id:
                self.fetch_hue_area(bridge.id, quiet=True)
        self._schedule_restart()
        self._schedule_save()

    def fetch_hue_area(self, bridge_id: str, quiet: bool = False) -> None:
        bridge = self.config.hue_bridge(bridge_id)
        if bridge is None or not bridge.paired or not bridge.entertainment_id:
            if not quiet and bridge is not None:
                QMessageBox.information(self, "Hue", f"Pair {bridge.name} and choose its entertainment area in "
                                        "Devices → Philips Hue first.")
            return
        api = HueApi(bridge.host, bridge.app_key)
        area_id = bridge.entertainment_id

        def done(area: HueArea) -> None:
            self.light_panel.set_hue_area(bridge_id, area)

        def failed(err: str) -> None:
            if not quiet:
                QMessageBox.warning(self, "Hue", f"Could not read {bridge.name}'s entertainment area: {err}")
            log.info("fetching Hue area from %s failed: %s", bridge.name, err)

        run_async(lambda: api.area(area_id, with_names=True), done, failed)

    def add_hue_lights(self, lights: list) -> None:
        if not lights:
            return
        self.config.lights.extend(lights)
        for light in lights:
            self._join_area(light)
        self._structure_changed()
        self.select_light(lights[0].id)
        bridge = self.config.hue_bridge(lights[0].hue_bridge)
        if bridge is not None:
            self.fetch_hue_area(bridge.id, quiet=True)
        QMessageBox.information(
            self, "Hue",
            f"Added {len(lights)} Hue light(s), placed where the Hue app has them. "
            "Drag them in the room view to fine-tune.",
        )

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
        if light is not None and light.transport == "hue":
            self._identify_hue(light)
            return
        dev = self.config.device(light.device_id) if light else None
        if light is None or dev is None or not light.entity:
            QMessageBox.information(self, "Identify", "Choose a device and light first.")
            return
        run_async(
            lambda: identify_light(dev, light.entity),
            on_error=lambda e: QMessageBox.warning(self, "Identify", f"Could not flash {light.name}: {e}"),
        )

    def _identify_hue(self, light: LightConfig) -> None:
        bridge = self.config.hue_bridge(light.hue_bridge)
        area = self.light_panel.hue_areas.get(light.hue_bridge)
        if bridge is None or area is None or not light.hue_channels:
            QMessageBox.information(self, "Identify", "Choose a Hue bridge and channel first.")
            return
        ids = {c.light_id for c in area.channels if c.channel_id in light.hue_channels and c.light_id}
        if not ids:
            QMessageBox.information(self, "Identify", "The bridge didn't say which light that channel is.")
            return
        if self.engine.sending:
            self.statusBar().showMessage("Hue lights only blink while sync is stopped", 5000)
        api = HueApi(bridge.host, bridge.app_key)

        async def blink() -> None:
            for lid in ids:
                await api.identify(lid)

        run_async(blink, on_error=lambda e: QMessageBox.warning(self, "Identify", f"Could not blink {light.name}: {e}"))

    # ---------------------------------------------------------------- ticks
    def _tick(self) -> None:
        colors = self.engine.colors()
        self.room.set_colors(colors)
        self.screen.set_colors(colors)
        if self.engine.sending and self.isVisible():
            # The Stop button's halo breathes while the lights are being driven.
            phase = 0.5 - 0.5 * math.cos(time.monotonic() * 2.6)
            self.sync_glow.setBlurRadius(20 + 16 * phase)

    def _tick_preview(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.screen.set_frame(self.engine.preview())
            self.screen.bars = self.engine.stats.bars
            self.screen.hdr = self.engine.stats.hdr
            self._update_dots()

    def _update_dots(self) -> None:
        """The dot beside each light in the list, and the bar under the header, follow what the lights show."""
        colors = self.engine.colors()
        ambient = []
        for i in range(self.light_list.count()):
            item = self.light_list.item(i)
            lid = item.data(Qt.ItemDataRole.UserRole)
            light = self.config.light(lid)
            c = colors.get(lid)
            if light is None or not self.config.is_active(light) or c is None or not len(c):
                rgb = None
            else:
                rgb = tuple(round(float(v), 2) for v in np.clip(np.asarray(c).mean(axis=0), 0.0, 1.0))
                ambient.append((light.x, QColor.fromRgbF(*rgb)))
            if self._dots.get(lid, 0) != rgb:
                self._dots[lid] = rgb
                item.setData(COLOUR_ROLE, QColor.fromRgbF(*rgb) if rgb else None)
        self.ambient.set_colours([c for _, c in sorted(ambient, key=lambda pair: pair[0])])

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
        state = ("error", "CAPTURE ERROR") if st.error else ("on", "SYNCING") if self.engine.sending else ("", "PREVIEW")
        if self.state_pill.text() != state[1]:
            self.state_pill.setText(state[1])
            self.state_pill.setProperty("state", state[0])
            restyle(self.state_pill)
        parts = []
        if st.backend:
            parts.append(f"{st.backend.upper()}{' · HDR' if st.hdr else ''}")
        parts.append(f"screen {st.capture_fps:.0f} fps")
        self.settings.show_traffic(st.send_rate if self.engine.sending else None, len(st.lights))
        if self.engine.sending:
            parts.append(f"capture→send {st.latency_ms:.1f} ms")
            parts.append(f"{st.send_rate:.0f} updates/s")
            errors = [f"{self.config.light(k).name}: {v}" for k, v in st.lights.items()
                      if v != "ready" and self.config.light(k)]
            ready = sum(1 for v in st.lights.values() if v == "ready")
            parts.append(f"{ready}/{len(st.lights)} lights ready")
            parts.extend(f"Hue {name}: {text}" for name, text in st.streams.items())
            if errors:
                parts.append(" · ".join(errors[:3]))
        else:
            parts.append("not sending to the lights")
        if st.game:
            parts.append(st.game)
        if st.error:
            parts.append(st.error)
        self.status_label.setText("   ·   ".join(parts))

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
