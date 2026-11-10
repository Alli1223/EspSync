"""Philips Hue bridges: discover, pair (link button), choose an entertainment area, import its lights."""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, HueBridgeConfig, LightConfig
from ..hue import DiscoveredBridge, HueApi, HueArea, bridge_info, discover_bridges, hue_to_room, lights_for_area, pair
from .tasks import run_async

PAIR_TIMEOUT = 30.0
AREA_HELP = "Create or change entertainment areas in the Hue app: Settings → Entertainment areas."


def describe_position(x: float, y: float, z: float) -> str:
    """Room position in words (EspSync coordinates)."""
    side = "left" if x < -0.25 else "right" if x > 0.25 else "centre"
    depth = "by the screen" if y < -0.35 else "behind you" if y > 0.35 else "mid-room"
    height = "low" if z < 0.35 else "high" if z > 0.65 else "mid-height"
    return f"{side}, {depth}, {height}"


def channel_text(ch) -> str:  # noqa: ANN001 - HueChannel
    return f"{ch.channel_id}: {ch.label}"


def channel_tooltip(ch) -> str:  # noqa: ANN001
    x, y, z = hue_to_room(ch.x, ch.y, ch.z)
    return f"Hue position ({ch.x:+.2f}, {ch.y:+.2f}, {ch.z:+.2f}) → {describe_position(x, y, z)}"


class HuePairDialog(QDialog):
    """Polls the bridge until its link button is pressed."""

    def __init__(self, host: str, name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pair Hue bridge")
        self.host = host
        self.info: dict = {}
        self.keys: tuple[str, str] | None = None
        self._cancel = threading.Event()
        self._left = PAIR_TIMEOUT
        col = QVBoxLayout(self)
        self.title = QLabel(f"<b>{name or 'Hue bridge'}</b> at {host}")
        self.message = QLabel("Checking the bridge…")
        self.message.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, int(PAIR_TIMEOUT))
        self.progress.setTextVisible(False)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.retry = self.buttons.addButton("Try again", QDialogButtonBox.ButtonRole.ActionRole)
        self.retry.setVisible(False)
        self.retry.clicked.connect(self._start)
        self.buttons.rejected.connect(self.reject)
        for w in (self.title, self.message, self.progress, self.buttons):
            col.addWidget(w)
        self._timer = QTimer(self, interval=250, timeout=self._tick)
        self.resize(420, 0)
        run_async(lambda: bridge_info(host), self._got_info, self._failed)

    def _got_info(self, info: dict) -> None:
        self.info = info
        self.title.setText(f"<b>{info.get('name', 'Hue bridge')}</b> at {self.host}")
        self._start()

    def _start(self) -> None:
        self.retry.setVisible(False)
        self._cancel.clear()
        self._left = PAIR_TIMEOUT
        self.message.setText(
            "Press the round <b>link button</b> on top of the bridge now. EspSync pairs as soon as you do."
        )
        self.progress.setValue(int(PAIR_TIMEOUT))
        self._timer.start()
        run_async(lambda: pair(self.host, PAIR_TIMEOUT, self._cancel), self._paired, self._failed)

    def _tick(self) -> None:
        self._left = max(0.0, self._left - 0.25)
        self.progress.setValue(int(self._left + 0.99))

    def _paired(self, keys: tuple[str, str]) -> None:
        self._timer.stop()
        self.keys = keys
        self.accept()

    def _failed(self, err: str) -> None:
        self._timer.stop()
        if self._cancel.is_set():
            return
        self.message.setText(f"✘ {err}")
        self.progress.setValue(0)
        self.retry.setVisible(bool(self.info))

    def reject(self) -> None:
        self._cancel.set()
        self._timer.stop()
        super().reject()

    def done(self, result: int) -> None:  # noqa: D401 - Qt override
        self._cancel.set()
        super().done(result)


class HuePage(QWidget):
    bridgesChanged = Signal()
    lightsImported = Signal(object)  # list[LightConfig]

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.areas: dict[str, list[HueArea]] = {}  # bridge id -> its areas
        self._scanned = False
        col = QVBoxLayout(self)

        col.addWidget(QLabel("<b>Your Hue bridges</b>"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Address", "Entertainment area"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_bridge_selected)
        col.addWidget(self.table, 1)
        row = QHBoxLayout()
        for label, slot in (("Pair by IP address…", self._pair_by_ip), ("Pair again", self._repair),
                            ("Remove", self._remove)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        col.addLayout(row)

        col.addSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Entertainment area</b>"))
        self.area = QComboBox()
        self.area.currentIndexChanged.connect(self._on_area)
        head.addWidget(self.area, 1)
        self.reload_btn = QPushButton("↻")
        self.reload_btn.setFixedWidth(32)
        self.reload_btn.setToolTip("Reload the bridge's entertainment areas")
        self.reload_btn.clicked.connect(lambda: self._load_areas(self._selected()))
        head.addWidget(self.reload_btn)
        col.addLayout(head)
        self.area_status = QLabel(AREA_HELP)
        self.area_status.setWordWrap(True)
        self.area_status.setProperty("role", "hint")
        col.addWidget(self.area_status)
        self.channels = QListWidget()
        col.addWidget(self.channels, 1)
        imp = QHBoxLayout()
        self.group = QCheckBox("Gradient lightstrips as one strip")
        self.group.setToolTip("Off: every segment is its own light you can place in the room.\n"
                              "On: one strip per gradient light, sampled along the screen path through its segments.")
        self.import_btn = QPushButton("Add the area's lights to the room")
        self.import_btn.clicked.connect(self._import)
        imp.addWidget(self.group)
        imp.addStretch(1)
        imp.addWidget(self.import_btn)
        col.addLayout(imp)

        col.addSpacing(6)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Found on your network</b>"))
        head.addStretch(1)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self.scan)
        head.addWidget(self.scan_btn)
        col.addLayout(head)
        self.found = QListWidget()
        self.found.setMaximumHeight(90)
        self.found.itemDoubleClicked.connect(lambda _: self._pair_found())
        col.addWidget(self.found)
        r = QHBoxLayout()
        pair_found = QPushButton("Pair selected…")
        pair_found.clicked.connect(self._pair_found)
        r.addWidget(pair_found)
        r.addStretch(1)
        col.addLayout(r)

        self.refresh()
        if self.config.hue_bridges:
            self.table.selectRow(0)

    # --------------------------------------------------------------- bridges
    def showEvent(self, event) -> None:  # noqa: ANN001
        super().showEvent(event)
        if not self._scanned:
            self._scanned = True
            self.scan()

    def refresh(self) -> None:
        current = self._selected()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for b in self.config.hue_bridges:
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(b.name or "Hue bridge")
            name.setData(Qt.ItemDataRole.UserRole, b.id)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(b.host))
            area = b.entertainment_name or ("— choose below —" if b.paired else "not paired")
            self.table.setItem(r, 2, QTableWidgetItem(area))
            if current is not None and b.id == current.id:
                self.table.selectRow(r)
        self.table.blockSignals(False)

    def _selected(self) -> HueBridgeConfig | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.config.hue_bridge(self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    def select(self, bridge_id: str) -> None:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == bridge_id:
                self.table.selectRow(r)
                return

    def _on_bridge_selected(self) -> None:
        b = self._selected()
        if b is None:
            self._show_areas(None)
        elif b.id in self.areas:
            self._show_areas(b)
        else:
            self._load_areas(b)

    def _pair(self, host: str, name: str = "") -> None:
        dlg = HuePairDialog(host, name, self)
        if not dlg.exec() or dlg.keys is None:
            return
        info = dlg.info
        bid = str(info.get("bridgeid", "")).lower()
        bridge = next((b for b in self.config.hue_bridges if (bid and b.bridge_id == bid) or b.host == host), None)
        if bridge is None:
            bridge = HueBridgeConfig()
            self.config.hue_bridges.append(bridge)
        bridge.name = info.get("name") or name or "Hue Bridge"
        bridge.host, bridge.bridge_id = host, bid
        bridge.app_key, bridge.client_key = dlg.keys
        self.areas.pop(bridge.id, None)
        self.refresh()
        self.select(bridge.id)
        self.bridgesChanged.emit()

    def _pair_by_ip(self) -> None:
        host, ok = QInputDialog.getText(self, "Pair Hue bridge", "Bridge IP address (see the Hue app → Settings → "
                                        "Hue Bridges → ⓘ):")
        if ok and host.strip():
            self._pair(host.strip())

    def _pair_found(self) -> None:
        item = self.found.currentItem()
        d = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(d, DiscoveredBridge):
            self._pair(d.host, d.name)

    def _repair(self) -> None:
        b = self._selected()
        if b is not None:
            self._pair(b.host, b.name)

    def _remove(self) -> None:
        b = self._selected()
        if b is None:
            return
        used = [l.name for l in self.config.lights if l.transport == "hue" and l.hue_bridge == b.id]
        msg = f"Remove {b.name}?"
        if used:
            msg += f"\nThese lights use it and will be switched to preview-only: {', '.join(used)}"
        if QMessageBox.question(self, "Remove bridge", msg) != QMessageBox.StandardButton.Yes:
            return
        for light in self.config.lights:
            if light.hue_bridge == b.id:
                light.hue_bridge = ""
                if light.transport == "hue":
                    light.transport = "virtual"
        self.config.hue_bridges.remove(b)
        self.areas.pop(b.id, None)
        self.refresh()
        self._show_areas(None)
        self.bridgesChanged.emit()

    def scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.found.clear()
        self.found.addItem("Scanning…")

        def done(bridges: list[DiscoveredBridge]) -> None:
            self.scan_btn.setEnabled(True)
            self.found.clear()
            paired = {b.bridge_id for b in self.config.hue_bridges if b.paired} | {
                b.host for b in self.config.hue_bridges if b.paired}
            for d in bridges:
                tag = " (paired)" if d.bridge_id in paired or d.host in paired else ""
                item = QListWidgetItem(f"{d.name} — {d.host}  {d.model}{tag}")
                item.setData(Qt.ItemDataRole.UserRole, d)
                self.found.addItem(item)
            if not bridges:
                self.found.addItem("No Hue bridges found (is mDNS blocked?). Use 'Pair by IP address'.")

        def failed(err: str) -> None:
            self.scan_btn.setEnabled(True)
            self.found.clear()
            self.found.addItem(f"Scan failed: {err}")

        run_async(lambda: discover_bridges(3.0), done, failed)

    # ----------------------------------------------------------------- areas
    def _load_areas(self, bridge: HueBridgeConfig | None) -> None:
        if bridge is None or not bridge.paired:
            self._show_areas(bridge)
            return
        self.area_status.setText("Loading entertainment areas…")
        api = HueApi(bridge.host, bridge.app_key)
        bid = bridge.id

        def done(areas: list[HueArea]) -> None:
            self.areas[bid] = areas
            if not bridge.entertainment_id and len(areas) == 1:
                bridge.entertainment_id, bridge.entertainment_name = areas[0].id, areas[0].name
                self.refresh()
                self.bridgesChanged.emit()
            sel = self._selected()
            if sel is not None and sel.id == bid:
                self._show_areas(sel)

        def failed(err: str) -> None:
            sel = self._selected()
            if sel is not None and sel.id == bid:
                self.area_status.setText(f"✘ {err}")

        run_async(api.areas, done, failed)

    def _current_area(self) -> HueArea | None:
        b = self._selected()
        if b is None:
            return None
        return next((a for a in self.areas.get(b.id, []) if a.id == b.entertainment_id), None)

    def _show_areas(self, bridge: HueBridgeConfig | None) -> None:
        self.area.blockSignals(True)
        self.area.clear()
        self.channels.clear()
        areas = self.areas.get(bridge.id, []) if bridge else []
        if bridge is not None and not bridge.paired:
            self.area_status.setText("Not paired: use 'Pair again'.")
        for a in areas:
            self.area.addItem(f"{a.name}  ({a.kind}, {len(a.channels)} channels)", a.id)
        if bridge is not None:
            idx = self.area.findData(bridge.entertainment_id)
            if idx < 0 and areas:
                self.area.insertItem(0, "— choose an area —", "")
                idx = 0
            self.area.setCurrentIndex(max(idx, 0))
        self.area.blockSignals(False)
        self.area.setEnabled(bool(areas))
        self.import_btn.setEnabled(self._current_area() is not None)
        if bridge is not None and bridge.paired:
            self._show_channels()

    def _show_channels(self) -> None:
        self.channels.clear()
        area = self._current_area()
        b = self._selected()
        if area is None:
            if b is not None and b.id in self.areas:
                self.area_status.setText(
                    "This bridge has no entertainment areas yet. " + AREA_HELP if not self.areas[b.id]
                    else "Choose the area EspSync streams to. " + AREA_HELP
                )
            return
        status = f"{len(area.channels)} channels. "
        if area.status == "active":
            status += "Something is streaming to this area right now (Hue Sync, a TV, or EspSync). "
        self.area_status.setText(status + AREA_HELP)
        for ch in area.channels:
            x, y, z = hue_to_room(ch.x, ch.y, ch.z)
            item = QListWidgetItem(f"{channel_text(ch)}  —  {describe_position(x, y, z)}")
            item.setToolTip(channel_tooltip(ch))
            self.channels.addItem(item)

    def _on_area(self) -> None:
        b = self._selected()
        area_id = self.area.currentData()
        if b is None or not area_id:
            return
        area = next((a for a in self.areas.get(b.id, []) if a.id == area_id), None)
        if area is None:
            return
        b.entertainment_id, b.entertainment_name = area.id, area.name
        if self.area.itemData(0) == "":
            self.area.blockSignals(True)
            self.area.removeItem(0)
            self.area.blockSignals(False)
        self.import_btn.setEnabled(True)
        self.refresh()
        self._show_channels()
        self.bridgesChanged.emit()

    def _import(self) -> None:
        b = self._selected()
        area = self._current_area()
        if b is None or area is None:
            return
        existing = {tuple(l.hue_channels) for l in self.config.lights if l.transport == "hue" and l.hue_bridge == b.id}
        new: list[LightConfig] = [
            l for l in lights_for_area(b, area, self.config.room, self.group.isChecked())
            if tuple(l.hue_channels) not in existing
        ]
        if not new:
            QMessageBox.information(self, "Hue", f"All of '{area.name}' is already in the room.")
            return
        self.lightsImported.emit(new)
