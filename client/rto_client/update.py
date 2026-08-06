"""
update.py - the self-update dance: check GitHub Releases once a day, and if
there's a newer binary, download it and swap it in (atomic rename + exec on
mac, a little batch script that waits for us to exit on Windows since you
can't overwrite a running .exe there). Isolated because it's the riskiest
code in the whole client — a bug here bricks someone's agent — so it's
easier to audit on its own.
"""

import sys
import os
import json
import time
import platform as _plat
import urllib.request
from pathlib import Path

from .config import logger, IS_MAC, IS_WIN, CONFIG_DIR, _BAKED_VERSION, _BAKED_GITHUB_REPO
from .api import _desktop_notify

UPDATE_CHECK_FILE = CONFIG_DIR / ".last_update_check"
UPDATE_INTERVAL   = 86400  # check once per day (seconds)

def _parse_version(v: str) -> tuple:
    """
    Parse version strings into comparable tuples.
    Handles: "0.0.22", "v0.0.22", "build-24" (-> (0,0,24))
    """
    try:
        # Handle "build-N" tag format from GitHub Actions
        if v.startswith("build-"):
            n = int(v.split("-")[1])
            return (0, 0, n)
        return tuple(int(x) for x in v.lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)

def check_and_apply_update() -> bool:
    """
    Check GitHub Releases for a newer binary. If found:
      - Download to a temp file next to current binary
      - Replace current binary atomically
      - Restart the process (Mac) or schedule replace+restart (Windows)
    Returns True if update was triggered (process will restart shortly).
    Never raises — silently skips on any failure so normal operation continues.
    Rate-limited to once per day.
    """
    # Dev builds: skip
    if _BAKED_VERSION == "0.0.0" or not _BAKED_GITHUB_REPO:
        return False

    # Rate limit: once per day
    try:
        if UPDATE_CHECK_FILE.exists():
            last = float(UPDATE_CHECK_FILE.read_text().strip())
            if time.time() - last < UPDATE_INTERVAL:
                return False
    except Exception:
        pass

    # Record check time immediately (prevents parallel runs checking simultaneously)
    try:
        UPDATE_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
        UPDATE_CHECK_FILE.write_text(str(time.time()))
    except Exception:
        pass

    logger.info(f"[update] Checking for updates (current: v{_BAKED_VERSION})")

    # Public repo — check GitHub API directly, no auth needed
    try:
        import ssl as _ssl
        ssl_ctx = None
        try:
            import certifi
            ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except Exception:
            try: ssl_ctx = _ssl.create_default_context()
            except Exception: ssl_ctx = _ssl._create_unverified_context()

        api_url = f"https://api.github.com/repos/{_BAKED_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(
            api_url,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "sky-rto-agent",
                     "Accept-Encoding": "identity"},
        )
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            release = json.loads(resp.read().decode())
    except Exception as e:
        logger.debug(f"[update] Version check skipped: {e}")
        return False

    latest_tag  = release.get("tag_name", "")
    latest_ver  = _parse_version(latest_tag)
    current_ver = _parse_version(_BAKED_VERSION)

    if latest_ver <= current_ver:
        logger.info(f"[update] Already up to date (v{_BAKED_VERSION})")
        return False

    logger.info(f"[update] New version available: {latest_tag} (have: v{_BAKED_VERSION})")

    # Find matching asset for this platform/arch
    if IS_WIN:
        asset_name = "rto-win.exe"
    elif IS_MAC:
        asset_name = "rto-mac-arm64" if _plat.machine() == "arm64" else "rto-mac-x86"
    else:
        logger.info("[update] Auto-update not supported on this platform")
        return False

    asset = next((a for a in release.get("assets", [])
                  if a["name"] == asset_name), None)
    if not asset:
        logger.warning(f"[update] Asset '{asset_name}' not found in release {latest_tag}")
        return False

    download_url = asset["browser_download_url"]
    logger.info(f"[update] Downloading {asset_name} ({asset.get('size',0)//1024} KB)")

    # Determine binary location
    if getattr(sys, "frozen", False):
        current_binary = Path(sys.executable)
    else:
        current_binary = Path(__file__)  # dev mode fallback

    tmp_path    = current_binary.with_suffix(".tmp")
    backup_path = current_binary.with_suffix(".bak")

    # Download
    try:
        req2 = urllib.request.Request(
            download_url,
            headers={"User-Agent": "sky-rto-agent", "Accept-Encoding": "identity"},
        )
        with urllib.request.urlopen(req2, timeout=120, context=ssl_ctx) as resp2:
            tmp_path.write_bytes(resp2.read())
        logger.info(f"[update] Download complete ({tmp_path.stat().st_size} bytes)")
    except Exception as e:
        logger.error(f"[update] Download failed: {e}")
        try: tmp_path.unlink(missing_ok=True)
        except Exception: pass
        return False

    # Replace and restart
    try:
        if IS_WIN:
            # Windows cannot replace a running .exe directly.
            # Write a small batch script to do it after we exit.
            bat = current_binary.parent / "_rto_updater.bat"
            bat_content = "\r\n".join([
                "@echo off",
                "timeout /t 3 /nobreak >nul",
                f"move /Y \"{tmp_path}\" \"{current_binary}\"",
                f"start \"\" \"{current_binary}\"",
                "del \"%~f0\"",
                "",
            ])
            bat.write_text(bat_content)
            import subprocess as _sp
            _sp.Popen(["cmd", "/c", str(bat)],
                      creationflags=0x00000008,  # DETACHED_PROCESS
                      close_fds=True)
            logger.info("[update] Updater script launched — exiting for replacement")
            # Notify desktop
            _desktop_notify("RTO Tracker", f"Updating to {latest_tag} — will restart shortly.")
            sys.exit(0)
        else:
            # macOS: atomic replace then exec into new binary
            if current_binary.exists():
                try: current_binary.rename(backup_path)
                except Exception: pass
            tmp_path.rename(current_binary)
            current_binary.chmod(0o755)
            logger.info(f"[update] Binary replaced. Restarting as v{latest_tag}...")
            _desktop_notify("RTO Tracker", f"Updated to {latest_tag} — restarting.")
            os.execv(str(current_binary), sys.argv)  # in-place restart
    except Exception as e:
        logger.error(f"[update] Replace/restart failed: {e}")
        # Restore backup if available
        try:
            tmp_path.unlink(missing_ok=True)
            if backup_path.exists() and not current_binary.exists():
                backup_path.rename(current_binary)
        except Exception: pass
        return False

    return True
