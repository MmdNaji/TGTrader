"""Entry point for the desktop app (and the PyInstaller build).

There is deliberately NO code-overlay layer here any more. It used to let a zip in the data
directory take precedence over the code bundled in the exe, which meant a machine could keep
running an old overlay forever while the installer reported a newer version - the app said one
version, the code was another, and no reinstall could fix it. Updates are full installers now,
so the exe on disk is the whole truth.
"""
from __future__ import annotations

import os
import shutil
import sys


def use_utf8_output(streams=None) -> None:
    """Make stdout and stderr speak UTF-8 on Windows.

    When stdout is NOT a console - a pipe, a file, another process - Python on Windows falls
    back to the ANSI code page (cp1252 here) instead of UTF-8. Every message this app prints is
    Persian, so the first one raises UnicodeEncodeError and the process exits 1 with an error
    that names an encoding nobody chose.

    It only shows up when the output is captured, which is exactly when nobody is watching:
        TGTrader.exe selftest > log.txt      -> a crash instead of a report
        run.py paper > paper.log             -> same
        any CI or wrapper that reads the output
    Straight in a console it works, and on Linux UTF-8 is the default - so neither the machine
    that wrote it nor the machine that ran it interactively could see it.

    reconfigure() is a no-op on a stream that is already UTF-8, and errors="replace" means a
    stream that cannot be reconfigured at all still prints something rather than dying.
    """
    if not sys.platform.startswith("win"):
        return
    for stream in (streams if streams is not None else (sys.stdout, sys.stderr)):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue          # a --windowed build has no real stdout to reconfigure
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _data_dir() -> str:
    override = os.environ.get("TGTRADER_HOME")
    if override:
        return override
    if sys.platform.startswith("win"):
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TGTrader")
    return os.path.join(os.path.expanduser("~"), ".tgtrader")


def clear_stale_overlay() -> None:
    """Remove an overlay left behind by an older build. It is no longer read, but leaving it
    there means the next person to read the data directory has to work out whether it matters."""
    for name in ("code", "code.new", "code.old", "code.broken"):
        p = os.path.join(_data_dir(), name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    use_utf8_output()          # before anything can print
    clear_stale_overlay()
    if len(sys.argv) > 1:
        from trader.cli import main as cli_main
        cli_main(sys.argv[1:])
    else:
        from trader.gui.app import main
        sys.exit(main())
