"""Is a given program running?  Polled on a background thread.

Listing processes takes a few milliseconds, so it never runs on the capture
thread; the engine just reads :attr:`ProcessWatcher.running`.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)


def _windows_process_names() -> set[str]:
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    TH32CS_SNAPPROCESS = 0x2
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    names: set[str] = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return names


def _proc_process_names() -> set[str]:
    """Linux: /proc/<pid>/comm (Wine shows Windows programs by their .exe name)."""
    names: set[str] = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return names
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as fh:
                names.add(fh.read().strip().lower())
        except OSError:
            continue
    return names


def process_names() -> set[str]:
    """Lower-case executable names of the running processes (empty if unknown)."""
    if sys.platform == "win32":
        return _windows_process_names()
    if os.path.isdir("/proc"):
        return _proc_process_names()
    return set()


class ProcessWatcher:
    """Sets :attr:`running` while any of `names` (case-insensitive) is running."""

    def __init__(
        self,
        names: Iterable[str],
        interval: float = 2.0,
        lister: Callable[[], set[str]] = process_names,
    ) -> None:
        self.names = frozenset(n.lower() for n in names)
        self.interval = interval
        self.running = False
        self._lister = lister
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> bool:
        try:
            self.running = bool(self.names & self._lister())
        except Exception:  # noqa: BLE001 - never take the engine down over this
            log.exception("listing processes failed")
            self.running = False
        return self.running

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="espsync-procwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.running = False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check()
            self._stop.wait(self.interval)
