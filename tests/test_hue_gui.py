import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import espsync.gui.hue_page as hue_page  # noqa: E402
from espsync.config import AppConfig, HueBridgeConfig, LightConfig  # noqa: E402
from espsync.gui.light_panel import LightPanel  # noqa: E402
from espsync.hue import HueArea, HueChannel  # noqa: E402

AREA_ID = "1a8d99cc-967b-44f2-9202-43f976c0fa6b"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _area() -> HueArea:
    return HueArea(id=AREA_ID, name="TV area", kind="screen", channels=[
        HueChannel(0, -0.8, 0.8, 0.0, name="Floor lamp", service="a"),
        HueChannel(1, -0.4, 1.0, 0.4, name="Gradient", service="g", index=0, segments=2),
        HueChannel(2, 0.4, 1.0, 0.4, name="Gradient", service="g", index=1, segments=2),
    ])


def _bridge(**kw) -> HueBridgeConfig:
    return HueBridgeConfig(name="Hue Bridge", host="127.0.0.1", app_key="user", client_key="00" * 16, **kw)


def _checked(panel: LightPanel) -> list[int]:
    lw = panel.hue_channels
    return [lw.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lw.count())
            if lw.item(i).checkState() == Qt.CheckState.Checked]


def test_light_panel_hue_channel_picker(app):
    bridge = _bridge(entertainment_id=AREA_ID, entertainment_name="TV area")
    light = LightConfig(name="New lamp", transport="hue", hue_bridge=bridge.id)
    cfg = AppConfig(hue_bridges=[bridge], lights=[light])
    panel = LightPanel(cfg)
    panel.set_light(light)
    assert panel.form.isRowVisible(panel.hue_row) and panel.form.isRowVisible(panel.hue_bridge)
    assert not panel.form.isRowVisible(panel.device) and not panel.form.isRowVisible(panel.udp_host)
    assert panel.hue_bridge.currentData() == bridge.id

    panel.set_hue_area(bridge.id, _area())
    assert panel.hue_channels.count() == 3 and _checked(panel) == []
    changes = []
    panel.changed.connect(changes.append)
    panel.hue_channels.item(0).setCheckState(Qt.CheckState.Checked)
    assert light.hue_channels == [0] and light.name == "Floor lamp"  # named after the channel
    assert changes == ["output"]

    light.kind = "strip"
    panel.hue_channels.item(1).setCheckState(Qt.CheckState.Checked)
    panel.hue_channels.item(2).setCheckState(Qt.CheckState.Checked)
    assert light.hue_channels == [0, 1, 2] and light.led_count == 3


def test_light_panel_switching_to_hue_picks_the_only_bridge(app):
    bridge = _bridge()
    light = LightConfig(name="x", transport="api")
    cfg = AppConfig(hue_bridges=[bridge], lights=[light])
    panel = LightPanel(cfg)
    panel.set_light(light)
    requested = []
    panel.refreshHueRequested.connect(requested.append)
    panel.transport.setCurrentIndex(panel.transport.findData("hue"))
    assert light.transport == "hue" and light.hue_bridge == bridge.id and requested == [bridge.id]


def test_hue_page_choose_area_and_import(app, monkeypatch):
    class FakeApi:
        def __init__(self, host, key):
            pass

        async def areas(self):
            return [_area()]

    monkeypatch.setattr(hue_page, "HueApi", FakeApi)
    bridge = _bridge()
    cfg = AppConfig(hue_bridges=[bridge])
    page = hue_page.HuePage(cfg)
    changed = []
    page.bridgesChanged.connect(lambda: changed.append(1))
    for _ in range(300):
        if bridge.id in page.areas:
            break
        QTest.qWait(10)
    # The only area is chosen automatically.
    assert bridge.entertainment_id == AREA_ID and bridge.entertainment_name == "TV area" and changed
    assert page.channels.count() == 3 and "Floor lamp" in page.channels.item(0).text()
    assert page.import_btn.isEnabled()

    imported = []
    page.lightsImported.connect(imported.append)
    page.group.setChecked(True)
    page._import()
    (lights,) = imported
    assert [l.kind for l in lights] == ["lamp", "strip"]
    assert lights[1].hue_channels == [1, 2] and all(l.hue_bridge == bridge.id for l in lights)


def test_devices_dialog_has_hue_tab(app, monkeypatch):
    from espsync.gui import devices_dialog

    async def no_devices(timeout):
        return []

    monkeypatch.setattr(devices_dialog, "discover", no_devices)
    dlg = devices_dialog.DevicesDialog(AppConfig(), None, "hue")
    assert dlg.tabs.currentWidget() is dlg.hue
    assert [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())] == ["ESPHome", "Philips Hue"]


def test_describe_position():
    assert hue_page.describe_position(-0.9, -0.9, 0.1) == "left, by the screen, low"
    assert hue_page.describe_position(0.0, 0.9, 0.9) == "centre, behind you, high"
