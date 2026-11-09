"""Top-down room editor: drag lights around, scroll over a lamp to set its height."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsSceneWheelEvent,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ..config import AppConfig, LightConfig, RoomConfig

ROOM_PX = 520.0
LAMP_R = 15.0
IDLE = QColor(90, 90, 100)


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
    def __init__(self, light: LightConfig, geo: Geometry, on_change: Callable[[str], None]) -> None:
        super().__init__()
        self.light = light
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
        return QRectF(-70, -LAMP_R - 4, 140, 2 * LAMP_R + 26)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addEllipse(QPointF(0, 0), LAMP_R + 3, LAMP_R + 3)
        return path

    def paint(self, p: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.light.enabled
        glow = QColor(self.color)
        glow.setAlpha(70 if enabled else 0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(0, 0), LAMP_R + 4, LAMP_R + 4)
        p.setBrush(self.color if enabled else IDLE.darker(150))
        pen = QPen(QColor("white") if self.isSelected() else QColor(30, 30, 35), 2.5 if self.isSelected() else 1.5)
        p.setPen(pen)
        p.drawEllipse(QPointF(0, 0), LAMP_R, LAMP_R)
        # Height gauge
        bar = QRectF(LAMP_R + 5, -LAMP_R, 5, 2 * LAMP_R)
        p.setPen(QPen(QColor(150, 150, 160), 1))
        p.setBrush(QColor(40, 40, 46))
        p.drawRect(bar)
        fill = bar.height() * max(0.0, min(1.0, self.light.z))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(200, 200, 210))
        p.drawRect(QRectF(bar.left(), bar.bottom() - fill, bar.width(), fill))
        # Label
        p.setPen(QColor(230, 230, 235) if enabled else QColor(130, 130, 135))
        f = QFont(p.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.drawText(QRectF(-70, LAMP_R + 4, 140, 16), Qt.AlignmentFlag.AlignHCenter, self.light.name)

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
        p.setBrush(QColor(60, 60, 70))
        if self.end == 0:
            p.drawEllipse(QPointF(0, 0), 5.5, 5.5)
        else:
            p.drawRect(QRectF(-5, -5, 10, 10))

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

    def __init__(self, light: LightConfig, geo: Geometry, on_change: Callable[[str], None]) -> None:
        super().__init__()
        self.light = light
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
        return self._path().boundingRect().adjusted(-12, -12, 12, 24)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        return stroker.createStroke(self._path())

    def paint(self, p: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self._path()
        if self.isSelected():
            p.setPen(QPen(QColor(255, 255, 255, 160), 11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(path)
        p.setPen(QPen(QColor(25, 25, 30), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawPath(path)
        colors = self.colors
        n = len(colors) if colors is not None and self.light.enabled else 0
        length = path.length()
        if n and length > 0:
            step = length / n
            for i in range(n):
                a = path.pointAtPercent(path.percentAtLength(i * step))
                b = path.pointAtPercent(path.percentAtLength(min(length, (i + 1) * step)))
                idx = n - 1 - i if self.light.reverse else i
                p.setPen(QPen(qcolor(colors[idx]), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                p.drawLine(a, b)
        else:
            p.setPen(QPen(IDLE, 5))
            p.drawPath(path)
        p.setPen(QColor(220, 220, 225))
        f = QFont(p.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        mid = path.pointAtPercent(0.5)
        p.drawText(QRectF(mid.x() - 70, mid.y() + 6, 140, 16), Qt.AlignmentFlag.AlignHCenter, self.light.name)

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
        self.setBackgroundBrush(QColor(24, 24, 28))
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
        scene.setSceneRect(room.adjusted(-40, -50, 40, 40))

        scene.addRect(room, QPen(QColor(120, 120, 130), 2), QBrush(QColor(34, 34, 40)))
        grid = QPen(QColor(48, 48, 56), 1, Qt.PenStyle.DashLine)
        for i in range(1, 4):
            x = room.left() + room.width() * i / 4
            y = room.top() + room.height() * i / 4
            scene.addLine(x, room.top(), x, room.bottom(), grid)
            scene.addLine(room.left(), y, room.right(), y, grid)

        tv = QRectF(-geo.tv_w / 2, room.top() - 6, geo.tv_w, 10)
        scene.addRect(tv, QPen(QColor(160, 160, 170), 1), QBrush(QColor(10, 10, 12)))
        label = scene.addSimpleText("SCREEN")
        label.setBrush(QColor(170, 170, 180))
        label.setPos(-label.boundingRect().width() / 2, room.top() - 28)
        sofa = QRectF(-geo.w * 0.18, geo.d * 0.22, geo.w * 0.36, 26)
        scene.addRect(sofa, QPen(QColor(70, 70, 80), 1), QBrush(QColor(44, 44, 52)))
        sofa_label = scene.addSimpleText("you")
        sofa_label.setBrush(QColor(110, 110, 120))
        sofa_label.setPos(-sofa_label.boundingRect().width() / 2, sofa.center().y() - 8)
        hint = scene.addSimpleText("Drag lights to where they are in the room · scroll over a lamp to set its height")
        hint.setBrush(QColor(110, 110, 120))
        hint.setPos(-hint.boundingRect().width() / 2, room.bottom() + 10)

        for light in self.config.lights:
            if light.kind == "strip":
                item: LampItem | StripItem = StripItem(light, geo, self._changed)
                scene.addItem(item)
                item.add_handles(scene)
            else:
                item = LampItem(light, geo, self._changed)
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
