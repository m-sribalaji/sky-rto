#!/usr/bin/env python3
"""
rto_agent_win.py - RTO Attendance Agent (Windows)

Compiled into a standalone .exe by PyInstaller — no Python needed on
the target machine.

Two-trigger system:
  1. WTS Session Notifications via ctypes (event-driven, instant)
     Falls back to OpenInputDesktop polling if WTS unavailable
  2. Background polling every 5 minutes (VPN reconnects, location
     changes, offline queue sync, missed unlocks)

CLI flags (same as the old checkin.py, now on the .exe itself):
  rto-win.exe                  normal background agent mode
  rto-win.exe --force          force a fresh check-in right now
  rto-win.exe --reset          clear all caches + force check-in
  rto-win.exe --retry          retry any queued offline check-ins
  rto-win.exe --install        (re)register startup entries
  rto-win.exe --uninstall      remove startup entries

First run (no --flag): auto-registers startup, then enters agent loop.

NOTE: pywin32 is NOT required. WTS notifications are handled via
pure-ctypes (bundled in stdlib). pywin32 is used as an optional
enhancement if present (slightly more reliable on some Windows builds)
but the binary works fully without it.
"""

import sys
import os
import json
import logging
import threading
import time
import subprocess
import argparse
from pathlib import Path
from datetime import date

# winreg and ctypes.windll are Windows-only — imported lazily inside
# functions so this file can be opened/linted on macOS without errors.
# At runtime (compiled .exe on Windows) they are always available.
if sys.platform == "win32":
    import winreg          # type: ignore[import]
    import ctypes
else:
    winreg = None          # type: ignore[assignment]
    import ctypes          # ctypes exists on macOS but has no windll

# ── checkin_core is bundled into this binary by PyInstaller ──────────────────
# When running as a .py script directly, add client/ to sys.path so Python
# can find checkin_core.py. Has no effect inside a compiled binary.
if not getattr(sys, 'frozen', False):
    import sys as _sys
    _client = str(Path(__file__).resolve().parent.parent / 'client')
    if _client not in _sys.path:
        _sys.path.insert(0, _client)
from checkin_core import (
    run_checkin,
    check_and_apply_update,
    run_reset,
    run_retry,
    load_config,
    save_config,
    CONFIG_DIR,
    CONFIG_FILE,
    LOG_FILE,
)

NO_WIN: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows-only flag

# ── PATHS ─────────────────────────────────────────────────────────────────────
HOME      = Path.home()
AGENT_LOG = HOME / ".rto_tracker" / "rto_agent.log"
CFG_FILE  = CONFIG_DIR / "config.json"

if getattr(sys, "frozen", False):
    BINARY_PATH = Path(sys.executable).resolve()
else:
    BINARY_PATH = Path(__file__).resolve()

RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SkyRTOTracker"

AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(AGENT_LOG), level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("rto_agent_win")

# ── POLL INTERVAL ─────────────────────────────────────────────────────────────
def _get_poll_interval() -> int:
    try:
        cfg = json.loads(CFG_FILE.read_text())
        return int(cfg.get("poll_interval_seconds", 300))
    except Exception:
        return 300

POLL_INTERVAL = _get_poll_interval()


# ── STARTUP INSTALL / UNINSTALL ───────────────────────────────────────────────
def install_startup(verbose: bool = True) -> bool:
    """
    Register the binary in HKCU Run key (no admin required) AND create
    a Startup folder shortcut as a backup. Both are user-scoped.
    """
    binary_str = str(BINARY_PATH)
    success = False

    # Method A: HKCU Run registry key (Windows only)
    if sys.platform == "win32" and winreg is not None:
        try:
            _reg = winreg  # type: ignore[union-attr]
            key = _reg.OpenKey(_reg.HKEY_CURRENT_USER, RUN_KEY, 0, _reg.KEY_SET_VALUE)
            _reg.SetValueEx(key, RUN_NAME, 0, _reg.REG_SZ, f'"{binary_str}"')
            _reg.CloseKey(key)
            if verbose:
                print(f"  [OK] HKCU Run key registered")
            logger.info(f"HKCU Run key set: {binary_str}")
            success = True
        except Exception as e:
            if verbose:
                print(f"  [WARN] HKCU Run key failed: {e}")

    # Method B: Startup folder shortcut via PowerShell COM (no extra packages)
    try:
        ps = (
            f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut('
            f'[Environment]::GetFolderPath("Startup")+"\\SkyRTOTracker.lnk");'
            f'$s.TargetPath="{binary_str}";'
            f'$s.WorkingDirectory="{str(BINARY_PATH.parent)}";'
            f'$s.WindowStyle=7;'
            f'$s.Description="Sky RTO Tracker";$s.Save()'
        )
        subprocess.run(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=15, creationflags=NO_WIN,
        )
        if verbose:
            print(f"  [OK] Startup shortcut created")
        success = True
    except Exception as e:
        if verbose:
            print(f"  [WARN] Startup shortcut failed: {e}")

    return success

def uninstall_startup(verbose: bool = True):
    """Remove Run key and Startup folder shortcut."""
    # Remove registry key (Windows only)
    if sys.platform == "win32" and winreg is not None:
        try:
            _reg = winreg  # type: ignore[union-attr]
            key = _reg.OpenKey(_reg.HKEY_CURRENT_USER, RUN_KEY, 0, _reg.KEY_SET_VALUE)
            _reg.DeleteValue(key, RUN_NAME)
            _reg.CloseKey(key)
            if verbose: print("  [OK] HKCU Run key removed")
        except FileNotFoundError:
            if verbose: print("  [INFO] Run key not found - already removed")
        except Exception as e:
            if verbose: print(f"  [WARN] Run key removal failed: {e}")

    # Remove Startup shortcut — get folder via PowerShell (no extra packages)
    try:
        startup_ps = subprocess.run(
            ["powershell", "-WindowStyle", "Hidden", "-Command",
             '[Environment]::GetFolderPath("Startup")'],
            capture_output=True, text=True, creationflags=NO_WIN,
        )
        startup_folder = startup_ps.stdout.strip()
        shortcut = os.path.join(startup_folder, "SkyRTOTracker.lnk")
    except Exception:
        shortcut = ""

    if os.path.exists(shortcut):
        os.remove(shortcut)
        if verbose: print("  [OK] Startup shortcut removed")
    logger.info("Startup entries uninstalled")

def is_startup_installed() -> bool:
    if sys.platform != "win32" or winreg is None:
        return False
    try:
        _reg = winreg  # type: ignore[union-attr]
        key = _reg.OpenKey(_reg.HKEY_CURRENT_USER, RUN_KEY, 0, _reg.KEY_READ)
        _reg.QueryValueEx(key, RUN_NAME)
        _reg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ── SMART DEDUP ───────────────────────────────────────────────────────────────
def should_run_checkin(trigger: str = "unknown") -> bool:
    today = date.today().isoformat()
    try:
        cfg         = json.loads(CFG_FILE.read_text())
        last_date   = cfg.get("last_checkin_date")
        last_status = cfg.get("last_status")

        from datetime import datetime as _dt
        is_weekend = _dt.strptime(today, "%Y-%m-%d").weekday() >= 5

        if last_date != today:
            if is_weekend:
                if not last_status:
                    logger.info(f"[{trigger}] Weekend, no status - running for registration")
                    return True
                logger.info(f"[{trigger}] Weekend + already {last_status} - skipping")
                return False
            logger.info(f"[{trigger}] New day - running checkin")
            return True

        if not last_status:
            logger.info(f"[{trigger}] No confirmed status yet - retrying")
            return True

        if is_weekend:
            logger.info(f"[{trigger}] Weekend + already {last_status} - skipping")
            return False

        logger.info(f"[{trigger}] Status={last_status} - running for location-change detection")
        return True

    except Exception:
        return True


# ── TRIGGER CHECK-IN ──────────────────────────────────────────────────────────
def trigger_checkin(trigger: str = "unknown", force: bool = False):
    if not force and not should_run_checkin(trigger):
        return
    logger.info(f"Triggering check-in (source: {trigger})")
    try:
        run_checkin(force=force)
    except Exception as e:
        logger.error(f"run_checkin failed ({trigger}): {e}")


# ── PERIODIC POLLER ───────────────────────────────────────────────────────────
def start_periodic_poller():
    def _poll():
        time.sleep(30)
        while True:
            try:
                trigger_checkin(trigger="periodic")
            except Exception as e:
                logger.error(f"Periodic poll error: {e}")
            time.sleep(POLL_INTERVAL)

    t = threading.Thread(target=_poll, daemon=True, name="periodic-poller")
    t.start()
    logger.info(f"Periodic poller started - every {POLL_INTERVAL}s ({POLL_INTERVAL//60} min)")
    return t


# ── WTS SESSION WATCHER (primary, ctypes — no pywin32 required) ───────────────
def start_wts_watcher():
    """
    Event-driven WTS session notifications via pure ctypes.
    WM_WTSSESSION_CHANGE fires instantly on screen unlock.
    Zero CPU between events — sits in Windows message pump.
    No pywin32 required.

    Key fix: explicit 64-bit integer types for WPARAM/LPARAM.
    Windows messages use UINT_PTR/LONG_PTR which are 64-bit on x64.
    Using c_void_p or c_ssize_t causes OverflowError on some messages.
    Correct types: c_uint64 (WPARAM) and c_int64 (LPARAM).
    Also: DefWindowProcW argtypes must be set explicitly or ctypes
    uses default marshalling which also overflows.
    """
    if sys.platform != "win32":
        raise RuntimeError("WTS watcher only runs on Windows")

    WTS_SESSION_UNLOCK   = 8
    WTS_SESSION_LOCK     = 7
    WM_WTSSESSION_CHANGE = 0x02B1

    _windll  = ctypes.windll   # type: ignore[attr-defined]
    user32   = _windll.user32
    kernel32 = _windll.kernel32

    # Explicit 64-bit types — the only reliable way on 64-bit Windows.
    # c_uint64 = WPARAM (UINT_PTR, unsigned 64-bit)
    # c_int64  = LPARAM (LONG_PTR, signed 64-bit)
    # c_int64  = LRESULT (return value, signed 64-bit)
    WPARAM  = ctypes.c_uint64
    LPARAM  = ctypes.c_int64
    LRESULT = ctypes.c_int64
    HWND    = ctypes.c_void_p

    WndProcType = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        LRESULT,
        HWND,
        ctypes.c_uint,   # message
        WPARAM,
        LPARAM,
    )

    # Set DefWindowProcW argtypes explicitly — prevents default marshalling
    # from overflowing on large pointer-valued lparam arguments
    user32.DefWindowProcW.restype  = LRESULT
    user32.DefWindowProcW.argtypes = [HWND, ctypes.c_uint, WPARAM, LPARAM]

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_UNLOCK:
                logger.info("WTS_SESSION_UNLOCK - screen unlocked")
                threading.Thread(
                    target=trigger_checkin,
                    kwargs={"trigger": "screen_unlock"},
                    daemon=True,
                ).start()
            elif wparam == WTS_SESSION_LOCK:
                logger.info("WTS_SESSION_LOCK - screen locked")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WndProcType(wnd_proc)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style",         ctypes.c_uint),
            ("lpfnWndProc",   WndProcType),
            ("cbClsExtra",    ctypes.c_int),
            ("cbWndExtra",    ctypes.c_int),
            ("hInstance",     HWND),
            ("hIcon",         HWND),
            ("hCursor",       HWND),
            ("hbrBackground", HWND),
            ("lpszMenuName",  ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
        ]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd",    HWND),
            ("message", ctypes.c_uint),
            ("wParam",  WPARAM),
            ("lParam",  LPARAM),
            ("time",    ctypes.c_uint),
            ("pt",      ctypes.c_long * 2),
        ]

    # Set argtypes on all user32 functions we call
    user32.RegisterClassW.argtypes    = [ctypes.c_void_p]
    user32.RegisterClassW.restype     = ctypes.c_uint16
    user32.CreateWindowExW.argtypes   = [
        ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        HWND, HWND, HWND, ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype    = HWND
    user32.GetMessageW.argtypes       = [ctypes.c_void_p, HWND, ctypes.c_uint, ctypes.c_uint]
    user32.GetMessageW.restype        = ctypes.c_int
    user32.TranslateMessage.argtypes  = [ctypes.c_void_p]
    user32.TranslateMessage.restype   = ctypes.c_int
    user32.DispatchMessageW.argtypes  = [ctypes.c_void_p]
    user32.DispatchMessageW.restype   = LRESULT

    hinstance  = kernel32.GetModuleHandleW(None)
    class_name = "RTOTrackerWatcher"

    wc = WNDCLASSW()
    wc.lpfnWndProc   = proc
    wc.hInstance     = hinstance
    wc.lpszClassName = class_name

    user32.RegisterClassW(ctypes.byref(wc))

    HWND_MESSAGE_VAL = ctypes.c_void_p(-3)
    hwnd = user32.CreateWindowExW(
        0, class_name, "RTO Tracker Watcher",
        0, 0, 0, 0, 0,
        HWND_MESSAGE_VAL, None, hinstance, None,
    )

    if not hwnd:
        raise RuntimeError(f"CreateWindowEx failed: {kernel32.GetLastError()}")

    # Register for WTS session notifications
    wtsapi32 = _windll.wtsapi32
    wtsapi32.WTSRegisterSessionNotification.argtypes = [HWND, ctypes.c_ulong]
    wtsapi32.WTSRegisterSessionNotification.restype  = ctypes.c_int
    wtsapi32.WTSRegisterSessionNotification(hwnd, 0)

    logger.info("WTS ctypes notifications registered - entering message loop")

    msg_buf = MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg_buf), None, 0, 0)
        if ret == 0 or ret == -1:
            break
        user32.TranslateMessage(ctypes.byref(msg_buf))
        user32.DispatchMessageW(ctypes.byref(msg_buf))


# ── OPENINPUTDESKTOP POLLING (fallback) ───────────────────────────────────────
def start_polling_fallback():
    """Fallback: poll OpenInputDesktop every 5s for screen lock/unlock."""
    logger.info("OpenInputDesktop polling started (every 5s)")
    was_locked = False

    def _is_locked():
        if sys.platform != "win32":
            return False
        try:
            _u32 = ctypes.windll.user32  # type: ignore[attr-defined]
            desktop = _u32.OpenInputDesktop(0, False, 0x0100)
            if desktop:
                _u32.CloseDesktop(desktop)
                return False
            return True
        except Exception:
            return False

    while True:
        try:
            is_locked = _is_locked()
        except Exception:
            is_locked = False

        if was_locked and not is_locked:
            logger.info("OpenInputDesktop: screen unlocked")
            threading.Thread(
                target=trigger_checkin,
                kwargs={"trigger": "screen_unlock_fallback"},
                daemon=True,
            ).start()

        was_locked = is_locked
        time.sleep(5)


# ── FIRST-RUN SETUP ───────────────────────────────────────────────────────────
def first_run_setup():
    """
    Called automatically on the very first run (startup not yet registered).
    Writes config, registers startup, runs first check-in.
    Shows a console window briefly so the user can see what's happening.
    """
    print("")
    print("------------------------------------------")
    print("  Sky RTO Tracker - Windows Setup")
    print("------------------------------------------")
    print("")

    cfg = load_config()
    server = cfg.get("server_url", "")
    print(f"  Server  : {server}")
    print(f"  Binary  : {BINARY_PATH}")
    print(f"  Logs    : {CONFIG_DIR}")
    print("")

    # Register auto-start
    print("  [1/2] Registering auto-start (runs on every login)...")
    install_startup(verbose=True)

    print("")
    print("  [2/2] Running first check-in...")
    print("        (Browser will open for registration if not yet registered)")
    print("")

    try:
        run_checkin(force=True)
    except Exception as e:
        logger.error(f"First-run check-in failed: {e}")
        print(f"  [WARN] Check-in error: {e}")
        print(f"         Check {CONFIG_DIR}\\checkin.log for details")

    print("")
    print("------------------------------------------")
    print("  RTO Tracker installed!")
    print("")
    print("  Triggers: screen unlock + every 5 min")
    print("  Auto-starts on every Windows login")
    print("")
    print(f"  Logs    : {AGENT_LOG}")
    print(f"  Force   : {BINARY_PATH.name} --force")
    print(f"  Reset   : {BINARY_PATH.name} --reset")
    print(f"  Retry   : {BINARY_PATH.name} --retry")
    print("------------------------------------------")
    print("")
    print("  This window will close in 10 seconds...")
    time.sleep(10)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Sky RTO Tracker Agent (Windows)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rto-win.exe                  Run as background agent (normal mode)
  rto-win.exe --force          Force a fresh check-in right now
  rto-win.exe --reset          Clear all caches + force check-in
  rto-win.exe --retry          Retry any queued offline check-ins
  rto-win.exe --install        (Re)register startup entries
  rto-win.exe --uninstall      Remove startup entries
        """,
    )
    parser.add_argument("--force",     action="store_true",
                        help="Force a fresh check-in now, bypass dedup")
    parser.add_argument("--reset",     action="store_true",
                        help="Clear all caches then force a fresh check-in")
    parser.add_argument("--retry",     action="store_true",
                        help="Retry any queued offline check-ins")
    parser.add_argument("--install",   action="store_true",
                        help="(Re)register auto-start startup entries")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove startup entries")
    args = parser.parse_args()

    # ── One-shot commands ────────────────────────────────────────────────────
    if args.uninstall:
        uninstall_startup(verbose=True)
        sys.exit(0)

    if args.install:
        print("")
        print("  Reinstalling startup entries...")
        ok = install_startup(verbose=True)
        sys.exit(0 if ok else 1)

    if args.reset:
        print("")
        run_reset()
        run_checkin(force=True)
        sys.exit(0)

    if args.retry:
        run_retry()
        sys.exit(0)

    if args.force:
        logger.info("--force: running forced check-in")
        run_checkin(force=True)
        sys.exit(0)

    # ── Background agent mode ────────────────────────────────────────────────
    logger.info(f"rto_agent_win starting | binary={BINARY_PATH} | poll={POLL_INTERVAL}s")

    # Check for updates once per day — silent, non-blocking
    try:
        check_and_apply_update()
    except Exception as _ue:
        logger.debug(f"Update check skipped: {_ue}")

    # ── Single-instance guard (Windows Mutex) ────────────────────────────────
    # Prevents two agent processes running simultaneously.
    # This happens when both HKCU\Run key AND Startup shortcut are registered,
    # or when the user double-clicks the binary while agent is already running.
    if sys.platform == "win32":
        _mutex_name = "Global\\SkyRTOTrackerAgent"
        try:
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, _mutex_name)
            _last_err = ctypes.windll.kernel32.GetLastError()
            if _last_err == 183:  # ERROR_ALREADY_EXISTS
                logger.info("Another agent instance is already running - exiting.")
                sys.exit(0)
        except Exception as e:
            logger.warning(f"Mutex creation failed: {e} - continuing without single-instance guard")

    # First run: startup not yet registered
    if not is_startup_installed():
        first_run_setup()
        # After setup, start the agent loop in the same process
        # (the HKCU Run key will launch a fresh instance on next login)
        logger.info("First-run setup complete - entering agent loop")
    else:
        logger.info("Agent started via startup registration - entering normal operation")

    # Run once at startup
    trigger_checkin(trigger="startup")

    # Start background periodic poller
    start_periodic_poller()

    # Start WTS session unlock listener (primary trigger — blocks forever)
    try:
        start_wts_watcher()
    except Exception as e:
        logger.warning(f"WTS watcher failed ({e}) - using OpenInputDesktop fallback")
        start_polling_fallback()


if __name__ == "__main__":
    main()