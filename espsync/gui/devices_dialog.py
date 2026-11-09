"""Manage ESPHome devices: discover, add (with encryption key), test, import lights."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, DeviceConfig
from ..discovery import DiscoveredDevice, discover
from ..lights.esphome import probe_device
from .tasks import run_async

KEY_HELP = (
    "The encryption key is in the device's ESPHome YAML (ESPHome dashboard → Edit) under "
    "api: → encryption: → key:. Leave it empty if the device has no encryption."
)


class DeviceEditDialog(QDialog):
    def __init__(self, device: DeviceConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ESPHome device")
        self.device = device
        form = QFormLayout(self)
        self.name = QLineEdit(device.name)
        self.host = QLineEdit(device.host)
        self.host.setPlaceholderText("living-room-lamp.local or 192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(device.port)
        self.key = QLineEdit(device.encryption_key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("base64 key from api: encryption: key:")
        show = QCheckBox("show")
        show.toggled.connect(
            lambda v: self.key.setEchoMode(QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password)
        )
        key_row = QHBoxLayout()
        key_row.addWidget(self.key, 1)
        key_row.addWidget(show)
        self.password = QLineEdit(device.password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("legacy API password (usually empty)")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.test_btn = QPushButton("Test connection")
        self.test_btn.clicked.connect(self._test)
        help_label = QLabel(KEY_HELP)
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #999;")

        form.addRow("Name", self.name)
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow("Encryption key", key_row)
        form.addRow("Password", self.password)
        form.addRow("", help_label)
        form.addRow(self.test_btn, self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(520, 0)

    def _values(self) -> DeviceConfig:
        return DeviceConfig(
            id=self.device.id,
            name=self.name.text().strip() or self.host.text().strip(),
            host=self.host.text().strip(),
            port=self.port.value(),
            encryption_key=self.key.text().strip(),
            password=self.password.text(),
        )

    def _test(self) -> None:
        cfg = self._values()
        self.status.setText("Connecting…")
        self.test_btn.setEnabled(False)

        def done(result: tuple) -> None:
            name, lights = result
            self.test_btn.setEnabled(True)
            names = ", ".join(l.name for l in lights) or "no lights"
            self.status.setText(f"✔ Connected to {name}: {names}")
            if not self.name.text().strip():
                self.name.setText(name)

        def failed(err: str) -> None:
            self.test_btn.setEnabled(True)
            self.status.setText(f"✘ {err}")

        run_async(lambda: probe_device(cfg), done, failed)

    def _accept(self) -> None:
        if not self.host.text().strip():
            QMessageBox.warning(self, "Device", "Please enter a host name or IP address.")
            return
        v = self._values()
        for attr in ("name", "host", "port", "encryption_key", "password"):
            setattr(self.device, attr, getattr(v, attr))
        self.accept()


class DevicesDialog(QDialog):
    devicesChanged = Signal()
    importLights = Signal(str)  # device id

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("ESPHome devices")
        self.resize(760, 560)
        col = QVBoxLayout(self)

        col.addWidget(QLabel("<b>Your devices</b>"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Host", "Encryption"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(self._edit)
        col.addWidget(self.table, 2)
        row = QHBoxLayout()
        for label, slot in (
            ("Add manually…", self._add),
            ("Edit…", self._edit),
            ("Remove", self._remove),
            ("Add its lights to the room", self._import),
        ):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        col.addLayout(row)

        col.addSpacing(10)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Found on your network</b>"))
        head.addStretch(1)
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._scan)
        head.addWidget(self.scan_btn)
        col.addLayout(head)
        self.found = QListWidget()
        self.found.itemDoubleClicked.connect(lambda _: self._add_found())
        col.addWidget(self.found, 2)
        add_found = QPushButton("Add selected…")
        add_found.clicked.connect(self._add_found)
        r2 = QHBoxLayout()
        r2.addWidget(add_found)
        r2.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        r2.addWidget(close)
        col.addLayout(r2)

        self._refresh()
        self._scan()

    def _refresh(self) -> None:
        self.table.setRowCount(0)
        for d in self.config.devices:
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(d.name)
            name.setData(Qt.ItemDataRole.UserRole, d.id)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, QTableWidgetItem(f"{d.host}:{d.port}"))
            self.table.setItem(r, 2, QTableWidgetItem("key set" if d.encryption_key else "none"))

    def _selected(self) -> DeviceConfig | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        dev_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.config.device(dev_id)

    def _add(self, prefill: DeviceConfig | None = None) -> None:
        dev = prefill if isinstance(prefill, DeviceConfig) else DeviceConfig()
        if DeviceEditDialog(dev, self).exec():
            self.config.devices.append(dev)
            self._refresh()
            self.devicesChanged.emit()

    def _edit(self) -> None:
        dev = self._selected()
        if dev and DeviceEditDialog(dev, self).exec():
            self._refresh()
            self.devicesChanged.emit()

    def _remove(self) -> None:
        dev = self._selected()
        if dev is None:
            return
        used = [l.name for l in self.config.lights if l.device_id == dev.id]
        msg = f"Remove {dev.name}?"
        if used:
            msg += f"\nThese lights use it and will be switched to preview-only: {', '.join(used)}"
        if QMessageBox.question(self, "Remove device", msg) != QMessageBox.StandardButton.Yes:
            return
        for light in self.config.lights:
            if light.device_id == dev.id:
                light.device_id = ""
                if light.transport == "api":
                    light.transport = "virtual"
        self.config.devices.remove(dev)
        self._refresh()
        self.devicesChanged.emit()

    def _import(self) -> None:
        dev = self._selected()
        if dev is None:
            QMessageBox.information(self, "Devices", "Select a device first.")
            return
        self.importLights.emit(dev.id)

    def _scan(self) -> None:
        self.scan_btn.setEnabled(False)
        self.found.clear()
        self.found.addItem("Scanning…")

        def done(devices: list[DiscoveredDevice]) -> None:
            self.scan_btn.setEnabled(True)
            self.found.clear()
            known = {d.host.lower().rstrip(".") for d in self.config.devices}
            for d in devices:
                tag = " (added)" if d.host.lower() in known or d.address in known else ""
                item = QListWidgetItem(f"{d.friendly_name} — {d.host} ({d.address}) v{d.version}{tag}")
                item.setData(Qt.ItemDataRole.UserRole, d)
                self.found.addItem(item)
            if not devices:
                self.found.addItem("No ESPHome devices found (is mDNS blocked?). Use 'Add manually'.")

        def failed(err: str) -> None:
            self.scan_btn.setEnabled(True)
            self.found.clear()
            self.found.addItem(f"Scan failed: {err}")

        run_async(lambda: discover(3.0), done, failed)

    def _add_found(self) -> None:
        item = self.found.currentItem()
        d = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(d, DiscoveredDevice):
            return
        self._add(DeviceConfig(name=d.friendly_name, host=d.host, port=d.port))
