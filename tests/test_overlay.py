"""The code overlay is gone on purpose: it could pin a machine to an old version forever while
the app reported a new one. These tests exist so it cannot come back by accident."""
from __future__ import annotations

import os
import sys
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
    """The mirror publishes a SHA-256; the GitHub API does not. When the file has to come from
    GitHub, it must still be verified against the mirror's figure for the same build.

    The server release deliberately carries NO installer here, so GitHub is the only release
    that can be chosen - otherwise this test passes whichever one wins and proves nothing."""
    server = updater.Release(version="9.9.9", tag="v9.9.9", notes="", asset_url=None,
                             asset_size=123, page_url="", sha256="deadbeef", source="server")
    github = updater.Release(version="9.9.9", tag="v9.9.9", notes="", asset_url="http://y/b.exe",
                             asset_size=0, page_url="", source="github")
    monkeypatch.setattr(updater, "_check_server", lambda t: server)
    monkeypatch.setattr(updater, "_check_github", lambda t: github)
    rel = updater.check(timeout=1)
    assert rel is not None
    assert rel.source == "github" and rel.asset_url == "http://y/b.exe", \
        "the release with no installer cannot be the one offered"
    assert rel.sha256 == "deadbeef", "the GitHub download must borrow the mirror's checksum"
    assert rel.asset_size == 123, "and its size, so a truncated download is still caught"


def test_a_release_candidate_never_outranks_its_own_release():
    assert updater._vtuple("0.5.0") > updater._vtuple("0.4.9")
    assert updater._vtuple("0.5.0") == updater._vtuple("v0.5.0")
    # the bug: (0,5,0,1) sorts above (0,5,0), so an rc pinned that user for good
    assert updater._vtuple("0.5.0-rc1") == updater._vtuple("0.5.0")
    assert updater._vtuple("0.5.1") > updater._vtuple("0.5.0-rc9")
    assert updater._vtuple("0.5.0+build7") == updater._vtuple("0.5.0")


def test_windows_output_is_reconfigured_to_utf8(monkeypatch):
    """On Windows, a stdout that is not a console falls back to the ANSI code page, and every
    message this app prints is Persian. `TGTrader.exe selftest > log.txt` died with
    UnicodeEncodeError and exit code 1 - only ever when the output was captured, which is
    exactly when nobody is watching."""
    class Stream:
        def __init__(self): self.kw = None
        def reconfigure(self, **kw): self.kw = kw

    class Deaf:
        """A --windowed build's stdout: no reconfigure at all."""

    class Angry:
        def reconfigure(self, **kw): raise ValueError("cannot reconfigure")

    monkeypatch.setattr(sys, "platform", "win32")
    a, b = Stream(), Stream()
    run.use_utf8_output((a, b))
    assert a.kw == {"encoding": "utf-8", "errors": "replace"}, a.kw
    assert b.kw == a.kw
    # neither of these may raise: a windowed build and a locked stream both have to survive
    run.use_utf8_output((Deaf(), Angry()))

    # and it does nothing off Windows, where UTF-8 is already the default
    monkeypatch.setattr(sys, "platform", "linux")
    c = Stream()
    run.use_utf8_output((c,))
    assert c.kw is None
