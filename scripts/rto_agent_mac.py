#!/usr/bin/env python3
"""
rto_agent_mac.py - RTO Attendance Agent (macOS)

Compiled into a standalone binary by PyInstaller — no Python needed on
the target machine.

Two-trigger system:
  1. NSWorkspace screen unlock (event-driven, instant)
     Falls back to CGSession polling if PyObjC unavailable
  2. Background polling every 5 minutes (VPN reconnects, location
     changes, offline queue sync, missed unlocks)

CLI flags (same as the old checkin.py, now on the binary itself):
  rto-mac                 normal background agent mode
  rto-mac --force         force a fresh check-in right now
  rto-mac --reset         clear all caches + force check-in
  rto-mac --retry         retry any queued offline check-ins
  rto-mac --install       (re)install the launchd agent
  rto-mac --uninstall     remove the launchd agent

First run (no --flag): auto-installs launchd, then enters agent loop.
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
    run_reset,
    run_retry,
    load_config,
    save_config,
    CONFIG_DIR,
    CONFIG_FILE,
    LOG_FILE,
)

# ── PATHS ────────────────────────────────────────────────────────────────────
HOME          = Path.home()
AGENT_LOG     = HOME / ".rto_tracker" / "rto_agent.log"
CFG_FILE      = CONFIG_DIR / "config.json"

# When running as a PyInstaller binary sys.executable IS the binary itself.
# When running as a .py script (dev), use the script path.
if getattr(sys, "frozen", False):
    # Running as compiled binary
    BINARY_PATH = Path(sys.executable).resolve()
else:
    # Running as plain .py (development)
    BINARY_PATH = Path(__file__).resolve()

PLIST_LABEL   = "com.sky.rto"
PLIST_PATH    = HOME / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(AGENT_LOG), level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("rto_agent_mac")

# ── POLL INTERVAL ─────────────────────────────────────────────────────────────
def _get_poll_interval() -> int:
    try:
        cfg = json.loads(CFG_FILE.read_text())
        return int(cfg.get("poll_interval_seconds", 300))
    except Exception:
        return 300

POLL_INTERVAL = _get_poll_interval()


# ── LAUNCHD INSTALL / UNINSTALL ───────────────────────────────────────────────
PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>

  <key>ProgramArguments</key>
  <array>
    <string>{binary}</string>
  </array>

  <!-- Restart automatically if it crashes -->
  <key>KeepAlive</key>
  <true/>

  <!-- Start at login -->
  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>{logdir}/launchd.log</string>

  <key>StandardErrorPath</key>
  <string>{logdir}/launchd_err.log</string>

  <key>ProcessType</key>
  <string>Background</string>

  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
"""

def install_launchd(verbose: bool = True) -> bool:
    """Write the plist and load it. Returns True on success."""
    plist_content = PLIST_TEMPLATE.format(
        label   = PLIST_LABEL,
        binary  = str(BINARY_PATH),
        logdir  = str(CONFIG_DIR),
    )
    try:
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLIST_PATH.write_text(plist_content)
        if verbose:
            print(f"  [OK] Plist written: {PLIST_PATH}")
    except Exception as e:
        if verbose:
            print(f"  [FAIL] Could not write plist: {e}")
        return False

    # Unload first (ignore error if not loaded)
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", str(PLIST_PATH)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        if verbose:
            print(f"  [OK] LaunchAgent loaded - auto-starts on every login")
        logger.info("LaunchAgent installed and loaded")
        return True
    else:
        if verbose:
            print(f"  [WARN] launchctl load returned: {result.stderr.strip()}")
            print(f"         Check: {CONFIG_DIR}/launchd_err.log")
        return False

def uninstall_launchd(verbose: bool = True):
    """Unload and remove the plist."""
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)],
                   capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        if verbose:
            print("  [OK] LaunchAgent removed")
    else:
        if verbose:
            print("  [INFO] LaunchAgent plist not found - nothing to remove")
    logger.info("LaunchAgent uninstalled")

def is_launchd_installed() -> bool:
    return PLIST_PATH.exists()


# ── SMART DEDUP ───────────────────────────────────────────────────────────────
def should_run_checkin(trigger: str = "unknown") -> bool:
    """
    Decide whether to call run_checkin() for this trigger.
    checkin_core.run_checkin() handles its own location-change dedup
    internally, but this outer guard prevents calling it at all on
    weekends when we already have a confirmed status.
    """
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
        return True  # config unreadable -> always run


# ── TRIGGER CHECK-IN ──────────────────────────────────────────────────────────
def trigger_checkin(trigger: str = "unknown", force: bool = False):
    """Called by screen-unlock events and the periodic poller."""
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
        time.sleep(30)  # let startup check-in finish first
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


# ── NSWWORKSPACE UNLOCK LISTENER ──────────────────────────────────────────────
def start_nsworkspace_listener():
    """
    Primary trigger: NSWorkspace + NSDistributedNotificationCenter.
    Fires instantly on screen unlock — no polling needed.
    Requires PyObjC (bundled by PyInstaller when building on macOS).
    """
    from Foundation import NSRunLoop, NSDate
    from AppKit import NSWorkspace
    from Foundation import NSDistributedNotificationCenter

    logger.info("PyObjC available - NSWorkspace notifications active")

    class UnlockObserver:
        def screenDidUnlock_(self, notification):
            name = notification.name() or ""
            if "lock" in name.lower() and "unlock" not in name.lower():
                logger.info(f"Screen locked: {name}")
                return
            logger.info(f"Screen unlock event: {name}")
            threading.Thread(
                target=trigger_checkin,
                kwargs={"trigger": "screen_unlock"},
                daemon=True,
            ).start()

        def sessionBecameActive_(self, notification):
            logger.info(f"Session active: {notification.name()}")
            threading.Thread(
                target=trigger_checkin,
                kwargs={"trigger": "session_active"},
                daemon=True,
            ).start()

    observer = UnlockObserver()
    workspace = NSWorkspace.sharedWorkspace()
    nc  = workspace.notificationCenter()
    dnc = NSDistributedNotificationCenter.defaultCenter()

    nc.addObserver_selector_name_object_(
        observer, "sessionBecameActive:",
        "NSWorkspaceSessionDidBecomeActiveNotification", None,
    )

    for event in [
        "com.apple.screenIsUnlocked",
        "com.apple.screensaver.didstop",
        "com.apple.screenIsLocked",
    ]:
        dnc.addObserver_selector_name_object_suspensionBehavior_(
            observer,
            "screenDidUnlock:",
            event, None, 4,
        )
        logger.info(f"Registered for: {event}")

    logger.info("All NSWorkspace notifications registered - entering run loop")
    loop = NSRunLoop.currentRunLoop()
    while True:
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))


# ── CGSESSION FALLBACK ────────────────────────────────────────────────────────
def start_cgsession_watcher():
    """Fallback when PyObjC unavailable: poll ioreg every 5s for unlock."""
    logger.info("CGSession watcher started (polls every 5s)")
    was_locked = False

    while True:
        try:
            r = subprocess.run(
                ["bash", "-c",
                 "ioreg -n Root -d1 | grep CGSSessionScreenIsLocked"],
                capture_output=True, text=True, timeout=5,
            )
            is_locked = "Yes" in r.stdout
        except Exception:
            is_locked = False

        if was_locked and not is_locked:
            logger.info("CGSession: screen unlocked")
            threading.Thread(
                target=trigger_checkin,
                kwargs={"trigger": "screen_unlock_cgsession"},
                daemon=True,
            ).start()

        was_locked = is_locked
        time.sleep(5)


# ── FIRST-RUN SETUP ───────────────────────────────────────────────────────────
def first_run_setup():
    """
    Called automatically when the binary is run for the first time
    (launchd plist not yet installed).
    Writes config with baked server URL, installs launchd, runs first check-in.
    """
    print("")
    print("------------------------------------------")
    print("  Sky RTO Tracker - macOS Setup")
    print("------------------------------------------")
    print("")

    # Write config with baked defaults (checkin_core handles this on load_config,
    # but we do it explicitly here so the user can see the server URL)
    cfg = load_config()
    server = cfg.get("server_url", "")
    print(f"  Server  : {server}")
    print(f"  Binary  : {BINARY_PATH}")
    print(f"  Logs    : {CONFIG_DIR}")
    print("")

    # Install launchd agent
    print("  [1/2] Installing LaunchAgent (auto-starts on login + screen unlock)...")
    install_launchd(verbose=True)

    print("")
    print("  [2/2] Running first check-in...")
    print("        (Browser will open for registration if not yet registered)")
    print("")

    # Small delay to let launchd settle
    time.sleep(2)

    try:
        run_checkin(force=True)
    except Exception as e:
        logger.error(f"First-run check-in failed: {e}")
        print(f"  [WARN] Check-in error: {e}")
        print(f"         Check {CONFIG_DIR}/checkin.log for details")

    print("")
    print("------------------------------------------")
    print("  RTO Tracker installed!")
    print("")
    print("  Every screen unlock -> auto check-in")
    print("  No action needed from you daily.")
    print("")
    print(f"  Logs    : tail -f {AGENT_LOG}")
    print(f"  Force   : {BINARY_PATH.name} --force")
    print(f"  Reset   : {BINARY_PATH.name} --reset")
    print(f"  Retry   : {BINARY_PATH.name} --retry")
    print("------------------------------------------")
    print("")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Sky RTO Tracker Agent (macOS)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  rto-mac                 Run as background agent (normal mode)
  rto-mac --force         Force a fresh check-in right now
  rto-mac --reset         Clear all caches + force check-in
  rto-mac --retry         Retry any queued offline check-ins
  rto-mac --install       (Re)install the launchd auto-start agent
  rto-mac --uninstall     Remove the launchd auto-start agent
        """,
    )
    parser.add_argument("--force",     action="store_true",
                        help="Force a fresh check-in right now, bypass dedup")
    parser.add_argument("--reset",     action="store_true",
                        help="Clear all caches then force a fresh check-in")
    parser.add_argument("--retry",     action="store_true",
                        help="Retry any queued offline check-ins")
    parser.add_argument("--install",   action="store_true",
                        help="(Re)install the launchd LaunchAgent")
    parser.add_argument("--uninstall", action="store_true",
                        help="Remove the launchd LaunchAgent")
    args = parser.parse_args()

    # ── One-shot commands (exit after) ───────────────────────────────────────
    if args.uninstall:
        uninstall_launchd(verbose=True)
        sys.exit(0)

    if args.install:
        print("")
        print("  Reinstalling LaunchAgent...")
        ok = install_launchd(verbose=True)
        sys.exit(0 if ok else 1)

    if args.reset:
        print("")
        run_reset()              # clears caches, prints confirmation
        run_checkin(force=True)  # fresh check-in immediately after reset
        sys.exit(0)

    if args.retry:
        run_retry()
        sys.exit(0)

    if args.force:
        # Force a check-in and exit — useful for manual testing
        logger.info("--force: running forced check-in")
        run_checkin(force=True)
        sys.exit(0)

    # ── Background agent mode ────────────────────────────────────────────────
    logger.info(f"rto_agent_mac starting | binary={BINARY_PATH} | poll={POLL_INTERVAL}s")

    # First run: launchd not yet installed -> do full setup then enter loop
    if not is_launchd_installed():
        first_run_setup()
        # After setup the launchd agent is now running as a separate process.
        # Exit this foreground invocation — launchd takes over from here.
        logger.info("First-run setup complete - launchd agent is now running")
        sys.exit(0)

    # Normal agent loop (launched by launchd on every login / after crash)
    logger.info("Agent started by launchd - entering normal operation")

    # Run once at startup
    trigger_checkin(trigger="startup")

    # Start background periodic poller
    start_periodic_poller()

    # Start screen unlock listener (blocks forever in run loop)
    try:
        start_nsworkspace_listener()
    except ImportError:
        logger.warning("PyObjC not available - using CGSession fallback")
        start_cgsession_watcher()


if __name__ == "__main__":
    main()