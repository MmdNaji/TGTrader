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
# And no market data either. A GUI test builds a real window; its chart page starts a candle
# fetch in the background, and on Windows that thread was still inside an SSL read when the
# suite finished. The teardown below then fell through to os._exit, which tears every thread
# down where it stands - and doing that to a thread inside OpenSSL is itself an access
# violation. The run reported "88 passed" and exited 0xC0000005, three times out of three.
os.environ.setdefault("TGTRADER_OFFLINE", "1")

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

    # MEASURED, because this is a probabilistic bug and one green run proves nothing:
    #   with these three lines      -> 25 of 25 runs clean
    #   with deleteLater removed    -> 4 of 15 runs segfaulted (27%)
    #
    # Close, then actually DESTROY. close() only hides a window - the C++ object stays alive,
    # owned by whatever Python reference a fixture still holds, and is then destroyed at
    # interpreter exit in an undefined order relative to the QApplication itself. That is the
    # segfault: inside libQt6Core, with no Python frame at all, on roughly one run in five.
    # Deleting them here, while the application is still up and can run the deletion queue, is
    # the whole fix.
    for w in list(app.topLevelWidgets()):
        try:
            w.close()
            w.deleteLater()
        except Exception:
            pass
    app.processEvents()
    import gc
    gc.collect()
    app.processEvents()

    alive = _live_threads()
    if alive:
        try:
            from trader.gui.app import _join_threads
            alive = _join_threads(alive, ms=5000)
        except Exception:
            pass

    # Plain threads too - the engine loop is a threading.Thread, not a QThread, so the Qt
    # registry cannot see it.
    import threading
    stragglers = [t for t in threading.enumerate()
                  if t is not threading.main_thread() and t.is_alive()]
    for t in stragglers:
        t.join(timeout=3.0)
    stragglers = [t for t in threading.enumerate()
                  if t is not threading.main_thread() and t.is_alive()]

    if not alive and not stragglers:
        # Nothing is running, so nothing can be destroyed while running: let Python exit the
        # ordinary way. os._exit maps to ExitProcess on Windows, which tears every thread down
        # WHERE IT STANDS - and doing that to a thread inside OpenSSL is its own access
        # violation. Forcing the exit when it is not needed swapped one crash for another.
        sys.stdout.flush()
        sys.stderr.flush()
        return

    names = [type(t).__name__ for t in alive] + [t.name for t in stragglers]
    print(f"\n[conftest] {len(names)} thread(s) still running at shutdown: {names}", file=sys.stderr)
    import faulthandler
    faulthandler.dump_traceback(file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    # Only now. Qt aborts the process when a running QThread is destroyed, so with something
    # genuinely stuck this is still the least bad way out - but it is the exception, not the
    # routine path.
    os._exit(int(exitstatus))
