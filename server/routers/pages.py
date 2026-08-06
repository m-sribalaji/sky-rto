"""
The handful of routes that return actual HTML instead of JSON: the
registration/confirm/missed-day pages the desktop agent opens in a
browser, plus the main dashboard and admin single-page apps. Kept apart
from the JSON API routers since these care about templates and static
files, not request/response schemas.
"""
import os
import json
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from database import get_db, Device, CheckIn, LeaveRequest
from deps import get_client_ip, today_str
from routers.auth import _reg_nonces

router = APIRouter()

_base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmpl_dir = os.path.join(_base_dir, "templates")
if not os.path.isdir(_tmpl_dir):
    _tmpl_dir = "/app/templates"
templates = Jinja2Templates(directory=_tmpl_dir)

# -- 11. HTML PAGES ----------------------------------------
@router.get("/register/{hostname}", response_class=HTMLResponse)
async def register_page(hostname: str, request: Request,
                        db: AsyncSession = Depends(get_db)):
    import time as _tr
    _deny = lambda msg: HTMLResponse(f"<html><body style='background:#111;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'><div style='text-align:center'><h2>Access Denied</h2><p>{msg}</p></div></body></html>", status_code=403)
    # VPN only
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        return _deny("Registration is only available on the Sky network.")

    # Require valid nonce for BOTH new registration and recovery — proves
    # this page load originated from the agent, not a guessed/direct URL.
    nonce = request.query_params.get("nonce", "")
    if not nonce:
        return _deny("Registration must be initiated from the RTO agent on your device.")
    nonce_data = _reg_nonces.get(nonce)
    if not nonce_data or nonce_data["hostname"] != hostname or _tr.time() > nonce_data["expires"]:
        _reg_nonces.pop(nonce, None)
        return _deny("Registration link has expired or is invalid. Run the agent again to get a new link.")

    existing = await db.get(Device, hostname)
    if existing:
        # Lost-token recovery: this hostname is already registered. The
        # valid nonce already proves this page load came from the agent on
        # this machine (same VPN-gated trust as new registration). Safe to
        # hand back the EXISTING token — does not rotate or affect any
        # other session using it. Stash it against the nonce so the
        # polling agent (GET /api/reg-nonce-status/{nonce}) can retrieve it.
        nonce_data["claimed_token"] = existing.api_token
        import logging
        logging.getLogger("rto").info(f"Recovery token claimed for {hostname} ({existing.employee_name})")
        return HTMLResponse(
            f"<html><body style='background:#111;color:#4ade80;font-family:sans-serif;"
            f"display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            f"<div style='text-align:center'><h2>Device Recovered</h2>"
            f"<p>Welcome back, <strong>{existing.employee_name}</strong>. "
            f"You can close this window — the app will finish automatically.</p></div>"
            f"</body></html>", status_code=200)

    tmpl = templates.get_template("register.html")
    html = tmpl.render(request=request, hostname=hostname)
    return HTMLResponse(html)


@router.get("/confirm/{hostname}", response_class=HTMLResponse)
async def vpn_confirm_page(hostname: str, request: Request,
                            db: AsyncSession = Depends(get_db)):
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        return HTMLResponse("<html><body style='background:#111;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'><div style='text-align:center'><h2>Access Restricted</h2><p>Only accessible on the Sky network.</p></div></body></html>", status_code=403)
    device  = await db.get(Device, hostname)
    name    = device.employee_name if device else hostname
    today   = today_str()
    emp_id  = device.employee_id if device else hostname
    q       = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == emp_id, CheckIn.date == today)))
    rec     = q.scalars().first()
    ctx = dict(request=request, hostname=hostname,
               first_name=name.split()[0], today=today,
               vpn_tunnel=rec.vpn_tunnel_ip if rec else "unknown",
               lan_ip=rec.lan_ip if rec else "unknown")
    html = templates.get_template("confirm.html").render(**ctx)
    return HTMLResponse(html)

@router.get("/missed/{hostname}", response_class=HTMLResponse)
async def missed_day_page(hostname: str, request: Request,
                           db: AsyncSession = Depends(get_db)):
    """
    Bulk missed-day page. Accepts comma-separated dates via ?dates=2026-05-19,2026-05-20
    or a single date via ?dates=2026-05-19 (backwards compat).
    Also accepts legacy /missed/{hostname}/{date} format via redirect.
    """
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        return HTMLResponse("<html><body style='background:#111;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'><div style='text-align:center'><h2>Access Restricted</h2><p>Only accessible on the Sky network.</p></div></body></html>", status_code=403)
    device = await db.get(Device, hostname)
    if not device: raise HTTPException(404, "Not registered")

    dates_param = request.query_params.get("dates", "")
    if not dates_param:
        return HTMLResponse("<p style='color:#888;font-family:monospace;padding:20px'>No dates specified.</p>")

    dates = [d.strip() for d in dates_param.split(",") if d.strip()]

    # Filter out dates that already have a record — prevents re-use
    # by refreshing or manually editing the URL.
    truly_missing = []
    for ds in sorted(dates):
        ci_q = await db.execute(select(CheckIn).where(and_(
            CheckIn.employee_id==device.employee_id, CheckIn.date==ds)))
        lv_q = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id==device.employee_id, LeaveRequest.date==ds)))
        if ci_q.scalars().first() or lv_q.scalars().first():
            continue
        truly_missing.append(ds)

    if not truly_missing:
        return HTMLResponse("<html><head><meta charset='UTF-8'><style>body{background:#0a0a0a;color:#ececec;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}.card{background:#111;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:32px 36px;max-width:420px;text-align:center}h2{font-size:18px;margin-bottom:8px}p{color:#888;font-size:13px;line-height:1.6}</style></head><body><div class='card'><h2>Already recorded</h2><p>Attendance has already been submitted for the requested date(s). You can close this tab.</p></div></body></html>")

    # Build missing_days list with nice labels
    missing_days = []
    for ds in truly_missing:
        try:
            nice = datetime.strptime(ds, "%Y-%m-%d").strftime("%A, %d %B %Y")
        except Exception:
            nice = ds
        cached_class = request.query_params.get(f"class_{ds}", "")
        cached_lan   = request.query_params.get(f"lan_{ds}", "")
        missing_days.append({
            "date":         ds,
            "nice_date":    nice,
            "cached_class": cached_class,
            "cached_lan":   cached_lan,
        })

    ctx = dict(
        request      = request,
        hostname     = hostname,
        first_name   = device.employee_name.split()[0],
        missing_days = missing_days,
        missing_days_json = json.dumps(missing_days),
    )
    html = templates.get_template("missed.html").render(**ctx)
    return HTMLResponse(html)

@router.get("/missed/{hostname}/{missed_date}", response_class=HTMLResponse)
async def missed_day_page_legacy(hostname: str, missed_date: str,
                                  request: Request):
    """Legacy single-date URL - redirects to bulk page."""
    client_ip = get_client_ip(request)
    if not (client_ip.startswith("10.") or client_ip in ("127.0.0.1","::1","172.17.0.1")):
        return HTMLResponse("<html><body style='background:#111;color:#f87171;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'><div style='text-align:center'><h2>Access Restricted</h2><p>Only accessible on the Sky network.</p></div></body></html>", status_code=403)
    cached = request.query_params.get("cached", "false")
    lan    = request.query_params.get("lan", "")
    cls    = request.query_params.get("class", "")
    url    = f"/missed/{hostname}?dates={missed_date}"
    if cls: url += f"&class_{missed_date}={cls}"
    if lan: url += f"&lan_{missed_date}={lan}"
    return RedirectResponse(url)

@router.get("/", response_class=FileResponse)
@router.get("/dashboard", response_class=FileResponse)
async def root():
    return FileResponse(os.path.join(_base_dir,"rto-ui.html"), media_type="text/html")

@router.get("/admin", response_class=FileResponse)
async def admin_page():
    return FileResponse(os.path.join(_base_dir,"admin.html"), media_type="text/html")
