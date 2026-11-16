"""Small custom widgets shared by the windows: cards, toggle switches, the
area selector, the live colour bar and the light list's rows."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Property, QEasingCurve, QModelIndex, QPointF, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .theme import BRAND, C, set_kind, set_role

# Item data roles of the light list (see LightDelegate).
COLOUR_ROLE = Qt.ItemDataRole.UserRole + 1  # QColor the light shows now, or None
DETAIL_ROLE = Qt.ItemDataRole.UserRole + 2  # second line, e.g. "Lamp · ESPHome"
TAG_ROLE = Qt.ItemDataRole.UserRole + 3  # why it is not driven ("off", "not in area"), or ""


class Card(QFrame):
    """A rounded panel with a small caps title and room for controls beside it."""

    def __init__(self, title: str = "", tone: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        if tone:
            self.setProperty("tone", tone)
        self.col = QVBoxLayout(self)
        self.col.setContentsMargins(12, 10, 12, 12)
        self.col.setSpacing(8)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self.title = set_role(QLabel(title.upper()), "heading")
        self.header.addWidget(self.title)
        self.header.addStretch(1)
        if title:
            self.col.addLayout(self.header)

    def add_to_header(self, widget: QWidget) -> QWidget:
        self.header.addWidget(widget)
        return widget

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.col.addWidget(widget, stretch)
        return widget


class ToggleSwitch(QCheckBox):
    """A check box drawn as a sliding switch."""

    TRACK_W, TRACK_H = 34, 18

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._knob = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._slide)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _slide(self, on: bool) -> None:
        self._anim.stop()
        if not self.isVisible():
            self.knob = float(on)
            return
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(float(on))
        self._anim.start()

    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, value: float) -> None:
        self._knob = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        text = fm.horizontalAdvance(self.text()) + 9 if self.text() else 0
        return QSize(self.TRACK_W + text + 2, max(self.TRACK_H + 6, fm.height() + 6))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:  # noqa: ANN001
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        track = QRectF(1, (self.height() - self.TRACK_H) / 2, self.TRACK_W, self.TRACK_H)
        off, on = QColor(C.border_strong), QColor(C.accent_deep)
        k = self._knob
        mix = QColor(
            round(off.red() + (on.red() - off.red()) * k),
            round(off.green() + (on.green() - off.green()) * k),
            round(off.blue() + (on.blue() - off.blue()) * k),
        )
        if not enabled:
            mix = QColor(C.border)
        p.setPen(QPen(QColor(C.accent), 1.2) if self.underMouse() and enabled else Qt.PenStyle.NoPen)
        p.setBrush(mix)
        p.drawRoundedRect(track, self.TRACK_H / 2, self.TRACK_H / 2)
        r = self.TRACK_H / 2 - 3
        x = track.left() + 3 + r + (track.width() - 6 - 2 * r) * k
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 60))
        p.drawEllipse(QPointF(x, track.center().y() + 1), r, r)
        p.setBrush(QColor("#ffffff") if enabled else QColor(C.faint))
        p.drawEllipse(QPointF(x, track.center().y()), r, r)
        if self.text():
            p.setPen(QColor(C.text) if enabled else QColor(C.faint))
            p.drawText(
                QRectF(track.right() + 9, 0, self.width() - track.right() - 9, self.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self.text(),
            )
        p.end()

    def enterEvent(self, event) -> None:  # noqa: ANN001
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self.update()
        super().leaveEvent(event)


def slider_row(slider: QSlider, fmt: Callable[[int], str] = lambda v: f"{v}%") -> QWidget:
    """The slider with its value shown beside it."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    value = set_role(QLabel(fmt(slider.value())), "value")
    value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    slider.valueChanged.connect(lambda v: value.setText(fmt(v)))
    row.addWidget(slider, 1)
    row.addWidget(value)
    return holder


class Segmented(QFrame):
    """A row of mutually exclusive buttons in one rounded frame (the area selector)."""

    chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segmented")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(3, 3, 3, 3)
        self.row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        self._options: list[tuple[str, str, str]] = []

    def set_options(self, options: list[tuple[str, str, str]], current: str) -> None:
        """options: (key, label, tooltip)."""
        if options == self._options:
            self.set_current(current)
            return
        self._options = list(options)
        for btn in self.buttons.values():
            self.group.removeButton(btn)
            self.row.removeWidget(btn)
            btn.hide()
            btn.deleteLater()
        self.buttons = {}
        for key, label, tip in options:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tip)
            set_kind(btn, "segment")
            btn.clicked.connect(lambda _=False, k=key: self.chosen.emit(k))
            self.group.addButton(btn)
            self.row.addWidget(btn)
            self.buttons[key] = btn
        self.set_current(current)

    def set_current(self, key: str) -> None:
        btn = self.buttons.get(key)
        if btn is not None:
            btn.setChecked(True)

    def current(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "")


class AmbientBar(QWidget):
    """A thin line of the colours the lights show right now (the app's colours while idle)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(3)
        self._colours: list[tuple[int, int, int]] = []

    def set_colours(self, colours: list[QColor]) -> None:
        new = [(c.red(), c.green(), c.blue()) for c in colours]
        if new != self._colours:
            self._colours = new
            self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        p = QPainter(self)
        grad = QLinearGradient(0, 0, self.width(), 0)
        if self._colours:
            stops = [QColor(*c) for c in self._colours]
        else:
            stops = [QColor(c) for c in BRAND]
            for c in stops:
                c.setAlpha(120)
        if len(stops) == 1:
            stops = stops * 2
        for i, c in enumerate(stops):
            grad.setColorAt(i / (len(stops) - 1), c)
        p.fillRect(self.rect(), QColor(C.window))
        p.fillRect(self.rect(), grad)
        p.end()


class LightDelegate(QStyledItemDelegate):
    """A light in the list: a glowing dot in its live colour, its name, and what it is."""

    HEIGHT = 48

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(120, self.HEIGHT)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(1, 2, -1, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected or option.state & QStyle.StateFlag.State_MouseOver:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.hover) if selected else QColor(C.raised))
            p.drawRoundedRect(r, 9, 9)
        if selected:
            p.setBrush(QColor(C.accent))
            p.drawRoundedRect(QRectF(r.left() + 1, r.top() + 12, 3, r.height() - 24), 1.5, 1.5)

        tag = index.data(TAG_ROLE) or ""
        colour = index.data(COLOUR_ROLE)
        centre = QPointF(r.left() + 24, r.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        if isinstance(colour, QColor) and not tag:
            halo = QRadialGradient(centre, 15)
            inner, outer = QColor(colour), QColor(colour)
            inner.setAlpha(120)
            outer.setAlpha(0)
            halo.setColorAt(0.3, inner)
            halo.setColorAt(1.0, outer)
            p.setBrush(halo)
            p.drawEllipse(centre, 15, 15)
            orb = QRadialGradient(centre + QPointF(-2, -2), 9)
            orb.setColorAt(0.0, colour.lighter(140))
            orb.setColorAt(1.0, colour)
            p.setBrush(orb)
            p.drawEllipse(centre, 7, 7)
        else:
            p.setPen(QPen(QColor(C.border_strong), 1.5))
            p.setBrush(QColor(C.raised))
            p.drawEllipse(centre, 6.5, 6.5)

        font = QFont(option.font)
        small = QFont(option.font)
        small.setPointSizeF(8.0)
        left = r.left() + 46
        right = r.right() - 10
        if tag:
            p.setFont(small)
            width = p.fontMetrics().horizontalAdvance(tag) + 14
            pill = QRectF(right - width, r.center().y() - 9, width, 18)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRoundedRect(pill, 9, 9)
            p.setPen(QColor(C.faint))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, tag)
            right = pill.left() - 8
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(C.faint) if tag else QColor(C.text))
        name = p.fontMetrics().elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideRight,
                                          int(right - left))
        p.drawText(QRectF(left, r.top() + 6, right - left, 18), Qt.AlignmentFlag.AlignVCenter, name)
        p.setFont(small)
        p.setPen(QColor(C.faint))
        detail = p.fontMetrics().elidedText(index.data(DETAIL_ROLE) or "", Qt.TextElideMode.ElideRight,
                                            int(right - left))
        p.drawText(QRectF(left, r.top() + 24, right - left, 16), Qt.AlignmentFlag.AlignVCenter, detail)
        p.restore()
