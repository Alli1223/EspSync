"""Run short async jobs (discovery, probing, identify) off the GUI thread."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_live: set[AsyncJob] = set()


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class AsyncJob(QRunnable):
    def __init__(self, factory: Callable[[], Awaitable[Any]]) -> None:
        super().__init__()
        self.factory = factory
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self) -> None:
        try:
            result = asyncio.run(self.factory())
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc) or type(exc).__name__)
        else:
            self.signals.done.emit(result)


def run_async(
    factory: Callable[[], Awaitable[Any]],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> AsyncJob:
    job = AsyncJob(factory)
    _live.add(job)

    def finish(*_: object) -> None:
        _live.discard(job)

    if on_done:
        job.signals.done.connect(on_done)
    if on_error:
        job.signals.failed.connect(on_error)
    job.signals.done.connect(finish)
    job.signals.failed.connect(finish)
    QThreadPool.globalInstance().start(job)
    return job
