"""
Shared plumbing used by pretty much every router: IP helpers, the role/auth
checks, the rate limiter, and the little app-settings file on disk. This
used to all live at the top of main.py, but once we split the routes into
their own files they all needed this stuff, so it made sense to give it a
home of its own.
"""
import json
import os
import hmac
import hashlib
import time as _time
import secrets
import logging
import pathlib as _pathlib
import json as _sjson

from fastapi import Request, HTTPException
from slowapi import Limiter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, date, timedelta

from database import Device, Role

logger = logging.getLogger("rto")

# ── App-wide settings (persisted to /app/data/app_settings.json) ─────────────
_SETTINGS_PATH = _pathlib.Path("/app/data/app_settings.json")
_APP_SETTINGS_DEFAULTS: dict = {
    "show_split_timestamps": True,   # show HH:MM in split-day labels for managers/admins
}

def _read_app_settings() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            return {**_APP_SETTINGS_DEFAULTS, **_sjson.loads(_SETTINGS_PATH.read_text())}
    except Exception:
        pass
    return dict(_APP_SETTINGS_DEFAULTS)

def _write_app_settings(data: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(_sjson.dumps(data, indent=2))

# -- SERVER-SIDE NOTIFIER ----------------------------------
# Set TEAMS_WEBHOOK env var on the server to enable push notifications
# for leave, overrides, and other server-side events.
_SERVER_WEBHOOK = os.environ.get("TEAMS_WEBHOOK") or None
_SERVER_URL     = os.environ.get("SERVER_URL", "http://10.132.176.3:9999")
try:
    from notifier import (
        notify_leave_applied, notify_override_applied,
        LEVEL_ALL,
    )
    NOTIFIER_AVAILABLE = bool(_SERVER_WEBHOOK)
    if not _SERVER_WEBHOOK:
        logger_temp = logging.getLogger("rto")
        logger_temp.warning("TEAMS_WEBHOOK env var not set - server notifications disabled")
except ImportError:
    NOTIFIER_AVAILABLE = False
    def notify_leave_applied(*a, **kw): pass
    def notify_override_applied(*a, **kw): pass

DEFAULT_TEAMS = [
    "Sky Mobile",
    "NSOS",
    "TSI",
    "DNE",
    "GNAI",
    "ACCESS",
    "ISP",
    "N2BI",
    "CDN",
    "Enterprise Networks"

]

def generate_api_token() -> str:
    return secrets.token_urlsafe(32)

# ── Token expiry / rotation ───────────────────────────────────────────────
# Tokens used to be issued once and live forever. That meant a leaked or
# misused token stayed valid indefinitely. TOKEN_TTL bounds that: a token
# stops working this many days after it was (re)issued, full stop, no
# matter how it's being used. TOKEN_RENEW_WINDOW is how early the client is
# expected to proactively rotate before that happens, so a well-behaved
# agent never actually hits the hard expiry in normal use — expiry is the
# backstop for tokens that got separated from a well-behaved agent.
TOKEN_TTL_DAYS          = 90
TOKEN_RENEW_WINDOW_DAYS = 14

def _utcnow() -> datetime:
    # Naive UTC on purpose — SQLite has no real timezone-aware datetime
    # type, so values round-trip through the DB as naive. Comparing a
    # timezone-aware "now" against a naive DB value throws; keeping
    # everything naive-but-UTC here avoids that mismatch everywhere a token
    # expiry gets checked.
    return datetime.utcnow()

def issue_token_expiry() -> tuple[datetime, datetime]:
    now = _utcnow()
    return now, now + timedelta(days=TOKEN_TTL_DAYS)

async def verify_device_auth(device: Device | None, token: str | None, db: AsyncSession) -> None:
    """
    The one place that decides "is this token good enough to act as this
    device" — used everywhere X-Device-Token shows up. Checks the token
    value itself, then whether it's still within its validity window.
    Devices registered before token expiry existed have no expiry set yet;
    rather than lock out the whole existing user base the moment this
    ships, the first time one of those older tokens is used successfully
    we quietly start its expiry clock from now. Every token gets a real
    expiry either way, just not a retroactive one that logs everyone out
    on deploy day.
    """
    if not token or not device or not device.api_token or not secrets.compare_digest(token, device.api_token):
        raise HTTPException(401, "Valid X-Device-Token header required.")
    if device.token_expires_at is not None and _utcnow() > device.token_expires_at:
        raise HTTPException(
            401,
            "Device token expired. Reconnect to the Sky network and let the "
            "agent renew its token, or re-run registration.",
            headers={"X-Token-Expired": "true"},
        )
    if device.token_issued_at is None or device.token_expires_at is None:
        device.token_issued_at, device.token_expires_at = issue_token_expiry()
        await db.commit()

# ── Request signing (HMAC) ────────────────────────────────────────────────
# Bearer-token auth alone doesn't prove a request's body wasn't tampered
# with in flight, and doesn't stop a captured request being replayed later.
# For /api/checkin specifically (the one endpoint whose whole job is
# recording "verified, high-confidence" signals) the agent signs each
# request with a key derived from its own device token, over the exact
# request body plus a timestamp and one-time nonce. This does NOT stop the
# device's legitimate owner from signing a request they fabricated
# themselves — nothing can, short of hardware attestation — but it does
# stop a captured request from being edited or replayed by anyone who
# doesn't hold the token, which a bare bearer token alone doesn't.
SIGNATURE_FRESHNESS_SECONDS = 300  # 5 minutes — generous for clock drift, tight enough to bound replay

_seen_nonces: dict[str, float] = {}  # "hostname:nonce" -> expiry epoch, purged lazily

def _purge_expired_nonces(now: float) -> None:
    expired = [k for k, exp in _seen_nonces.items() if exp < now]
    for k in expired:
        _seen_nonces.pop(k, None)

def verify_request_signature(request: Request, raw_body: bytes, device: Device) -> None:
    sig       = request.headers.get("X-Signature")
    ts_header = request.headers.get("X-Timestamp")
    nonce     = request.headers.get("X-Nonce")
    if not sig or not ts_header or not nonce:
        raise HTTPException(401, "Request signature required.")
    try:
        ts = int(ts_header)
    except ValueError:
        raise HTTPException(401, "Invalid request timestamp.")

    now = _time.time()
    if abs(now - ts) > SIGNATURE_FRESHNESS_SECONDS:
        raise HTTPException(401, "Request timestamp outside allowed window — check the agent's clock.")

    _purge_expired_nonces(now)
    nonce_key = f"{device.hostname}:{nonce}"
    if nonce_key in _seen_nonces:
        raise HTTPException(401, "Request already used (replay detected).")

    message  = f"{ts_header}.{nonce}.".encode() + raw_body
    expected = hmac.new(device.api_token.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "Invalid request signature.")

    _seen_nonces[nonce_key] = now + SIGNATURE_FRESHNESS_SECONDS

# ── Rate limiting ──────────────────────────────────────────────────────────
# Protects credential-issuance and self-declared-attendance endpoints from
# enumeration/abuse. Keyed by the SAME client-IP resolution the rest of the
# app already uses (get_client_ip, defined below — respects X-Forwarded-For)
# rather than slowapi's default get_remote_address, which only reads
# request.client.host and would disagree with the app's own IP-range checks
# if this ever sits behind a reverse proxy.
def _limiter_key(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

limiter = Limiter(key_func=_limiter_key, key_style="endpoint")

def get_client_ip(r: Request) -> str:
    xff = r.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else r.client.host

def is_sky_network(client_ip: str) -> bool:
    """Same "are you actually on our network" check several pre-auth
    endpoints use (registration, token issuance, nonce creation...) — pulled
    out to one place instead of copy-pasted at each call site, so the
    trusted-IP definition can't quietly drift between them."""
    return client_ip.startswith("10.") or client_ip in ("127.0.0.1", "::1", "172.17.0.1")

def today_str() -> str:
    return date.today().isoformat()

def is_weekend(d: str) -> bool:
    return datetime.strptime(d, "%Y-%m-%d").weekday() >= 5

# -- ROLE HELPERS ------------------------------------------
async def get_role(employee_id: str, db: AsyncSession) -> str:
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    return r.role if r else "employee"

def get_caller_id(request: Request) -> str | None:
    return request.headers.get("X-Employee-Id") or None

def get_device_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return request.headers.get("X-Device-Token") or None

async def get_managed_teams(employee_id: str, db: AsyncSession):
    """Returns list of teams this manager can see, or None meaning all teams.
    None  = admin or unset (sees all teams — backwards compatible).
    List  = restricted to those specific teams only."""
    if not employee_id: return None
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    if not r: return None                                    # no role row = employee default
    if r.role == "admin": return None                        # admin always sees all
    if not r.managed_teams: return None                      # null/empty = all access
    try:
        teams = json.loads(r.managed_teams)
        return teams if isinstance(teams, list) and len(teams) > 0 else None
    except Exception:
        return None

async def require_role(request: Request, db: AsyncSession,
                       minimum: str, caller_id: str | None = None) -> str:
    HIERARCHY = {"employee": 0, "manager": 1, "admin": 2}
    # Always use header identity — never trust caller_id from request body alone.
    # Body-supplied caller_id is only used as a hint; header token is the real auth.
    header_eid = get_caller_id(request)
    token      = get_device_token(request)
    if not header_eid or not token:
        raise HTTPException(401, "X-Employee-Id and X-Device-Token headers required.")
    q = await db.execute(select(Device).where(Device.employee_id == header_eid))
    device = q.scalars().first()
    await verify_device_auth(device, token, db)
    # If caller_id provided (e.g. from body), it must match the authenticated header identity
    if caller_id and caller_id != header_eid:
        raise HTTPException(403, "Caller ID mismatch — cannot act on behalf of another user.")
    role = await get_role(header_eid, db)
    if HIERARCHY.get(role, 0) < HIERARCHY.get(minimum, 1):
        raise HTTPException(403, f"Requires {minimum} role. Your role: {role}.")
    return header_eid

async def require_registered_caller(request: Request, db: AsyncSession):
    eid = get_caller_id(request)
    if not eid:
        raise HTTPException(401, "X-Employee-Id header required.")
    q = await db.execute(select(Device).where(Device.employee_id == eid))
    device = q.scalars().first()
    if not device:
        raise HTTPException(404, "Not registered")
    token = get_device_token(request)
    await verify_device_auth(device, token, db)
    return device

async def get_caller_context(request: Request, db: AsyncSession):
    device = await require_registered_caller(request, db)
    role = await get_role(device.employee_id, db)
    return device, role
