"""The app's look: one palette, one stylesheet.

Colours live in :data:`C` so painted widgets (room, screen preview) and the
stylesheet agree.  The few glyphs the stylesheet needs (arrows, the checkbox
tick) are drawn at start-up into a temp folder, so nothing has to ship as a
resource file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

C = SimpleNamespace(
    window="#14161b",  # behind everything
    surface="#1b1e25",  # panels, cards
    raised="#242833",  # buttons, inputs
    hover="#2d3240",
    border="#2b303b",
    border_strong="#3a4150",
    text="#e7e9ee",
    muted="#98a0ae",
    faint="#6b7280",
    accent="#6c8cff",
    accent_hover="#829dff",
    accent_pressed="#5877e6",
    on_accent="#0d1020",
    danger="#ef5a5f",
    danger_hover="#f47378",
    ok="#4cc38a",
    warn="#f0b45a",
    canvas="#101216",  # room view / screen preview background
    floor="#191c23",  # the room's floor
    grid="#252a34",
)


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
    pal.setColor(QPalette.ColorRole.Highlight, QColor(C.accent))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(C.on_accent))
    pal.setColor(QPalette.ColorRole.PlaceholderText, muted)
    pal.setColor(QPalette.ColorRole.Link, QColor(C.accent_hover))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, muted)
    return pal


def stylesheet() -> str:
    down = _glyph("down", _chevron(C.muted))
    up = _glyph("up", _chevron(C.muted, up=True))
    tick = _glyph("tick", _tick)
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {C.text}; }}
QMainWindow, QDialog {{ background: {C.window}; }}
QToolTip {{ background: {C.raised}; color: {C.text}; border: 1px solid {C.border_strong}; padding: 5px 8px; border-radius: 6px; }}

/* ---- text roles */
QLabel[role="title"] {{ font-size: 13pt; font-weight: 600; }}
QLabel[role="heading"] {{ color: {C.muted}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }}
QLabel[role="muted"] {{ color: {C.muted}; }}
QLabel[role="hint"] {{ color: {C.faint}; font-size: 8.5pt; }}
QLabel[role="pill"] {{ background: {C.raised}; color: {C.muted}; border-radius: 9px; padding: 2px 10px; font-weight: 600; font-size: 8.5pt; }}
QLabel[role="pill"][state="on"] {{ background: {C.ok}; color: {C.on_accent}; }}
QLabel[role="pill"][state="error"] {{ background: {C.danger}; color: {C.on_accent}; }}

/* ---- panels */
QWidget#header {{ background: {C.surface}; border-bottom: 1px solid {C.border}; }}
QFrame#card, QWidget#card {{ background: {C.surface}; border: 1px solid {C.border}; border-radius: 10px; }}
QFrame#divider {{ background: {C.border}; max-width: 1px; min-width: 1px; }}
QStatusBar {{ background: {C.surface}; border-top: 1px solid {C.border}; color: {C.muted}; padding: 3px 8px; min-height: 26px; }}
QGraphicsView {{ background: {C.canvas}; border: 0; }}
QStatusBar::item {{ border: 0; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}

/* ---- buttons */
QPushButton {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 7px; padding: 6px 14px; }}
QPushButton:hover {{ background: {C.hover}; }}
QPushButton:pressed {{ background: {C.border}; }}
QPushButton:disabled {{ color: {C.faint}; background: {C.surface}; border-color: {C.border}; }}
QPushButton::menu-indicator {{ image: url({down}); width: 14px; height: 14px; subcontrol-position: right center; right: 6px; }}
QPushButton[kind="primary"] {{ background: {C.accent}; border-color: {C.accent}; color: {C.on_accent}; font-weight: 700; padding: 8px 20px; border-radius: 9px; font-size: 10.5pt; }}
QPushButton[kind="primary"]:hover {{ background: {C.accent_hover}; border-color: {C.accent_hover}; }}
QPushButton[kind="primary"]:pressed {{ background: {C.accent_pressed}; }}
QPushButton[kind="primary"]:checked {{ background: {C.danger}; border-color: {C.danger}; }}
QPushButton[kind="primary"]:checked:hover {{ background: {C.danger_hover}; border-color: {C.danger_hover}; }}
QPushButton[kind="chip"] {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 13px; padding: 4px 13px 4px 10px; min-height: 18px; color: {C.text}; }}
QPushButton[kind="chip"]:hover {{ background: {C.hover}; border-color: {C.faint}; }}
QPushButton[kind="chip"]:pressed {{ background: {C.border}; }}
QPushButton[kind="quiet"] {{ background: transparent; border-color: transparent; color: {C.muted}; padding: 6px 10px; }}
QPushButton[kind="quiet"]:hover {{ background: {C.hover}; color: {C.text}; }}

/* ---- inputs */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 7px; padding: 5px 8px; min-height: 18px; selection-background-color: {C.accent}; selection-color: {C.on_accent}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color: {C.accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {C.faint}; background: {C.surface}; border-color: {C.border}; }}
QComboBox::drop-down {{ border: 0; width: 24px; subcontrol-position: right center; }}
QComboBox::down-arrow {{ image: url({down}); width: 16px; height: 16px; }}
QComboBox QAbstractItemView {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 7px; padding: 4px; selection-background-color: {C.accent}; selection-color: {C.on_accent}; }}
/* the line edit inside a spin box or editable combo: no box of its own */
QAbstractSpinBox > QLineEdit, QComboBox > QLineEdit {{ border: 0; background: transparent; padding: 0 0 0 5px; min-height: 0; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: 0; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: 0; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({up}); width: 14px; height: 14px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({down}); width: 14px; height: 14px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QListView::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {C.border_strong}; background: {C.raised}; }}
QCheckBox::indicator:hover, QListView::indicator:hover {{ border-color: {C.accent}; }}
QCheckBox::indicator:checked, QListView::indicator:checked {{ background: {C.accent}; border-color: {C.accent}; image: url({tick}); }}
QCheckBox::indicator:disabled {{ background: {C.surface}; border-color: {C.border}; }}

QSlider::groove:horizontal {{ height: 4px; background: {C.border_strong}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {C.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -6px 0; border-radius: 8px; background: {C.text}; border: 1px solid {C.window}; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::sub-page:horizontal:disabled {{ background: {C.faint}; }}
QSlider::handle:horizontal:disabled {{ background: {C.faint}; }}

/* ---- containers */
QGroupBox {{ background: {C.surface}; border: 1px solid {C.border}; border-radius: 10px; margin-top: 22px; padding: 12px 12px 10px 12px; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 4px; top: 2px; color: {C.muted}; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }}
QScrollArea {{ border: 0; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QTabWidget::pane {{ border: 0; border-top: 1px solid {C.border}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {C.muted}; padding: 8px 14px; margin-right: 2px; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:hover {{ color: {C.text}; }}
QTabBar::tab:selected {{ color: {C.text}; border-bottom: 2px solid {C.accent}; }}

QListWidget, QTreeWidget, QTableWidget, QPlainTextEdit, QTextEdit {{ background: {C.surface}; border: 1px solid {C.border}; border-radius: 10px; padding: 4px; }}
QListWidget::item {{ padding: 7px 8px; border-radius: 6px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {C.raised}; }}
QListWidget::item:selected {{ background: {C.hover}; color: {C.text}; border-left: 3px solid {C.accent}; }}
QHeaderView::section {{ background: {C.surface}; color: {C.muted}; border: 0; border-bottom: 1px solid {C.border}; padding: 6px; font-weight: 600; }}

QMenu {{ background: {C.raised}; border: 1px solid {C.border_strong}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {C.accent}; color: {C.on_accent}; }}
QMenu::separator {{ height: 1px; background: {C.border_strong}; margin: 5px 6px; }}

QProgressBar {{ background: {C.raised}; border: 0; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {C.accent}; border-radius: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C.border_strong}; border-radius: 3px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {C.faint}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {C.border_strong}; border-radius: 3px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {C.faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
"""


def apply(app: QApplication) -> None:
    """Fusion + our palette + stylesheet; call once, after creating the QApplication."""
    app.setStyle("Fusion")
    app.setPalette(palette())
    font = QFont(app.font())
    if font.pointSizeF() < 9.5:
        font.setPointSizeF(9.5)
    app.setFont(font)
    app.setStyleSheet(stylesheet())


def set_role(widget: QWidget, role: str) -> QWidget:
    """Give a label one of the stylesheet's text roles (title, heading, muted, hint, pill)."""
    widget.setProperty("role", role)
    return widget


def set_kind(button: QWidget, kind: str) -> QWidget:
    """Give a button one of the stylesheet's looks (primary, chip, quiet)."""
    button.setProperty("kind", kind)
    return button


def restyle(widget: QWidget) -> None:
    """Re-apply the stylesheet after a dynamic property changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def swatch(rgb: tuple[float, float, float], size: int = 14, ring: bool = True) -> QPixmap:
    """A round colour dot (scene buttons, light list)."""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r, g, b = (min(max(c, 0.0), 1.0) for c in rgb)
    p.setBrush(QColor.fromRgbF(r, g, b))
    p.setPen(QPen(QColor(255, 255, 255, 70), 1.0) if ring else Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(size / 2, size / 2), size / 2 - 1, size / 2 - 1)
    p.end()
    return pm
