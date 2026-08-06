"""
api.py - the actual HTTP calls to the RTO server (plain urllib, no requests
dependency to keep the binary small), plus the handful of "talk to the human"
helpers that go with them: opening a browser tab, firing a desktop toast,
building the nonce-protected registration URL. Grouped together because they
all boil down to "reach outside this process."
"""

import json
import time
import hmac
import hashlib
import secrets
import subprocess
import webbrowser
import urllib.request
import urllib.error

from .config import logger, IS_MAC, IS_WIN, _NO_WIN


def _sign_request(sign_key: str, body: bytes) -> dict:
    """
    Build the X-Signature/X-Timestamp/X-Nonce headers the server checks in
    deps.verify_request_signature — see that function's docstring for what
    this does and doesn't protect against. The important part here: the
    bytes we sign have to be byte-for-byte identical to what actually goes
    out over the wire, so this is computed from the exact same `data` the
    caller already serialized, not re-encoded from the payload dict (which
    could reorder keys and produce a different signature than the request
    the server actually receives).
    """
    ts    = str(int(time.time()))
    nonce = secrets.token_hex(8)
    message = f"{ts}.{nonce}.".encode() + body
    sig = hmac.new(sign_key.encode(), message, hashlib.sha256).hexdigest()
    return {"X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig}


def api_post(url, payload, timeout=10, auth_headers: dict = None, return_status: bool = False,
             sign_key: str = None):
    """
    POST JSON. Returns parsed response dict on success.
    If return_status=True, returns (response_or_None, http_status_or_None) instead —
    lets callers distinguish "denied" (403) from "unreachable" (timeout/network error).
    Pass sign_key (the device token) for endpoints that require a signed
    request — currently just /api/checkin. Leave it out for everything else;
    signing only makes sense once a device has a token to sign with, and
    only matters for the endpoint that produces "verified" attendance data.
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
        if sign_key:
            hdrs.update(_sign_request(sign_key, data))
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

def _get_reg_url(server: str, hostname: str) -> tuple:
    """
    Get a nonce-protected registration/recovery URL.
    Calls /api/reg-nonce/{hostname} to get a one-time nonce (now issued for
    BOTH new registration and lost-token recovery — server distinguishes
    internally), returns (url, nonce).
    The nonce is also used afterward to poll /api/reg-nonce-status/{nonce}
    for the resulting token, since neither /api/device nor the old
    "Already Registered" page ever exposed it.
    Falls back to (url_without_nonce, None) if the endpoint is unreachable
    (old server) — polling will then have no nonce to check and the caller
    should treat that as "can't auto-recover, manual registration only".
    """
    try:
        resp = api_post(f"{server}/api/reg-nonce/{hostname}", {})
        if resp and resp.get("nonce"):
            nonce = resp["nonce"]
            nonce_url = f"{server}/register/{hostname}?nonce={nonce}"
            logger.info("[OK] Registration nonce obtained")
            return nonce_url, nonce
    except Exception as e:
        logger.debug(f"Could not get reg nonce: {e}")
    return f"{server}/register/{hostname}", None

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
