"""Shared Qt setup for the GUI tests.

One QApplication for the whole session, torn down explicitly at the end:
leaving Qt widgets for the interpreter to destroy at exit can segfault on
Linux (seen intermittently in CI after all tests had passed).
"""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    qapp = QApplication.instance()
    if qapp is None:
        return
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QApplication.processEvents()
    gc.collect()
    qapp.shutdown()
