"""
RTO Tracker v2 — app entrypoint.

This used to be one 2400+ line file with every route, helper, and schema
crammed together. It got hard to review and easy to break by accident, so
we split it up: shared helpers live in deps.py, request schemas in
schemas.py, and each group of related endpoints got its own file under
routers/. This file just wires everything together — creates the FastAPI
app, sets up middleware, and mounts the routers. If you're looking for
actual endpoint logic, it's not here anymore.
"""
import os
import sys
import logging
from collections import defaultdict as _dd
import time as _t

# notifier.py lives in ../shared so it's shared with the client agent instead
# of being duplicated. Put it on the path before anything below imports it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared"))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import select

from database import init_db, get_db, AsyncSessionLocal, Device, TeamConfig
from config import APP_TITLE
from deps import limiter, generate_api_token, DEFAULT_TEAMS

from routers import auth, roles, checkin, leave, dashboard, insights, pages, admin_api, misc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rto")

app = FastAPI(
    title=APP_TITLE, version="2.0.0",
    docs_url=None, redoc_url=None, openapi_url=None,
)

# ── Rate limiting ──────────────────────────────────────────────────────────
# Protects credential-issuance and self-declared-attendance endpoints from
# enumeration/abuse. Keyed by the SAME client-IP resolution the rest of the
# app already uses (get_client_ip in deps.py — respects X-Forwarded-For)
# rather than slowapi's default get_remote_address, which only reads
# request.client.host and would disagree with the app's own IP-range checks
# if this ever sits behind a reverse proxy.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://10.132.176.3:9999", "http://localhost:9999"],
    allow_methods=["GET","POST","PUT","PATCH","DELETE"],
    allow_headers=["Content-Type","X-Employee-Id","X-Device-Token","Authorization"],
    expose_headers=["X-Employee-Id"],
)

_rl: dict = _dd(list)

# Paths that should never be rate-limited (static files, health)
_RL_EXEMPT = {"/health", "/api/version", "/", "/dashboard", "/admin"}
# Path prefixes exempt from rate limiting
_RL_EXEMPT_PREFIX = ("/static/",)

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # Block oversized requests
    cl = request.headers.get("content-length")
    if cl and int(cl) > 1_048_576:
        return JSONResponse(status_code=413, content={"detail": "Request too large"})

    path = request.url.path

    # Skip rate limiting for health/static/version endpoints
    if path not in _RL_EXEMPT and not any(path.startswith(p) for p in _RL_EXEMPT_PREFIX):
        ip  = request.client.host if request.client else "unknown"
        now = _t.time()
        # Rolling 60-second window
        _rl[ip] = [t for t in _rl[ip] if t > now - 60]

        # Limits based on real usage analysis for 200 employees:
        # - Browser dashboard fires ~8 parallel requests per refresh
        # - Refreshes every 60s = ~8 req/min steady state
        # - Manual navigation / page loads add ~20 more
        # - Agent: ~2 req/min (poll every 5min + health check)
        # - Admin/manager: heavier usage, bulk compliance loads
        # Limits are per-IP (per person), not global
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # Writes: agent polls ~2/min, browser actions ~5/min
            # Set to 60/min — well above normal, blocks actual abuse (600+/min)
            limit = 60
        else:
            # Reads: dashboard ~30/min active use, set to 300/min
            # This allows burst of 8 parallel + normal navigation
            limit = 300

        if len(_rl[ip]) >= limit:
            logger.warning(f"Rate limit hit: {ip} {request.method} {path} ({len(_rl[ip])}/min)")
            return JSONResponse(status_code=429,
                                content={"detail": "Too many requests. Please slow down."})
        _rl[ip].append(now)

    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.on_event("startup")
async def startup():
    await init_db()
    try:
        async with AsyncSessionLocal() as db:
            # Get existing team configs
            q = await db.execute(select(TeamConfig))
            existing = {t.name for t in q.scalars().all()}
            added = 0
            tokens_added = 0
            # Seed defaults if none exist
            if not existing:
                for t in DEFAULT_TEAMS:
                    db.add(TeamConfig(name=t, created_by="system"))
                    added += 1
            # Also import any team names already used by registered devices
            dq = await db.execute(select(Device))
            for device in dq.scalars().all():
                if device.team and device.team not in existing:
                    db.add(TeamConfig(name=device.team, created_by="system"))
                    existing.add(device.team)
                    added += 1
                if not device.api_token:
                    device.api_token = generate_api_token()
                    tokens_added += 1
            if added or tokens_added:
                await db.commit()
                logger.info(f"[OK] Startup sync: added {added} teams, generated {tokens_added} device tokens")
    except Exception as e:
        logger.error(f"[FAIL] Team seed error: {e}")
    logger.info("[OK] RTO Tracker v2 started")


# ── Routers ────────────────────────────────────────────────────────────────
# Every route already carries its full path in its own decorator, so none
# of these need a prefix here.
app.include_router(auth.router)
app.include_router(roles.router)
app.include_router(checkin.router)
app.include_router(leave.router)
app.include_router(dashboard.router)
app.include_router(insights.router)
app.include_router(pages.router)
app.include_router(admin_api.router)
app.include_router(misc.router)

# -- STATIC FILES - must be mounted AFTER all routes -------
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
