"""Entry point for the desktop app (and the PyInstaller build).

Code overlay
------------
The frozen exe bundles a copy of the ``trader`` package. Code-only updates are delivered as a
zip extracted to ``<data dir>/code/trader`` and take precedence over the bundled copy through
an import finder installed here, so most updates never need a new exe. If the overlay fails to
import (a broken update), it is set aside as ``code.broken`` and the bundled code runs.
"""
from __future__ import annotations

import importlib.util
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


class OverlayFinder:
    """Resolve ``trader`` and ``trader.*`` from a directory, ahead of PyInstaller's bundled copy."""

    def __init__(self, root: str):
        self.root = root

    def find_spec(self, name, path=None, target=None):
        if name != "trader" and not name.startswith("trader."):
            return None
        p = os.path.join(self.root, *name.split("."))
        init = os.path.join(p, "__init__.py")
        if os.path.isdir(p) and os.path.exists(init):
            return importlib.util.spec_from_file_location(name, init, submodule_search_locations=[p])
        if os.path.exists(p + ".py"):
            return importlib.util.spec_from_file_location(name, p + ".py")
        return None


def _bundled_version() -> str:
    try:
        import trader as _t  # bundled or source copy
        v = getattr(_t, "__version__", "0")
        for k in [k for k in sys.modules if k == "trader" or k.startswith("trader.")]:
            del sys.modules[k]
        return v
    except Exception:
        return "0"


def install_overlay() -> str | None:
    """Activate the code overlay if present. Returns its path or None."""
    if not getattr(sys, "frozen", False):
        return None
    base_version = _bundled_version()
    os.environ["TGTRADER_BASE_VERSION"] = base_version
    root = os.path.join(_data_dir(), "code")
    if not os.path.exists(os.path.join(root, "trader", "__init__.py")):
        return None
    sys.meta_path.insert(0, OverlayFinder(root))
    try:
        import trader  # noqa: F401  (from the overlay)
        import trader.gui.app  # noqa: F401  make sure the heavy module imports too
        os.environ["TGTRADER_OVERLAY"] = root
        return root
    except Exception as exc:  # broken update: set it aside and fall back to the bundled code
        sys.meta_path = [f for f in sys.meta_path if not isinstance(f, OverlayFinder)]
        for k in [k for k in sys.modules if k == "trader" or k.startswith("trader.")]:
            del sys.modules[k]
        try:
            broken = root + ".broken"
            shutil.rmtree(broken, ignore_errors=True)
            os.rename(root, broken)
            with open(os.path.join(_data_dir(), "overlay-error.txt"), "w", encoding="utf-8") as f:
                f.write(f"overlay disabled: {exc!r}\n")
        except Exception:
            pass
        return None


if __name__ == "__main__":
    install_overlay()
    if len(sys.argv) > 1:
        from trader.cli import main as cli_main
        cli_main(sys.argv[1:])
    else:
        from trader.gui.app import main
        sys.exit(main())
