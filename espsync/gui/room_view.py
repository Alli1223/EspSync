"""Top-down room editor: drag lights around, scroll over a lamp to set its height."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsSceneWheelEvent,
    QFrame,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ..config import AppConfig, LightConfig, RoomConfig
from .theme import C

ROOM_PX = 520.0
LAMP_R = 15.0
GLOW_R = 84.0  # how far a lamp's light spills over the floor
IDLE = QColor(84, 90, 104)
LABEL_BG = QColor(9, 11, 15, 190)


def draw_label(p: QPainter, centre_x: float, top: float, text: str, colour: QColor) -> None:
    """A light's name on a dark pill, so it reads over the glows."""
    f = QFont(p.font())
    f.setPointSizeF(8.0)
    f.setWeight(QFont.Weight.DemiBold)
    p.setFont(f)
    width = min(p.fontMetrics().horizontalAdvance(text) + 14.0, 150.0)
    pill = QRectF(centre_x - width / 2, top, width, 17)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(LABEL_BG)
    p.drawRoundedRect(pill, 8.5, 8.5)
    p.setPen(colour)
    p.drawText(pill, Qt.AlignmentFlag.AlignCenter,
               p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(width - 8)))


def qcolor(rgb: np.ndarray | None) -> QColor:
    if rgb is None:
        return IDLE
    r, g, b = (float(min(max(c, 0.0), 1.0)) for c in rgb[:3])
    return QColor.fromRgbF(r, g, b)


class Geometry:
    def __init__(self, room: RoomConfig) -> None:
        aspect = max(room.width_m, 0.5) / max(room.depth_m, 0.5)
        if aspect >= 1:
            self.w, self.d = ROOM_PX, ROOM_PX / aspect
        else:
            self.w, self.d = ROOM_PX * aspect, ROOM_PX
        self.tv_w = room.tv_width * self.w

    @property
    def rect(self) -> QRectF:
        return QRectF(-self.w / 2, -self.d / 2, self.w, self.d)

    def to_scene(self, x: float, y: float) -> QPointF:
        return QPointF(x * self.w / 2, y * self.d / 2)

    def from_scene(self, p: QPointF) -> tuple[float, float]:
        x = max(-1.0, min(1.0, p.x() / (self.w / 2)))
        y = max(-1.0, min(1.0, p.y() / (self.d / 2)))
        return x, y

    def clamp(self, p: QPointF) -> QPointF:
        return self.to_scene(*self.from_scene(p))


class LampItem(QGraphicsObject):
    def __init__(self, light: LightConfig, geo: Geometry, on_change: Callable[[str], None],
                 config: AppConfig) -> None:
        super().__init__()
        self.light = light
        self.config = config
        self.geo = geo
        self.on_change = on_change
        self.color: QColor = IDLE
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setPos(geo.to_scene(light.x, light.y))
        self.setZValue(10)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move · scroll to change height")

    def boundingRect(self) -> QRectF:
        return QRectF(-GLOW_R, -GLOW_R, 2 * GLOW_R, 2 * GLOW_R)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), LAMP_R + 3, LAMP_R + 3)
        return path

    def paint(self, p: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.config.is_active(self.light)
        lit = active and self.color != IDLE
        selected = self.isSelected()
        p.setPen(Qt.PenStyle.NoPen)
        if lit:
            # The light it throws on the floor; pools from several lamps add up.
            glow = QRadialGradient(QPointF(0, 0), GLOW_R)
            for stop, alpha in ((0.0, 120), (0.3, 62), (0.65, 18), (1.0, 0)):
                c = QColor(self.color)
                c.setAlpha(alpha)
                glow.setColorAt(stop, c)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            p.setBrush(glow)
            p.drawEllipse(QPointF(0, 0), GLOW_R, GLOW_R)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if selected:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C.accent), 2.0))
            p.drawEllipse(QPointF(0, 0), LAMP_R + 5, LAMP_R + 5)
        if active:
            base = self.color
            orb = QRadialGradient(QPointF(-4, -5), LAMP_R * 1.7)
            orb.setColorAt(0.0, base.lighter(155) if lit else base.lighter(125))
            orb.setColorAt(0.55, base)
            orb.setColorAt(1.0, base.darker(125))
            p.setBrush(orb)
            p.setPen(QPen(QColor("white") if selected else QColor(C.canvas), 2.0))
        else:
            p.setBrush(QColor(C.raised))
            p.setPen(QPen(QColor(C.border_strong), 1.5, Qt.PenStyle.DashLine))
        p.drawEllipse(QPointF(0, 0), LAMP_R, LAMP_R)
        # Height gauge
        bar = QRectF(LAMP_R + 7, -LAMP_R + 2, 4, 2 * LAMP_R - 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(bar, 2, 2)
        fill = bar.height() * max(0.0, min(1.0, self.light.z))
        p.setBrush(QColor(C.text) if active else QColor(C.faint))
        p.drawRoundedRect(QRectF(bar.left(), bar.bottom() - fill, bar.width(), fill), 2, 2)
        draw_label(p, 0, LAMP_R + 7, self.light.name, QColor(C.text) if active else QColor(C.faint))

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):  # noqa: ANN001
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self.geo.clamp(value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.light.x, self.light.y = self.geo.from_scene(self.pos())
            self.on_change(self.light.id)
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def wheelEvent(self, event: QGraphicsSceneWheelEvent) -> None:
        steps = event.delta() / 120.0
        self.light.z = round(max(0.0, min(1.0, self.light.z + 0.05 * steps)), 3)
        self.update()
        self.on_change(self.light.id)
        event.accept()


class HandleItem(QGraphicsObject):
    """Draggable end point of a room-layout strip."""

    def __init__(self, strip: StripItem, end: int) -> None:
        super().__init__()
        self.strip = strip
        self.end = end
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        light = strip.light
        x, y = (light.x, light.y) if end == 0 else (light.x2, light.y2)
        self.setPos(strip.geo.to_scene(x, y))
        self.setZValue(12)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Strip start" if end == 0 else "Strip end")

    def boundingRect(self) -> QRectF:
        return QRectF(-7, -7, 14, 14)

    def paint(self, p: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("white"), 1.5))
        p.setBrush(QColor(C.accent_deep))
        if self.end == 0:
            p.drawEllipse(QPointF(0, 0), 5.5, 5.5)
        else:
            p.drawRoundedRect(QRectF(-5, -5, 10, 10), 2.5, 2.5)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value):  # noqa: ANN001
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return self.strip.geo.clamp(value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            x, y = self.strip.geo.from_scene(self.pos())
            if self.end == 0:
                self.strip.light.x, self.strip.light.y = x, y
            else:
                self.strip.light.x2, self.strip.light.y2 = x, y
            self.strip.prepareGeometryChange()
            self.strip.update()
            self.strip.on_change(self.strip.light.id)
        return super().itemChange(change, value)


class StripItem(QGraphicsObject):
    """An addressable strip: a line in the room, or a band behind the TV."""

    def __init__(self, light: LightConfig, geo: Geometry, on_change: Callable[[str], None],
                 config: AppConfig) -> None:
        super().__init__()
        self.light = light
        self.config = config
        self.geo = geo
        self.on_change = on_change
        self.colors: np.ndarray | None = None
        self.handles: list[HandleItem] = []
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(8)

    def _points(self) -> list[QPointF]:
        light = self.light
        if light.strip_layout == "screen":
            # Draw the screen path as a band hugging the TV's back edge.
            left = -self.geo.tv_w / 2
            top = -self.geo.d / 2 + 4
            return [QPointF(left + u * self.geo.tv_w, top + v * 12) for u, v in light.screen_path]
        return [self.geo.to_scene(light.x, light.y), self.geo.to_scene(light.x2, light.y2)]

    def _path(self) -> QPainterPath:
        pts = self._points()
        path = QPainterPath(pts[0])
        for pt in pts[1:]:
            path.lineTo(pt)
        return path

    def boundingRect(self) -> QRectF:
        return self._path().boundingRect().adjusted(-76, -20, 76, 30)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        return stroker.createStroke(self._path())

    def paint(self, p: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self._path()
        active = self.config.is_active(self.light)
        colors = self.colors
        n = len(colors) if colors is not None and active else 0
        length = path.length()
        segments = []
        if n and length > 0:
            step = length / n
            for i in range(n):
                a = path.pointAtPercent(path.percentAtLength(i * step))
                b = path.pointAtPercent(path.percentAtLength(min(length, (i + 1) * step)))
                segments.append((a, b, qcolor(colors[n - 1 - i if self.light.reverse else i])))
        if segments and n <= 150:
            # The strip's light on the floor around it.
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
            for a, b, c in segments:
                halo = QColor(c)
                halo.setAlpha(34)
                p.setPen(QPen(halo, 22, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawLine(a, b)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if self.isSelected():
            p.setPen(QPen(QColor(C.accent), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(path)
        p.setPen(QPen(QColor(C.canvas), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(path)
        if segments:
            for a, b, c in segments:
                p.setPen(QPen(c, 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawLine(a, b)
        else:
            p.setPen(QPen(IDLE if active else QColor(C.border_strong), 5, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawPath(path)
        mid = path.pointAtPercent(0.5)
        draw_label(p, mid.x(), mid.y() + 9, self.light.name, QColor(C.text) if active else QColor(C.faint))

    def add_handles(self, scene: QGraphicsScene) -> None:
        if self.light.strip_layout == "screen":
            return
        for end in (0, 1):
            h = HandleItem(self, end)
            scene.addItem(h)
            self.handles.append(h)


class RoomView(QGraphicsView):
    lightSelected = Signal(str)
    lightChanged = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setFrameShape(QFrame.Shape.NoFrame)  # it sits in a card that supplies the background
        self.setMinimumSize(360, 300)
        self.items_by_id: dict[str, LampItem | StripItem] = {}
        self._suppress = False
        self.scene().selectionChanged.connect(self._on_selection)
        self.rebuild()

    # -------------------------------------------------------------- building
    def rebuild(self) -> None:
        selected = self.selected_id()
        self._suppress = True
        scene = self.scene()
        scene.clear()
        self.items_by_id.clear()
        geo = self.geo = Geometry(self.config.room)
        room = geo.rect
        scene.setSceneRect(room.adjusted(-44, -42, 44, 34))

        floor = QPainterPath()
        floor.addRoundedRect(room, 12, 12)
        wood = QLinearGradient(room.topLeft(), room.bottomLeft())
        wood.setColorAt(0.0, QColor(C.floor_edge))
        wood.setColorAt(1.0, QColor(C.floor))
        scene.addPath(floor, QPen(QColor(C.border_strong), 1.2), QBrush(wood))
        dots = QPainterPath()
        for i in range(1, 8):
            for j in range(1, 8):
                centre = QPointF(room.left() + room.width() * i / 8, room.top() + room.height() * j / 8)
                dots.addEllipse(centre, 1.1, 1.1)
        scene.addPath(dots, QPen(Qt.PenStyle.NoPen), QBrush(QColor(C.grid)))

        # The screen, and the light it throws into the room.
        throw = QPainterPath()
        reach = geo.d * 0.42
        throw.moveTo(-geo.tv_w / 2, room.top() + 2)
        throw.lineTo(geo.tv_w / 2, room.top() + 2)
        throw.lineTo(geo.tv_w / 2 + reach * 0.45, room.top() + reach)
        throw.lineTo(-geo.tv_w / 2 - reach * 0.45, room.top() + reach)
        throw.closeSubpath()
        beam = QLinearGradient(0, room.top(), 0, room.top() + reach)
        beam.setColorAt(0.0, QColor(160, 175, 255, 30))
        beam.setColorAt(1.0, QColor(160, 175, 255, 0))
        scene.addPath(throw.intersected(floor), QPen(Qt.PenStyle.NoPen), QBrush(beam))
        tv = QPainterPath()
        tv.addRoundedRect(QRectF(-geo.tv_w / 2, room.top() - 5, geo.tv_w, 8), 4, 4)
        scene.addPath(tv, QPen(QColor(C.accent), 1.2), QBrush(QColor("#05060a")))
        caps = QFont(self.font())
        caps.setPointSizeF(7.5)
        caps.setWeight(QFont.Weight.Bold)
        caps.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        label = scene.addSimpleText("SCREEN", caps)
        label.setBrush(QColor(C.muted))
        label.setPos(-label.boundingRect().width() / 2, room.top() - 26)
        sofa = QPainterPath()
        sofa_rect = QRectF(-geo.w * 0.18, geo.d * 0.22, geo.w * 0.36, 30)
        sofa.addRoundedRect(sofa_rect, 9, 9)
        sofa.addRoundedRect(sofa_rect.adjusted(-7, 6, -sofa_rect.width() + 1, 4), 4, 4)
        sofa.addRoundedRect(sofa_rect.adjusted(sofa_rect.width() - 1, 6, 7, 4), 4, 4)
        sofa.setFillRule(Qt.FillRule.WindingFill)
        scene.addPath(sofa, QPen(QColor(C.border_strong), 1), QBrush(QColor(C.raised)))
        sofa_label = scene.addSimpleText("YOU", caps)
        sofa_label.setBrush(QColor(C.faint))
        sofa_label.setPos(-sofa_label.boundingRect().width() / 2,
                          sofa_rect.center().y() - sofa_label.boundingRect().height() / 2)

        for light in self.config.lights:
            if light.kind == "strip":
                item: LampItem | StripItem = StripItem(light, geo, self._changed, self.config)
                scene.addItem(item)
                item.add_handles(scene)
            else:
                item = LampItem(light, geo, self._changed, self.config)
                scene.addItem(item)
            self.items_by_id[light.id] = item
        self._suppress = False
        if selected:
            self.select(selected)
        self.fit()

    def fit(self) -> None:
        self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        self.fit()

    # ------------------------------------------------------------- updating
    def _changed(self, light_id: str) -> None:
        if not self._suppress:
            self.lightChanged.emit(light_id)

    def refresh_light(self, light_id: str) -> None:
        item = self.items_by_id.get(light_id)
        if item is None:
            return
        self._suppress = True
        if isinstance(item, LampItem):
            item.setPos(self.geo.to_scene(item.light.x, item.light.y))
        else:
            item.prepareGeometryChange()
        item.update()
        self._suppress = False

    def set_colors(self, colors: dict[str, np.ndarray]) -> None:
        for lid, item in self.items_by_id.items():
            c = colors.get(lid)
            if isinstance(item, LampItem):
                new = qcolor(c[0]) if c is not None and len(c) else IDLE
                if new != item.color:
                    item.color = new
                    item.update()
            else:
                item.colors = c
                item.update()

    # ------------------------------------------------------------- selection
    def selected_id(self) -> str | None:
        if not hasattr(self, "items_by_id"):
            return None
        for lid, item in self.items_by_id.items():
            if item.isSelected():
                return lid
        return None

    def select(self, light_id: str | None) -> None:
        self._suppress = True
        for lid, item in self.items_by_id.items():
            item.setSelected(lid == light_id)
        self._suppress = False

    def _on_selection(self) -> None:
        if self._suppress:
            return
        lid = self.selected_id()
        if lid:
            self.lightSelected.emit(lid)
