"""
config.py - the baked-in build secrets, on-disk config file, paths, logger,
and the notifier import. This is the one file inject_version.py writes into
at build time, so the three _BAKED_* placeholder lines below need to keep
their exact wording — the regex substitution in inject_version.py matches
on them directly.
"""

import sys, os, json, socket, platform, subprocess
import logging, time
from pathlib import Path
from datetime import datetime, timedelta

# ── HARDCODED DEFAULTS (baked into binary at build time) ────────────────────
# These placeholders are replaced by inject_version.py during GitHub Actions.
# The source code never contains real server URLs or webhook tokens.
_BAKED_SERVER_URL      = "__SERVER_URL__"       # injected from GitHub Secret
_BAKED_TEAMS_WEBHOOK   = "__TEAMS_WEBHOOK__"    # injected from GitHub Secret
_BAKED_NOTIFY_LEVEL    = "all"
_BAKED_GITHUB_REPO     = "m-sribalaji/sky-rto"  # used for auto-update checks
_BAKED_VERSION         = "0.0.0"                # injected at build time

# ── PATHS ───────────────────────────────────────────────────────────────────
CONFIG_DIR   = Path.home() / ".rto_tracker"
CONFIG_FILE  = CONFIG_DIR / "config.json"
LOG_FILE     = CONFIG_DIR / "checkin.log"
QUEUE_FILE   = CONFIG_DIR / "pending_queue.json"
LOCK_FILE    = CONFIG_DIR / ".checkin.lock"
MISSED_DAY_FILE = CONFIG_DIR / ".last_missed_check"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# notifier.py lives in ../shared (not duplicated in client/ anymore). In a
# PyInstaller binary it's bundled straight into the same directory as this
# file (see build.sh), so this only matters when running from source.
if not getattr(sys, "frozen", False):
    _shared_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared")
    if _shared_dir not in sys.path:
        sys.path.insert(0, os.path.abspath(_shared_dir))

from logging.handlers import RotatingFileHandler
_log_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)  # caps at ~20MB total (current + 3 rotated backups), rotates automatically
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger("rto_client")

IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"
# CREATE_NO_WINDOW suppresses console flash on Windows; 0 is a no-op on macOS
_NO_WIN: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ── NOTIFIER ─────────────────────────────────────────────────────────────────
# notifier.py is bundled into the binary by PyInstaller.
try:
    from notifier import (
        notify,
        notify_checkin_wfo, notify_checkin_wfh,
        notify_vpn_ambiguous, notify_server_unreachable,
        notify_missed_days, notify_queue_flushed,
        notify_queue_saved, notify_registration_needed,
        notify_registration_complete, post_employee_reply,
        LEVEL_ALL, LEVEL_IMPORTANT, LEVEL_ERRORS,
    )
    NOTIFIER_AVAILABLE = True
except ImportError:
    NOTIFIER_AVAILABLE = False
    LEVEL_ALL = LEVEL_IMPORTANT = LEVEL_ERRORS = "all"
    def notify(*a, **kw): pass
    def notify_checkin_wfo(*a, **kw): pass
    def notify_checkin_wfh(*a, **kw): pass
    def notify_vpn_ambiguous(*a, **kw): pass
    def notify_server_unreachable(*a, **kw): pass
    def notify_missed_days(*a, **kw): pass
    def notify_queue_flushed(*a, **kw): pass
    def notify_queue_saved(*a, **kw): pass
    def notify_registration_needed(*a, **kw): pass
    def notify_registration_complete(*a, **kw): pass
    def post_employee_reply(*a, **kw): return False
    logger.warning("[WARN] notifier not available - Teams notifications disabled")

# ── CONFIG ───────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "server_url":           _BAKED_SERVER_URL,
    "teams_webhook":        _BAKED_TEAMS_WEBHOOK,
    "teams_notify_level":   _BAKED_NOTIFY_LEVEL,
    "last_checkin_date":    None,
    "last_status":          None,
    "last_detected_class":  None,
    "last_reg_attempt_ts":  None,
    "last_reg_attempt_date": None,
    "last_token_recovery_ts": None,
    "_pending_reg_nonce":    None,
    "employee_id":          None,
    "device_token":         None,
    "token_expires_at":     None,  # ISO timestamp — when the server will stop honouring device_token
    "poll_interval_seconds": 300,
    # Set once native_signer successfully enrolls this device (base64
    # public key) — presence of this key is what tells api.py to sign
    # with the OS-protected private key instead of the legacy HMAC
    # device_token. See client/native_signer/README.md for build status.
    "native_public_key":    None,
}

# Rotate before the server-side expiry actually hits, so a well-behaved
# agent that's online regularly never gets caught by the hard cutoff — the
# hard cutoff (see TOKEN_TTL_DAYS on the server) is the backstop for tokens
# that got separated from a well-behaved agent, not the normal path.
TOKEN_RENEW_WINDOW_DAYS = 14

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            raw = CONFIG_FILE.read_bytes()
            if raw.startswith(b'\xef\xbb\xbf'):   # strip UTF-8 BOM (Windows PS)
                raw = raw[3:]
            data = json.loads(raw.decode('utf-8'))
            merged = {**DEFAULT_CONFIG, **data}
            # Always ensure baked URL is used if config has the placeholder
            if not merged.get("server_url") or merged["server_url"] == "http://YOUR_SERVER_IP:9999":
                merged["server_url"] = _BAKED_SERVER_URL
            return merged
        except Exception:
            pass
    # First run — write a clean config with baked defaults
    cfg = DEFAULT_CONFIG.copy()
    save_config(cfg)
    return cfg

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    # Restrict to owner read/write only — config.json holds the device_token,
    # which is a bearer credential. Default umask leaves it world-readable on
    # shared/multi-user machines. os.chmod is a no-op on Windows (NTFS ACLs
    # differ) but harmless there — Windows per-user profile dirs already
    # aren't readable by other standard users by default.
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass  # best-effort — don't let permission hardening break check-in

def get_hostname() -> str:
    return socket.gethostname().upper()

def get_platform() -> str:
    return "windows" if IS_WIN else "macos"

def _get_notify_cfg(cfg: dict) -> tuple:
    return (
        cfg.get("teams_webhook") or _BAKED_TEAMS_WEBHOOK or None,
        cfg.get("teams_notify_level", LEVEL_ALL) if NOTIFIER_AVAILABLE else LEVEL_ALL,
        cfg.get("server_url", _BAKED_SERVER_URL),
    )

def _get_auth_headers(cfg: dict) -> dict:
    headers = {}
    employee_id = cfg.get("employee_id")
    device_token = cfg.get("device_token")
    if employee_id:
        headers["X-Employee-Id"] = employee_id
    if device_token:
        headers["X-Device-Token"] = device_token
    return headers

def _sync_device_auth(server: str, hostname: str, cfg: dict) -> dict:
    """
    Sync device info from server. If token is missing (existing user migrating
    to token auth, or local config was lost/purged), call /api/token-refresh.

    /api/token-refresh only auto-issues a token when the device has never had
    one (server-side bootstrap case). If the server already has a token for
    this hostname (e.g. this machine had one before but config.json was lost,
    purged, or the disk was wiped), it correctly refuses with 403 rather than
    handing out the existing token to whoever asks — that's the intended
    security behaviour. In that case, fall back to full re-registration via
    the browser nonce flow, same as a never-registered device. This is
    throttled by last_reg_attempt_ts (1 hour) so it doesn't retry every poll
    cycle and doesn't spam the rate-limited endpoint.
    """
    # Deferred import — api.py imports logger/IS_WIN/etc from this module, so
    # importing it at module load time here would be circular. By call time
    # both modules are fully loaded, so this is safe.
    from .api import api_get, api_post, open_browser, _get_reg_url, _desktop_notify

    device = api_get(f"{server}/api/device/{hostname}")
    if device and device.get("registered"):
        changed = False
        if device.get("employee_id") and cfg.get("employee_id") != device.get("employee_id"):
            cfg["employee_id"] = device.get("employee_id")
            changed = True
        if device.get("employee_name") and cfg.get("employee_name") != device.get("employee_name"):
            cfg["employee_name"] = device.get("employee_name")
            changed = True
        # If we have no local token, try token-refresh (bootstrap case)
        if not cfg.get("device_token"):
            refresh, status = api_post(
                f"{server}/api/token-refresh/{hostname}", {}, return_status=True)
            if refresh and refresh.get("api_token"):
                cfg["device_token"] = refresh["api_token"]
                cfg["token_expires_at"] = refresh.get("token_expires_at")
                changed = True
                logger.info("[OK] Device token obtained via token-refresh")
            elif status == 403:
                # Server already has a token for this hostname but we don't
                # hold it locally — config was lost. Fall back to full
                # re-registration, throttled to avoid hammering the endpoint.
                # Uses its OWN timestamp field (last_token_recovery_ts), not
                # last_reg_attempt_ts — that field belongs to the separate
                # "never registered at all" flow in run_checkin(). Sharing it
                # caused this path to silently skip opening the browser if
                # the other flow had recently set it, even though no browser
                # had actually been opened for THIS reason.
                last_recovery_ts = cfg.get("last_token_recovery_ts") or 0
                hour_elapsed = (time.time() - float(last_recovery_ts)) > 3600
                if hour_elapsed:
                    logger.warning(
                        "[WARN] Token-refresh denied (device already has a "
                        "server-side token but local config was lost) - "
                        "opening re-registration page"
                    )
                    reg_url, nonce = _get_reg_url(server, hostname)
                    if NOTIFIER_AVAILABLE:
                        wh, lvl, _ = _get_notify_cfg(cfg)
                        notify_registration_needed(hostname, reg_url, webhook=wh, level=lvl)
                    else:
                        _desktop_notify("RTO Tracker",
                            "Device token was lost. Please re-register in the browser window.")
                    open_browser(reg_url)
                    cfg["last_token_recovery_ts"] = time.time()
                    changed = True
                    # Poll for the recovered token — the page auto-claims it
                    # server-side as soon as it loads (no form to fill in for
                    # recovery), so this should resolve within a few seconds.
                    if nonce:
                        for _ in range(12):  # up to ~60s
                            time.sleep(5)
                            status_resp = api_get(f"{server}/api/reg-nonce-status/{nonce}")
                            if status_resp and status_resp.get("ready") and status_resp.get("api_token"):
                                cfg["device_token"] = status_resp["api_token"]
                                cfg["token_expires_at"] = status_resp.get("token_expires_at")
                                logger.info("[OK] Token recovered via re-registration page")
                                break
                        else:
                            logger.info("Token recovery not confirmed within 60s - will retry next cycle")
                else:
                    logger.info(
                        "Token-refresh denied recently - re-registration "
                        "browser opened within the last hour, waiting"
                    )
        else:
            # We already have a token — check if it's due for a proactive
            # rotation before the server's hard expiry hits. A missing or
            # unparseable expires_at means an older token issued before
            # this feature existed; the server backfills those to a real
            # expiry on next use, so just let this pass quietly and pick up
            # a real value on the next sync rather than guessing.
            due_for_rotation = False
            expires_raw = cfg.get("token_expires_at")
            if expires_raw:
                try:
                    exp = datetime.strptime(expires_raw.replace("Z",""), "%Y-%m-%dT%H:%M:%S.%f") \
                          if "." in expires_raw else datetime.strptime(expires_raw.replace("Z",""), "%Y-%m-%dT%H:%M:%S")
                    due_for_rotation = (exp - datetime.utcnow()) < timedelta(days=TOKEN_RENEW_WINDOW_DAYS)
                except Exception:
                    pass
            if due_for_rotation:
                refresh, status = api_post(
                    f"{server}/api/token-refresh/{hostname}", {},
                    auth_headers=_get_auth_headers(cfg), return_status=True)
                if refresh and refresh.get("api_token"):
                    cfg["device_token"]     = refresh["api_token"]
                    cfg["token_expires_at"] = refresh.get("token_expires_at")
                    changed = True
                    logger.info(f"[OK] Device token rotated (was within {TOKEN_RENEW_WINDOW_DAYS}d of expiry)")
                else:
                    logger.warning(f"[WARN] Proactive token rotation failed (status={status}) - will retry next sync")
        if changed and cfg.get("device_token"):
            # Enroll under the native_signer public-key scheme if this
            # module is available (macOS-only, real-world tested; falls
            # back to a no-op everywhere else) and this device hasn't
            # already sent a public key. Runs once per device, right
            # after any change that means we have a fresh/valid
            # device_token — covers both a brand-new registration and an
            # already-registered device picking this up for the first
            # time after upgrading to a build that has it.
            _maybe_enroll_native_signer(server, hostname, cfg)
        if changed:
            save_config(cfg)
    return device

def _maybe_enroll_native_signer(server: str, hostname: str, cfg: dict) -> None:
    if cfg.get("native_public_key"):
        return
    try:
        import native_signer
    except ImportError:
        return  # not built into this binary (or non-macOS) - stays on HMAC, silently
    from .api import api_post
    try:
        try:
            pub_b64 = native_signer.generate_device_keypair()
        except RuntimeError:
            # Most likely errSecDuplicateItem — this device already has a
            # Keychain key from a previous run whose enrollment call
            # didn't reach the server (e.g. offline at the time). Reuse
            # the existing key instead of failing enrollment outright.
            pub_b64 = native_signer.get_public_key()
        resp = api_post(f"{server}/api/register", {
            "hostname": hostname,
            "employee_name": cfg.get("employee_name") or hostname,
            "employee_id": cfg.get("employee_id"),
            "team": cfg.get("team"),
            "platform": "macos",
            "public_key": pub_b64,
        }, auth_headers={"X-Device-Token": cfg["device_token"]})
        if resp and resp.get("public_key_enrolled"):
            cfg["native_public_key"] = pub_b64
            logger.info("[OK] Enrolled under native_signer (Keychain-protected key)")
        else:
            logger.warning(f"[WARN] native_signer enrollment call did not confirm: {resp}")
    except Exception as e:
        # Never let enrollment issues break the normal HMAC-token flow —
        # this is a pure upgrade attempt, not a required step.
        logger.warning(f"[WARN] native_signer enrollment failed, staying on HMAC: {e}")
