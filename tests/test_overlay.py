"""The code overlay is gone on purpose: it could pin a machine to an old version forever while
the app reported a new one. These tests exist so it cannot come back by accident."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("TGTRADER_HOME", tempfile.mkdtemp(prefix="tgtrader-ovl-"))

import run  # noqa: E402
from trader import updater, __version__  # noqa: E402


def test_no_overlay_machinery_survives():
    assert not hasattr(run, "OverlayFinder") and not hasattr(run, "install_overlay")
    assert not hasattr(updater, "apply_code") and not hasattr(updater, "overlay_dir")
    # the version the update button compares against is the real one, with no env override
    os.environ["TGTRADER_BASE_VERSION"] = "9.9.9"
    try:
        assert updater.base_version() == __version__
    finally:
        os.environ.pop("TGTRADER_BASE_VERSION", None)


def test_a_stale_overlay_is_cleaned_up():
    root = Path(os.environ["TGTRADER_HOME"])
    for name in ("code", "code.broken"):
        (root / name / "trader").mkdir(parents=True, exist_ok=True)
        (root / name / "trader" / "__init__.py").write_text("__version__='0.0.1'")
    run.clear_stale_overlay()
    assert not (root / "code").exists() and not (root / "code.broken").exists()


def test_check_prefers_the_newest_source_not_the_first_that_answers(monkeypatch):
    old = updater.Release(version="0.1.0", tag="v0.1.0", notes="", asset_url="http://x/a.exe",
                          asset_size=10, page_url="", sha256="aa", source="server")
    new = updater.Release(version="9.9.9", tag="v9.9.9", notes="", asset_url="http://y/b.exe",
                          asset_size=10, page_url="", source="github")
    monkeypatch.setattr(updater, "_check_server", lambda t: old)
    monkeypatch.setattr(updater, "_check_github", lambda t: new)
    rel = updater.check(timeout=1)
    assert rel and rel.version == "9.9.9"
    # and a source that is merely unreachable must not hide the one that answered
    monkeypatch.setattr(updater, "_check_server", lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    rel = updater.check(timeout=1)
    assert rel and rel.version == "9.9.9"


def test_a_github_release_borrows_the_mirrors_checksum(monkeypatch):
    server = updater.Release(version="9.9.9", tag="v9.9.9", notes="", asset_url="http://x/a.exe",
                             asset_size=123, page_url="", sha256="deadbeef", source="server")
    github = updater.Release(version="9.9.9", tag="v9.9.9", notes="", asset_url="http://y/b.exe",
                             asset_size=0, page_url="", source="github")
    monkeypatch.setattr(updater, "_check_server", lambda t: server)
    monkeypatch.setattr(updater, "_check_github", lambda t: github)
    rel = updater.check(timeout=1)
    # whichever of the two equal versions is chosen, it must carry a checksum to verify against
    assert rel and rel.sha256 == "deadbeef"
