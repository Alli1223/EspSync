"""Install the EspSync World of Warcraft addon from the app.

The addon ships with EspSync: in a source checkout it is
``integrations/wow/EspSync``; the PyInstaller build bundles the same folder
(``_internal/integrations/wow/EspSync``) and the installer also puts a copy in
``<install folder>/WoW addon/EspSync`` for people who copy it by hand.

WoW installs are found from the Windows uninstall entries Battle.net writes,
plus Blizzard's own registry key.  One WoW folder holds a folder per game
flavour (``_retail_``, ``_forever_``, ...), each with its own
``Interface/AddOns``.  Installing only ever writes ``Interface/AddOns/EspSync``
and leaves every other addon alone.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

ADDON_NAME = "EspSync"
TOC_NAME = f"{ADDON_NAME}.toc"
ADDON_SUBDIR = Path("integrations", "wow", ADDON_NAME)  # under the source / bundle root
INSTALLED_COPY = Path("WoW addon", ADDON_NAME)  # next to EspSync.exe (installer.iss)

# Known flavour folders, in the order the dialog lists them.
FLAVOURS: tuple[tuple[str, str], ...] = (
    ("_retail_", "Retail"),
    ("_forever_", "Forever"),
    ("_classic_", "Classic"),
    ("_classic_era_", "Classic Era"),
    ("_anniversary_", "Anniversary"),
    ("_ptr_", "PTR"),
    ("_beta_", "Beta"),
)
_FLAVOUR_LABELS = dict(FLAVOURS)
_FLAVOUR_DIR = re.compile(r"^_[a-z0-9_]+_$", re.IGNORECASE)

UNINSTALL_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
)
BLIZZARD_KEY = r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"
DEFAULT_ROOTS = (
    r"C:\Program Files (x86)\World of Warcraft",
    r"C:\Program Files\World of Warcraft",
)


class AddonInstallError(Exception):
    """Installing into one flavour failed; the message is shown to the user."""


# --------------------------------------------------------------- the addon


_SOURCE_ROOT = Path(__file__).resolve().parent.parent  # the repo, in a source checkout


def _addon_candidates() -> list[Path]:
    found = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            found.append(Path(meipass) / ADDON_SUBDIR)  # PyInstaller's _internal
        exe_dir = Path(sys.executable).resolve().parent
        found += [exe_dir / "_internal" / ADDON_SUBDIR, exe_dir / INSTALLED_COPY]
    found.append(_SOURCE_ROOT / ADDON_SUBDIR)
    return found


def addon_source() -> Path | None:
    """The folder holding the bundled EspSync.toc, or None if it's missing."""
    for folder in _addon_candidates():
        if (folder / TOC_NAME).is_file():
            return folder
    return None


_VERSION_LINE = re.compile(r"^\s*##\s*Version\s*:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)


def toc_version(toc: Path) -> str | None:
    """The ``## Version:`` of a .toc file, or None if unreadable / not set."""
    try:
        text = toc.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    m = _VERSION_LINE.search(text)
    return (m.group(1) or None) if m else None


def _version_key(version: str | None) -> tuple[int, ...] | None:
    nums = re.findall(r"\d+", version or "")
    if not nums:
        return None
    key = [int(n) for n in nums]
    while len(key) > 1 and key[-1] == 0:
        key.pop()  # 1.0 == 1.0.0
    return tuple(key)


def compare_versions(a: str | None, b: str | None) -> int | None:
    """-1, 0 or 1 like ``a <=> b``; None when either has no version number."""
    ka, kb = _version_key(a), _version_key(b)
    if ka is None or kb is None:
        return None
    return (ka > kb) - (ka < kb)


# ------------------------------------------------------------ WoW folders


@dataclass(frozen=True)
class WowFlavour:
    folder: Path  # e.g. ...\World of Warcraft\_forever_
    label: str  # "Forever"

    @property
    def addons_dir(self) -> Path:
        return self.folder / "Interface" / "AddOns"

    @property
    def addon_dir(self) -> Path:
        return self.addons_dir / ADDON_NAME


def flavour_label(folder_name: str) -> str:
    key = folder_name.lower()
    if key in _FLAVOUR_LABELS:
        return _FLAVOUR_LABELS[key]
    return key.strip("_").replace("_", " ").title() or folder_name


class RegistryReader(Protocol):
    """HKEY_LOCAL_MACHINE, read-only.  Missing keys/values give [] / None."""

    def subkeys(self, path: str) -> list[str]: ...

    def value(self, path: str, name: str) -> str | None: ...


class WinRegistry:
    def __init__(self) -> None:
        import winreg

        self._winreg = winreg

    def _open(self, path: str):  # noqa: ANN202
        reg = self._winreg
        return reg.OpenKey(reg.HKEY_LOCAL_MACHINE, path, 0, reg.KEY_READ | reg.KEY_WOW64_64KEY)

    def subkeys(self, path: str) -> list[str]:
        try:
            with self._open(path) as key:
                count = self._winreg.QueryInfoKey(key)[0]
                return [self._winreg.EnumKey(key, i) for i in range(count)]
        except OSError:
            return []

    def value(self, path: str, name: str) -> str | None:
        try:
            with self._open(path) as key:
                data, _kind = self._winreg.QueryValueEx(key, name)
        except OSError:
            return None
        return data if isinstance(data, str) else None


def _registry_paths(reg: RegistryReader) -> list[str]:
    paths = []
    for base in UNINSTALL_KEYS:
        for sub in reg.subkeys(base):
            entry = f"{base}\\{sub}"
            if "world of warcraft" in (reg.value(entry, "DisplayName") or "").lower():
                paths.append(reg.value(entry, "InstallLocation") or "")
    paths.append(reg.value(BLIZZARD_KEY, "InstallPath") or "")
    paths.append(reg.value(BLIZZARD_KEY, "GamePath") or "")
    return paths


def _game_root(raw: str) -> Path | None:
    """The 'World of Warcraft' folder for a registry path (which may name a flavour or an exe)."""
    raw = raw.strip().strip('"').strip()
    if not raw:
        return None
    path = Path(raw)
    if path.suffix.lower() == ".exe":
        path = path.parent
    if _FLAVOUR_DIR.match(path.name):
        path = path.parent
    return path


def find_wow_roots(reg: RegistryReader | None = None, defaults: Iterable[str] | None = None) -> list[Path]:
    """Existing WoW install folders, from the registry then the default locations."""
    if reg is None:
        if sys.platform != "win32":
            return []
        reg = WinRegistry()
    raw = _registry_paths(reg) + list(DEFAULT_ROOTS if defaults is None else defaults)
    roots: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        root = _game_root(item)
        if root is None:
            continue
        key = os.path.normcase(os.path.normpath(str(root)))
        if key in seen:
            continue
        seen.add(key)
        if root.is_dir():
            roots.append(root)
    return roots


def _looks_like_flavour(folder: Path) -> bool:
    try:
        return any(folder.glob("Wow*.exe")) or (folder / "Interface").is_dir()
    except OSError:
        return False


def find_flavours(roots: Iterable[Path]) -> list[WowFlavour]:
    """The flavour folders under each WoW root: known ones first, then any other
    ``_name_`` folder that holds a game exe."""
    found: list[WowFlavour] = []
    for root in roots:
        try:
            children = {p.name.lower(): p for p in root.iterdir() if p.is_dir()}
        except OSError:
            continue
        for name, label in FLAVOURS:
            if name in children:
                found.append(WowFlavour(children[name], label))
        for name in sorted(children):
            if name not in _FLAVOUR_LABELS and _FLAVOUR_DIR.match(name):
                folder = children[name]
                if any(folder.glob("Wow*.exe")):
                    found.append(WowFlavour(folder, flavour_label(name)))
    return found


def detect_flavours(reg: RegistryReader | None = None) -> list[WowFlavour]:
    return find_flavours(find_wow_roots(reg))


def flavours_for_folder(path: Path) -> list[WowFlavour]:
    """What a folder the user picked means: a WoW root (all its flavours), a
    flavour folder, or that flavour's Interface / AddOns folder.  [] if it
    doesn't look like WoW."""
    path = Path(path)
    if path.name.lower() == "addons" and path.parent.name.lower() == "interface":
        path = path.parent.parent
    elif path.name.lower() == "interface":
        path = path.parent
    if _FLAVOUR_DIR.match(path.name):
        return [WowFlavour(path, flavour_label(path.name))]
    inside = find_flavours([path])
    if inside:
        return inside
    if _looks_like_flavour(path):
        return [WowFlavour(path, path.name)]
    return []


# ------------------------------------------------------------- install


@dataclass(frozen=True)
class AddonStatus:
    installed: str | None  # installed version; None = not installed
    bundled: str | None
    present: bool  # our addon folder exists (with our .toc)

    @property
    def state(self) -> str:
        """'missing' | 'older' | 'current' | 'newer' | 'unknown'."""
        if not self.present:
            return "missing"
        cmp = compare_versions(self.installed, self.bundled)
        if cmp is None:
            return "unknown"
        return {-1: "older", 0: "current", 1: "newer"}[cmp]

    def describe(self) -> str:
        state = self.state
        if state == "missing":
            return "not installed"
        version = self.installed
        return {
            "older": f"version {version} installed, older than {self.bundled}",
            "current": f"version {version} installed, up to date",
            "newer": f"version {version} installed, newer than this app's {self.bundled}",
            "unknown": f"version {version} installed" if version else "installed, version unknown",
        }[state]


def addon_status(flavour: WowFlavour, source: Path | None = None) -> AddonStatus:
    src = source or addon_source()
    bundled = toc_version(src / TOC_NAME) if src else None
    toc = flavour.addon_dir / TOC_NAME
    present = toc.is_file()
    return AddonStatus(installed=toc_version(toc) if present else None, bundled=bundled, present=present)


def _is_link(path: Path) -> bool:
    """A symlink or (Windows) junction, e.g. a developer linking AddOns to a checkout."""
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    # Junctions aren't symlinks to pathlib (and os.path.isjunction is 3.12+).
    real = os.path.normcase(os.path.realpath(path))
    expected = os.path.normcase(os.path.join(os.path.realpath(path.parent), path.name))
    return real != expected


def _source_files(source: Path) -> list[Path]:
    return sorted(p for p in source.iterdir() if p.is_file() and not p.name.startswith("."))


def install_addon(flavour: WowFlavour, source: Path | None = None) -> Path:
    """Copy the bundled addon to ``<flavour>/Interface/AddOns/EspSync``; returns that folder.

    Creates Interface/AddOns if needed and replaces an existing copy of our
    addon (files the new version doesn't have are removed).  Never touches
    anything else in AddOns, and refuses if an ``EspSync`` folder there isn't ours.
    """
    src = source or addon_source()
    if src is None or not (src / TOC_NAME).is_file():
        raise AddonInstallError("The addon files are missing from this copy of EspSync. Reinstall EspSync.")
    if not flavour.folder.is_dir():
        raise AddonInstallError(f"{flavour.folder} doesn't exist.")
    dest = flavour.addon_dir
    if _is_link(dest):
        raise AddonInstallError(
            f"{dest} is a link to {os.path.realpath(dest)}, so it was left alone. Update that copy yourself."
        )
    if dest.exists() and not dest.is_dir():
        raise AddonInstallError(f"{dest} is a file, not a folder. Remove it and try again.")
    if dest.is_dir() and not (dest / TOC_NAME).is_file() and any(dest.iterdir()):
        raise AddonInstallError(
            f"{dest} exists but doesn't hold the EspSync addon, so it was left alone. "
            "Move it away and try again."
        )
    files = _source_files(src)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copyfile(f, dest / f.name)
        wanted = {f.name.lower() for f in files}
        for old in dest.iterdir():
            if old.name.lower() in wanted:
                continue
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)
            else:
                old.unlink()
    except PermissionError as exc:
        raise AddonInstallError(
            f"Windows refused to write to {dest} ({exc.strerror or exc}). "
            "Quit WoW and try again, or copy the addon by hand."
        ) from exc
    except OSError as exc:
        raise AddonInstallError(f"Couldn't copy the addon to {dest}: {exc.strerror or exc}") from exc
    return dest
