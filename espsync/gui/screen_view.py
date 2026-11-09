"""Live screen preview with each light's sample region drawn on top.

Drag a lamp's region to override it, scroll to resize, double-click to go back
to the automatic (room-position based) region.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..config import AppConfig, LightConfig
from ..mapping import Region, light_regions, normalise_region
from .room_view import IDLE, qcolor


class ScreenView(QWidget):
    lightSelected = Signal(str)
    regionEdited = Signal(str)

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.image: QImage | None = None
        self.aspect = 16 / 9
        self.colors: dict[str, np.ndarray] = {}
        self.bars = (0.0, 0.0)
        self.selected: str | None = None
        self.hdr = False
        self._drag: tuple[str, QPointF, list[float]] | None = None
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setToolTip("Drag a lamp's box to choose where it samples · scroll to resize · double-click to reset")

    # ------------------------------------------------------------------ data
    def set_frame(self, rgb: np.ndarray | None) -> None:
        if rgb is None:
            return
        h, w = rgb.shape[:2]
        data = np.ascontiguousarray(rgb)
        self.image = QImage(data.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.aspect = w / h if h else 16 / 9
        self.update()

    def set_colors(self, colors: dict[str, np.ndarray]) -> None:
        self.colors = colors
        self.update()

    def select(self, light_id: str | None) -> None:
        self.selected = light_id
        self.update()

    # -------------------------------------------------------------- geometry
    def _picture(self) -> QRectF:
        r = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        if r.width() / max(r.height(), 1) > self.aspect:
            w = r.height() * self.aspect
            return QRectF(r.center().x() - w / 2, r.top(), w, r.height())
        h = r.width() / self.aspect
        return QRectF(r.left(), r.center().y() - h / 2, r.width(), h)

    def _active(self) -> QRectF:
        pic = self._picture()
        bv, bh = self.bars
        return pic.adjusted(bh * pic.width(), bv * pic.height(), -bh * pic.width(), -bv * pic.height())

    @staticmethod
    def _to_rect(region: Region, active: QRectF) -> QRectF:
        return QRectF(
            active.left() + region.u0 * active.width(),
            active.top() + region.v0 * active.height(),
            region.width * active.width(),
            region.height * active.height(),
        )

    def _lamp_at(self, pos: QPointF) -> LightConfig | None:
        active = self._active()
        lamps = [l for l in self.config.lights if l.enabled and l.kind == "lamp"]
        # Prefer the selected lamp, then the smallest region under the cursor.
        hits = []
        for light in lamps:
            rect = self._to_rect(light_regions(light, self.config.room, self.aspect)[0], active)
            if rect.contains(pos):
                hits.append((light.id != self.selected, rect.width() * rect.height(), light))
        if not hits:
            return None
        hits.sort(key=lambda h: (h[0], h[1]))
        return hits[0][2]

    # --------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: ANN001
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(18, 18, 22))
        pic = self._picture()
        if self.image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(pic, self.image)
        else:
            p.fillRect(pic, QColor(30, 30, 36))
            p.setPen(QColor(140, 140, 150))
            p.drawText(pic, Qt.AlignmentFlag.AlignCenter, "Waiting for screen capture…")
        p.fillRect(pic, QColor(0, 0, 0, 70))  # dim so the overlays read clearly

        bv, bh = self.bars
        if bv > 0 or bh > 0:
            hatch = QBrush(QColor(255, 255, 255, 40), Qt.BrushStyle.BDiagPattern)
            active = self._active()
            for r in (
                QRectF(pic.left(), pic.top(), pic.width(), active.top() - pic.top()),
                QRectF(pic.left(), active.bottom(), pic.width(), pic.bottom() - active.bottom()),
                QRectF(pic.left(), pic.top(), active.left() - pic.left(), pic.height()),
                QRectF(active.right(), pic.top(), pic.right() - active.right(), pic.height()),
            ):
                if r.width() > 0 and r.height() > 0:
                    p.fillRect(r, hatch)

        active = self._active()
        font = QFont(self.font())
        font.setPointSizeF(8.5)
        p.setFont(font)
        lights = sorted(
            (l for l in self.config.lights if l.enabled), key=lambda l: l.id == self.selected
        )
        for light in lights:
            regions = light_regions(light, self.config.room, self.aspect)
            cols = self.colors.get(light.id)
            selected = light.id == self.selected
            if light.kind == "strip":
                for i, region in enumerate(regions):
                    c = qcolor(cols[i]) if cols is not None and i < len(cols) else IDLE
                    c.setAlpha(220)
                    p.setPen(QPen(QColor(255, 255, 255, 200 if selected else 60), 1))
                    p.setBrush(c)
                    p.drawRect(self._to_rect(region, active))
                continue
            rect = self._to_rect(regions[0], active)
            c = qcolor(cols[0]) if cols is not None and len(cols) else IDLE
            fill = QColor(c)
            fill.setAlpha(90 if selected else 45)
            pen = QPen(QColor("white") if selected else c.lighter(130), 2.5 if selected else 1.5)
            if light.region:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(fill)
            p.drawRect(rect)
            p.setPen(QColor("white"))
            p.drawText(rect.adjusted(5, 3, -5, -3), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       light.name + (" (manual)" if light.region else ""))

        if self.hdr:
            p.setPen(QColor(255, 210, 90))
            p.drawText(pic.adjusted(6, 4, -6, -4), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                       "HDR → tone-mapped")
        p.end()

    # ------------------------------------------------------------------ input
    def mousePressEvent(self, event: QMouseEvent) -> None:
        lamp = self._lamp_at(event.position())
        if lamp is None:
            return
        self.selected = lamp.id
        self.lightSelected.emit(lamp.id)
        region = light_regions(lamp, self.config.room, self.aspect)[0].as_list()
        self._drag = (lamp.id, event.position(), region)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is None:
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._lamp_at(event.position()) else Qt.CursorShape.ArrowCursor)
            return
        lid, start, (u0, v0, u1, v1) = self._drag
        light = self.config.light(lid)
        if light is None:
            return
        active = self._active()
        du = (event.position().x() - start.x()) / max(active.width(), 1)
        dv = (event.position().y() - start.y()) / max(active.height(), 1)
        du = max(-u0, min(1 - u1, du))
        dv = max(-v0, min(1 - v1, dv))
        light.region = [u0 + du, v0 + dv, u1 + du, v1 + dv]
        self.regionEdited.emit(lid)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        lamp = self._lamp_at(event.position())
        if lamp is not None and lamp.region:
            lamp.region = None
            self.regionEdited.emit(lamp.id)
            self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:
        lamp = self._lamp_at(event.position())
        if lamp is None:
            return
        r = light_regions(lamp, self.config.room, self.aspect)[0]
        factor = 1.1 ** (event.angleDelta().y() / 120.0)
        cu, cv = r.center
        w = max(0.02, min(1.0, r.width * factor))
        h = max(0.02, min(1.0, r.height * factor))
        u0 = max(0.0, min(1.0 - w, cu - w / 2))
        v0 = max(0.0, min(1.0 - h, cv - h / 2))
        lamp.region = normalise_region([u0, v0, u0 + w, v0 + h]).as_list()
        self.regionEdited.emit(lamp.id)
        self.update()
        event.accept()
