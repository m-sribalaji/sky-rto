"""
Everything to do with a device proving who it is: registering a new
machine, looking up device/employee info, and the token handoff/nonce
dance that lets the desktop agent hand credentials to a browser tab
without ever putting a long-lived secret in a URL.

The in-memory dicts below (_handoff_tokens, _reg_nonces, _token_refresh_log)
are deliberately module-level state, not DB tables — they're short-lived,
one-time-use tokens and it's fine if a server restart wipes them.
_reg_nonces is also read by routers/pages.py (the /register/{hostname} page
checks it), so don't rename it without updating that import too.
"""
import secrets
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db, Device, Role, AnomalyLog
from deps import (
    get_client_ip, get_device_token, get_role, generate_api_token, limiter,
)
from schemas import RegisterPayload

router = APIRouter()
logger = logging.getLogger("rto")

# One-time auth handoff tokens — agent deposits these, browser consumes them once
_handoff_tokens: dict = {}  # token -> {employee_id, hostname, api_token, expires}
_reg_nonces: dict     = {}  # nonce -> {hostname, expires} — one-time registration tokens
_token_refresh_log: dict = {}  # source_ip -> [(hostname, timestamp)] — enumeration detection

# -- 1. REGISTER -------------------------------------------
@router.post("/api/register")
async def register(p: RegisterPayload, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1", "::1", "172.17.0.1")):
        raise HTTPException(403, "Registration only allowed from Sky network.")
    existing = await db.get(Device, p.hostname)
    if existing:
        existing.employee_name=p.employee_name; existing.employee_id=p.employee_id
        existing.team=p.team; existing.platform=p.platform
        if not existing.api_token:
            existing.api_token = generate_api_token()
        await db.commit()
        return {"status": "updated", "hostname": p.hostname,
                "employee_id": existing.employee_id, "api_token": existing.api_token}
    token = generate_api_token()
    db.add(Device(hostname=p.hostname, employee_name=p.employee_name,
                  employee_id=p.employee_id, team=p.team, platform=p.platform,
                  api_token=token))
    await db.commit()
    # Stash the token against the nonce that gated this page load, so the
    # polling agent can retrieve it via /api/reg-nonce-status/{nonce}
    # instead of the old (broken) approach of expecting /api/device to
    # return it — that endpoint deliberately never does.
    nonce = p.nonce
    if nonce and nonce in _reg_nonces:
        _reg_nonces[nonce]["claimed_token"] = token
    # First registered user -> admin
    q = await db.execute(select(Role))
    if not q.scalars().first():
        db.add(Role(employee_id=p.employee_id, role="admin", assigned_by="system"))
        await db.commit()
        logger.info(f"Auto-assigned admin to first user: {p.employee_id}")
    logger.info(f"Registered: {p.hostname} -> {p.employee_name}")
    return {"status": "registered", "hostname": p.hostname,
            "employee_id": p.employee_id, "api_token": token}

@router.get("/api/device/{hostname}")
async def get_device(hostname: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint - NEVER returns api_token. Token only returned at register/token-refresh."""
    d = await db.get(Device, hostname)
    if not d: return {"registered": False}
    role = await get_role(d.employee_id, db)
    return {"registered": True, "employee_name": d.employee_name,
            "employee_id": d.employee_id, "team": d.team, "role": role}

@router.get("/api/device-by-employee/{employee_id}")
async def get_device_by_employee(employee_id: str, db: AsyncSession = Depends(get_db)):
    """Returns hostname for an employee ID — used by UI for token-refresh lookup.
    Never returns api_token."""
    q = await db.execute(select(Device).where(Device.employee_id == employee_id))
    d = q.scalars().first()
    if not d: return {"found": False}
    return {"found": True, "hostname": d.hostname, "employee_name": d.employee_name}

@router.post("/api/auth-handoff")
async def create_auth_handoff(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Agent calls this after getting its device token.
    Returns a one-time handoff token the agent embeds in the dashboard URL.
    Browser reads it once, populates localStorage, token is then deleted.
    Only callable from Sky VPN.
    """
    import time as _time
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        raise HTTPException(403, "Only accessible on Sky network.")
    token = get_device_token(request)
    if not token:
        raise HTTPException(401, "X-Device-Token required.")
    # Verify token
    q = await db.execute(select(Device).where(Device.api_token == token))
    device = q.scalars().first()
    if not device:
        raise HTTPException(401, "Invalid token.")
    # Generate one-time handoff token
    handoff = secrets.token_urlsafe(16)
    _handoff_tokens[handoff] = {
        "employee_id": device.employee_id,
        "hostname":    device.hostname,
        "api_token":   device.api_token,
        "expires":     _time.time() + 60,  # expires in 60 seconds
    }
    return {"handoff": handoff}

@router.get("/api/auth-handoff/{token}")
async def consume_auth_handoff(token: str):
    """
    Browser calls this once with the handoff token from the URL.
    Returns credentials and immediately deletes the token.
    """
    import time as _time
    data = _handoff_tokens.pop(token, None)
    if not data:
        raise HTTPException(404, "Invalid or expired handoff token.")
    if _time.time() > data["expires"]:
        raise HTTPException(410, "Handoff token expired.")
    return {
        "employee_id": data["employee_id"],
        "hostname":    data["hostname"],
        "api_token":   data["api_token"],
    }

@router.post("/api/token-refresh/{hostname}")
@limiter.limit("5/minute")
async def token_refresh(hostname: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Issues a token for existing registered devices that predate token auth.
    Security:
      - Only callable from Sky VPN (10.x.x.x) — network-level trust boundary,
        same as before.
      - Rate-limited to 5/minute per source IP (slowapi) — blocks brute-force
        hostname enumeration from a single caller.
      - Only issues a NEW token when one doesn't already exist (legacy
        bootstrap case). If a token already exists for this hostname, the
        caller must already present it via X-Device-Token — closes the gap
        where anyone on VPN could pull any already-registered device's
        token just by knowing/guessing its hostname.
      - Distinct-hostname requests per source IP are tracked in a rolling
        5-minute window; 5 or more distinct hostnames from one IP is logged
        as a token_enumeration anomaly for admin visibility. (Threshold is
        deliberately AT the rate-limit ceiling of 5/minute, not above it —
        a caller who exhausts their entire per-minute budget scanning
        different hostnames is exhibiting the enumeration pattern by
        definition; requiring more than 5 is unreachable since request 6
        is blocked by the rate limiter before it can be counted here.)
    """
    import time as _time
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        raise HTTPException(403, "Only accessible on Sky network.")

    # Enumeration detection: track distinct hostnames requested per source IP
    now = _time.time()
    window_start = now - 300  # 5-minute window
    log = _token_refresh_log.setdefault(client_ip, [])
    log[:] = [(h, t) for (h, t) in log if t > window_start]
    log.append((hostname, now))
    distinct_hosts = {h for h, _ in log}
    if len(distinct_hosts) >= 5:
        logger.warning(
            f"[SECURITY] Possible token enumeration from {client_ip}: "
            f"{len(distinct_hosts)} distinct hostnames requested in 5 min "
            f"(latest: {hostname})"
        )
        db.add(AnomalyLog(
            employee_id="unknown", employee_name=f"IP {client_ip}",
            anomaly_type="token_enumeration",
            description=f"{len(distinct_hosts)} distinct hostnames requested "
                        f"from {client_ip} within 5 minutes via token-refresh.",
            severity="high",
        ))
        await db.commit()

    d = await db.get(Device, hostname)
    if not d:
        raise HTTPException(404, "Device not registered.")

    if not d.api_token:
        # Legacy bootstrap: device has never had a token. Issue one — this
        # is the intended one-time use case for this endpoint.
        d.api_token = generate_api_token()
        await db.commit()
        logger.info(f"Token issued for existing device (bootstrap): {hostname} from {client_ip}")
        return {"api_token": d.api_token, "employee_id": d.employee_id,
                "employee_name": d.employee_name}

    # Device already has a token — caller must prove they already hold it.
    presented = get_device_token(request)
    if not presented or not secrets.compare_digest(presented, d.api_token):
        logger.warning(
            f"[SECURITY] token-refresh denied for {hostname}: caller at "
            f"{client_ip} did not present the existing valid token."
        )
        raise HTTPException(
            403,
            "Device already has a token. The existing token must be "
            "presented to refresh — contact an admin if it was lost."
        )
    return {"api_token": d.api_token, "employee_id": d.employee_id,
            "employee_name": d.employee_name}

@router.post("/api/reg-nonce/{hostname}")
async def create_reg_nonce(hostname: str, request: Request,
                           db: AsyncSession = Depends(get_db)):
    """
    Agent calls this to get a one-time nonce before opening the registration
    URL. Only callable from Sky VPN. Nonce expires in 5 minutes.
    The register/recovery page requires this nonce in the ?nonce= query
    param — prevents anyone from opening the registration URL directly.

    Also used for lost-token recovery: if the device already exists, a nonce
    is still issued (recovery=True) — the nonce still proves this specific
    agent process, on this VPN, initiated the flow, which is exactly the
    same trust property as new registration.
    """
    import time as _tn
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        raise HTTPException(403, "Only accessible on Sky network.")
    existing = await db.get(Device, hostname)
    nonce = secrets.token_urlsafe(24)
    _reg_nonces[nonce] = {
        "hostname": hostname, "expires": _tn.time() + 300,
        "recovery": bool(existing), "claimed_token": None,
    }
    logger.info(
        f"{'Recovery' if existing else 'Registration'} nonce created for {hostname}")
    return {"nonce": nonce, "recovery": bool(existing)}

@router.get("/api/reg-nonce-status/{nonce}")
@limiter.limit("30/minute")
async def reg_nonce_status(nonce: str, request: Request):
    """
    Agent polls this with its nonce after opening the registration/recovery
    page. Returns the device token once the browser-side action (either the
    registration form submit, or the recovery page's auto-claim) has
    completed and stashed it against this nonce. One-time consumption:
    the token is popped from the nonce record on successful read, so a
    stolen/leaked nonce can't be replayed to re-read the token later
    (though by then it's expired within 5 minutes regardless).
    """
    import time as _tn
    data = _reg_nonces.get(nonce)
    if not data:
        raise HTTPException(404, "Invalid or expired nonce.")
    if _tn.time() > data["expires"]:
        _reg_nonces.pop(nonce, None)
        raise HTTPException(410, "Nonce expired.")
    if not data.get("claimed_token"):
        return {"ready": False}
    token = data.pop("claimed_token")
    return {"ready": True, "api_token": token}
