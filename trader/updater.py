"""In-app updates from GitHub Releases.

check()    -> the newest release, or None when this build is current
download() -> fetches TGTrader-Setup.exe to a temp folder, reporting progress
install()  -> runs the installer silently and asks the app to quit; the installer
              closes the running copy, replaces the files and starts the new one.

The repo is ``UPDATE_REPO`` in trader/__init__.py. When the app runs from source
(not a PyInstaller build) it only reports that an update exists.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import __version__, UPDATE_REPO

ASSET_NAME = "TGTrader-Setup.exe"
API = "https://api.github.com/repos/{repo}/releases/latest"


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    asset_url: str | None
    asset_size: int
    page_url: str


def _vtuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def configured() -> bool:
    return bool(UPDATE_REPO) and "/" in UPDATE_REPO and not UPDATE_REPO.startswith("OWNER/")


def check(timeout: float = 20.0) -> Release | None:
    """Return the latest release if it is newer than the running version, else None."""
    if not configured():
        raise RuntimeError("update repository is not configured")
    r = httpx.get(API.format(repo=UPDATE_REPO), timeout=timeout, follow_redirects=True,
                  headers={"Accept": "application/vnd.github+json", "User-Agent": f"TGTrader/{__version__}"})
    if r.status_code == 404:
        return None                        # no release published yet
    r.raise_for_status()
    data = r.json()
    tag = data.get("tag_name", "")
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    rel = Release(
        version=tag.lstrip("vV"), tag=tag, notes=(data.get("body") or "").strip(),
        asset_url=asset["browser_download_url"] if asset else None,
        asset_size=int(asset["size"]) if asset else 0,
        page_url=data.get("html_url", ""),
    )
    return rel if _vtuple(rel.version) > _vtuple(__version__) else None


def download(rel: Release, progress: Callable[[int, int], None] | None = None) -> Path:
    if not rel.asset_url:
        raise RuntimeError("this release has no Windows installer attached")
    dest = Path(tempfile.gettempdir()) / f"TGTrader-Setup-{rel.version}.exe"
    done = 0
    with httpx.stream("GET", rel.asset_url, follow_redirects=True, timeout=120,
                      headers={"User-Agent": f"TGTrader/{__version__}"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or rel.asset_size or 0)
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if rel.asset_size and dest.stat().st_size != rel.asset_size:
        dest.unlink(missing_ok=True)
        raise RuntimeError("download was incomplete, try again")
    return dest


def install(setup_path: Path) -> None:
    """Start the silent installer. The caller must quit the app right after this returns."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("the installer only runs on Windows")
    # /CLOSEAPPLICATIONS closes the running TGTrader, /RESTARTAPPLICATIONS starts it again afterwards.
    subprocess.Popen(
        [str(setup_path), "/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"],
        close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


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
    """Download the official MetaTrader 5 installer and run it (the user completes the wizard)."""
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
