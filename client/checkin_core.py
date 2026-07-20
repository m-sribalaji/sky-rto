#!/usr/bin/env python3
"""
checkin_core.py - RTO Tracker core logic (importable module)

This module contains ALL check-in logic extracted from checkin.py so it can
be imported directly by rto_agent_mac and rto_agent_win inside a compiled
binary — no subprocess, no external Python needed.

The original checkin.py is kept unchanged for backwards compatibility with
anyone still running the script-based setup.

PUBLIC API (called by agents):
    run_checkin(force=False)
    run_reset()
    run_retry()

HARDCODED CONFIG:
    SERVER_URL  — baked in at build time (no user prompt needed)
    TEAMS_WEBHOOK / TEAMS_NOTIFY_LEVEL — same
"""

import sys, os, json, socket, platform, subprocess
import logging, webbrowser, time, random
import urllib.request, urllib.error
from pathlib import Path
from datetime import date, datetime

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
        notify_registration_complete,
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
    "employee_id":          None,
    "device_token":         None,
    "poll_interval_seconds": 300,
}

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
                changed = True
                logger.info("[OK] Device token obtained via token-refresh")
            elif status == 403:
                # Server already has a token for this hostname but we don't
                # hold it locally — config was lost. Fall back to full
                # re-registration, throttled to avoid hammering the endpoint.
                last_reg_ts  = cfg.get("last_reg_attempt_ts") or 0
                hour_elapsed = (time.time() - float(last_reg_ts)) > 3600
                if hour_elapsed:
                    logger.warning(
                        "[WARN] Token-refresh denied (device already has a "
                        "server-side token but local config was lost) - "
                        "opening re-registration page"
                    )
                    reg_url = _get_reg_url(server, hostname)
                    if NOTIFIER_AVAILABLE:
                        wh, lvl, _ = _get_notify_cfg(cfg)
                        notify_registration_needed(hostname, reg_url, webhook=wh, level=lvl)
                    else:
                        _desktop_notify("RTO Tracker",
                            "Device token was lost. Please re-register in the browser window.")
                    open_browser(reg_url)
                    cfg["last_reg_attempt_ts"] = time.time()
                    changed = True
                else:
                    logger.info(
                        "Token-refresh denied recently - re-registration "
                        "browser opened within the last hour, waiting"
                    )
        if changed:
            save_config(cfg)
    return device

# ── NETWORK SIGNAL COLLECTION ────────────────────────────────────────────────
import ipaddress as _ipaddress

def _nets(cidrs):
    nets = []
    for c in cidrs:
        try: nets.append(_ipaddress.ip_network(c, strict=False))
        except ValueError: pass
    return nets

def _ip_in(ip, nets):
    if not ip: return False
    try:
        addr = _ipaddress.ip_address(ip.strip().split("%")[0])
        return any(addr in n for n in nets)
    except ValueError: return False

_OFFICE_LAN_NETS = _nets(["10.126.0.0/16"])
_OFFICE_DNS_NETS = _nets(["10.126.63.0/24", "10.5.0.0/16", "10.20.0.0/16"])
_OFFICE_DNS_DOMS = ("bskyb.com", "sssl.bskyb.com")
_VPN_TUNNEL_NETS = _nets(["10.109.0.0/16", "10.23.0.0/16", "10.8.0.0/16"])
_HOME_LAN_NETS   = _nets(["192.168.0.0/16", "172.16.0.0/12"])
_ALL_PRIVATE     = _nets(["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"])

def get_lan_ip():
    if IS_WIN:
        try:
            flags = _NO_WIN
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5, creationflags=flags
            ).stdout
            for line in out.splitlines():
                s = line.strip()
                if "IPv4 Address" in s and ":" in s:
                    ip = s.split(":")[-1].strip().rstrip("(Preferred)").strip()
                    if ip and not _ip_in(ip, _VPN_TUNNEL_NETS):
                        return ip
        except Exception:
            pass
    else:
        try:
            for iface in ["en0", "en1", "en2", "en3"]:
                r = subprocess.run(["ipconfig", "getifaddr", iface],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    ip = r.stdout.strip()
                    if not _ip_in(ip, _VPN_TUNNEL_NETS):
                        return ip
        except Exception:
            pass
    # UDP route probe fallback
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not _ip_in(ip, _VPN_TUNNEL_NETS):
            return ip
    except Exception:
        pass
    return None

def get_vpn_tunnel_ip():
    if IS_MAC: return _vpn_macos()
    if IS_WIN: return _vpn_windows()
    return None

def _vpn_macos():
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        current, ifaces = None, {}
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("utun") and "flags" in s:
                current = s.split(":")[0]; ifaces[current] = None
            if current and current in ifaces:
                if s.startswith("inet ") and "-->" in s:
                    ifaces[current] = s.split()[1]
                elif s.startswith("inet ") and "netmask" in s:
                    ifaces[current] = s.split()[1]
        for ip in ifaces.values():
            if ip and not ip.startswith("fe80") and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None

def _vpn_windows():
    try:
        flags = _NO_WIN
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             timeout=5, creationflags=flags).stdout
        VPN_KEYWORDS = [
            "Cisco AnyConnect", "AnyConnect", "Cisco Secure",
            "Cisco VPN", "VPN Adapter", "Virtual Private",
            "Tunnel Adapter", "PPP Adapter",
        ]
        in_vpn = False; current_ip = None
        for line in out.splitlines():
            stripped = line.strip()
            if line and not line.startswith(" ") and not line.startswith("\t"):
                in_vpn = any(k.lower() in line.lower() for k in VPN_KEYWORDS)
                current_ip = None
            elif in_vpn and "IPv4 Address" in line and ":" in line:
                ip = line.split(":")[-1].strip().rstrip("(Preferred)").strip()
                if ip: current_ip = ip
            elif in_vpn and stripped == "" and current_ip:
                return current_ip
        if in_vpn and current_ip:
            return current_ip
        # Fallback: scan all interfaces for IPs in VPN ranges
        VPN_NETS = [_ipaddress.ip_network(c, strict=False) for c in
                    ["10.109.0.0/16", "10.23.0.0/16", "10.8.0.0/16"]]
        for line in out.splitlines():
            if "IPv4 Address" in line and ":" in line:
                ip_str = line.split(":")[-1].strip().rstrip("(Preferred)").strip()
                try:
                    addr = _ipaddress.ip_address(ip_str)
                    if any(addr in net for net in VPN_NETS):
                        return ip_str
                except ValueError:
                    pass
    except Exception:
        pass
    return None

def get_dns_info():
    if IS_MAC: return _dns_macos()
    if IS_WIN: return _dns_windows()
    return [], []

def _dns_macos():
    servers, domains = [], []
    try:
        out = subprocess.run(["scutil", "--dns"],
            capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("resolver #2"): break
            if "nameserver[" in line and ":" in line:
                ns = line.split(":")[-1].strip()
                if ns and ns not in servers: servers.append(ns)
            if "search domain[" in line and ":" in line:
                d = line.split(":")[-1].strip()
                if d and d not in domains: domains.append(d)
    except Exception as e:
        logger.warning(f"[WARN] DNS detect failed: {e}")
    return servers, domains

def _dns_windows():
    servers, domains = [], []
    try:
        flags = _NO_WIN
        out = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True,
                             timeout=5, creationflags=flags).stdout
        for line in out.splitlines():
            s = line.strip()
            if "DNS Servers" in s and ":" in s:
                ip = s.split(":")[-1].strip()
                if ip and ip not in servers: servers.append(ip)
            elif "Connection-specific DNS Suffix" in s and ":" in s:
                d = s.split(":")[-1].strip()
                if d and d not in domains: domains.append(d)
    except Exception as e:
        logger.warning(f"[WARN] DNS detect failed: {e}")
    return servers, domains

def get_is_ethernet():
    if IS_MAC: return _eth_macos()
    if IS_WIN: return _eth_windows()
    return False

def _eth_macos():
    try:
        for iface in ["en1", "en2", "en3", "eth0"]:
            r = subprocess.run(["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                ip = r.stdout.strip()
                if not _ip_in(ip, _VPN_TUNNEL_NETS):
                    return True
    except Exception:
        pass
    return False

def _eth_windows():
    try:
        flags = _NO_WIN
        out = subprocess.run(["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=5, creationflags=flags).stdout
        for line in out.splitlines():
            if "Connected" in line and ("Ethernet" in line or "Local Area" in line):
                return True
    except Exception:
        pass
    return False

# ── LOCAL CLASSIFIER ─────────────────────────────────────────────────────────
def _classify_lan(lan_ip):
    if not lan_ip: return "unknown"
    if _ip_in(lan_ip, _OFFICE_LAN_NETS):  return "office"
    if _ip_in(lan_ip, _VPN_TUNNEL_NETS):  return "vpn_tunnel"
    if _ip_in(lan_ip, _HOME_LAN_NETS):    return "home"
    if _ip_in(lan_ip, _ALL_PRIVATE):      return "private_unknown"
    return "unknown"

def _dns_is_office(dns_servers, dns_domains):
    for s in (dns_servers or []):
        if _ip_in(s, _OFFICE_DNS_NETS): return True
    for d in (dns_domains or []):
        if any(d.lower().endswith(od) for od in _OFFICE_DNS_DOMS): return True
    return False

def classify_locally(lan_ip, vpn_tunnel_ip, dns_servers, dns_domains, is_ethernet):
    vpn_active             = bool(vpn_tunnel_ip)
    lan_class              = _classify_lan(lan_ip)
    dns_office             = _dns_is_office(dns_servers, dns_domains)
    lan_is_office          = (lan_class == "office")
    lan_is_home            = (lan_class == "home")
    lan_is_private_unknown = (lan_class == "private_unknown")

    if lan_is_office: return "wfo"
    if dns_office:
        if lan_is_home or lan_is_private_unknown: return "wfh"
        return "wfo"
    if vpn_active:
        if lan_is_office or dns_office:  return "wfo"
        if lan_is_home:                  return "wfh"
        if lan_is_private_unknown:       return "wfh"
        return "vpn_ambiguous"
    if is_ethernet and not lan_is_home and not lan_is_private_unknown:
        return "wfo"
    if lan_is_home:            return "wfh"
    if lan_is_private_unknown: return "wfh"
    return "unknown"

# ── LOCATION CHANGE DETECTION ────────────────────────────────────────────────
def location_changed(cfg: dict, current_class: str) -> bool:
    last_status = cfg.get("last_status")
    today       = date.today().isoformat()

    if cfg.get("last_checkin_date") != today: return True
    if not last_status: return True
    if current_class == "vpn_ambiguous": return False
    if last_status == "wfh" and current_class == "wfo":
        logger.info("Location change: WFH -> WFO detected"); return True
    if last_status == "wfo" and current_class == "wfh":
        logger.info("Location change: WFO -> WFH detected"); return True
    if current_class == "unknown" and last_status in ("wfo", "wfh"):
        return False
    return False

# ── OFFLINE QUEUE ────────────────────────────────────────────────────────────
def _write_secure_file(path: Path, content: str):
    """Write text and lock down permissions to owner-only (0600).
    Used for any local file that could aid signal fabrication or credential
    theft if readable by other local users (pending_queue.json holds queued
    check-in payloads; config.json holds the device token)."""
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

def queue_checkin(payload: dict):
    from datetime import datetime as _dt
    date_str = payload.get("date", "")
    try:
        if _dt.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
            logger.info(f"Weekend date {date_str} - not queuing"); return
    except Exception: pass

    queue = []
    if QUEUE_FILE.exists():
        try: queue = json.loads(QUEUE_FILE.read_text())
        except Exception: queue = []
    queue = [q for q in queue if q.get("date") != payload.get("date")]
    payload["queued_at"] = datetime.utcnow().isoformat() + "Z"
    queue.append(payload)
    _write_secure_file(QUEUE_FILE, json.dumps(queue, indent=2))
    logger.info(f"Queued check-in for {payload.get('date')} (server unreachable)")

def flush_queue(server: str, cfg: dict = None) -> tuple:
    """Flush offline queue. Returns (count, [(payload, response)]) so caller
    can send per-record WFH/WFO Teams cards for each synced check-in."""
    cfg = cfg or load_config()
    if not QUEUE_FILE.exists(): return 0, []
    try: queue = json.loads(QUEUE_FILE.read_text())
    except Exception: return 0, []
    if not queue: return 0, []

    synced = []; failed = []; synced_results = []
    from datetime import datetime as _dt
    for payload in queue:
        date_str = payload.get("date", "")
        try:
            if _dt.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
                synced.append(payload); continue
        except Exception: pass
        resp = api_post(f"{server}/api/checkin", payload, auth_headers=_get_auth_headers(cfg))
        if resp and resp.get("action") in ("ok", "already_checked_in",
                                            "confirm_needed", "override_locked",
                                            "leave_recorded"):
            synced.append(payload)
            synced_results.append((payload, resp))
            logger.info(f"Flushed queued: {payload.get('date')} -> {resp.get('action')}")
        else:
            failed.append(payload)
            logger.warning(f"[WARN] Failed to flush: {payload.get('date')}")

    if failed: _write_secure_file(QUEUE_FILE, json.dumps(failed, indent=2))
    else: QUEUE_FILE.unlink(missing_ok=True)
    if synced: logger.info(f"Flushed {len(synced)} queued check-in(s)")
    return len(synced), synced_results

# ── API ──────────────────────────────────────────────────────────────────────
def api_post(url, payload, timeout=10, auth_headers: dict = None, return_status: bool = False):
    """
    POST JSON. Returns parsed response dict on success.
    If return_status=True, returns (response_or_None, http_status_or_None) instead —
    lets callers distinguish "denied" (403) from "unreachable" (timeout/network error).
    """
    try:
        data = json.dumps(payload).encode()
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if auth_headers:
            hdrs.update(auth_headers)
        req  = urllib.request.Request(url, data=data,
                   headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
            return (body, r.status) if return_status else body
    except urllib.error.HTTPError as e:
        logger.error(f"[FAIL] POST {url}: HTTP Error {e.code}: {e.reason}")
        return (None, e.code) if return_status else None
    except Exception as e:
        logger.error(f"[FAIL] POST {url}: {e}")
        return (None, None) if return_status else None

def api_get(url, timeout=10, auth_headers: dict = None):
    try:
        hdrs = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if auth_headers:
            hdrs.update(auth_headers)
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.error(f"[FAIL] GET {url}: {e}"); return None

def server_reachable(server: str) -> bool:
    return bool(api_get(f"{server}/health", timeout=5))

def open_browser(url: str):
    try:
        if IS_WIN:
            flags = _NO_WIN
            subprocess.Popen(["cmd", "/c", "start", "", url],
                             creationflags=flags, shell=False)
            logger.info(f"Browser opened via cmd start: {url}")
        else:
            webbrowser.open(url)
            logger.info(f"Browser opened: {url}")
    except Exception as e:
        logger.error(f"[FAIL] Browser failed: {e}")
        try: webbrowser.open(url)
        except Exception: pass

def _get_reg_url(server: str, hostname: str) -> str:
    """
    Get a nonce-protected registration URL.
    Calls /api/reg-nonce/{hostname} to get a one-time nonce,
    returns /register/{hostname}?nonce={nonce}.
    Prevents direct URL access to the registration page.
    Falls back to plain URL if nonce endpoint unavailable (old server).
    """
    try:
        resp = api_post(f"{server}/api/reg-nonce/{hostname}", {})
        if resp and resp.get("nonce"):
            nonce_url = f"{server}/register/{hostname}?nonce={resp['nonce']}"
            logger.info("[OK] Registration nonce obtained")
            return nonce_url
    except Exception as e:
        logger.debug(f"Could not get reg nonce: {e}")
    return f"{server}/register/{hostname}"

def _desktop_notify(title: str, message: str):
    try:
        if IS_MAC:
            subprocess.run(["osascript", "-e",
                f'display notification "{message}" with title "{title}"'],
                timeout=5, capture_output=True)
        elif IS_WIN:
            ps = (f'Add-Type -AssemblyName System.Windows.Forms;'
                  f'$n=New-Object System.Windows.Forms.NotifyIcon;'
                  f'$n.Icon=[System.Drawing.SystemIcons]::Information;'
                  f'$n.Visible=$true;$n.ShowBalloonTip(5000,"{title}","{message}",'
                  f'[System.Windows.Forms.ToolTipIcon]::Info)')
            flags = _NO_WIN
            subprocess.run(["powershell","-WindowStyle","Hidden","-Command",ps],
                           timeout=10, capture_output=True, creationflags=flags)
    except Exception as e:
        logger.warning(f"[WARN] Notification failed: {e}")

# ── LOCK ──────────────────────────────────────────────────────────────────────
def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, "a+")
        lock_fd.seek(0); lock_fd.truncate()
        lock_fd.write(str(os.getpid())); lock_fd.flush()
        if os.name == "nt":
            import msvcrt
            lock_fd.seek(0)
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_fd
    except (IOError, OSError, BlockingIOError):
        try:
            if lock_fd: lock_fd.close()
        except Exception: pass
        return None

def release_lock(lock_fd):
    if not lock_fd: return
    try:
        if os.name == "nt":
            import msvcrt
            try: lock_fd.seek(0); msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception: pass
        else:
            import fcntl
            try: fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception: pass
    except Exception: pass
    finally:
        try: lock_fd.close()
        except Exception: pass
        try: LOCK_FILE.unlink(missing_ok=True)
        except Exception: pass


# ── AUTO-UPDATE ───────────────────────────────────────────────────────────────
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
        import platform as _plat
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

# ── MISSED DAY CHECK ──────────────────────────────────────────────────────────
def check_missed_yesterday(server: str, hostname: str, cfg: dict, today: str):
    from datetime import timedelta
    try:
        last_checked = MISSED_DAY_FILE.read_text().strip() if MISSED_DAY_FILE.exists() else ""
        if last_checked == today: return
    except Exception: pass
    try: MISSED_DAY_FILE.write_text(today)
    except Exception: pass

    if not server_reachable(server): return
    device = _sync_device_auth(server, hostname, cfg)
    if not device or not device.get("registered"): return

    emp_id = device.get("employee_id", "")
    ph_dates = set()
    ph_data = api_get(f"{server}/api/holidays?year={today[:4]}")
    if ph_data:
        ph_dates = {h["date"] for h in ph_data.get("holidays", [])}

    days_to_check = []
    for i in range(7, 0, -1):
        dt = datetime.now() - timedelta(days=i)
        if dt.weekday() >= 5: continue
        ds = dt.strftime("%Y-%m-%d")
        if ds in ph_dates: continue
        if ds >= today: continue
        days_to_check.append(ds)

    if not days_to_check: return

    months_needed = list(dict.fromkeys(d[:7] for d in days_to_check))
    records_by_date = {}
    for month in months_needed:
        history = api_get(f"{server}/api/history/{emp_id}?month={month}",
                          auth_headers=_get_auth_headers(cfg))
        if history:
            for r in history.get("records", []):
                records_by_date[r["date"]] = r

    missing_to_prompt = []
    for day in days_to_check:
        if day in records_by_date: continue
        cached_payload = None
        try:
            if QUEUE_FILE.exists():
                queue_data = json.loads(QUEUE_FILE.read_text())
                cached_payload = next(
                    (q for q in queue_data if q.get("date") == day), None)
        except Exception: pass

        if cached_payload:
            local_class = classify_locally(
                cached_payload.get("lan_ip"),
                cached_payload.get("vpn_tunnel_ip"),
                cached_payload.get("dns_servers", []),
                cached_payload.get("dns_domains", []),
                cached_payload.get("is_ethernet", False),
            )
            if server_reachable(server):
                resp = api_post(f"{server}/api/checkin", cached_payload, auth_headers=_get_auth_headers(cfg))
                if resp and resp.get("action") in ("ok", "already_checked_in"):
                    logger.info(f"Soft miss auto-resolved: {day}")
                    try:
                        qd = json.loads(QUEUE_FILE.read_text())
                        qd = [q for q in qd if q.get("date") != day]
                        if qd: _write_secure_file(QUEUE_FILE, json.dumps(qd, indent=2))
                        else: QUEUE_FILE.unlink(missing_ok=True)
                    except Exception: pass
                    continue
            missing_to_prompt.append({
                "date": day, "cached_class": local_class,
                "cached_lan": cached_payload.get("lan_ip", ""),
            })
        else:
            missing_to_prompt.append({"date": day, "cached_class": "", "cached_lan": ""})

    if not missing_to_prompt:
        logger.info("Missed day check: all days auto-resolved"); return

    dates_str = ",".join(d["date"] for d in missing_to_prompt)
    missed_url = f"{server}/missed/{hostname}?dates={dates_str}"
    for d in missing_to_prompt:
        if d["cached_class"]: missed_url += f"&class_{d['date']}={d['cached_class']}"
        if d["cached_lan"]:   missed_url += f"&lan_{d['date']}={d['cached_lan']}"

    n = len(missing_to_prompt)
    if n == 1:
        _desktop_notify("RTO Tracker",
               f"No record for {missing_to_prompt[0]['date']}. Please fill in your attendance.")
    else:
        _desktop_notify("RTO Tracker",
               f"{n} days with no attendance record. Please fill them in.")

    logger.info(f"Opening bulk missed page for {n} day(s): {dates_str}")
    open_browser(missed_url)
    if NOTIFIER_AVAILABLE:
        wh, lvl, _ = _get_notify_cfg(cfg)
        notify_missed_days(cfg.get("employee_name", hostname),
                           [d["date"] for d in missing_to_prompt],
                           missed_url, webhook=wh, level=lvl)

# ── MAIN CHECK-IN ─────────────────────────────────────────────────────────────
def run_checkin(force: bool = False):
    cfg      = load_config()
    hostname = get_hostname()
    server   = cfg["server_url"].rstrip("/")
    today    = date.today().isoformat()

    logger.info(f"Check-in triggered | {hostname} | {today} | force={force}")

    lock = acquire_lock()
    if not lock:
        logger.info("Another instance is running - skipping"); return

    try:
        lan_ip               = get_lan_ip()
        vpn_tun              = get_vpn_tunnel_ip()
        dns_servers, dns_dom = get_dns_info()
        ethernet             = get_is_ethernet()
        local_class = classify_locally(lan_ip, vpn_tun, dns_servers, dns_dom, ethernet)

        logger.info(
            f"Signals: lan={lan_ip} vpn={vpn_tun} "
            f"dns={dns_servers} domains={dns_dom} eth={ethernet} "
            f"-> class={local_class}"
        )

        if server_reachable(server):
            _sync_device_auth(server, hostname, cfg)

        changed = location_changed(cfg, local_class)
        if not force and not changed:
            if QUEUE_FILE.exists() and server_reachable(server):
                flushed, flushed_results = flush_queue(server, cfg)
                if flushed > 0:
                    logger.info(f"Flushed {flushed} queued offline record(s)")
                    if NOTIFIER_AVAILABLE:
                        wh, lvl, srv = _get_notify_cfg(cfg)
                        emp_name = cfg.get("employee_name", hostname)
                        # Send individual check-in card per flushed record
                        for f_payload, f_resp in flushed_results:
                            if f_resp.get("action") == "ok":
                                f_status = f_resp.get("status","")
                                f_conf   = f_resp.get("confidence","")
                                if f_status == "wfo":
                                    notify_checkin_wfo(emp_name,
                                        f_payload.get("lan_ip"), f_conf,
                                        webhook=wh, level=lvl, server_url=srv)
                                elif f_status == "wfh":
                                    notify_checkin_wfh(emp_name,
                                        f_payload.get("lan_ip"), f_conf,
                                        vpn=bool(f_payload.get("vpn_tunnel_ip")),
                                        webhook=wh, level=lvl, server_url=srv)
                        # Summary card
                        notify_queue_flushed(emp_name, flushed,
                                             webhook=wh, level=lvl, server_url=srv)
            logger.info(f"Same location ({local_class}), same day - skipping"); return

        payload = {
            "hostname":      hostname,
            "lan_ip":        lan_ip,
            "vpn_tunnel_ip": vpn_tun,
            "ssid":          None,
            "is_ethernet":   ethernet,
            "dns_servers":   dns_servers,
            "dns_domains":   dns_dom,
            "platform":      get_platform(),
            "date":          today,
        }

        if not server_reachable(server):
            logger.warning(f"[WARN] Server unreachable at {server}")
            if local_class in ("wfo", "wfh"):
                queue_checkin(payload)
                cfg["last_checkin_date"]   = today
                cfg["last_status"]         = local_class
                cfg["last_detected_class"] = local_class
                save_config(cfg)
                # Desktop notification only when offline —
                # Teams webhook also needs internet so don't attempt it.
                # The proper WFH/WFO Teams card fires when VPN reconnects
                # and the queue is flushed.
                _desktop_notify("RTO Tracker",
                    f"Server offline. {local_class.upper()} check-in saved locally "
                    f"and will sync automatically when VPN connects.")
            else:
                _desktop_notify("RTO Tracker",
                       "Server unreachable. Connect Sky VPN for attendance tracking.")
            return

        flushed, flushed_results = flush_queue(server, cfg)
        if flushed > 0:
            logger.info(f"Flushed {flushed} offline record(s)")
            if NOTIFIER_AVAILABLE:
                wh, lvl, srv = _get_notify_cfg(cfg)
                emp_name = cfg.get("employee_name", hostname)
                # Send individual check-in card per flushed record
                for f_payload, f_resp in flushed_results:
                    if f_resp.get("action") == "ok":
                        f_status = f_resp.get("status","")
                        f_conf   = f_resp.get("confidence","")
                        if f_status == "wfo":
                            notify_checkin_wfo(emp_name,
                                f_payload.get("lan_ip"), f_conf,
                                webhook=wh, level=lvl, server_url=srv)
                        elif f_status == "wfh":
                            notify_checkin_wfh(emp_name,
                                f_payload.get("lan_ip"), f_conf,
                                vpn=bool(f_payload.get("vpn_tunnel_ip")),
                                webhook=wh, level=lvl, server_url=srv)
                # Summary card
                notify_queue_flushed(emp_name, flushed,
                                     webhook=wh, level=lvl, server_url=srv)

        device = _sync_device_auth(server, hostname, cfg)
        if not device or not device.get("registered"):
            logger.info("Not registered - opening registration page")
            last_reg_ts  = cfg.get("last_reg_attempt_ts") or 0
            now_ts       = time.time()
            hour_elapsed = (now_ts - float(last_reg_ts)) > 3600

            if hour_elapsed:
                reg_url = _get_reg_url(server, hostname)
                if NOTIFIER_AVAILABLE:
                    wh, lvl, _ = _get_notify_cfg(cfg)
                    notify_registration_needed(hostname, reg_url, webhook=wh, level=lvl)
                else:
                    _desktop_notify("RTO Tracker", "Please register your device.")
                open_browser(reg_url)
                cfg["last_reg_attempt_ts"] = now_ts
                save_config(cfg)
            else:
                logger.info("Registration browser opened recently - waiting")

            logger.info("Waiting up to 3 minutes for registration...")
            for attempt in range(36):
                time.sleep(5)
                check = api_get(f"{server}/api/device/{hostname}")
                if check and check.get("registered"):
                    emp_name = check.get("employee_name", hostname)
                    logger.info(f"Registration completed: {emp_name}")
                    cfg.pop("last_reg_attempt_ts", None)
                    cfg.pop("last_reg_attempt_date", None)
                    cfg["employee_name"] = emp_name
                    cfg["employee_id"] = check.get("employee_id")
                    cfg["device_token"] = check.get("api_token")
                    save_config(cfg)
                    if NOTIFIER_AVAILABLE:
                        wh, lvl, srv = _get_notify_cfg(cfg)
                        notify_registration_complete(
                            emp_name, hostname, check.get("team", ""),
                            srv, webhook=wh, level=lvl)
                    else:
                        _desktop_notify("RTO Tracker", f"Welcome, {emp_name}! Device registered.")
                    device = check; break
            else:
                logger.info("Registration not completed in 3 minutes - retry on next unlock")
                return

        # Weekend skip (attendance only — registration always runs above)
        from datetime import datetime as _dt
        if _dt.strptime(today, "%Y-%m-%d").weekday() >= 5:
            logger.info(f"Weekend ({today}) - skipping attendance check-in"); return

        # Missed day check (once per day)
        check_missed_yesterday(server, hostname, cfg, today)

        response = api_post(f"{server}/api/checkin", payload, auth_headers=_get_auth_headers(cfg))
        if not response:
            logger.error("[FAIL] Check-in POST failed - queuing for retry")
            if local_class in ("wfo", "wfh"):
                queue_checkin(payload)
            return

        action = response.get("action")
        logger.info(f"Server: action={action} detail={response.get('detail','')}")

        if action == "ok":
            status     = response.get("status")
            confidence = response.get("confidence", "")
            logger.info(f"[OK] Checked in: {status} (conf: {confidence})")
            cfg["last_checkin_date"]   = today
            cfg["last_status"]         = status
            cfg["last_detected_class"] = local_class
            if not cfg.get("employee_name"):
                dev_check = api_get(f"{server}/api/device/{hostname}")
                if dev_check and dev_check.get("employee_name"):
                    cfg["employee_name"] = dev_check["employee_name"]
                    cfg["employee_id"] = dev_check.get("employee_id")
                    cfg["device_token"] = dev_check.get("api_token")
            save_config(cfg)
            if NOTIFIER_AVAILABLE:
                wh, lvl, srv = _get_notify_cfg(cfg)
                emp_name = cfg.get("employee_name") or hostname
                if status == "wfo":
                    notify_checkin_wfo(emp_name, lan_ip, confidence,
                                       webhook=wh, level=lvl, server_url=srv)
                elif status == "wfh":
                    notify_checkin_wfh(emp_name, lan_ip, confidence,
                                       vpn=bool(vpn_tun),
                                       webhook=wh, level=lvl, server_url=srv)

        elif action == "already_checked_in":
            existing_status = response.get("status")
            if local_class in ("wfo", "wfh") and existing_status != local_class and force:
                force_payload = {**payload, "force_update": True}
                resp2 = api_post(f"{server}/api/checkin", force_payload, auth_headers=_get_auth_headers(cfg))
                if resp2 and resp2.get("action") == "ok":
                    status = resp2.get("status")
                    logger.info(f"[OK] Status updated: {status}")
                    cfg["last_checkin_date"]   = today
                    cfg["last_status"]         = status
                    cfg["last_detected_class"] = local_class
                    save_config(cfg); return
            logger.info(f"Already checked in today: {existing_status}")
            cfg["last_checkin_date"]   = today
            cfg["last_status"]         = existing_status
            cfg["last_detected_class"] = local_class
            save_config(cfg)

        elif action == "confirm_needed":
            logger.info("VPN ambiguous - opening confirmation page")
            open_browser(f"{server}/confirm/{hostname}")

        elif action == "register_first":
            open_browser(_get_reg_url(server, hostname))

        elif action == "override_locked":
            locked_status = response.get("status", "unknown")
            logger.info(f"Day locked as {locked_status} - check-in skipped")
            cfg["last_checkin_date"] = today
            cfg["last_status"]       = locked_status
            save_config(cfg)

        elif action == "leave_recorded":
            logger.info("Leave recorded for today - check-in skipped")
            cfg["last_checkin_date"] = today
            cfg["last_status"]       = "leave"
            save_config(cfg)

        elif action == "weekend_skip":
            logger.info("Weekend - server skipped")

        else:
            logger.warning(f"[WARN] Unknown action: {action}")

    finally:
        release_lock(lock)

# ── RESET ─────────────────────────────────────────────────────────────────────
def run_reset():
    """Clear ALL local caches then return (caller runs run_checkin after)."""
    cleared = []
    for f in [CONFIG_DIR / ".last_watcher_run",
              CONFIG_DIR / ".checkin.lock",
              CONFIG_DIR / ".last_missed_check"]:
        if f.exists():
            try: f.unlink(); cleared.append(f.name)
            except Exception: pass

    cfg = load_config()
    cfg["last_checkin_date"]   = None
    cfg["last_status"]         = None
    cfg["last_detected_class"] = None
    cfg.pop("last_reg_attempt_ts", None)
    cfg.pop("last_reg_attempt_date", None)
    save_config(cfg)
    cleared.append("config cache")
    logger.info(f"Reset complete - cleared: {', '.join(cleared)}")
    print(f"\n  [OK] Reset complete - cleared: {', '.join(cleared)}")
    print(f"  Running fresh check-in...\n")

# ── RETRY QUEUE ───────────────────────────────────────────────────────────────
def run_retry():
    cfg    = load_config()
    hostname = get_hostname()
    server = cfg["server_url"].rstrip("/")
    if not QUEUE_FILE.exists():
        print("  No queued check-ins to retry."); return
    try: queue = json.loads(QUEUE_FILE.read_text())
    except Exception: print("  [FAIL] Queue file unreadable."); return
    if not queue: print("  Queue is empty."); return

    print(f"\n  Found {len(queue)} queued check-in(s):")
    for q in queue:
        print(f"    {q.get('date')} - locally classified as "
              f"{classify_locally(q.get('lan_ip'), q.get('vpn_tunnel_ip'), q.get('dns_servers',[]), q.get('dns_domains',[]), q.get('is_ethernet',False))}")

    if not server_reachable(server):
        print(f"\n    [WARN] Server unreachable at {server}")
        print(f"  Connect VPN and try again.\n"); return

    _sync_device_auth(server, hostname, cfg)
    flushed, _ = flush_queue(server, cfg)
    print(f"\n    [OK] Synced {flushed} record(s) to server.")
    remaining = json.loads(QUEUE_FILE.read_text()) if QUEUE_FILE.exists() else []
    if remaining:
        print(f"    [FAIL] {len(remaining)} record(s) still failed - check logs.")
    print()