"""Games tab -> "Install WoW addon…": copy the bundled addon into WoW."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import wow_install as wi

_ROLE = Qt.ItemDataRole.UserRole


class WowAddonDialog(QDialog):
    """Lists the WoW game versions found (with checkboxes) and installs the addon into the ticked ones.

    `flavours` and `source` default to what's detected on this PC and the addon
    bundled with this copy of EspSync; the tests pass their own.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        flavours: list[wi.WowFlavour] | None = None,
        source: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Install WoW addon")
        self.resize(620, 380)
        self.source = source if source is not None else wi.addon_source()
        self.bundled = wi.toc_version(self.source / wi.TOC_NAME) if self.source else None
        col = QVBoxLayout(self)

        version = f" {self.bundled}" if self.bundled else ""
        head = QLabel(
            f"Copies the EspSync addon{version} into the game versions you tick, as "
            "<code>Interface\\AddOns\\EspSync</code>. Your other addons aren't touched."
        )
        head.setWordWrap(True)
        col.addWidget(head)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        col.addWidget(self.list, 1)
        self.empty = QLabel(
            "No World of Warcraft installation found. Use <b>Choose folder…</b> to pick your "
            "World of Warcraft folder (the one that holds <code>_retail_</code>, <code>_forever_</code>, …)."
        )
        self.empty.setWordWrap(True)
        col.addWidget(self.empty)

        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.result)

        row = QHBoxLayout()
        self.choose_btn = QPushButton("Choose folder…")
        self.choose_btn.setToolTip("Pick a World of Warcraft folder, a game version folder like _retail_, "
                                   "or its Interface\\AddOns folder")
        self.close_btn = QPushButton("Close")
        self.install_btn = QPushButton("Install")
        self.install_btn.setDefault(True)
        row.addWidget(self.choose_btn)
        row.addStretch(1)
        row.addWidget(self.close_btn)
        row.addWidget(self.install_btn)
        col.addLayout(row)

        self.choose_btn.clicked.connect(self._choose_folder)
        self.close_btn.clicked.connect(self.reject)
        self.install_btn.clicked.connect(self.install)
        self.list.itemChanged.connect(lambda _item: self._update_buttons())

        found = wi.detect_flavours() if flavours is None else flavours
        statuses = [wi.addon_status(f, self.source) for f in found]
        # Update the copies already installed; with none yet, offer every version found.
        anywhere = any(s.present for s in statuses)
        for flavour, status in zip(found, statuses):
            self._add(flavour, checked=status.state in ("older", "unknown") if anywhere else True)

        if self.source is None:
            self.result.setText("The addon files are missing from this copy of EspSync. Reinstall EspSync, "
                                "or copy the addon by hand (see the setup help).")
        self._update_buttons()

    # ---------------------------------------------------------------- rows
    def flavours(self) -> list[wi.WowFlavour]:
        return [self.list.item(i).data(_ROLE) for i in range(self.list.count())]

    def checked(self) -> list[wi.WowFlavour]:
        return [
            self.list.item(i).data(_ROLE)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _find(self, flavour: wi.WowFlavour) -> QListWidgetItem | None:
        key = os.path.normcase(os.path.normpath(str(flavour.folder)))
        for i in range(self.list.count()):
            item = self.list.item(i)
            if os.path.normcase(os.path.normpath(str(item.data(_ROLE).folder))) == key:
                return item
        return None

    def _add(self, flavour: wi.WowFlavour, checked: bool) -> QListWidgetItem:
        item = self._find(flavour)
        if item is None:
            item = QListWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(_ROLE, flavour)
            self.list.addItem(item)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._describe(item)
        return item

    def _describe(self, item: QListWidgetItem) -> None:
        flavour: wi.WowFlavour = item.data(_ROLE)
        status = wi.addon_status(flavour, self.source)
        item.setText(f"{flavour.label} — {status.describe()}\n{flavour.folder}")
        item.setToolTip(str(flavour.addon_dir))

    def _update_buttons(self) -> None:
        self.empty.setVisible(self.list.count() == 0)
        self.install_btn.setEnabled(self.source is not None and bool(self.checked()))

    # ------------------------------------------------------------- actions
    def _ask_folder(self) -> str:
        start = str(self.flavours()[0].folder.parent) if self.list.count() else ""
        return QFileDialog.getExistingDirectory(self, "Choose your World of Warcraft folder", start)

    def _choose_folder(self) -> None:
        picked = self._ask_folder()
        if not picked:
            return
        found = wi.flavours_for_folder(Path(picked))
        if not found:
            self.result.setText(
                f"{picked} doesn't look like a World of Warcraft folder. Choose the folder that holds "
                "_retail_, _forever_ or _classic_era_, or one of those folders."
            )
            return
        for flavour in found:
            self._add(flavour, checked=True)
        self.result.setText("")
        self._update_buttons()

    def install(self) -> list[tuple[wi.WowFlavour, Path | str]]:
        """Install into the ticked versions; returns (flavour, addon folder or error) per version."""
        results: list[tuple[wi.WowFlavour, Path | str]] = []
        for flavour in self.checked():
            try:
                results.append((flavour, wi.install_addon(flavour, self.source)))
            except wi.AddonInstallError as exc:
                results.append((flavour, str(exc)))
        for i in range(self.list.count()):
            self._describe(self.list.item(i))
        done = [f.label for f, r in results if isinstance(r, Path)]
        lines = []
        if done:
            version = f" {self.bundled}" if self.bundled else ""
            lines.append(f"Installed the EspSync addon{version} for {', '.join(done)}.")
            lines.append("In game, type /reload (or start WoW). If the AddOns list on the character "
                         "screen says EspSync is out of date, tick \"Load out of date AddOns\".")
        lines += [f"Couldn't install for {f.label}: {r}" for f, r in results if isinstance(r, str)]
        self.result.setText("\n".join(lines))
        return results
