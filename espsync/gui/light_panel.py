"""Property editor for the selected light."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, LightConfig
from ..mapping import SCREEN_PRESETS

TRANSPORTS = [
    ("api", "ESPHome API (direct)"),
    ("wled", "WLED realtime UDP (per-LED)"),
    ("e131", "E1.31 / sACN UDP (per-LED)"),
    ("virtual", "Preview only (no device)"),
]
ROOM_LAYOUT = "In the room (start → end)"


@dataclass
class EntityChoice:
    object_id: str
    name: str
    effects: list[str]


def _slider(lo: int, hi: int) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    return s


class LightPanel(QWidget):
    changed = Signal(str)  # "layout" | "output" | "appearance"
    identifyRequested = Signal(str)
    refreshEntitiesRequested = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.light: LightConfig | None = None
        self.entities: dict[str, list[EntityChoice]] = {}
        self._loading = False

        outer = QVBoxLayout(self)
        self.empty = QLabel("Select a light in the room or the list.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty)
        self.body = QWidget()
        outer.addWidget(self.body)
        outer.addStretch(1)
        form = self.form = QFormLayout(self.body)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.name = QLineEdit()
        self.enabled = QCheckBox("Sync this light")
        self.kind = QComboBox()
        self.kind.addItem("Lamp / bulb (one colour)", "lamp")
        self.kind.addItem("Addressable strip (per-LED)", "strip")
        self.transport = QComboBox()
        for key, label in TRANSPORTS:
            self.transport.addItem(label, key)

        self.device = QComboBox()
        self.entity = QComboBox()
        self.entity.setEditable(True)
        self.entity.setToolTip("ESPHome light object_id, e.g. 'living_room_lamp'")
        self.refresh_btn = QPushButton("↻")
        self.refresh_btn.setFixedWidth(32)
        self.refresh_btn.setToolTip("Fetch the device's lights")
        ent_row = QHBoxLayout()
        ent_row.setContentsMargins(0, 0, 0, 0)
        ent_row.addWidget(self.entity, 1)
        ent_row.addWidget(self.refresh_btn)
        self.entity_row = QWidget()
        self.entity_row.setLayout(ent_row)

        self.udp_host = QLineEdit()
        self.udp_host.setPlaceholderText("blank = the device's address")
        self.udp_port = QSpinBox()
        self.udp_port.setRange(0, 65535)
        self.udp_port.setSpecialValueText("default")
        self.universe = QSpinBox()
        self.universe.setRange(1, 63999)
        self.udp_effect = QComboBox()
        self.udp_effect.setEditable(True)
        self.udp_effect.addItems(["", "WLED", "E1.31"])
        self.udp_effect.setToolTip("Effect EspSync switches on while syncing (blank = leave the light alone)")

        self.height = _slider(0, 100)
        self.height.setToolTip("0 = floor, 50 = screen centre height, 100 = ceiling")
        self.size = _slider(2, 100)
        self.brightness = _slider(0, 200)
        self.leds = QSpinBox()
        self.leds.setRange(1, 3000)
        self.layout_combo = QComboBox()
        self.layout_combo.addItem(ROOM_LAYOUT)
        self.layout_combo.addItems(list(SCREEN_PRESETS))
        self.reverse = QCheckBox("LED order reversed")
        self.manual = QLabel()
        self.reset_region = QPushButton("Use automatic region")
        self.identify = QPushButton("Identify (flash)")

        form.addRow("Name", self.name)
        form.addRow("", self.enabled)
        form.addRow("Type", self.kind)
        form.addRow("Output", self.transport)
        form.addRow("Device", self.device)
        form.addRow("Light", self.entity_row)
        form.addRow("UDP host", self.udp_host)
        form.addRow("UDP port", self.udp_port)
        form.addRow("Universe", self.universe)
        form.addRow("Effect", self.udp_effect)
        form.addRow("LED count", self.leds)
        form.addRow("Strip layout", self.layout_combo)
        form.addRow("", self.reverse)
        form.addRow("Height", self.height)
        form.addRow("Sample size", self.size)
        form.addRow("Brightness", self.brightness)
        form.addRow("Region", self.manual)
        form.addRow("", self.reset_region)
        form.addRow("", self.identify)

        self.name.textEdited.connect(self._on_name)
        self.enabled.toggled.connect(lambda v: self._set("enabled", v, "output"))
        self.kind.currentIndexChanged.connect(self._on_kind)
        self.transport.currentIndexChanged.connect(self._on_transport)
        self.device.currentIndexChanged.connect(self._on_device)
        self.entity.currentTextChanged.connect(self._on_entity)
        self.refresh_btn.clicked.connect(lambda: self.refreshEntitiesRequested.emit(self.light.device_id if self.light else ""))
        self.udp_host.textEdited.connect(lambda v: self._set("udp_host", v.strip(), "output"))
        self.udp_port.valueChanged.connect(lambda v: self._set("udp_port", v, "output"))
        self.universe.valueChanged.connect(lambda v: self._set("universe", v, "output"))
        self.udp_effect.currentTextChanged.connect(lambda v: self._set("udp_effect", v.strip(), "output"))
        self.leds.valueChanged.connect(lambda v: self._set("led_count", v, "output"))
        self.layout_combo.currentIndexChanged.connect(self._on_layout)
        self.reverse.toggled.connect(lambda v: self._set("reverse", v, "layout"))
        self.height.valueChanged.connect(lambda v: self._set("z", v / 100, "layout"))
        self.size.valueChanged.connect(lambda v: self._set("region_size", v / 100, "layout"))
        self.brightness.valueChanged.connect(lambda v: self._set("brightness", v / 100, "appearance"))
        self.reset_region.clicked.connect(self._on_reset_region)
        self.identify.clicked.connect(lambda: self.identifyRequested.emit(self.light.id if self.light else ""))
        self.set_light(None)

    # ---------------------------------------------------------------- loading
    def refresh_devices(self) -> None:
        self._loading = True
        self.device.clear()
        self.device.addItem("— none —", "")
        for d in self.config.devices:
            self.device.addItem(d.name or d.host, d.id)
        if self.light is not None:
            idx = self.device.findData(self.light.device_id)
            self.device.setCurrentIndex(max(idx, 0))
        self._loading = False

    def set_entities(self, device_id: str, choices: list[EntityChoice]) -> None:
        self.entities[device_id] = choices
        if self.light is not None and self.light.device_id == device_id:
            self._fill_entities()

    def _fill_entities(self) -> None:
        assert self.light is not None
        self._loading = True
        self.entity.clear()
        for c in self.entities.get(self.light.device_id, []):
            self.entity.addItem(f"{c.object_id}", c.object_id)
            self.entity.setItemData(self.entity.count() - 1, c.name, Qt.ItemDataRole.ToolTipRole)
        self.entity.setEditText(self.light.entity)
        self._loading = False

    def set_light(self, light: LightConfig | None) -> None:
        self.light = light
        self.empty.setVisible(light is None)
        self.body.setVisible(light is not None)
        if light is None:
            return
        self._loading = True
        self.name.setText(light.name)
        self.enabled.setChecked(light.enabled)
        self.kind.setCurrentIndex(max(self.kind.findData(light.kind), 0))
        self.transport.setCurrentIndex(max(self.transport.findData(light.transport), 0))
        self.refresh_devices()
        self._loading = True
        self._fill_entities()
        self._loading = True
        self.udp_host.setText(light.udp_host)
        self.udp_port.setValue(light.udp_port)
        self.universe.setValue(light.universe)
        self.udp_effect.setEditText(light.udp_effect)
        self.leds.setValue(light.led_count)
        preset = next((k for k, v in SCREEN_PRESETS.items() if v == light.screen_path), None)
        if light.strip_layout == "room" or preset is None:
            self.layout_combo.setCurrentIndex(0)
        else:
            self.layout_combo.setCurrentText(preset)
        self.reverse.setChecked(light.reverse)
        self.height.setValue(round(light.z * 100))
        self.size.setValue(round(light.region_size * 100))
        self.brightness.setValue(round(light.brightness * 100))
        self._loading = False
        self.update_visibility()
        self.update_region_label()

    def update_region_label(self) -> None:
        if self.light is None:
            return
        manual = bool(self.light.region)
        self.manual.setText("manual (drawn on the screen preview)" if manual else "automatic, from room position")
        self.reset_region.setEnabled(manual)

    def update_visibility(self) -> None:
        light = self.light
        if light is None:
            return
        strip = light.kind == "strip"
        udp = light.transport in ("wled", "e131")
        api = light.transport == "api"
        rows = {
            self.device: api or udp,
            self.entity_row: api or (udp and bool(light.udp_effect or light.device_id)),
            self.udp_host: udp,
            self.udp_port: udp,
            self.universe: light.transport == "e131",
            self.udp_effect: udp,
            self.leds: strip,
            self.layout_combo: strip,
            self.reverse: strip,
            self.height: not strip or light.strip_layout == "room",
            self.manual: not strip,
            self.reset_region: not strip,
            self.identify: api,
        }
        for widget, visible in rows.items():
            self.form.setRowVisible(widget, visible)
        self.size.setToolTip(
            "Depth of each LED's sample area" if strip else "How much of the screen this lamp averages"
        )

    # ---------------------------------------------------------------- editing
    def _set(self, attr: str, value: object, kind: str) -> None:
        if self._loading or self.light is None:
            return
        setattr(self.light, attr, value)
        if attr == "udp_effect":
            self.update_visibility()
        self.changed.emit(kind)

    def _on_name(self, text: str) -> None:
        self._set("name", text, "appearance")

    def _on_kind(self) -> None:
        if self._loading or self.light is None:
            return
        kind = self.kind.currentData()
        self.light.kind = kind
        if kind == "strip":
            self.light.region = None
            if self.light.transport == "api":
                self.light.transport = "wled"
                self.transport.setCurrentIndex(self.transport.findData("wled"))
            self.light.region_size = min(self.light.region_size, 0.15)
            self.size.setValue(round(self.light.region_size * 100))
        self.update_visibility()
        self.changed.emit("output")

    def _on_transport(self) -> None:
        if self._loading or self.light is None:
            return
        self.light.transport = self.transport.currentData()
        self.update_visibility()
        self.changed.emit("output")

    def _on_device(self) -> None:
        if self._loading or self.light is None:
            return
        self.light.device_id = self.device.currentData() or ""
        self._fill_entities()
        if self.light.device_id and self.light.device_id not in self.entities:
            self.refreshEntitiesRequested.emit(self.light.device_id)
        self.update_visibility()
        self.changed.emit("output")

    def _on_entity(self, text: str) -> None:
        if self._loading or self.light is None:
            return
        self.light.entity = text.strip()
        for c in self.entities.get(self.light.device_id, []):
            if c.object_id == self.light.entity and self.light.name in ("Light", "New lamp", "New strip"):
                self.light.name = c.name
                self.name.setText(c.name)
        self.changed.emit("output")

    def _on_layout(self) -> None:
        if self._loading or self.light is None:
            return
        text = self.layout_combo.currentText()
        if text == ROOM_LAYOUT:
            self.light.strip_layout = "room"
        else:
            self.light.strip_layout = "screen"
            self.light.screen_path = [list(p) for p in SCREEN_PRESETS[text]]
        self.update_visibility()
        self.changed.emit("rebuild")

    def _on_reset_region(self) -> None:
        if self.light is None:
            return
        self.light.region = None
        self.update_region_label()
        self.changed.emit("layout")
