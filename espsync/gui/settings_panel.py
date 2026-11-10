"""Global sync / capture / room settings."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..capture import available_backends, list_monitors
from ..config import AppConfig
from .wow_addon_dialog import WowAddonDialog

BACKEND_LABELS = {
    "dxgi": "DXGI (Windows, HDR, fastest)",
    "mss": "mss (X11 / portable, SDR)",
}

WOW_HELP_URL = "https://github.com/Alli1223/EspSync/blob/main/integrations/wow/README.md"

SMOOTHING_PRESETS = [("Instant (gaming)", 0), ("Fast", 40), ("Balanced", 80), ("Smooth (movies)", 160), ("Subtle", 320)]


def _slider(lo: int, hi: int) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    return s


def _spin(lo: float, hi: float, step: float, decimals: int = 2, suffix: str = "") -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setDecimals(decimals)
    s.setSuffix(suffix)
    return s


class SettingsPanel(QScrollArea):
    changed = Signal(str)  # "sync" | "room" | "output" | "updates"
    checkUpdatesRequested = Signal()
    testEffectRequested = Signal(str)  # "levelup" | "lowhealth"

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._loading = False
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        col = QVBoxLayout(body)

        # ----------------------------------------------------------- capture
        cap = QGroupBox("Capture")
        f = QFormLayout(cap)
        self.monitor = QComboBox()
        self.backend = QComboBox()
        self.backend.addItem("Automatic", "auto")
        for b in available_backends():
            self.backend.addItem(BACKEND_LABELS.get(b, b), b)
        self.fps = QSpinBox()
        self.fps.setRange(10, 240)
        self.fps.setSuffix(" fps")
        self.hdr = QComboBox()
        self.hdr.addItem("Automatic", "auto")
        self.hdr.addItem("Always treat as HDR", "on")
        self.hdr.addItem("Off (8-bit capture)", "off")
        self.sdr_white = _spin(0, 1000, 10, 0, " nits")
        self.sdr_white.setSpecialValueText("from Windows")
        self.sdr_white.setToolTip("Brightness of SDR white in HDR mode (Windows: 'SDR content brightness')")
        self.peak = _spin(0, 10000, 50, 0, " nits")
        self.peak.setSpecialValueText("from display")
        self.gpu_priority = QCheckBox("High GPU priority for capture (recommended while gaming)")
        f.addRow("Display", self.monitor)
        f.addRow("Backend", self.backend)
        f.addRow("Rate", self.fps)
        f.addRow("HDR", self.hdr)
        f.addRow("SDR white", self.sdr_white)
        f.addRow("HDR peak", self.peak)
        f.addRow("", self.gpu_priority)
        col.addWidget(cap)

        # ------------------------------------------------------------ colour
        colour = QGroupBox("Colour")
        f = QFormLayout(colour)
        self.mode = QComboBox()
        self.mode.addItem("Vivid (favour saturated colours)", "vivid")
        self.mode.addItem("Average", "average")
        self.brightness = _slider(0, 200)
        self.saturation = _slider(50, 250)
        self.hdr_saturation = _slider(50, 250)
        self.smoothing_preset = QComboBox()
        for label, _ in SMOOTHING_PRESETS:
            self.smoothing_preset.addItem(label)
        self.smoothing_preset.addItem("Custom")
        self.smoothing = QSpinBox()
        self.smoothing.setRange(0, 2000)
        self.smoothing.setSuffix(" ms")
        self.min_brightness = _slider(0, 30)
        self.black_off = QCheckBox("Turn lamps off when the screen is black")
        self.letterbox = QCheckBox("Ignore letterbox / pillarbox bars")
        f.addRow("Mode", self.mode)
        f.addRow("Brightness", self.brightness)
        f.addRow("Saturation", self.saturation)
        f.addRow("HDR saturation", self.hdr_saturation)
        f.addRow("Smoothing", self.smoothing_preset)
        f.addRow("", self.smoothing)
        f.addRow("Min brightness", self.min_brightness)
        f.addRow("", self.black_off)
        f.addRow("", self.letterbox)
        col.addWidget(colour)

        # ------------------------------------------------------------ output
        out = QGroupBox("Lights")
        f = QFormLayout(out)
        self.api_rate = QSpinBox()
        self.api_rate.setRange(1, 100)
        self.api_rate.setSuffix(" updates/s")
        self.api_rate.setToolTip("Per light over the ESPHome API. ESP32: 30-50 is fine; ESP8266: keep ≤ 25")
        self.udp_rate = QSpinBox()
        self.udp_rate.setRange(1, 240)
        self.udp_rate.setSuffix(" updates/s")
        self.hue_rate = QSpinBox()
        self.hue_rate.setRange(10, 60)
        self.hue_rate.setSuffix(" packets/s")
        self.hue_rate.setToolTip("Philips Hue Entertainment stream, per bridge. The bridge drives the lights at "
                                 "about 25 Hz; 50 rides out lost packets")
        self.transition = QSpinBox()
        self.transition.setRange(0, 1000)
        self.transition.setSuffix(" ms")
        self.transition.setToolTip("Transition the ESP applies per update (0 = immediate, lowest latency)")
        self.restore = QCheckBox("Restore lights to how they were when sync stops")
        f.addRow("API rate", self.api_rate)
        f.addRow("UDP rate", self.udp_rate)
        f.addRow("Hue rate", self.hue_rate)
        f.addRow("Transition", self.transition)
        f.addRow("", self.restore)
        col.addWidget(out)

        # ------------------------------------------------------ game effects
        game = QGroupBox("Game effects")
        f = QFormLayout(game)
        self.wow = QCheckBox("World of Warcraft: red pulse at low health, gold sweep on level-up")
        self.wow.setToolTip("Active only while WoW runs with the EspSync addon, which draws a thin strip "
                            "in the top-left corner of the screen")
        self.wow_threshold = QSpinBox()
        self.wow_threshold.setRange(5, 90)
        self.wow_threshold.setSuffix(" %")
        self.wow_threshold.setToolTip("The lights pulse red below this much health, faster and stronger as it drops")
        tests = QWidget()
        row = QHBoxLayout(tests)
        row.setContentsMargins(0, 0, 0, 0)
        self.test_low_health = QPushButton("Test low health")
        self.test_levelup = QPushButton("Test level-up")
        row.addWidget(self.test_low_health)
        row.addWidget(self.test_levelup)
        row.addStretch(1)
        self.test_low_health.clicked.connect(lambda: self.testEffectRequested.emit("lowhealth"))
        self.test_levelup.clicked.connect(lambda: self.testEffectRequested.emit("levelup"))
        addon = QWidget()
        row = QHBoxLayout(addon)
        row.setContentsMargins(0, 0, 0, 0)
        self.install_addon = QPushButton("Install WoW addon…")
        self.install_addon.setToolTip("Copy the EspSync addon, which the effects need, into World of Warcraft")
        self.install_addon.clicked.connect(self._on_install_addon)
        help_label = QLabel(f'<a href="{WOW_HELP_URL}">Setup help</a>')
        help_label.setOpenExternalLinks(True)
        row.addWidget(self.install_addon)
        row.addWidget(help_label)
        row.addStretch(1)
        f.addRow("", self.wow)
        f.addRow("Low health below", self.wow_threshold)
        f.addRow("", tests)
        f.addRow("Addon", addon)
        col.addWidget(game)

        # -------------------------------------------------------------- room
        room = QGroupBox("Room")
        f = QFormLayout(room)
        self.room_w = _spin(1, 30, 0.5, 1, " m")
        self.room_d = _spin(1, 30, 0.5, 1, " m")
        self.tv_width = _slider(5, 100)
        self.spread = _slider(20, 100)
        self.spread.setToolTip("Lower = lights near the side walls take colour from the very edge of the screen")
        f.addRow("Width", self.room_w)
        f.addRow("Depth", self.room_d)
        f.addRow("Screen width", self.tv_width)
        f.addRow("Horizontal spread", self.spread)
        col.addWidget(room)

        # ----------------------------------------------------------- updates
        from .. import updater

        upd = QGroupBox("Updates")
        f = QFormLayout(upd)
        installed = updater.is_installed_build()
        f.addRow("Version", QLabel(updater.current_version() + ("" if installed else " (from source)")))
        self.update_check = QCheckBox("Check GitHub for new releases")
        self.update_auto = QCheckBox("Install updates automatically (never while syncing)")
        self.update_auto.setEnabled(installed)
        if not installed:
            self.update_auto.setToolTip("Only the installed app can update itself")
        check_now = QPushButton("Check now")
        check_now.clicked.connect(self.checkUpdatesRequested.emit)
        f.addRow("", self.update_check)
        f.addRow("", self.update_auto)
        f.addRow("", check_now)
        col.addWidget(upd)
        self.update_check.setChecked(self.config.updates.check_automatically)
        self.update_auto.setChecked(self.config.updates.install_automatically)
        for box, attr in ((self.update_check, "check_automatically"), (self.update_auto, "install_automatically")):
            box.toggled.connect(lambda v, a=attr: (setattr(self.config.updates, a, v), self.changed.emit("updates")))

        col.addWidget(QLabel("Settings save automatically."))
        col.addStretch(1)

        self.load()
        self._connect()

    # ----------------------------------------------------------------- load
    def refresh_monitors(self) -> None:
        self._loading = True
        self.monitor.clear()
        try:
            for m in list_monitors(self.config.sync.capture_backend):
                self.monitor.addItem(m.label(), m.index)
        except Exception as exc:  # noqa: BLE001
            self.monitor.addItem(f"(could not list displays: {exc})", 0)
        idx = self.monitor.findData(self.config.sync.monitor)
        self.monitor.setCurrentIndex(max(idx, 0))
        self._loading = False

    def load(self) -> None:
        s, r = self.config.sync, self.config.room
        self.refresh_monitors()
        self._loading = True
        self.backend.setCurrentIndex(max(self.backend.findData(s.capture_backend), 0))
        self.fps.setValue(s.target_fps)
        self.hdr.setCurrentIndex(max(self.hdr.findData(s.hdr), 0))
        self.sdr_white.setValue(s.sdr_white_nits)
        self.peak.setValue(s.hdr_peak_nits)
        self.gpu_priority.setChecked(s.gpu_priority)
        self.mode.setCurrentIndex(max(self.mode.findData(s.mode), 0))
        self.brightness.setValue(round(s.brightness * 100))
        self.saturation.setValue(round(s.saturation * 100))
        self.hdr_saturation.setValue(round(s.hdr_saturation * 100))
        self.smoothing.setValue(round(s.smoothing_ms))
        self._sync_preset()
        self.min_brightness.setValue(round(s.min_brightness * 100))
        self.black_off.setChecked(s.black_turns_off)
        self.letterbox.setChecked(s.letterbox_detection)
        self.api_rate.setValue(round(s.api_rate_hz))
        self.udp_rate.setValue(round(s.udp_rate_hz))
        self.hue_rate.setValue(round(s.hue_rate_hz))
        self.transition.setValue(round(s.transition_ms))
        self.restore.setChecked(s.restore_on_stop)
        self.wow.setChecked(s.wow_effects)
        self.wow_threshold.setValue(round(s.wow_low_health * 100))
        self.wow_threshold.setEnabled(s.wow_effects)
        self.room_w.setValue(r.width_m)
        self.room_d.setValue(r.depth_m)
        self.tv_width.setValue(round(r.tv_width * 100))
        self.spread.setValue(round(r.horizontal_spread * 100))
        self._loading = False

    def show_brightness(self) -> None:
        """Brightness changed elsewhere (the main window's slider)."""
        self._loading = True
        self.brightness.setValue(round(self.config.sync.brightness * 100))
        self._loading = False

    def _sync_preset(self) -> None:
        ms = self.config.sync.smoothing_ms
        idx = next((i for i, (_, v) in enumerate(SMOOTHING_PRESETS) if v == ms), len(SMOOTHING_PRESETS))
        self.smoothing_preset.setCurrentIndex(idx)

    # ---------------------------------------------------------------- wiring
    def _connect(self) -> None:
        s, r = self.config.sync, self.config.room

        def bind(signal, obj, attr, conv=lambda v: v, kind="sync"):  # noqa: ANN001
            def apply(value: object) -> None:
                if self._loading:
                    return
                setattr(obj, attr, conv(value))
                self.changed.emit(kind)

            signal.connect(apply)

        bind(self.monitor.currentIndexChanged, s, "monitor", lambda _: self.monitor.currentData() or 0)
        bind(self.fps.valueChanged, s, "target_fps")
        bind(self.hdr.currentIndexChanged, s, "hdr", lambda _: self.hdr.currentData())
        bind(self.sdr_white.valueChanged, s, "sdr_white_nits")
        bind(self.peak.valueChanged, s, "hdr_peak_nits")
        bind(self.gpu_priority.toggled, s, "gpu_priority")
        bind(self.mode.currentIndexChanged, s, "mode", lambda _: self.mode.currentData())
        bind(self.brightness.valueChanged, s, "brightness", lambda v: v / 100)
        bind(self.saturation.valueChanged, s, "saturation", lambda v: v / 100)
        bind(self.hdr_saturation.valueChanged, s, "hdr_saturation", lambda v: v / 100)
        bind(self.smoothing.valueChanged, s, "smoothing_ms", float)
        bind(self.min_brightness.valueChanged, s, "min_brightness", lambda v: v / 100)
        bind(self.black_off.toggled, s, "black_turns_off")
        bind(self.letterbox.toggled, s, "letterbox_detection")
        bind(self.api_rate.valueChanged, s, "api_rate_hz", float, "output")
        bind(self.udp_rate.valueChanged, s, "udp_rate_hz", float, "output")
        bind(self.hue_rate.valueChanged, s, "hue_rate_hz", float, "output")
        bind(self.transition.valueChanged, s, "transition_ms", float)
        bind(self.restore.toggled, s, "restore_on_stop")
        bind(self.wow.toggled, s, "wow_effects")
        bind(self.wow_threshold.valueChanged, s, "wow_low_health", lambda v: v / 100)
        self.wow.toggled.connect(self.wow_threshold.setEnabled)
        bind(self.room_w.valueChanged, r, "width_m", float, "room")
        bind(self.room_d.valueChanged, r, "depth_m", float, "room")
        bind(self.tv_width.valueChanged, r, "tv_width", lambda v: v / 100, "room")
        bind(self.spread.valueChanged, r, "horizontal_spread", lambda v: v / 100, "room")

        self.backend.currentIndexChanged.connect(self._on_backend)
        self.smoothing_preset.currentIndexChanged.connect(self._on_preset)
        self.smoothing.valueChanged.connect(lambda _: None if self._loading else self._sync_preset_silently())

    def _sync_preset_silently(self) -> None:
        self._loading = True
        self._sync_preset()
        self._loading = False

    def _on_backend(self) -> None:
        if self._loading:
            return
        self.config.sync.capture_backend = self.backend.currentData()
        self.refresh_monitors()
        self.changed.emit("sync")

    def _on_install_addon(self) -> None:
        WowAddonDialog(self).exec()

    def _on_preset(self, idx: int) -> None:
        if self._loading or idx >= len(SMOOTHING_PRESETS):
            return
        self.smoothing.setValue(SMOOTHING_PRESETS[idx][1])
