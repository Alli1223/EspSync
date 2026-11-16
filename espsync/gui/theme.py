"""The app's look: one palette, one stylesheet.

Colours live in :data:`C` so painted widgets (room, screen preview, the light
list) and the stylesheet agree.  The few glyphs the stylesheet needs (arrows,
the checkbox tick) are drawn at start-up into a temp folder, so nothing has to
ship as a resource file.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QObject, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QDialog, QGraphicsDropShadowEffect, QMainWindow, QWidget

C = SimpleNamespace(
    window="#0c0e13",  # behind everything
    surface="#151821",  # cards
    raised="#1e2230",  # buttons, inputs
    hover="#282d3e",
    border="#242937",
    border_strong="#343b4f",
    text="#eef0f6",
    muted="#9aa3b5",
    faint="#657089",
    accent="#7c8cff",
    accent_hover="#96a3ff",
    accent_deep="#5b6cff",  # with `violet`: the gradient on primary buttons
    violet="#8b5cf6",
    on_accent="#ffffff",
    danger="#ff5c6c",
    danger_deep="#f0476a",
    ember="#ff7a59",  # with `danger_deep`: the Stop gradient
    ok="#3ddc97",
    warn="#f5b85c",
    canvas="#090b0f",  # room view / screen preview background
    floor="#12151d",  # the room's floor
    floor_edge="#191d28",
    grid="#2a3040",
)

# The app icon's colours (see app.render_icon): the idle ambient bar, the logo.
BRAND = ("#ffbe5a", "#e646a0", "#5b6cff")


def _glyph(name: str, draw, size: int = 20) -> str:  # noqa: ANN001
    """Draw a small glyph once and return its path as a stylesheet url."""
    folder = Path(tempfile.gettempdir()) / "espsync-theme"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.png"
    img = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(2.0)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw(p, float(size))
    p.end()
    img.save(str(path))
    return path.as_posix()


def _chevron(colour: str, up: bool = False):  # noqa: ANN202
    def draw(p: QPainter, s: float) -> None:
        p.setPen(QPen(QColor(colour), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        y0, y1 = (0.58, 0.42) if up else (0.42, 0.58)
        p.drawPolyline([QPointF(s * 0.32, s * y0), QPointF(s * 0.5, s * y1), QPointF(s * 0.68, s * y0)])

    return draw


def _tick(p: QPainter, s: float) -> None:
    p.setPen(QPen(QColor(C.on_accent), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPolyline([QPointF(s * 0.27, s * 0.52), QPointF(s * 0.44, s * 0.68), QPointF(s * 0.74, s * 0.34)])


def palette() -> QPalette:
    pal = QPalette()
    text, muted = QColor(C.text), QColor(C.faint)
    pal.setColor(QPalette.ColorRole.Window, QColor(C.window))
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, QColor(C.surface))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(C.raised))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(C.raised))
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, QColor(C.raised))
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.Highlight, QColor(C.accent_deep))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(C.on_accent))
    pal.setColor(QPalette.ColorRole.PlaceholderText, muted)
    pal.setColor(QPalette.ColorRole.Link, QColor(C.accent_hover))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, muted)
    return pal


def _gradient(a: str, b: str, vertical: bool = False) -> str:
    end = "x2:0, y2:1" if vertical else "x2:1, y2:1"
    return f"qlineargradient(x1:0, y1:0, {end}, stop:0 {a}, stop:1 {b})"


def stylesheet() -> str:
    down = _glyph("down", _chevron(C.muted))
    up = _glyph("up", _chevron(C.muted, up=True))
    tick = _glyph("tick", _tick)
    go = _gradient(C.accent_deep, C.violet)
    go_hover = _gradient("#6f7eff", "#9d74ff")
    go_pressed = _gradient("#4c5ce6", "#7848e0")
    stop = _gradient(C.danger_deep, C.ember)
    stop_hover = _gradient("#f76080", "#ff8d70")
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {C.text}; }}
QMainWindow, QDialog {{ background: {C.window}; }}
QToolTip {{ background: {C.raised}; color: {C.text}; border: 1px solid {C.border_strong}; padding: 6px 9px; border-radius: 7px; }}

/* ---- text roles */
QLabel[role="brand"] {{ font-size: 12.5pt; font-weight: 700; letter-spacing: 0.3px; }}
QLabel[role="title"] {{ font-size: 13pt; font-weight: 600; }}
QLabel[role="section"] {{ font-size: 10.5pt; font-weight: 600; }}
QLabel[role="heading"] {{ color: {C.muted}; font-size: 8pt; font-weight: 700; letter-spacing: 1.2px; }}
QLabel[role="muted"] {{ color: {C.muted}; }}
QLabel[role="hint"] {{ color: {C.faint}; font-size: 8.5pt; }}
QLabel[role="value"] {{ color: {C.muted}; font-size: 8.5pt; font-weight: 600; min-width: 38px; }}
QLabel[role="count"] {{ background: {C.raised}; color: {C.muted}; border-radius: 8px; padding: 1px 8px; font-weight: 700; font-size: 8pt; }}
QLabel[role="pill"] {{ background: {C.raised}; color: {C.muted}; border: 1px solid {C.border_strong}; border-radius: 10px; padding: 2px 11px; font-weight: 700; font-size: 8pt; letter-spacing: 0.8px; }}
QLabel[role="pill"][state="on"] {{ background: rgba(61, 220, 151, 36); color: {C.ok}; border-color: rgba(61, 220, 151, 110); }}
QLabel[role="pill"][state="error"] {{ background: rgba(255, 92, 108, 40); color: {C.danger}; border-color: rgba(255, 92, 108, 120); }}

/* ---- panels */
QWidget#header {{ background: {_gradient("#171a25", C.surface, vertical=True)}; }}
QFrame#card {{ background: {C.surface}; border: 1px solid {C.border}; border-radius: 12px; }}
QFrame#card[tone="canvas"] {{ background: {C.canvas}; }}
QFrame#divider {{ background: {C.border_strong}; max-width: 1px; min-width: 1px; }}
QFrame#segmented {{ background: {C.canvas}; border: 1px solid {C.border}; border-radius: 10px; }}
QStatusBar {{ background: {C.surface}; border-top: 1px solid {C.border}; color: {C.muted}; padding: 3px 10px; min-height: 28px; }}
QGraphicsView {{ background: transparent; border: 0; }}
QStatusBar::item {{ border: 0; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}

/* ---- buttons */
QPushButton {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 8px; padding: 6px 14px; }}
QPushButton:hover {{ background: {C.hover}; border-color: {C.faint}; }}
QPushButton:pressed {{ background: {C.border}; }}
QPushButton:disabled {{ color: {C.faint}; background: {C.surface}; border-color: {C.border}; }}
QPushButton::menu-indicator {{ image: url({down}); width: 14px; height: 14px; subcontrol-position: right center; right: 6px; }}
QPushButton[kind="primary"] {{ background: {go}; border: 1px solid rgba(255, 255, 255, 46); color: {C.on_accent}; font-weight: 700; padding: 8px 22px; border-radius: 10px; font-size: 10.5pt; }}
QPushButton[kind="primary"]:hover {{ background: {go_hover}; border-color: rgba(255, 255, 255, 90); }}
QPushButton[kind="primary"]:pressed {{ background: {go_pressed}; }}
QPushButton[kind="primary"]:checked {{ background: {stop}; }}
QPushButton[kind="primary"]:checked:hover {{ background: {stop_hover}; }}
QPushButton[kind="accent"] {{ background: {go}; border: 1px solid rgba(255, 255, 255, 40); color: {C.on_accent}; font-weight: 600; }}
QPushButton[kind="accent"]:hover {{ background: {go_hover}; }}
QPushButton[kind="accent"]:pressed {{ background: {go_pressed}; }}
QPushButton[kind="accent"]:disabled {{ background: {C.surface}; color: {C.faint}; border-color: {C.border}; }}
QPushButton[kind="chip"] {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 13px; padding: 4px 13px 4px 10px; min-height: 18px; color: {C.text}; }}
QPushButton[kind="chip"]:hover {{ background: {C.hover}; border-color: {C.faint}; }}
QPushButton[kind="chip"]:pressed {{ background: {C.border}; }}
QPushButton[kind="tile"] {{ background: rgba(255, 255, 255, 8); border: 1px solid {C.border}; border-radius: 9px; padding: 8px 8px 8px 10px; text-align: left; }}
QPushButton[kind="tile"]:hover {{ background: {C.hover}; border-color: {C.accent}; }}
QPushButton[kind="tile"]:pressed {{ background: {C.border}; }}
QPushButton[kind="segment"] {{ background: transparent; border: 0; border-radius: 7px; padding: 5px 13px; color: {C.muted}; font-weight: 600; }}
QPushButton[kind="segment"]:hover {{ background: {C.raised}; color: {C.text}; }}
QPushButton[kind="segment"]:checked {{ background: {go}; color: {C.on_accent}; }}
QPushButton[kind="quiet"] {{ background: transparent; border-color: transparent; color: {C.muted}; padding: 6px 10px; }}
QPushButton[kind="quiet"]:hover {{ background: {C.hover}; color: {C.text}; }}
QPushButton[kind="quiet"]:pressed {{ background: {C.border}; }}
QPushButton[kind="danger"] {{ background: transparent; border-color: transparent; color: {C.muted}; padding: 6px 10px; }}
QPushButton[kind="danger"]:hover {{ background: rgba(255, 92, 108, 36); color: {C.danger}; }}
QPushButton[kind="icon"] {{ padding: 0; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; font-size: 11pt; }}

/* ---- inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 8px; padding: 5px 9px; min-height: 18px; selection-background-color: {C.accent_deep}; selection-color: {C.on_accent}; }}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{ border-color: {C.faint}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {C.accent}; background: {C.hover}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {C.faint}; background: {C.surface}; border-color: {C.border}; }}
QComboBox::drop-down {{ border: 0; width: 24px; subcontrol-position: right center; }}
QComboBox::down-arrow {{ image: url({down}); width: 16px; height: 16px; }}
QComboBox QAbstractItemView {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 8px; padding: 4px; selection-background-color: {C.accent_deep}; selection-color: {C.on_accent}; }}
/* the line edit inside a spin box or editable combo: no box of its own */
QAbstractSpinBox > QLineEdit, QComboBox > QLineEdit {{ border: 0; background: transparent; padding: 0 0 0 5px; min-height: 0; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: 0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: 0; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({up}); width: 14px; height: 14px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({down}); width: 14px; height: 14px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QListView::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {C.border_strong}; background: {C.raised}; }}
QCheckBox::indicator:hover, QListView::indicator:hover {{ border-color: {C.accent}; }}
QCheckBox::indicator:checked, QListView::indicator:checked {{ background: {C.accent_deep}; border-color: {C.accent}; image: url({tick}); }}
QCheckBox::indicator:disabled {{ background: {C.surface}; border-color: {C.border}; }}

QSlider {{ min-height: 22px; }}
QSlider::groove:horizontal {{ height: 4px; background: {C.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {_gradient(C.accent_deep, C.accent)}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0; border-radius: 8px; background: {C.text}; border: 1px solid {C.window}; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; border: 2px solid {C.accent}; }}
QSlider::handle:horizontal:pressed {{ background: {C.accent_hover}; border: 2px solid #ffffff; }}
QSlider::sub-page:horizontal:disabled {{ background: {C.faint}; }}
QSlider::handle:horizontal:disabled {{ background: {C.faint}; }}

/* ---- containers */
QGroupBox {{ background: rgba(255, 255, 255, 7); border: 1px solid {C.border}; border-radius: 10px; margin-top: 24px; padding: 12px 12px 10px 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px; top: 4px; color: {C.muted}; font-size: 8pt; font-weight: 700; letter-spacing: 1.2px; }}
QScrollArea {{ border: 0; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QTabWidget::pane {{ border: 0; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {C.muted}; padding: 6px 15px; margin: 2px 4px 8px 0; border-radius: 8px; font-weight: 600; }}
QTabBar::tab:hover {{ color: {C.text}; background: {C.raised}; }}
QTabBar::tab:selected {{ color: {C.text}; background: {C.hover}; }}

QListWidget, QTreeWidget, QTableWidget, QPlainTextEdit, QTextEdit {{ background: {C.surface}; border: 1px solid {C.border}; border-radius: 10px; padding: 4px; }}
QListWidget[flat="true"] {{ background: transparent; border: 0; padding: 0; }}
QListWidget::item {{ padding: 7px 8px; border-radius: 7px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {C.raised}; }}
QListWidget::item:selected {{ background: {C.hover}; color: {C.text}; }}
QTableWidget {{ gridline-color: transparent; }}
QTableWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
QTableWidget::item:selected {{ background: {C.hover}; color: {C.text}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: transparent; color: {C.muted}; border: 0; border-bottom: 1px solid {C.border}; padding: 6px 8px; font-weight: 700; font-size: 8pt; }}
QTableCornerButton::section {{ background: transparent; border: 0; }}

QMenu {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {C.accent_deep}; color: {C.on_accent}; }}
QMenu::separator {{ height: 1px; background: {C.border_strong}; margin: 5px 6px; }}

QProgressBar {{ background: {C.raised}; border: 0; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {_gradient(C.accent_deep, C.violet)}; border-radius: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C.border_strong}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {C.faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {C.border_strong}; border-radius: 3px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {C.faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


class _TitleBars(QObject):
    """Windows 11: tint each window's title bar to match the app instead of leaving it light or grey."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(obj, (QMainWindow, QDialog)):
            _tint_title_bar(obj)
        return False


def _tint_title_bar(window: QWidget) -> None:
    try:
        import ctypes

        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi
        colour = QColor(C.window)
        for attr, value in (
            (20, 1),  # DWMWA_USE_IMMERSIVE_DARK_MODE
            (35, colour.red() | colour.green() << 8 | colour.blue() << 16),  # DWMWA_CAPTION_COLOR (COLORREF)
        ):
            ref = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ref), ctypes.sizeof(ref))
    except Exception:  # noqa: BLE001 - cosmetic; older Windows has no such attribute
        pass


def apply(app: QApplication) -> None:
    """Fusion + our palette + stylesheet; call once, after creating the QApplication."""
    app.setStyle("Fusion")
    app.setPalette(palette())
    font = QFont(app.font())
    if font.pointSizeF() < 9.5:
        font.setPointSizeF(9.5)
    app.setFont(font)
    app.setStyleSheet(stylesheet())
    if sys.platform == "win32" and app.platformName() == "windows":
        app._espsync_title_bars = _TitleBars(app)  # keep a reference
        app.installEventFilter(app._espsync_title_bars)


def set_role(widget: QWidget, role: str) -> QWidget:
    """Give a label one of the stylesheet's text roles (title, heading, muted, hint, pill, …)."""
    widget.setProperty("role", role)
    return widget


def set_kind(button: QWidget, kind: str) -> QWidget:
    """Give a button one of the stylesheet's looks (primary, accent, chip, tile, segment, quiet, danger, icon)."""
    button.setProperty("kind", kind)
    return button


def restyle(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def glow(widget: QWidget, colour: str, radius: float = 26.0, alpha: int = 150) -> QGraphicsDropShadowEffect:
    """A soft coloured halo around a widget (the sync button)."""
    effect = QGraphicsDropShadowEffect(widget)
    c = QColor(colour)
    c.setAlpha(alpha)
    effect.setColor(c)
    effect.setBlurRadius(radius)
    effect.setOffset(0, 2)
    widget.setGraphicsEffect(effect)
    return effect


def swatch(rgb: tuple[float, float, float], size: int = 14, ring: bool = True) -> QPixmap:
    """A round colour dot with a soft highlight (scene buttons, menus)."""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r, g, b = (min(max(c, 0.0), 1.0) for c in rgb)
    base = QColor.fromRgbF(r, g, b)
    centre = QPointF(size / 2, size / 2)
    grad = QRadialGradient(QPointF(size * 0.38, size * 0.34), size * 0.75)
    grad.setColorAt(0.0, base.lighter(135))
    grad.setColorAt(1.0, base.darker(125))
    p.setBrush(grad)
    p.setPen(QPen(QColor(255, 255, 255, 70), 1.0) if ring else Qt.PenStyle.NoPen)
    p.drawEllipse(centre, size / 2 - 1, size / 2 - 1)
    p.end()
    return pm
