"""Areas: named sets of lights to sync on their own ("Desk", "TV only")."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, AreaConfig
from .theme import set_kind, set_role

INTRO = (
    "An area is a set of lights to sync on its own, like the lights on your desk or the strip behind the "
    "monitor. Pick one in the main window to sync (and set scenes on) only its lights; "
    "<b>Full room</b> is always there and drives every light."
)


class AreasDialog(QDialog):
    changed = Signal()  # an area was added, renamed, removed, or its lights changed

    def __init__(self, config: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._loading = False
        self.setWindowTitle("Areas")
        self.resize(640, 480)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        intro = set_role(QLabel(INTRO), "muted")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        cols = QHBoxLayout()
        cols.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(set_role(QLabel("AREAS"), "heading"))
        self.list = QListWidget()
        self.list.setToolTip("Double-click an area to rename it")
        self.list.currentRowChanged.connect(self._show_area)
        self.list.itemChanged.connect(self._renamed)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.add_btn = QPushButton("+  New area")
        set_kind(self.add_btn, "accent")
        self.add_btn.clicked.connect(self.add_area)
        self.remove_btn = QPushButton("Remove")
        set_kind(self.remove_btn, "danger")
        self.remove_btn.clicked.connect(self.remove_area)
        row.addWidget(self.add_btn, 1)
        row.addWidget(self.remove_btn)
        left.addLayout(row)
        cols.addLayout(left, 2)

        right = QVBoxLayout()
        right.setSpacing(8)
        self.lights_title = set_role(QLabel("LIGHTS IN THIS AREA"), "heading")
        right.addWidget(self.lights_title)
        self.lights = QListWidget()
        self.lights.itemChanged.connect(self._ticked)
        right.addWidget(self.lights, 1)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.all_btn = QPushButton("Tick all")
        self.none_btn = QPushButton("Untick all")
        for btn, state in ((self.all_btn, Qt.CheckState.Checked), (self.none_btn, Qt.CheckState.Unchecked)):
            set_kind(btn, "quiet")
            btn.clicked.connect(lambda _=False, st=state: self._tick_all(st))
            row.addWidget(btn)
        row.addStretch(1)
        right.addLayout(row)
        cols.addLayout(right, 3)
        outer.addLayout(cols, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(set_role(QLabel("Changes apply straight away."), "hint"))
        bottom.addStretch(1)
        close = QPushButton("Done")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        outer.addLayout(bottom)

        self._fill_areas(select=config.active_area)

    # ----------------------------------------------------------------- areas
    def current_area(self) -> AreaConfig | None:
        item = self.list.currentItem()
        return self.config.area(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _fill_areas(self, select: str = "") -> None:
        self._loading = True
        self.list.clear()
        for area in self.config.areas:
            item = QListWidgetItem(area.name)
            item.setData(Qt.ItemDataRole.UserRole, area.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.addItem(item)
            if area.id == select:
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self._loading = False
        self._show_area()

    def add_area(self) -> AreaConfig:
        taken = {a.name for a in self.config.areas}
        name = next(n for n in ("Desk", *(f"Area {i}" for i in range(2, 1000))) if n not in taken)
        area = AreaConfig(name=name)
        self.config.areas.append(area)
        self._fill_areas(select=area.id)
        self.changed.emit()
        self.list.editItem(self.list.currentItem())  # type the name straight away
        return area

    def remove_area(self) -> None:
        area = self.current_area()
        if area is None:
            return
        self.config.areas.remove(area)
        if self.config.active_area == area.id:
            self.config.active_area = ""  # back to the full room
        self._fill_areas()
        self.changed.emit()

    def _renamed(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        area = self.config.area(item.data(Qt.ItemDataRole.UserRole))
        if area is None:
            return
        name = item.text().strip()
        if not name:  # a blank name would be an invisible button
            self._loading = True
            item.setText(area.name)
            self._loading = False
            return
        area.name = name
        self._show_area()
        self.changed.emit()

    # ---------------------------------------------------------------- lights
    def _show_area(self, _row: int = 0) -> None:
        if self._loading:
            return
        area = self.current_area()
        self._loading = True
        self.lights.clear()
        for light in self.config.lights if area else ():
            detail = "strip" if light.kind == "strip" else "lamp"
            item = QListWidgetItem(f"{light.name}  ·  {detail}" + ("" if light.enabled else "  ·  off"))
            item.setData(Qt.ItemDataRole.UserRole, light.id)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if light.id in area.light_ids else Qt.CheckState.Unchecked)
            self.lights.addItem(item)
        if area is None:
            hint = QListWidgetItem("Make an area with “New area”, then tick its lights here.")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.lights.addItem(hint)
        elif not self.config.lights:
            hint = QListWidgetItem("No lights yet: add some in the main window first.")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.lights.addItem(hint)
        self._loading = False
        self.lights_title.setText(f"LIGHTS IN “{area.name.upper()}”" if area else "LIGHTS IN THIS AREA")
        for btn in (self.remove_btn, self.all_btn, self.none_btn):
            btn.setEnabled(area is not None)

    def _ticked(self, _item: QListWidgetItem | None = None) -> None:
        area = self.current_area()
        if self._loading or area is None:
            return
        ticked = set()
        for i in range(self.lights.count()):
            item = self.lights.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ticked.add(item.data(Qt.ItemDataRole.UserRole))
        area.light_ids = [l.id for l in self.config.lights if l.id in ticked]  # in the room's order
        self.changed.emit()

    def _tick_all(self, state: Qt.CheckState) -> None:
        self._loading = True
        for i in range(self.lights.count()):
            item = self.lights.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(state)
        self._loading = False
        self._ticked()
