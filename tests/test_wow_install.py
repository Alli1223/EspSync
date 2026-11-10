"""Installing the WoW addon from the app: finding WoW, copying, versions, the dialog."""

import os
import sys
from pathlib import Path

import pytest

from espsync import wow_install as wi
from espsync.wow_install import (
    UNINSTALL_KEYS,
    AddonInstallError,
    AddonStatus,
    WowFlavour,
    compare_versions,
    find_flavours,
    find_wow_roots,
    flavours_for_folder,
    install_addon,
    toc_version,
)

UNINSTALL = UNINSTALL_KEYS[0]


class FakeRegistry:
    """HKLM as {key path: {value name: data}}; subkeys come from the paths."""

    def __init__(self, keys: dict[str, dict[str, str]]):
        self.keys = {k.lower(): v for k, v in keys.items()}
        self.names = {k.lower(): k for k in keys}

    def subkeys(self, path):
        prefix = path.lower() + "\\"
        return sorted({self.names[k][len(prefix):].split("\\")[0] for k in self.keys if k.startswith(prefix)})

    def value(self, path, name):
        return self.keys.get(path.lower(), {}).get(name)


@pytest.fixture(autouse=True)
def no_real_wow(monkeypatch):
    """Nothing here may find (let alone write to) the developer's own WoW install."""
    monkeypatch.setattr(wi, "WinRegistry", lambda: FakeRegistry({}))
    monkeypatch.setattr(wi, "DEFAULT_ROOTS", ())


def make_toc(folder: Path, version: str | None = "1.0.0", title: str = "EspSync") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["## Interface: 120105", f"## Title: {title}"]
    if version is not None:
        lines.append(f"## Version: {version}")
    toc = folder / f"{folder.name}.toc"
    toc.write_text("\n".join(lines + ["", f"{folder.name}.lua", ""]), encoding="utf-8")
    return toc


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "bundle" / "EspSync"
    make_toc(src, "1.2.0")
    (src / "EspSync.lua").write_text("-- new addon\n", encoding="utf-8")
    return src


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "World of Warcraft"
    for name in ("_retail_", "_forever_", "_classic_era_", "Data", "Cache"):
        (root / name).mkdir(parents=True)
    return root


# ------------------------------------------------------------------ discovery


def test_registry_finds_wow_from_uninstall_entries_and_the_blizzard_key(tmp_path, game):
    other = tmp_path / "Games" / "WoW PTR"
    (other / "_ptr_").mkdir(parents=True)
    reg = FakeRegistry({
        f"{UNINSTALL}\\World of Warcraft": {"DisplayName": "World of Warcraft", "InstallLocation": str(game)},
        f"{UNINSTALL}\\World of Warcraft Classic Era": {
            "DisplayName": "World of Warcraft Classic Era", "InstallLocation": f'"{game}{os.sep}"'},
        f"{UNINSTALL}\\Steam": {"DisplayName": "Steam", "InstallLocation": str(tmp_path / "Steam")},
        f"{UNINSTALL}\\Gone": {"DisplayName": "World of Warcraft Beta", "InstallLocation": str(tmp_path / "gone")},
        f"{UNINSTALL}\\NoLocation": {"DisplayName": "World of Warcraft"},
        # Blizzard's key names a flavour folder (or its exe), not the game folder.
        wi.BLIZZARD_KEY: {"InstallPath": str(other / "_ptr_") + os.sep, "GamePath": str(game / "_retail_" / "Wow.exe")},
    })
    (tmp_path / "Steam").mkdir()
    assert find_wow_roots(reg, defaults=[]) == [game, other]


def test_default_locations_are_a_fallback(tmp_path, game):
    assert find_wow_roots(FakeRegistry({}), defaults=[str(game), str(tmp_path / "nope")]) == [game]


def test_no_registry_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert find_wow_roots() == []


def test_flavours_known_ones_in_order_then_others_with_a_game_exe(game):
    (game / "_xptr_").mkdir()
    (game / "_xptr_" / "WowT.exe").write_bytes(b"")
    (game / "_junk_").mkdir()  # no exe: not a game version
    found = find_flavours([game])
    assert [(f.folder.name, f.label) for f in found] == [
        ("_retail_", "Retail"), ("_forever_", "Forever"), ("_classic_era_", "Classic Era"), ("_xptr_", "Xptr"),
    ]
    assert found[1].addon_dir == game / "_forever_" / "Interface" / "AddOns" / "EspSync"
    assert find_flavours([game / "missing"]) == []


def test_detect_flavours_uses_the_registry(game):
    reg = FakeRegistry({f"{UNINSTALL}\\WoW": {"DisplayName": "World of Warcraft", "InstallLocation": str(game)}})
    assert [f.label for f in wi.detect_flavours(reg)] == ["Retail", "Forever", "Classic Era"]


def test_a_picked_folder_can_be_the_game_a_version_or_its_addons_folder(tmp_path, game):
    assert [f.label for f in flavours_for_folder(game)] == ["Retail", "Forever", "Classic Era"]
    assert flavours_for_folder(game / "_forever_") == [WowFlavour(game / "_forever_", "Forever")]
    addons = game / "_forever_" / "Interface" / "AddOns"
    addons.mkdir(parents=True)
    assert flavours_for_folder(addons) == [WowFlavour(game / "_forever_", "Forever")]
    assert flavours_for_folder(addons.parent) == [WowFlavour(game / "_forever_", "Forever")]
    custom = tmp_path / "MyWoW"
    custom.mkdir()
    (custom / "Wow.exe").write_bytes(b"")
    assert flavours_for_folder(custom) == [WowFlavour(custom, "MyWoW")]
    assert flavours_for_folder(tmp_path / "bundle") == []


# --------------------------------------------------------------- the addon


def test_addon_source_in_a_source_checkout():
    src = wi.addon_source()
    assert src == Path(__file__).resolve().parent.parent / "integrations" / "wow" / "EspSync"
    assert toc_version(src / "EspSync.toc")


def test_addon_source_in_the_frozen_app(tmp_path, monkeypatch):
    internal = tmp_path / "app" / "_internal"
    make_toc(internal / "integrations" / "wow" / "EspSync")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "EspSync.exe"))
    monkeypatch.setattr(wi, "_SOURCE_ROOT", tmp_path / "no-checkout")
    assert wi.addon_source() == internal / "integrations" / "wow" / "EspSync"


def test_addon_source_falls_back_to_the_copy_next_to_the_exe(tmp_path, monkeypatch):
    make_toc(tmp_path / "app" / "WoW addon" / "EspSync")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "app" / "_internal"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "app" / "EspSync.exe"))
    monkeypatch.setattr(wi, "_SOURCE_ROOT", tmp_path / "no-checkout")
    assert wi.addon_source() == tmp_path / "app" / "WoW addon" / "EspSync"
    (tmp_path / "app" / "WoW addon" / "EspSync" / "EspSync.toc").unlink()
    assert wi.addon_source() is None


def test_toc_version(tmp_path):
    toc = tmp_path / "EspSync.toc"
    toc.write_bytes(b"\xef\xbb\xbf## Interface: 120105\r\n## Title: EspSync\r\n##Version :  1.4.2 \r\nEspSync.lua\r\n")
    assert toc_version(toc) == "1.4.2"
    toc.write_text("## Title: EspSync\n## Version:\n", encoding="utf-8")
    assert toc_version(toc) is None
    toc.write_text("## Title: EspSync\n", encoding="utf-8")
    assert toc_version(toc) is None
    assert toc_version(tmp_path / "missing.toc") is None


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("1.0.0", "1.0.0", 0),
        ("1.0", "1.0.0", 0),
        ("1.0.9", "1.0.10", -1),
        ("2", "1.9.9", 1),
        ("v1.2.0-beta", "1.2", 0),
        (None, "1.0.0", None),
        ("dev", "1.0.0", None),
    ],
)
def test_compare_versions(a, b, expected):
    assert compare_versions(a, b) == expected


@pytest.mark.parametrize(
    ("status", "state", "text"),
    [
        (AddonStatus(None, "1.2.0", present=False), "missing", "not installed"),
        (AddonStatus("1.0.0", "1.2.0", present=True), "older", "version 1.0.0 installed, older than 1.2.0"),
        (AddonStatus("1.2.0", "1.2.0", present=True), "current", "version 1.2.0 installed, up to date"),
        (AddonStatus("1.3", "1.2.0", present=True), "newer", "version 1.3 installed, newer than this app's 1.2.0"),
        (AddonStatus(None, "1.2.0", present=True), "unknown", "installed, version unknown"),
        (AddonStatus("1.0.0", None, present=True), "unknown", "version 1.0.0 installed"),
    ],
)
def test_addon_status(status, state, text):
    assert status.state == state
    assert status.describe() == text


# ----------------------------------------------------------------- install


def test_install_creates_addons_and_leaves_other_addons_alone(game, source):
    flavour = WowFlavour(game / "_forever_", "Forever")
    assert wi.addon_status(flavour, source).state == "missing"
    # Another flavour already has addons, including one whose name starts like ours.
    retail = WowFlavour(game / "_retail_", "Retail")
    weak = make_toc(retail.addons_dir / "WeakAuras", "5.0")
    near = make_toc(retail.addons_dir / "EspSyncExtras", "0.1")
    before = {p: p.read_bytes() for p in (weak, near)}

    dest = install_addon(flavour, source)
    assert dest == game / "_forever_" / "Interface" / "AddOns" / "EspSync"
    assert sorted(p.name for p in dest.iterdir()) == ["EspSync.lua", "EspSync.toc"]
    assert wi.addon_status(flavour, source).state == "current"

    install_addon(retail, source)
    assert sorted(p.name for p in retail.addons_dir.iterdir()) == ["EspSync", "EspSyncExtras", "WeakAuras"]
    assert {p: p.read_bytes() for p in (weak, near)} == before


def test_install_replaces_an_older_copy_of_our_addon(game, source):
    flavour = WowFlavour(game / "_retail_", "Retail")
    old = flavour.addon_dir
    make_toc(old, "0.9.0")
    (old / "EspSync.lua").write_text("-- old addon\n", encoding="utf-8")
    (old / "Old.lua").write_text("-- dropped in 1.2\n", encoding="utf-8")
    (old / "media").mkdir()
    (old / "media" / "x.tga").write_bytes(b"x")
    assert wi.addon_status(flavour, source).describe() == "version 0.9.0 installed, older than 1.2.0"

    install_addon(flavour, source)
    assert sorted(p.name for p in old.iterdir()) == ["EspSync.lua", "EspSync.toc"]
    assert (old / "EspSync.lua").read_text(encoding="utf-8") == "-- new addon\n"
    assert wi.addon_status(flavour, source).state == "current"


def test_install_into_an_empty_espsync_folder(game, source):
    flavour = WowFlavour(game / "_retail_", "Retail")
    flavour.addon_dir.mkdir(parents=True)
    install_addon(flavour, source)
    assert (flavour.addon_dir / "EspSync.toc").is_file()


def test_install_refuses_a_folder_that_is_not_our_addon(game, source):
    flavour = WowFlavour(game / "_retail_", "Retail")
    flavour.addon_dir.mkdir(parents=True)
    keep = flavour.addon_dir / "Something.lua"
    keep.write_text("-- someone else's\n", encoding="utf-8")
    with pytest.raises(AddonInstallError, match="doesn't hold the EspSync addon"):
        install_addon(flavour, source)
    assert keep.read_text(encoding="utf-8") == "-- someone else's\n"
    assert not (flavour.addon_dir / "EspSync.toc").exists()


def test_install_refuses_a_linked_addon_folder(tmp_path, game, source):
    checkout = tmp_path / "checkout" / "EspSync"
    make_toc(checkout, "1.1.0-dev")
    flavour = WowFlavour(game / "_retail_", "Retail")
    flavour.addons_dir.mkdir(parents=True)
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(checkout), str(flavour.addon_dir))
        else:
            os.symlink(checkout, flavour.addon_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"can't create a link here: {exc}")
    with pytest.raises(AddonInstallError, match="is a link"):
        install_addon(flavour, source)
    assert toc_version(checkout / "EspSync.toc") == "1.1.0-dev"


def test_install_errors(tmp_path, game, source):
    with pytest.raises(AddonInstallError, match="missing from this copy"):
        install_addon(WowFlavour(game / "_retail_", "Retail"), tmp_path / "nothing")
    with pytest.raises(AddonInstallError, match="doesn't exist"):
        install_addon(WowFlavour(game / "_gone_", "Gone"), source)
    flavour = WowFlavour(game / "_retail_", "Retail")
    flavour.addons_dir.mkdir(parents=True)
    flavour.addon_dir.write_text("not a folder", encoding="utf-8")
    with pytest.raises(AddonInstallError, match="is a file"):
        install_addon(flavour, source)


def test_install_reports_access_denied(game, source, monkeypatch):
    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(wi.shutil, "copyfile", denied)
    with pytest.raises(AddonInstallError, match="Windows refused to write"):
        install_addon(WowFlavour(game / "_retail_", "Retail"), source)


# ------------------------------------------------------------------ dialog


def _dialog(flavours, source):
    from espsync.gui.wow_addon_dialog import WowAddonDialog

    return WowAddonDialog(flavours=flavours, source=source)


def _uncheck(dlg, *rows):
    from PySide6.QtCore import Qt

    for i in rows:
        dlg.list.item(i).setCheckState(Qt.CheckState.Unchecked)


def _row_text(dlg, label):
    from PySide6.QtCore import Qt

    for i in range(dlg.list.count()):
        if dlg.list.item(i).data(Qt.ItemDataRole.UserRole).label == label:
            return dlg.list.item(i).text()
    raise AssertionError(label)


def test_dialog_ticks_outdated_copies_and_installs(app, game, source):
    retail = WowFlavour(game / "_retail_", "Retail")
    forever = WowFlavour(game / "_forever_", "Forever")
    era = WowFlavour(game / "_classic_era_", "Classic Era")
    make_toc(retail.addon_dir, "1.2.0")
    make_toc(forever.addon_dir, "1.0.0")
    dlg = _dialog([retail, forever, era], source)
    assert dlg.flavours() == [retail, forever, era]
    # Only the out-of-date copy is ticked; versions without the addon are offered, not ticked.
    assert dlg.checked() == [forever]
    assert "up to date" in _row_text(dlg, "Retail")
    assert "version 1.0.0 installed, older than 1.2.0" in _row_text(dlg, "Forever")
    assert "not installed" in _row_text(dlg, "Classic Era")
    assert str(era.folder) in _row_text(dlg, "Classic Era")
    assert dlg.empty.isHidden() and dlg.install_btn.isEnabled()

    results = dlg.install()
    assert results == [(forever, forever.addon_dir)]
    assert toc_version(forever.addon_dir / "EspSync.toc") == "1.2.0"
    assert not era.addon_dir.exists()
    assert "up to date" in _row_text(dlg, "Forever")
    assert "Installed the EspSync addon 1.2.0 for Forever." in dlg.result.text()
    assert "/reload" in dlg.result.text()
    dlg.deleteLater()


def test_dialog_ticks_everything_when_the_addon_is_nowhere_yet(app, game, source):
    flavours = find_flavours([game])
    dlg = _dialog(flavours, source)
    assert dlg.checked() == flavours
    _uncheck(dlg, 2)
    dlg.install()
    assert [f.addon_dir.is_dir() for f in flavours] == [True, True, False]
    assert "for Retail, Forever." in dlg.result.text()
    _uncheck(dlg, 0, 1)
    assert not dlg.install_btn.isEnabled()
    dlg.deleteLater()


def test_dialog_reports_failures(app, game, source):
    retail = WowFlavour(game / "_retail_", "Retail")
    retail.addon_dir.mkdir(parents=True)
    (retail.addon_dir / "Other.lua").write_text("--", encoding="utf-8")
    forever = WowFlavour(game / "_forever_", "Forever")
    dlg = _dialog([retail, forever], source)
    results = dlg.install()
    assert results[1] == (forever, forever.addon_dir)
    assert isinstance(results[0][1], str)
    text = dlg.result.text()
    assert "for Forever." in text and "Couldn't install for Retail:" in text
    dlg.deleteLater()


def test_dialog_with_no_wow_found_and_choose_folder(app, tmp_path, game, source):
    dlg = _dialog([], source)
    assert not dlg.empty.isHidden() and not dlg.install_btn.isEnabled()

    dlg._ask_folder = lambda: ""  # cancelled
    dlg._choose_folder()
    assert dlg.list.count() == 0

    dlg._ask_folder = lambda: str(tmp_path / "bundle")
    dlg._choose_folder()
    assert dlg.list.count() == 0 and "doesn't look like a World of Warcraft folder" in dlg.result.text()

    dlg._ask_folder = lambda: str(game / "_forever_")
    dlg._choose_folder()
    assert [f.label for f in dlg.checked()] == ["Forever"] and dlg.result.text() == ""
    assert dlg.empty.isHidden() and dlg.install_btn.isEnabled()

    dlg._ask_folder = lambda: str(game)  # adds the others, no duplicate Forever row
    dlg._choose_folder()
    assert [f.label for f in dlg.flavours()] == ["Forever", "Retail", "Classic Era"]
    dlg.deleteLater()


def test_dialog_without_bundled_addon_files(app, game, monkeypatch):
    monkeypatch.setattr(wi, "addon_source", lambda: None)
    dlg = _dialog(find_flavours([game]), None)
    assert dlg.source is None and not dlg.install_btn.isEnabled()
    assert "missing from this copy" in dlg.result.text()
    dlg.deleteLater()


def test_dialog_detects_wow_by_default(app, game, source, monkeypatch):
    reg = FakeRegistry({f"{UNINSTALL}\\WoW": {"DisplayName": "World of Warcraft", "InstallLocation": str(game)}})
    monkeypatch.setattr(wi, "WinRegistry", lambda: reg)
    monkeypatch.setattr(sys, "platform", "win32")
    dlg = _dialog(None, source)
    assert [f.label for f in dlg.flavours()] == ["Retail", "Forever", "Classic Era"]
    dlg.deleteLater()


def test_settings_button_opens_the_dialog(app, monkeypatch):
    from espsync.config import AppConfig
    from espsync.gui import settings_panel

    opened = []

    class Stub:
        def __init__(self, parent):
            opened.append(parent)

        def exec(self):
            opened.append("exec")

    monkeypatch.setattr(settings_panel, "WowAddonDialog", Stub)
    panel = settings_panel.SettingsPanel(AppConfig())
    assert panel.install_addon.text() == "Install WoW addon…"
    panel.install_addon.click()
    assert opened == [panel.install_addon.window(), "exec"]  # parented to the window the button is in
    panel.deleteLater()
