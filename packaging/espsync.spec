# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for Windows: one folder with EspSync.exe (windowed) and
espsync-cli.exe (console) sharing the same runtime.

    pyinstaller packaging/espsync.spec --noconfirm

Set ESPSYNC_VERSION (e.g. 0.2.0 or 0.2.0.17) to stamp the exe version info.
"""

import os
import re

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 - provided by PyInstaller
ICON = os.path.join(SPECPATH, "espsync.ico")  # noqa: F821

with open(os.path.join(ROOT, "espsync", "__init__.py"), encoding="utf-8") as fh:
    base_version = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)
version = os.environ.get("ESPSYNC_VERSION") or base_version
nums = [int(n) for n in re.findall(r"\d+", version)][:4]
nums += [0] * (4 - len(nums))

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=tuple(nums), prodvers=tuple(nums)),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "EspSync"),
                StringStruct("FileDescription", "EspSync - screen sync for ESPHome lights"),
                StringStruct("FileVersion", version),
                StringStruct("InternalName", "EspSync"),
                StringStruct("OriginalFilename", "EspSync.exe"),
                StringStruct("ProductName", "EspSync"),
                StringStruct("ProductVersion", version),
            ])
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)
version_file = os.path.join(workpath, "version_info.txt")  # noqa: F821
os.makedirs(workpath, exist_ok=True)  # noqa: F821
with open(version_file, "w", encoding="utf-8") as fh:
    fh.write(str(version_info))

# aioesphomeapi and zeroconf import parts of themselves dynamically (and ship
# compiled extension modules), so collect them wholesale.  The Hue Entertainment
# stream's DTLS (espsync.lights.dtls) only needs cryptography's AES-GCM, which
# aioesphomeapi already uses; PyInstaller's cryptography hook bundles it.
hidden = collect_submodules("aioesphomeapi") + collect_submodules("zeroconf") + collect_submodules("espsync")

# Qt modules we never use; keeps the bundle and installer small.
excludes = [
    "tkinter",
    "pytest",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtSql",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
]


def analysis(script):
    return Analysis(  # noqa: F821
        [os.path.join(SPECPATH, script)],  # noqa: F821
        pathex=[ROOT],
        hiddenimports=hidden,
        excludes=excludes,
        noarchive=False,
    )


gui = analysis("espsync_gui.py")
cli = analysis("espsync_cli.py")

# Files Qt's hooks pull in that a QPainter/Widgets app doesn't need: the 20 MB
# software OpenGL fallback, QML/Quick and PDF (dragged in by plugins), and
# translations (the UI is English).
DROP = re.compile(
    r"(opengl32sw\.dll|Qt6(Quick|Qml|QmlModels|QmlMeta|QmlWorkerScript|Pdf|VirtualKeyboard)\w*\.dll"
    r"|[\\/]translations[\\/]|[\\/]qml[\\/]|qpdf\.dll|qtvirtualkeyboardplugin\.dll)",
    re.IGNORECASE,
)


def _trim(toc):
    return [entry for entry in toc if not DROP.search(entry[0])]


for a in (gui, cli):
    a.binaries = _trim(a.binaries)
    a.datas = _trim(a.datas)

gui_exe = EXE(  # noqa: F821
    PYZ(gui.pure),  # noqa: F821
    gui.scripts,
    [],
    exclude_binaries=True,
    name="EspSync",
    console=False,
    icon=ICON,
    version=version_file,
    upx=False,
)
cli_exe = EXE(  # noqa: F821
    PYZ(cli.pure),  # noqa: F821
    cli.scripts,
    [],
    exclude_binaries=True,
    name="espsync-cli",
    console=True,
    icon=ICON,
    version=version_file,
    upx=False,
)
COLLECT(  # noqa: F821
    gui_exe,
    cli_exe,
    gui.binaries,
    gui.datas,
    cli.binaries,
    cli.datas,
    name="EspSync",
    upx=False,
)
