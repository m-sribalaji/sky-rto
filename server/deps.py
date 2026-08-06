"""
Shared plumbing used by pretty much every router: IP helpers, the role/auth
checks, the rate limiter, and the little app-settings file on disk. This
used to all live at the top of main.py, but once we split the routes into
their own files they all needed this stuff, so it made sense to give it a
home of its own.
"""
import json
import os
import secrets
import logging
import pathlib as _pathlib
import json as _sjson

from fastapi import Request, HTTPException
from slowapi import Limiter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, date

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
    if not device or not device.api_token or not secrets.compare_digest(token, device.api_token):
        raise HTTPException(401, "Invalid token.")
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
    if not token or not device.api_token or not secrets.compare_digest(token, device.api_token):
        raise HTTPException(401, "Valid X-Device-Token header required.")
    return device

async def get_caller_context(request: Request, db: AsyncSession):
    device = await require_registered_caller(request, db)
    role = await get_role(device.employee_id, db)
    return device, role
