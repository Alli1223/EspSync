import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from espsync.config import AppConfig  # noqa: E402
from espsync.gui.settings_panel import SettingsPanel  # noqa: E402
from espsync.gui.updates import UpdateDialog  # noqa: E402
from espsync.updater import UpdateInfo  # noqa: E402

INFO = UpdateInfo("9.0.0", "v9.0.0", "**New** stuff", "https://example.invalid", "EspSync-Setup-9.0.0.exe",
                  "https://github.com/x", 10, "ab" * 32)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_countdown_starts_install(app):
    dlg = UpdateDialog(INFO, installable=True, countdown=1)
    started = []
    dlg.accept_install = lambda: started.append(True)
    dlg.show()
    QTest.qWait(1300)
    assert started == [True]
    assert dlg.choice == "install" and not dlg.go_btn.isEnabled()
    dlg.set_progress(5, 10)
    assert dlg.progress.value() == 5
    dlg.show_error("boom")
    assert dlg.go_btn.isEnabled() and "boom" in dlg.status.text()


def test_skip_and_source_build_buttons(app):
    dlg = UpdateDialog(INFO, installable=False)
    assert dlg.go_btn.text() == "Open release page"
    dlg.skip_btn.click()
    assert dlg.choice == "skip"


def test_settings_update_toggles(app):
    cfg = AppConfig()
    panel = SettingsPanel(cfg)
    kinds = []
    panel.changed.connect(kinds.append)
    panel.update_check.setChecked(False)
    assert cfg.updates.check_automatically is False and "updates" in kinds
    asked = []
    panel.checkUpdatesRequested.connect(lambda: asked.append(1))
    panel.findChild(type(panel.update_check).__mro__[0])  # noqa: B018 - widgets exist
    from PySide6.QtWidgets import QPushButton

    next(b for b in panel.findChildren(QPushButton) if b.text() == "Check now").click()
    assert asked == [1]
