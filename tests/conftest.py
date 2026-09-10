"""Session teardown for a Qt test suite.

Qt aborts the whole process when a QThread is destroyed while still running. That happens
during interpreter shutdown, AFTER pytest has printed its results - so a completely green run
exits non-zero and CI reports a failure that nothing in the output explains. It cost one
release: "43 passed" followed by exit code 1 on windows-latest.

The rule here is report, then exit truthfully. Anything still running is named with its stack
so it cannot hide, and the process then leaves with pytest's own status rather than a signal.
The application's real exit path is NOT covered by this: it is tested separately, in a
subprocess, by test_the_real_main_starts_and_exits_cleanly.
"""
from __future__ import annotations

import os
import sys


def _qt_threads_still_running():
    try:
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication
    except Exception:
        return []
    app = QApplication.instance()
    if app is None:
        return []
    alive = []
    for obj in app.findChildren(QThread):
        try:
            if obj.isRunning():
                alive.append(obj)
        except RuntimeError:
            pass          # already deleted on the C++ side
    return alive


_EXIT_STATUS = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    # Remember it here; act in pytest_unconfigure, which runs AFTER the summary is printed.
    # Exiting from this hook swallowed the "43 passed" line, which is the one thing anybody
    # actually reads.
    _EXIT_STATUS["code"] = int(exitstatus)


def pytest_unconfigure(config):
    exitstatus = _EXIT_STATUS["code"]
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    app = QApplication.instance()
    if app is None:
        return

    # Give every window its normal close path first: that is what stops the price feed.
    for w in list(app.topLevelWidgets()):
        try:
            w.close()
        except Exception:
            pass
    app.processEvents()

    try:
        from trader.gui.app import _join_threads
        _join_threads(_qt_threads_still_running(), ms=3000)
    except Exception:
        pass

    alive = _qt_threads_still_running()
    if alive:
        print("\n[conftest] Qt threads still running at shutdown "
              f"({len(alive)}): {[t.objectName() or type(t).__name__ for t in alive]}",
              file=sys.stderr)
        import faulthandler
        faulthandler.dump_traceback(file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    # Leave with the status pytest computed. Falling off the end here hands the process to
    # Qt's destructors, and a running thread among them turns "43 passed" into exit code 1.
    os._exit(int(exitstatus))
