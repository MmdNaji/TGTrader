"""In-app updates.

Where releases come from
------------------------
1. The owner's own server: ``UPDATE_URL`` (latest.json on a plain IP:port, mirrored from the
   build every two minutes). This is what the app tries first - fast from Iran, no GitHub.
2. GitHub Releases (``UPDATE_REPO``) as a fallback when the server is unreachable.

How an update is applied (Windows)
----------------------------------
``install()`` writes a small .cmd helper that waits for this process to exit, runs the
installer silently INTO THE FOLDER THE APP IS RUNNING FROM (so it can never install a second
copy somewhere else and relaunch the old one), logs to %TEMP%\\tgtrader-update.log, and starts
the new exe. If that folder is not writable (Program Files) the helper is started elevated.
A loop guard refuses to auto-install the same version twice within 30 minutes.
"""
from __future__ import annotations

import ctypes
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


def _vtuple(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def configured() -> bool:
    return bool(UPDATE_URL) or (bool(UPDATE_REPO) and "/" in UPDATE_REPO and not UPDATE_REPO.startswith("OWNER/"))


def install_dir() -> Path:
    return Path(sys.executable).parent if is_frozen() else Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- check
def _check_server(timeout: float) -> Release | None:
    r = httpx.get(UPDATE_URL, timeout=timeout, headers=UA)
    r.raise_for_status()
    d = r.json()
    return Release(version=str(d["version"]), tag=str(d.get("tag", "")), notes=str(d.get("notes", "")).strip(),
                   asset_url=d.get("setup_url"), asset_size=int(d.get("setup_size") or 0),
                   page_url=str(d.get("page_url", "")), sha256=str(d.get("setup_sha256", "")), source="server")


def _check_github(timeout: float) -> Release | None:
    r = httpx.get(API.format(repo=UPDATE_REPO), timeout=timeout, follow_redirects=True,
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
    """Return the newest release if it is newer than the running version, else None."""
    if not configured():
        raise RuntimeError("update source is not configured")
    rel: Release | None = None
    err: Exception | None = None
    for fn in (_check_server, _check_github):
        try:
            rel = fn(timeout)
            if rel:
                break
        except Exception as exc:  # try the next source
            err = exc
    if rel is None:
        if err:
            raise err
        return None
    return rel if _vtuple(rel.version) > _vtuple(__version__) else None


# ---------------------------------------------------------------- download
def download(rel: Release, progress: Callable[[int, int], None] | None = None) -> Path:
    if not rel.asset_url:
        raise RuntimeError("this release has no Windows installer attached")
    dest = Path(tempfile.gettempdir()) / f"TGTrader-Setup-{rel.version}.exe"
    done = 0
    h = hashlib.sha256()
    with httpx.stream("GET", rel.asset_url, follow_redirects=True, timeout=120, headers=UA) as r:
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
    """Start the silent installer through a helper that waits for this process to exit.
    The caller must quit the app right after this returns."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("the installer only runs on Windows")
    target = install_dir()
    log = log_path()
    script = Path(tempfile.gettempdir()) / "tgtrader-update.cmd"
    pid = os.getpid()
    script.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        ":wait\r\n"
        f"tasklist /FI \"PID eq {pid}\" 2>nul | find \"{pid}\" >nul\r\n"
        "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto wait )\r\n"
        f"\"{setup_path}\" /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS "
        f"/DIR=\"{target}\" /LOG=\"{log}\"\r\n"
        f"if exist \"{target}\\TGTrader.exe\" start \"\" \"{target}\\TGTrader.exe\"\r\n",
        encoding="utf-8",
    )
    if _writable(target):
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["cmd.exe", "/c", str(script)], close_fds=True, creationflags=flags)
    else:
        # Program Files: ask for elevation once (UAC), then the same helper runs as admin.
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c "{script}"', None, 0)  # type: ignore[attr-defined]
        if int(rc) <= 32:
            raise RuntimeError(f"elevation refused (code {rc}); install manually from {setup_path}")


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
