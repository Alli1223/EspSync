import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from espsync.config import AppConfig, LightConfig  # noqa: E402
from espsync.gui.light_panel import LightPanel  # noqa: E402
from espsync.gui.room_view import LampItem, RoomView, StripItem  # noqa: E402
from espsync.gui.screen_view import ScreenView  # noqa: E402
from espsync.mapping import light_regions  # noqa: E402


def test_room_drag_updates_config_and_clamps(app):
    cfg = AppConfig(lights=[LightConfig(name="a", x=0, y=0)])
    view = RoomView(cfg)
    item = view.items_by_id[cfg.lights[0].id]
    assert isinstance(item, LampItem)
    moved = []
    view.lightChanged.connect(moved.append)
    item.setPos(view.geo.to_scene(-0.5, 0.25))
    assert cfg.lights[0].x == pytest.approx(-0.5)
    assert cfg.lights[0].y == pytest.approx(0.25)
    assert moved and moved[-1] == cfg.lights[0].id
    item.setPos(QPointF(10_000, -10_000))
    assert (cfg.lights[0].x, cfg.lights[0].y) == (1.0, -1.0)


def test_room_strip_handles_move_endpoints(app):
    cfg = AppConfig(lights=[LightConfig(name="s", kind="strip", x=-0.2, y=0, x2=0.2, y2=0)])
    view = RoomView(cfg)
    strip = view.items_by_id[cfg.lights[0].id]
    assert isinstance(strip, StripItem) and len(strip.handles) == 2
    strip.handles[1].setPos(view.geo.to_scene(0.8, 0.5))
    assert (cfg.lights[0].x2, cfg.lights[0].y2) == (pytest.approx(0.8), pytest.approx(0.5))


def test_screen_drag_sets_manual_region_and_double_click_resets(app):
    light = LightConfig(name="a", x=-1.0, y=-1.0, z=0.5)
    cfg = AppConfig(lights=[light])
    sv = ScreenView(cfg)
    sv.resize(640, 400)
    before = light_regions(light, cfg.room, sv.aspect)[0]
    centre = sv._to_rect(before, sv._active()).center().toPoint()
    QTest.mousePress(sv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
    QTest.mouseMove(sv, centre + QPoint(60, 0))
    QTest.mouseRelease(sv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre + QPoint(60, 0))
    assert light.region is not None
    assert light.region[0] > before.u0
    QTest.mouseDClick(sv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre + QPoint(60, 0))
    assert light.region is None


def test_light_panel_switching_to_strip_picks_udp(app):
    light = LightConfig(name="a")
    cfg = AppConfig(lights=[light])
    panel = LightPanel(cfg)
    panel.set_light(light)
    changes = []
    panel.changed.connect(changes.append)
    panel.kind.setCurrentIndex(panel.kind.findData("strip"))
    assert light.kind == "strip" and light.transport == "wled"
    assert "output" in changes
    panel.height.setValue(20)
    assert light.z == pytest.approx(0.2)


def test_cli_edits_config(tmp_path):
    from espsync.cli import main
    from espsync.config import load_config

    path = tmp_path / "c.json"
    assert main(["--config", str(path), "add-device", "--host", "lamp.local", "--name", "Lamp", "--key", "k"]) == 0
    assert main(["--config", str(path), "add-light", "--name", "Left", "--device", "Lamp",
                 "--entity", "rgb", "--x", "-0.9"]) == 0
    assert main(["--config", str(path), "add-light", "--name", "TV", "--kind", "strip", "--transport", "wled",
                 "--udp-host", "10.0.0.9", "--preset", "Bottom edge (left to right)"]) == 0
    cfg = load_config(path)
    assert cfg.devices[0].encryption_key == "k"
    assert cfg.lights[0].device_id == cfg.devices[0].id and cfg.lights[0].x == -0.9
    assert cfg.lights[1].strip_layout == "screen" and cfg.lights[1].screen_path == [[0.0, 1.0], [1.0, 1.0]]
