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
    clear_stale_overlay()
    if len(sys.argv) > 1:
        from trader.cli import main as cli_main
        cli_main(sys.argv[1:])
    else:
        from trader.gui.app import main
        sys.exit(main())
