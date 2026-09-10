"""Session teardown for a Qt test suite.

Qt aborts the whole process when a QThread is destroyed while still running. That happens
during interpreter shutdown, AFTER pytest has printed its results, so a completely green run
exits non-zero and CI reports a failure that nothing in the output explains. It cost one
release: "43 passed" followed by exit code 1 on windows-latest.

The rule here is REPORT, then exit truthfully - and the reporting has to be real. The first
version of this file looked for surviving threads with QApplication.findChildren(QThread),
which returns an empty list for this app's threads: both thread classes are created with no
parent, so Qt cannot walk to them. That version therefore did nothing at all except swallow
the exit code, which is the exact failure it was written to prevent. It now asks the
application module for its own registry of live threads.

The application's real exit path is NOT covered by this shortcut: it is tested separately, in
a subprocess, by test_the_real_main_starts_and_exits_cleanly.
"""
from __future__ import annotations

import os
import sys

# The window checks for an update four seconds after it opens, in its own thread, over the
# network. The one test that must exercise that path clears this again for its subprocess.
os.environ.setdefault("TGTRADER_NO_AUTOUPDATE", "1")

_EXIT_STATUS = {"code": 0}


def _live_threads():
    """Ask the app module, not Qt. See the note above about findChildren."""
    try:
        from trader.gui.app import live_threads
    except Exception:
        return []
    try:
        return live_threads()
    except Exception:
        return []


def pytest_sessionfinish(session, exitstatus):
    # Remembered here, acted on in pytest_unconfigure, which runs AFTER the summary is
    # printed. Exiting from this hook swallowed the "43 passed" line.
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

    alive = _live_threads()
    if alive:
        try:
            from trader.gui.app import _join_threads
            alive = _join_threads(alive, ms=5000)
        except Exception:
            pass
    if alive:
        print(f"\n[conftest] {len(alive)} Qt thread(s) still running at shutdown: "
              f"{[type(t).__name__ for t in alive]}", file=sys.stderr)
        import faulthandler
        faulthandler.dump_traceback(file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    # Leave with the status pytest computed. Falling off the end here hands the process to
    # Qt's destructors, and a running thread among them turns "44 passed" into exit code 1.
    os._exit(int(exitstatus))
