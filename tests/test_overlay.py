"""The code overlay: finder resolves trader.* from a directory; apply_code swaps a zip in safely."""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("TGTRADER_HOME", tempfile.mkdtemp(prefix="tgtrader-overlay-"))

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import run as runmod  # noqa: E402
from trader import updater  # noqa: E402


def _make_overlay(root: Path, version: str) -> None:
    pkg = root / "trader"; pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(f'__version__ = "{version}"\nUPDATE_REPO = "x/y"\n')
    (pkg / "extra_mod.py").write_text("VALUE = 42\n")


def test_overlay_finder_resolves_package_and_submodule():
    root = Path(tempfile.mkdtemp()); _make_overlay(root, "9.9.9")
    finder = runmod.OverlayFinder(str(root))
    spec = finder.find_spec("trader")
    assert spec and spec.submodule_search_locations == [str(root / "trader")]
    assert finder.find_spec("trader.extra_mod").origin.endswith("extra_mod.py")
    assert finder.find_spec("os") is None and finder.find_spec("trader.missing") is None
    # load through the finder for real, then clean up so other tests keep the source package
    saved = {k: v for k, v in sys.modules.items() if k == "trader" or k.startswith("trader.")}
    for k in saved:
        del sys.modules[k]
    sys.meta_path.insert(0, finder)
    try:
        t = importlib.import_module("trader"); m = importlib.import_module("trader.extra_mod")
        assert t.__version__ == "9.9.9" and m.VALUE == 42
    finally:
        sys.meta_path.remove(finder)
        for k in [k for k in sys.modules if k == "trader" or k.startswith("trader.")]:
            del sys.modules[k]
        sys.modules.update(saved)


def test_apply_code_swaps_zip_in_and_rejects_bad_archives():
    z = Path(tempfile.mkdtemp()) / "code.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("trader/__init__.py", '__version__ = "1.2.3"\n')
        zf.writestr("trader/gui/__init__.py", "")
    dest = updater.apply_code(z)
    assert dest == updater.overlay_dir() and (dest / "trader" / "__init__.py").exists()
    # a second apply replaces the first
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("trader/__init__.py", '__version__ = "1.2.4"\n')
    updater.apply_code(z)
    assert "1.2.4" in (dest / "trader" / "__init__.py").read_text()
    bad = z.with_name("bad.zip")
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("nothing/here.txt", "x")
    try:
        updater.apply_code(bad); assert False, "should have rejected"
    except RuntimeError:
        pass
    assert "1.2.4" in (dest / "trader" / "__init__.py").read_text()   # untouched by the failed apply
    evil = z.with_name("evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.py", "x"); zf.writestr("trader/__init__.py", "")
    try:
        updater.apply_code(evil); assert False
    except RuntimeError:
        pass
