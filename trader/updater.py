"""In-app updates.

Where releases come from
------------------------
1. The owner's own server: ``UPDATE_URL`` (latest.json on a plain IP:port, mirrored from the
   build every two minutes). This is what the app tries first - fast from Iran, no GitHub.
2. GitHub Releases (``UPDATE_REPO``).

BOTH are asked and the NEWEST wins. The server is a mirror on a two-minute cron: taking
whichever source answered first meant that for the two minutes after a release - or forever if
the mirror ever wedged - the app reported "you are up to date" while a newer build existed.

How an update is applied (Windows)
----------------------------------
``install()`` writes a small .cmd helper that waits for this process to exit, runs the
installer silently INTO THE FOLDER THE APP IS RUNNING FROM (so it can never install a second
copy somewhere else and relaunch the old one), logs to %TEMP%\\tgtrader-update.log, and starts
the new exe. If that folder is not writable (Program Files) the helper is started elevated.
A loop guard refuses to auto-install the same version twice within 30 minutes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import __version__, UPDATE_REPO

UPDATE_URL = "http://91.107.163.109:40002/latest.json"
ASSET_NAME = "TGTrader-Setup.exe"
API = "https://api.github.com/repos/{repo}/releases/latest"
UA = {"User-Agent": f"TGTrader/{__version__}"}


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    asset_url: str | None
    asset_size: int
    page_url: str
    sha256: str = ""
    source: str = "server"
    kind: str = "exe"          # only full installers are shipped; the zip-overlay path is gone


def base_version() -> str:
    """The version actually running. There is no code-overlay layer any more: what the exe
    reports is what it is, so the update button can never claim a version the app is not."""
    return __version__


def _vtuple(v: str) -> tuple[int, ...]:
    """Numeric version only. A pre-release suffix is DROPPED, not parsed as another number:
    "0.5.0-rc1" read as (0,5,0,1) sorts ABOVE the real (0,5,0), so anyone who ever installed a
    release candidate would be told the final release was older and never offered it again."""
    core = re.split(r"[-+]", str(v).strip().lstrip("vV"), maxsplit=1)[0]
    nums = re.findall(r"\d+", core)
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def configured() -> bool:
    return bool(UPDATE_URL) or (bool(UPDATE_REPO) and "/" in UPDATE_REPO and not UPDATE_REPO.startswith("OWNER/"))


def install_dir() -> Path:
    return Path(sys.executable).parent if is_frozen() else Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- check
def _proxy() -> str | None:
    try:
        from .config import Settings
        from .net import resolve_proxy
        return resolve_proxy(Settings.load())
    except Exception:
        return None


def _get(url: str, timeout: float, **kw):
    p = _proxy()
    return httpx.get(url, timeout=timeout, proxy=p, **kw) if p else httpx.get(url, timeout=timeout, **kw)


def _check_server(timeout: float) -> Release | None:
    r = _get(UPDATE_URL, timeout, headers=UA)
    r.raise_for_status()
    d = r.json()
    exe = Release(version=str(d["version"]), tag=str(d.get("tag", "")), notes=str(d.get("notes", "")).strip(),
                  asset_url=d.get("setup_url"), asset_size=int(d.get("setup_size") or 0),
                  page_url=str(d.get("page_url", "")), sha256=str(d.get("setup_sha256", "")), source="server", kind="exe")
    # Always use the full installer. The code-overlay path did not restart reliably on Windows
    # and produced a download loop, so it is deliberately not used from the update button.
    return exe


def _check_github(timeout: float) -> Release | None:
    r = _get(API.format(repo=UPDATE_REPO), timeout, follow_redirects=True,
             headers={"Accept": "application/vnd.github+json", **UA})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    tag = data.get("tag_name", "")
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    return Release(version=tag.lstrip("vV"), tag=tag, notes=(data.get("body") or "").strip(),
                   asset_url=asset["browser_download_url"] if asset else None,
                   asset_size=int(asset["size"]) if asset else 0, page_url=data.get("html_url", ""), source="github")


def check(timeout: float = 20.0) -> Release | None:
    """Return the newest release across every source, or None when we are already on it."""
    if not configured():
        raise RuntimeError("update source is not configured")
    found: list[Release] = []
    err: Exception | None = None
    for fn in (_check_server, _check_github):
        try:
            r = fn(timeout)
            if r:
                found.append(r)
        except Exception as exc:
            err = exc
    if not found:
        if err:
            raise err
        return None
    found.sort(key=lambda r: _vtuple(r.version), reverse=True)
    best = found[0]
    if not best.asset_url:
        # A source that names a version but has no file to download is useless on its own;
        # prefer an equal-or-older source that actually has an installer.
        best = next((r for r in found if r.asset_url), best)
    # GitHub's API does not publish a checksum. When the mirror has the same build, take its
    # hash, so the fallback route is verified rather than trusted.
    if not best.sha256:
        same = next((r for r in found if r.sha256 and _vtuple(r.version) == _vtuple(best.version)), None)
        if same:
            best.sha256 = same.sha256
            if not best.asset_size:
                best.asset_size = same.asset_size
    return best if _vtuple(best.version) > _vtuple(__version__) else None


# ---------------------------------------------------------------- download
def download(rel: Release, progress: Callable[[int, int], None] | None = None) -> Path:
    if not rel.asset_url:
        raise RuntimeError("this release has no Windows installer attached")
    dest = Path(tempfile.gettempdir()) / f"TGTrader-Setup-{rel.version}.exe"
    done = 0
    h = hashlib.sha256()
    p = _proxy()
    client = httpx.Client(proxy=p, follow_redirects=True, timeout=120) if p else httpx.Client(follow_redirects=True, timeout=120)
    with client, client.stream("GET", rel.asset_url, headers=UA) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or rel.asset_size or 0)
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk); h.update(chunk); done += len(chunk)
                if progress:
                    progress(done, total)
    if rel.asset_size and dest.stat().st_size != rel.asset_size:
        dest.unlink(missing_ok=True)
        raise RuntimeError("download was incomplete, try again")
    if rel.sha256 and h.hexdigest().lower() != rel.sha256.lower():
        dest.unlink(missing_ok=True)
        raise RuntimeError("downloaded file is corrupted (checksum mismatch), try again")
    if not rel.sha256:
        # Say so rather than implying the file was checked. Everything here travels over plain
        # HTTP, so an unverified installer is a real (if small) risk worth naming.
        pass
    return dest


# ---------------------------------------------------------------- loop guard
def _guard_file() -> Path:
    from .config import data_dir
    return data_dir() / "update_attempt.json"


def attempted_recently(version: str, within_s: int = 1800) -> bool:
    try:
        d = json.loads(_guard_file().read_text())
        return d.get("version") == version and time.time() - float(d.get("ts", 0)) < within_s
    except Exception:
        return False


def mark_attempt(version: str) -> None:
    try:
        _guard_file().write_text(json.dumps({"version": version, "ts": time.time(), "from": __version__}))
    except Exception:
        pass


def log_path() -> Path:
    return Path(tempfile.gettempdir()) / "tgtrader-update.log"


def _writable(p: Path) -> bool:
    try:
        t = p / ".write_test"
        t.write_text("x"); t.unlink()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- install
def install(setup_path: Path) -> None:
    """Open the downloaded installer the ordinary way - exactly what works when the user runs
    it by hand. No silent flags, no file-swapping, no self-restart (those caused the update
    loop on Windows). The Inno wizard closes the running app, installs into the same folder it
    remembers, and offers 'Run TGTrader' at the end. The caller quits right after so nothing is
    locked."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("the installer only runs on Windows")
    try:
        os.startfile(str(setup_path))  # type: ignore[attr-defined]  # like double-clicking it
    except Exception:
        subprocess.Popen([str(setup_path)], close_fds=True)


# ---------------------------------------------------------------- optional components
MT5_URL = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"


def mt5_installed() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import MetaTrader5  # type: ignore  # noqa: F401
    except Exception:
        return False
    candidates = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "MetaTrader 5" / "terminal64.exe"]
    return any(p.exists() for p in candidates)


def install_mt5(progress: Callable[[int, int], None] | None = None) -> None:
    dest = Path(tempfile.gettempdir()) / "mt5setup.exe"
    done = 0
    with httpx.stream("GET", MT5_URL, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk); done += len(chunk)
                if progress:
                    progress(done, total)
    subprocess.Popen([str(dest)], close_fds=True)
