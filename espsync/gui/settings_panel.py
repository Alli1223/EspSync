"""Global settings: capture / game / room pages for the main window's tabs, and
the Settings window (light output for all lights, updates)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
from .theme import set_role
from .wow_addon_dialog import WowAddonDialog

BACKEND_LABELS = {
    "dxgi": "DXGI (Windows, HDR, fastest)",
    "mss": "mss (X11 / portable, SDR)",
}

WOW_HELP_URL = "https://github.com/Alli1223/EspSync/blob/main/integrations/wow/README.md"

SMOOTHING_PRESETS = [("Instant (gaming)", 0), ("Fast", 40), ("Balanced", 80), ("Smooth (movies)", 160), ("Subtle", 320)]

# Network load presets: SyncSettings values.  Fewer updates are smoothed over by
# a matching fade on the lamp, so the gentle ones trade a little latency, not looks.
LOAD_PRESETS: list[tuple[str, dict[str, float]]] = [
    ("Responsive (most traffic)", {"api_rate_hz": 30.0, "udp_rate_hz": 60.0, "hue_rate_hz": 50.0,
                                   "max_total_rate_hz": 0.0, "min_change": 0.0, "transition_ms": 0.0}),
    ("Balanced", {"api_rate_hz": 20.0, "udp_rate_hz": 40.0, "hue_rate_hz": 50.0,
                  "max_total_rate_hz": 150.0, "min_change": 0.01, "transition_ms": 50.0}),
    ("Gentle on Wi-Fi (gaming)", {"api_rate_hz": 10.0, "udp_rate_hz": 25.0, "hue_rate_hz": 25.0,
                                  "max_total_rate_hz": 60.0, "min_change": 0.02, "transition_ms": 100.0}),
]

OVERWATCH_TESTS = [("Damage", "damage"), ("Healing", "heal"), ("Critical", "lowhealth"), ("Eliminated", "elimination")]


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


def _form(box: QGroupBox) -> QFormLayout:
    f = QFormLayout(box)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    f.setHorizontalSpacing(14)
    f.setVerticalSpacing(9)
    return f


def _row(*widgets: QWidget) -> QWidget:
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    for w in widgets:
        row.addWidget(w)
    row.addStretch(1)
    return holder


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    set_role(label, "hint")
    return label


class SettingsDialog(QDialog):
    """The Settings window: what applies to every light, and app updates."""

    def __init__(self, panel: SettingsPanel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("EspSync settings")
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 8, 0, 10)
        col.addWidget(panel.general, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        buttons.setContentsMargins(12, 0, 12, 0)
        col.addWidget(buttons)
        self.resize(560, 740)


class SettingsPanel(QObject):
    """Owns every settings widget; `pages` are (title, widget) pairs for a tab widget."""

    changed = Signal(str)  # "sync" | "room" | "output" | "updates"
    checkUpdatesRequested = Signal()
    # "levelup" | "lowhealth" (World of Warcraft), "ow:damage" | "ow:heal" | "ow:lowhealth" | "ow:elimination"
    testEffectRequested = Signal(str)

    def __init__(self, config: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._loading = False
        self.pages: list[tuple[str, QWidget]] = []

        # ----------------------------------------------------------- capture
        cap = QGroupBox("CAPTURE")
        f = _form(cap)
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
        f.addRow(self.gpu_priority)

        # ------------------------------------------------------------ colour
        colour = QGroupBox("COLOUR")
        f = _form(colour)
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
        f.addRow(self.black_off)
        f.addRow(self.letterbox)
        self._add_page("Picture", cap, colour)

        # ------------------------------------------------------ game effects
        wow = QGroupBox("WORLD OF WARCRAFT")
        f = _form(wow)
        self.wow = QCheckBox("Red pulse at low health, gold sweep on level-up")
        self.wow.setToolTip("Active only while WoW runs with the EspSync addon, which draws a thin strip "
                            "in the top-left corner of the screen")
        self.wow_threshold = QSpinBox()
        self.wow_threshold.setRange(5, 90)
        self.wow_threshold.setSuffix(" %")
        self.wow_threshold.setToolTip("The lights pulse red below this much health, faster and stronger as it drops")
        self.test_low_health = QPushButton("Low health")
        self.test_levelup = QPushButton("Level-up")
        self.test_low_health.clicked.connect(lambda: self.testEffectRequested.emit("lowhealth"))
        self.test_levelup.clicked.connect(lambda: self.testEffectRequested.emit("levelup"))
        self.install_addon = QPushButton("Install WoW addon…")
        self.install_addon.setToolTip("Copy the EspSync addon, which the effects need, into World of Warcraft")
        self.install_addon.clicked.connect(self._on_install_addon)
        help_label = QLabel(f'<a href="{WOW_HELP_URL}">Setup help</a>')
        help_label.setOpenExternalLinks(True)
        f.addRow(self.wow)
        f.addRow("Low health below", self.wow_threshold)
        f.addRow("Try it", _row(self.test_low_health, self.test_levelup))
        f.addRow("Addon", _row(self.install_addon, help_label))

        ow = QGroupBox("OVERWATCH")
        f = _form(ow)
        self.ow = QCheckBox("React to damage, healing, critical health and eliminations")
        self.ow.setToolTip("Active only while Overwatch runs. EspSync reads the health bar from the captured "
                           "picture; it never touches the game itself")
        self.ow_threshold = QSpinBox()
        self.ow_threshold.setRange(5, 90)
        self.ow_threshold.setSuffix(" %")
        self.ow_threshold.setToolTip("A red heartbeat starts below this much health and quickens as it drops")
        self.ow_tests: dict[str, QPushButton] = {}
        for label, kind in OVERWATCH_TESTS:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, k=kind: self.testEffectRequested.emit(f"ow:{k}"))
            self.ow_tests[kind] = btn
        f.addRow(self.ow)
        f.addRow("Critical below", self.ow_threshold)
        f.addRow("Try it", _row(*self.ow_tests.values()))
        f.addRow(_hint("No addon needed: the health bar in the bottom-left of the screen is read while you play, "
                       "so keep the HUD at its default size and position. The status bar shows what was read."))
        self._add_page("Games", wow, ow)

        # ------------------------------------------------------------ output
        out = QGroupBox("ALL LIGHTS")
        f = _form(out)
        self.load_preset = QComboBox()
        for label, _ in LOAD_PRESETS:
            self.load_preset.addItem(label)
        self.load_preset.addItem("Custom")
        self.load_preset.setToolTip("How much network traffic syncing may make. Gentler settings send fewer, "
                                    "smoother updates; the values below show what each one does")
        self.api_rate = QSpinBox()
        self.api_rate.setRange(1, 100)
        self.api_rate.setSuffix(" updates/s")
        self.api_rate.setToolTip("Per lamp over the ESPHome API. ESP32: 30-50 is fine; ESP8266: keep ≤ 25")
        self.udp_rate = QSpinBox()
        self.udp_rate.setRange(1, 240)
        self.udp_rate.setSuffix(" updates/s")
        self.udp_rate.setToolTip("Per addressable strip (WLED / E1.31). Every update is a full packet of LED colours")
        self.hue_rate = QSpinBox()
        self.hue_rate.setRange(10, 60)
        self.hue_rate.setSuffix(" packets/s")
        self.hue_rate.setToolTip("Philips Hue Entertainment stream, per bridge. The bridge drives the lights at "
                                 "about 25 Hz; 50 rides out lost packets")
        self.total_rate = QSpinBox()
        self.total_rate.setRange(0, 2000)
        self.total_rate.setSingleStep(10)
        self.total_rate.setSuffix(" updates/s")
        self.total_rate.setSpecialValueText("No limit")
        self.total_rate.setToolTip("Ceiling for all ESPHome lights together. With many lights each one is slowed "
                                   "down to fit, so adding lights doesn't add traffic")
        self.min_change = _spin(0, 10, 0.5, 1, " %")
        self.min_change.setSpecialValueText("Off")
        self.min_change.setToolTip("Don't send colour changes smaller than this. Stops film grain and flicker in "
                                   "the picture from causing a constant stream of updates; the exact colour "
                                   "still arrives within half a second")
        self.transition = QSpinBox()
        self.transition.setRange(0, 1000)
        self.transition.setSuffix(" ms")
        self.transition.setToolTip("Fade each ESPHome lamp applies per update (0 = immediate, lowest latency). "
                                   "At low update rates, about one update's length hides the steps")
        self.traffic = _hint("")
        self.restore = QCheckBox("Restore lights to how they were when sync stops")
        f.addRow("Network load", self.load_preset)
        f.addRow("Lamp rate", self.api_rate)
        f.addRow("Strip rate", self.udp_rate)
        f.addRow("Hue rate", self.hue_rate)
        f.addRow("Limit in total", self.total_rate)
        f.addRow("Ignore changes under", self.min_change)
        f.addRow("Lamp fade", self.transition)
        f.addRow("Sending now", self.traffic)
        f.addRow(_hint("If the game itself stutters rather than the connection, also lower Picture → Capture → "
                       "Rate: it sets how often the screen is read, and nothing is sent faster than that."))
        f.addRow(self.restore)
        self.show_traffic(None, 0)

        # -------------------------------------------------------------- room
        room = QGroupBox("ROOM")
        f = _form(room)
        self.room_w = _spin(1, 30, 0.5, 1, " m")
        self.room_d = _spin(1, 30, 0.5, 1, " m")
        self.tv_width = _slider(5, 100)
        self.spread = _slider(20, 100)
        self.spread.setToolTip("Lower = lights near the side walls take colour from the very edge of the screen")
        f.addRow("Width", self.room_w)
        f.addRow("Depth", self.room_d)
        f.addRow("Screen width", self.tv_width)
        f.addRow("Horizontal spread", self.spread)

        # ----------------------------------------------------------- updates
        from .. import updater

        upd = QGroupBox("UPDATES")
        f = _form(upd)
        installed = updater.is_installed_build()
        f.addRow("Version", QLabel(updater.current_version() + ("" if installed else " (from source)")))
        self.update_check = QCheckBox("Check GitHub for new releases")
        self.update_auto = QCheckBox("Install updates automatically (never while syncing)")
        self.update_auto.setEnabled(installed)
        if not installed:
            self.update_auto.setToolTip("Only the installed app can update itself")
        self.check_now = QPushButton("Check now")
        self.check_now.clicked.connect(self.checkUpdatesRequested.emit)
        f.addRow(self.update_check)
        f.addRow(self.update_auto)
        f.addRow(_row(self.check_now))
        self.update_check.setChecked(self.config.updates.check_automatically)
        self.update_auto.setChecked(self.config.updates.install_automatically)
        for box, attr in ((self.update_check, "check_automatically"), (self.update_auto, "install_automatically")):
            box.toggled.connect(lambda v, a=attr: (setattr(self.config.updates, a, v), self.changed.emit("updates")))
        self._add_page("Room", room)
        # App-wide settings: shown in their own window (SettingsDialog), not a tab.
        self.general = self._page(out, upd, _hint("Settings save automatically."))

        self.load()
        self._connect()

    @staticmethod
    def _page(*widgets: QWidget) -> QScrollArea:
        page = QScrollArea()
        page.setWidgetResizable(True)
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(12, 6, 12, 12)
        col.setSpacing(4)
        for w in widgets:
            col.addWidget(w)
        col.addStretch(1)
        page.setWidget(body)
        return page

    def _add_page(self, title: str, *widgets: QWidget) -> None:
        self.pages.append((title, self._page(*widgets)))

    def deleteLater(self) -> None:
        for page in (*(p for _, p in self.pages), self.general):
            if page.parent() is None:
                page.deleteLater()
        super().deleteLater()

    def show_traffic(self, rate: float | None, lights: int) -> None:
        """Live network load for the Settings window; rate=None while not syncing."""
        if rate is None:
            self.traffic.setText("Nothing: sync is stopped")
        else:
            self.traffic.setText(f"{rate:.0f} updates/s to {lights} light{'' if lights == 1 else 's'}")

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
        self.total_rate.setValue(round(s.max_total_rate_hz))
        self.min_change.setValue(s.min_change * 100)
        self._sync_load_preset()
        self.restore.setChecked(s.restore_on_stop)
        self.wow.setChecked(s.wow_effects)
        self.wow_threshold.setValue(round(s.wow_low_health * 100))
        self.wow_threshold.setEnabled(s.wow_effects)
        self.ow.setChecked(s.ow_effects)
        self.ow_threshold.setValue(round(s.ow_low_health * 100))
        self.ow_threshold.setEnabled(s.ow_effects)
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

    def _sync_load_preset(self) -> None:
        s = self.config.sync
        idx = next(
            (i for i, (_, values) in enumerate(LOAD_PRESETS)
             if all(abs(getattr(s, k) - v) < 1e-6 for k, v in values.items())),
            len(LOAD_PRESETS),
        )
        self.load_preset.setCurrentIndex(idx)

    def _on_load_preset(self, idx: int) -> None:
        if self._loading or idx >= len(LOAD_PRESETS):
            return
        for key, value in LOAD_PRESETS[idx][1].items():
            setattr(self.config.sync, key, value)
        self.load()
        self.changed.emit("output")

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
        bind(self.total_rate.valueChanged, s, "max_total_rate_hz", float, "output")
        bind(self.min_change.valueChanged, s, "min_change", lambda v: v / 100)
        for w in (self.api_rate, self.udp_rate, self.hue_rate, self.transition, self.total_rate, self.min_change):
            w.valueChanged.connect(lambda _: None if self._loading else self._silently(self._sync_load_preset))
        self.load_preset.currentIndexChanged.connect(self._on_load_preset)
        bind(self.restore.toggled, s, "restore_on_stop")
        bind(self.wow.toggled, s, "wow_effects")
        bind(self.wow_threshold.valueChanged, s, "wow_low_health", lambda v: v / 100)
        self.wow.toggled.connect(self.wow_threshold.setEnabled)
        bind(self.ow.toggled, s, "ow_effects")
        bind(self.ow_threshold.valueChanged, s, "ow_low_health", lambda v: v / 100)
        self.ow.toggled.connect(self.ow_threshold.setEnabled)
        bind(self.room_w.valueChanged, r, "width_m", float, "room")
        bind(self.room_d.valueChanged, r, "depth_m", float, "room")
        bind(self.tv_width.valueChanged, r, "tv_width", lambda v: v / 100, "room")
        bind(self.spread.valueChanged, r, "horizontal_spread", lambda v: v / 100, "room")

        self.backend.currentIndexChanged.connect(self._on_backend)
        self.smoothing_preset.currentIndexChanged.connect(self._on_preset)
        self.smoothing.valueChanged.connect(lambda _: None if self._loading else self._sync_preset_silently())

    def _sync_preset_silently(self) -> None:
        self._silently(self._sync_preset)

    def _silently(self, update) -> None:  # noqa: ANN001
        self._loading = True
        update()
        self._loading = False

    def _on_backend(self) -> None:
        if self._loading:
            return
        self.config.sync.capture_backend = self.backend.currentData()
        self.refresh_monitors()
        self.changed.emit("sync")

    def _on_install_addon(self) -> None:
        WowAddonDialog(self.install_addon.window()).exec()

    def _on_preset(self, idx: int) -> None:
        if self._loading or idx >= len(SMOOTHING_PRESETS):
            return
        self.smoothing.setValue(SMOOTHING_PRESETS[idx][1])
