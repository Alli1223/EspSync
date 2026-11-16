"""Areas: syncing (and setting scenes on) only some of the lights."""

import os

import pytest

from espsync.config import AppConfig, AreaConfig, LightConfig, load_config, save_config
from espsync.scenes import scene_targets


def _config() -> tuple[AppConfig, list[LightConfig]]:
    lights = [
        LightConfig(name="Left", device_id="d", entity="left"),
        LightConfig(name="Desk lamp", device_id="d", entity="desk"),
        LightConfig(name="Monitor", transport="hue", hue_bridge="b", hue_channels=[0, 1]),
        LightConfig(name="Shelf", device_id="d", entity="shelf", enabled=False),
    ]
    cfg = AppConfig(lights=list(lights))
    cfg.areas.append(AreaConfig(name="Desk", light_ids=[lights[1].id, lights[2].id, lights[3].id]))
    return cfg, lights


def test_full_room_drives_every_enabled_light():
    cfg, lights = _config()
    assert cfg.area() is None
    assert cfg.active_lights() == lights[:3]


def test_an_area_drives_only_its_enabled_lights():
    cfg, lights = _config()
    cfg.active_area = cfg.areas[0].id
    assert cfg.area() is cfg.areas[0]
    assert cfg.active_lights() == [lights[1], lights[2]]  # Shelf is in the area but switched off
    assert not cfg.is_active(lights[0])


def test_an_area_that_no_longer_exists_means_the_full_room():
    cfg, lights = _config()
    cfg.active_area = "gone"
    assert cfg.area() is None and cfg.active_lights() == lights[:3]


def test_prune_forgets_removed_lights():
    cfg, lights = _config()
    cfg.lights.remove(lights[2])
    cfg.prune_areas()
    assert cfg.areas[0].light_ids == [lights[1].id, lights[3].id]


def test_areas_survive_save_and_load(tmp_path):
    cfg, lights = _config()
    cfg.active_area = cfg.areas[0].id
    path = save_config(cfg, tmp_path / "c.json")
    loaded = load_config(path)
    assert [(a.id, a.name, a.light_ids) for a in loaded.areas] == [(a.id, a.name, a.light_ids) for a in cfg.areas]
    assert loaded.active_area == cfg.active_area
    assert [l.name for l in loaded.active_lights()] == ["Desk lamp", "Monitor"]


def test_scenes_reach_only_the_area():
    cfg, _ = _config()
    assert scene_targets(cfg) == ({"d": {"left", "desk"}}, {"b": {0, 1}}, 0)
    cfg.active_area = cfg.areas[0].id
    assert scene_targets(cfg) == ({"d": {"desk"}}, {"b": {0, 1}}, 0)


def test_engine_plans_only_the_area():
    from espsync.engine import SyncEngine

    cfg, lights = _config()
    engine = SyncEngine(cfg)
    try:
        assert [light for light, _ in engine._build_plan(16 / 9)[1]] == lights[:3]
        cfg.active_area = cfg.areas[0].id
        assert [light for light, _ in engine._build_plan(16 / 9)[1]] == [lights[1], lights[2]]
    finally:
        engine.shutdown()


# ------------------------------------------------------------------------- GUI
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QIcon

    import espsync.gui.main_window as mw
    from test_main_window import StubEngine

    monkeypatch.setenv("ESPSYNC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(mw, "SyncEngine", StubEngine)
    cfg, _ = _config()
    cfg.devices.clear()
    win = mw.MainWindow(cfg, QIcon())
    yield win
    for t in (win._ui_timer, win._preview_timer, win._stats_timer, win._save_timer, win._restart_timer):
        t.stop()
    win.deleteLater()


def test_area_selector_lists_the_full_room_and_each_area(window):
    desk = window.config.areas[0]
    assert [b.text() for b in window.area_bar.buttons.values()] == ["Full room", "Desk"]
    assert window.area_bar.current() == "" and window.light_count.text() == "3 of 4"
    window.area_bar.buttons[desk.id].click()
    assert window.config.active_area == desk.id and window.area_bar.current() == desk.id
    assert window.light_count.text() == "2 of 4"
    assert window._save_timer.isActive()


def test_switching_area_mid_sync_stops_properly_so_dropped_lights_are_put_back(window):
    desk = window.config.areas[0]
    window.sync_btn.setChecked(True)
    window.engine.stops = []
    window.set_area(desk.id)
    assert window._restart_timer.isActive()
    window._restart_timer.stop()
    window._restart_engine()
    assert window.engine.stops == [None] and window.engine.sending  # a full stop (restores), then syncing again


def test_a_light_added_in_an_area_joins_it_and_a_removed_one_leaves(window):
    desk = window.config.areas[0]
    window.set_area(desk.id)
    light = window.add_light("lamp")
    assert light.id in desk.light_ids and window.config.is_active(light)
    window.remove_light()
    assert light.id not in desk.light_ids


def test_light_panel_switch_moves_the_light_in_and_out_of_the_area(window):
    desk = window.config.areas[0]
    left = window.config.lights[0]
    window.select_light(left.id)
    assert window.light_panel.form.isRowVisible(window.light_panel.in_area) is False  # full room: nothing to show
    window.set_area(desk.id)
    assert window.light_panel.form.isRowVisible(window.light_panel.in_area)
    assert not window.light_panel.in_area.isChecked()
    window.light_panel.in_area.setChecked(True)
    assert left.id in desk.light_ids and window.light_count.text() == "3 of 4"
    window.light_panel.in_area.setChecked(False)
    assert left.id not in desk.light_ids


def test_areas_dialog_adds_fills_renames_and_removes(window):
    from PySide6.QtCore import Qt

    from espsync.gui.areas_dialog import AreasDialog

    dlg = AreasDialog(window.config, window)
    dlg.changed.connect(window._on_areas_edited)
    area = dlg.add_area()
    assert area in window.config.areas and area.light_ids == []
    assert area.id in window.area_bar.buttons
    dlg.lights.item(0).setCheckState(Qt.CheckState.Checked)
    assert area.light_ids == [window.config.lights[0].id]
    dlg.list.currentItem().setText("TV only")
    assert area.name == "TV only" and window.area_bar.buttons[area.id].text() == "TV only"
    window.set_area(area.id)
    dlg.remove_area()
    assert area not in window.config.areas
    assert window.config.active_area == "" and window.area_bar.current() == ""
    dlg.deleteLater()
