import json
# main.py - RTO Tracker v2
import sys, os, json, logging, csv, io
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc, func
from pydantic import BaseModel
from datetime import datetime, date, timezone, timedelta
from typing import Optional

from database import (init_db, get_db, AsyncSessionLocal, Device, CheckIn, AnomalyLog,
                      DaySegment, LeaveRequest, PublicHoliday, Role, TeamConfig)
from detection import classify
from segments import (dominant_status_from_segments, handle_checkin, get_day_summary, get_all_segments,
                      get_open_segment, close_segment, open_new_segment,
                      get_leave_meta, LEAVE_TYPES)
from config import APP_TITLE, PORT

# -- SERVER-SIDE NOTIFIER ----------------------------------
# Set TEAMS_WEBHOOK env var on the server to enable push notifications
# for leave, overrides, and other server-side events.
_SERVER_WEBHOOK = os.environ.get("TEAMS_WEBHOOK") or None
_SERVER_URL     = os.environ.get("SERVER_URL", "http://10.131.80.141:8989")
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rto")

app = FastAPI(title=APP_TITLE, version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Employee-Id"])
_base_dir = os.path.dirname(os.path.abspath(__file__))
_tmpl_dir = os.path.join(_base_dir, "templates")
if not os.path.isdir(_tmpl_dir):
    _tmpl_dir = "/app/templates"
templates = Jinja2Templates(directory=_tmpl_dir)


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

@app.on_event("startup")
async def startup():
    await init_db()
    try:
        async with AsyncSessionLocal() as db:
            # Get existing team configs
            q = await db.execute(select(TeamConfig))
            existing = {t.name for t in q.scalars().all()}
            added = 0
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
            if added:
                await db.commit()
                logger.info(f"[OK] Team sync: added {added} teams")
    except Exception as e:
        logger.error(f"[FAIL] Team seed error: {e}")
    logger.info("[OK] RTO Tracker v2 started")

def get_client_ip(r: Request) -> str:
    xff = r.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else r.client.host

def today_str() -> str:
    return date.today().isoformat()

def is_weekend(d: str) -> bool:
    return datetime.strptime(d, "%Y-%m-%d").weekday() >= 5

# -- SCHEMAS ----------------------------------------------
class RegisterPayload(BaseModel):
    hostname: str; employee_name: str; employee_id: str
    team: Optional[str]=None; platform: Optional[str]=None

class CheckInPayload(BaseModel):
    hostname: str; lan_ip: Optional[str]=None; vpn_tunnel_ip: Optional[str]=None
    ssid: Optional[str]=None; is_ethernet: bool=False
    dns_servers: Optional[list]=None; dns_domains: Optional[list]=None
    platform: Optional[str]=None; date: Optional[str]=None
    force_update: bool=False; source: Optional[str]="auto_detected"
    queued_at: Optional[str]=None  # ISO timestamp from offline queue — used as started_at

class ConfirmPayload(BaseModel):
    hostname: str; declared_status: str

class OverridePayload(BaseModel):
    employee_id: str; date: str; new_status: str
    override_by: str; note: Optional[str]=None

class LeavePayload(BaseModel):
    employee_id: str; date: str; leave_type: str
    half_day_period: Optional[str]=None; note: Optional[str]=None
    applied_by: Optional[str]=None; source: Optional[str]="self"

class DeleteLeavePayload(BaseModel):
    employee_id: str; date: str

class PublicHolidayPayload(BaseModel):
    date: str; name: str; country: str="GB"
    region: Optional[str]=None; optional: bool=False

class MissedDayPayload(BaseModel):
    hostname: str; date: str; status: str
    leave_type: Optional[str]=None; source: str="missed_prompt_no_data"
    lan_ip: Optional[str]=None; dns_servers: Optional[list]=None
    dns_domains: Optional[list]=None; vpn_tunnel_ip: Optional[str]=None
    is_ethernet: bool=False; has_cached_data: bool=False

class RolePayload(BaseModel):
    employee_id: str; role: str; assigned_by: Optional[str]=None

# -- ROLE HELPERS ------------------------------------------
async def get_role(employee_id: str, db: AsyncSession) -> str:
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    return r.role if r else "employee"

def get_caller_id(request: Request) -> str | None:
    return request.headers.get("X-Employee-Id") or None

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
    eid = caller_id or get_caller_id(request)
    if not eid:
        raise HTTPException(403, "X-Employee-Id header required.")
    role = await get_role(eid, db)
    if HIERARCHY.get(role, 0) < HIERARCHY.get(minimum, 1):
        raise HTTPException(403, f"Requires {minimum} role. Your role: {role}.")
    return eid

# -- 1. REGISTER -------------------------------------------
@app.post("/api/register")
async def register(p: RegisterPayload, db: AsyncSession = Depends(get_db)):
    existing = await db.get(Device, p.hostname)
    if existing:
        existing.employee_name=p.employee_name; existing.employee_id=p.employee_id
        existing.team=p.team; existing.platform=p.platform
        await db.commit()
        return {"status": "updated", "hostname": p.hostname}
    db.add(Device(hostname=p.hostname, employee_name=p.employee_name,
                  employee_id=p.employee_id, team=p.team, platform=p.platform))
    await db.commit()
    # First registered user -> admin
    q = await db.execute(select(Role))
    if not q.scalars().first():
        db.add(Role(employee_id=p.employee_id, role="admin", assigned_by="system"))
        await db.commit()
        logger.info(f"Auto-assigned admin to first user: {p.employee_id}")
    logger.info(f"Registered: {p.hostname} -> {p.employee_name}")
    return {"status": "registered", "hostname": p.hostname}

@app.get("/api/device/{hostname}")
async def get_device(hostname: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Device, hostname)
    if not d: return {"registered": False}
    role = await get_role(d.employee_id, db)
    return {"registered": True, "employee_name": d.employee_name,
            "employee_id": d.employee_id, "team": d.team, "role": role}

# -- 2. ROLES ----------------------------------------------
@app.get("/api/roles")
async def get_roles(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(Role).order_by(Role.role))
    return {"roles": [{"employee_id": r.employee_id, "role": r.role,
                       "assigned_by": r.assigned_by,
                       "managed_teams": json.loads(r.managed_teams) if r.managed_teams else None,
                       "assigned_at": r.assigned_at.isoformat()+"Z"} for r in q.scalars().all()]}

@app.post("/api/roles")
async def set_role(p: RolePayload, request: Request,
                   db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin", caller_id=p.assigned_by)
    if p.role not in ("admin","manager","employee"):
        raise HTTPException(400, "role must be admin|manager|employee")
    dq = await db.execute(select(Device).where(Device.employee_id == p.employee_id))
    if not dq.scalars().first(): raise HTTPException(404, "Employee not found")
    q   = await db.execute(select(Role).where(Role.employee_id == p.employee_id))
    rec = q.scalars().first()
    if rec:
        rec.role=p.role; rec.assigned_by=p.assigned_by; rec.assigned_at=datetime.now(timezone.utc)
    else:
        db.add(Role(employee_id=p.employee_id, role=p.role, assigned_by=p.assigned_by))
    await db.commit()
    return {"status": "ok", "employee_id": p.employee_id, "role": p.role}

@app.get("/api/managed-teams/{employee_id}")
async def get_managed_teams_api(employee_id: str, request: Request,
                                db: AsyncSession=Depends(get_db)):
    """Get managed teams for an employee. Admin can get any, manager can get own."""
    caller_id = get_caller_id(request)
    caller_role = await get_role(caller_id, db) if caller_id else "employee"
    if caller_role == "employee":
        raise HTTPException(403, "Manager or admin role required")
    if caller_role == "manager" and caller_id != employee_id:
        raise HTTPException(403, "Managers can only view their own team access")
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    if not r: return {"employee_id": employee_id, "managed_teams": None}
    try:
        teams = json.loads(r.managed_teams) if r.managed_teams else None
    except Exception:
        teams = None
    return {"employee_id": employee_id, "role": r.role, "managed_teams": teams}

@app.put("/api/managed-teams/{employee_id}")
async def set_managed_teams_api(employee_id: str, request: Request,
                                db: AsyncSession=Depends(get_db)):
    """Set managed teams. Admin can update any, manager can update own only."""
    caller_id = get_caller_id(request)
    caller_role = await get_role(caller_id, db) if caller_id else "employee"
    if caller_role == "employee":
        raise HTTPException(403, "Manager or admin role required")
    if caller_role == "manager" and caller_id != employee_id:
        raise HTTPException(403, "Managers can only update their own team access")
    body = await request.json()
    managed = body.get("managed_teams")  # null = all access, list = restricted
    q = await db.execute(select(Role).where(Role.employee_id == employee_id))
    r = q.scalars().first()
    if not r: raise HTTPException(404, "Role not found — assign a role first")
    r.managed_teams = json.dumps(managed) if managed is not None else None
    await db.commit()
    return {"employee_id": employee_id, "managed_teams": managed}

@app.delete("/api/roles/{employee_id}")
async def remove_role(employee_id: str, request: Request,
                      db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(Role).where(Role.employee_id == employee_id))
    rec = q.scalars().first()
    if rec: await db.delete(rec); await db.commit()
    return {"status": "ok"}

# -- 3. CHECK-IN -------------------------------------------
@app.post("/api/checkin")
async def checkin(p: CheckInPayload, request: Request, db: AsyncSession = Depends(get_db)):
    device = await db.get(Device, p.hostname)
    if not device: return {"action": "register_first"}
    today     = p.date if p.date else today_str()
    # Skip weekends
    if is_weekend(today):
        logger.info(f"Weekend check-in skipped: {today} ({device.employee_id})")
        return {"action": "weekend_skip", "date": today}
    public_ip = get_client_ip(request)
    result    = classify(public_ip=public_ip, lan_ip=p.lan_ip,
                         vpn_tunnel_ip=p.vpn_tunnel_ip, ssid=None,
                         is_ethernet=p.is_ethernet,
                         dns_servers=p.dns_servers, dns_domains=p.dns_domains)
    logger.info(f"CheckIn: {device.employee_id} lan={p.lan_ip} dns={p.dns_servers} -> {result.auto_status}({result.confidence})")
    if result.auto_status == "vpn_ambiguous":
        return {"action": "confirm_needed", "lan_ip": p.lan_ip, "public_ip": public_ip, "detail": result.detail}
    seg_result = await handle_checkin(
        device.employee_id, device.employee_name, p.hostname, today,
        result.auto_status, result.confidence, p.source or "auto_detected",
        public_ip, p.lan_ip, bool(p.vpn_tunnel_ip), p.vpn_tunnel_ip,
        p.dns_servers or [], p.dns_domains or [], p.is_ethernet, p.platform,
        result.flagged, result.flag_reason, db,
        queued_at=p.queued_at)

    # Don't overwrite DB if day is locked by override or leave
    if seg_result.get("action") in ("override_locked", "leave_recorded"):
        # WFO-on-leave/holiday alert: if employee is on leave/holiday but
        # office signals are detected, notify their team manager
        if result.auto_status == "wfo":
            is_ph = False
            ph_check = await db.execute(select(PublicHoliday).where(
                PublicHoliday.date == today))
            if ph_check.scalars().first():
                is_ph = True
            leave_type = seg_result.get("status", "leave")
            alert_reason = "public holiday" if is_ph else f"leave ({leave_type})"
            logger.info(
                f"WFO-on-{alert_reason}: {device.employee_id} "
                f"({device.employee_name}) detected in office on {today} "
                f"while marked as {alert_reason}"
            )
            # Log anomaly so it appears in the Anomalies panel
            db.add(AnomalyLog(
                employee_id   = device.employee_id,
                employee_name = device.employee_name,
                anomaly_type  = "wfo_on_leave",
                description   = (
                    f"{device.employee_name} ({device.employee_id}) "
                    f"has office network signals on {today} "
                    f"but is recorded as {alert_reason}. "
                    f"LAN: {p.lan_ip or 'unknown'}."
                ),
                severity = "medium",
            ))
            await db.commit()
            # Send Teams notification to channel
            from notifier import notify_wfo_on_leave
            try:
                from config import APP_TITLE
                import os
                wh = os.environ.get("TEAMS_WEBHOOK", "")
                notify_wfo_on_leave(
                    employee_name = device.employee_name,
                    employee_id   = device.employee_id,
                    date          = today,
                    leave_type    = alert_reason,
                    lan_ip        = p.lan_ip,
                    webhook       = wh or None,
                )
            except Exception as e:
                logger.warning(f"[WARN] notify_wfo_on_leave failed: {e}")
        return {**seg_result, "detail": result.detail}

    await _upsert_checkin(device, today, public_ip, p, result, db)
    if result.flagged and result.flag_reason:
        db.add(AnomalyLog(employee_id=device.employee_id, employee_name=device.employee_name,
                          anomaly_type="lan_mismatch", description=result.flag_reason, severity="high"))
        await db.commit()
    return {**seg_result, "detail": result.detail}

async def _upsert_checkin(device, today, public_ip, p, result, db):
    q   = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == device.employee_id, CheckIn.date == today)))
    rec = q.scalars().first()
    if not rec:
        rec = CheckIn(employee_id=device.employee_id, employee_name=device.employee_name,
                      hostname=p.hostname, date=today); db.add(rec)
    rec.public_ip=public_ip; rec.lan_ip=p.lan_ip; rec.vpn_tunnel_ip=p.vpn_tunnel_ip
    rec.vpn_active=bool(p.vpn_tunnel_ip); rec.is_ethernet=p.is_ethernet
    rec.dns_servers=json.dumps(p.dns_servers or []); rec.dns_domains=json.dumps(p.dns_domains or [])
    rec.platform=p.platform; rec.auto_status=result.auto_status; rec.final_status=result.auto_status
    rec.confidence=result.confidence; rec.flagged=result.flagged; rec.flag_reason=result.flag_reason
    await db.commit()

# -- 4. CONFIRM --------------------------------------------
@app.post("/api/confirm")
async def confirm(p: ConfirmPayload, db: AsyncSession = Depends(get_db)):
    device = await db.get(Device, p.hostname)
    if not device: raise HTTPException(404, "Not registered")
    if p.declared_status not in ("wfo","wfh"): raise HTTPException(400, "must be wfo|wfh")
    today    = today_str()
    open_seg = await get_open_segment(device.employee_id, today, db)
    if open_seg: await close_segment(open_seg, db)
    await open_new_segment(device.employee_id, device.employee_name, p.hostname, today,
                           p.declared_status, p.declared_status, "medium", "user_confirmed",
                           None, None, True, None, [], [], False, None, False, None, db)
    q   = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == device.employee_id, CheckIn.date == today)))
    rec = q.scalars().first()
    if rec: rec.final_status=p.declared_status; rec.user_declared=True; await db.commit()
    return {"status": "confirmed", "final_status": p.declared_status}

# -- 5. OVERRIDE -------------------------------------------
@app.post("/api/override")
async def override(p: OverridePayload, request: Request,
                   db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "manager", caller_id=p.override_by)
    dq     = await db.execute(select(Device).where(Device.employee_id == p.employee_id))
    device = dq.scalars().first()
    if not device: raise HTTPException(404, "Employee not found")

    old_status = "unknown"

    # -- If new status is a leave type, upsert LeaveRequest ---
    if p.new_status in LEAVE_TYPES:
        lq  = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id == p.employee_id, LeaveRequest.date == p.date)))
        rec = lq.scalars().first()
        if not rec:
            db.add(LeaveRequest(employee_id=p.employee_id, employee_name=device.employee_name,
                                date=p.date, leave_type=p.new_status, note=p.note,
                                applied_by=p.override_by, source="manager"))
        else:
            old_status = rec.leave_type
            rec.leave_type=p.new_status; rec.note=p.note; rec.applied_by=p.override_by
    else:
        # -- Overriding TO wfo/wfh - delete any leave record for this date --
        lq  = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id == p.employee_id, LeaveRequest.date == p.date)))
        leave_rec = lq.scalars().first()
        if leave_rec:
            old_status = leave_rec.leave_type
            await db.delete(leave_rec)

        # -- Upsert DaySegment so UI picks it up (segments take priority) --
        seg_q = await db.execute(select(DaySegment).where(and_(
            DaySegment.employee_id == p.employee_id,
            DaySegment.date == p.date,
        )).order_by(DaySegment.segment_number))
        segs = seg_q.scalars().all()
        if segs:
            # Update the first segment's final_status; remove extras
            old_status = old_status if old_status != "unknown" else (segs[0].final_status or segs[0].status)
            segs[0].final_status = p.new_status
            segs[0].overridden   = True
            segs[0].override_by  = p.override_by
            segs[0].override_note = p.note
            for extra in segs[1:]:
                await db.delete(extra)
        else:
            # No segment exists - create one so the UI shows the override
            from datetime import datetime as _dt
            db.add(DaySegment(
                employee_id=p.employee_id, employee_name=device.employee_name,
                hostname=device.hostname or "", date=p.date, segment_number=1,
                status=p.new_status, final_status=p.new_status,
                confidence="high", source="manager_override",
                started_at=_dt.now(timezone.utc),
                overridden=True, override_by=p.override_by, override_note=p.note,
            ))

    # -- Always update legacy CheckIn too (for backwards compat) --
    q    = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == p.employee_id, CheckIn.date == p.date)))
    crec = q.scalars().first()
    if not crec:
        crec = CheckIn(employee_id=p.employee_id, employee_name=device.employee_name,
                       hostname=device.hostname or "", date=p.date, auto_status="manual"); db.add(crec)
    if old_status == "unknown":
        old_status = crec.auto_status or "unknown"
    crec.final_status=p.new_status; crec.overridden=True
    crec.override_by=p.override_by; crec.override_note=p.note; crec.confidence="high"
    await db.commit()
    # -- Notify Teams --------------------------------------
    if NOTIFIER_AVAILABLE:
        notify_override_applied(
            target_name=device.employee_name,
            target_id=p.employee_id,
            date=p.date,
            old_status=old_status,
            new_status=p.new_status,
            override_by=p.override_by,
            note=p.note,
            webhook=_SERVER_WEBHOOK,
            level=LEVEL_ALL,
            server_url=_SERVER_URL,
        )
    return {"status": "overridden", "employee_id": p.employee_id, "date": p.date}

# -- 6. LEAVE ---------------------------------------------
@app.post("/api/leave")
async def apply_leave(p: LeavePayload, request: Request,
                      db: AsyncSession = Depends(get_db)):
    # Self-apply always allowed; applying for someone else requires manager role
    caller = get_caller_id(request) or p.applied_by or p.employee_id
    if caller != p.employee_id:
        await require_role(request, db, "manager", caller_id=caller)
    dq     = await db.execute(select(Device).where(Device.employee_id == p.employee_id))
    device = dq.scalars().first()
    if not device: raise HTTPException(404, "Employee not found")
    lq  = await db.execute(select(LeaveRequest).where(and_(
        LeaveRequest.employee_id == p.employee_id, LeaveRequest.date == p.date)))
    rec = lq.scalars().first()
    if rec:
        rec.leave_type=p.leave_type; rec.half_day_period=p.half_day_period
        rec.note=p.note; rec.applied_by=p.applied_by or p.employee_id; rec.source=p.source
    else:
        db.add(LeaveRequest(employee_id=p.employee_id, employee_name=device.employee_name,
                            date=p.date, leave_type=p.leave_type,
                            half_day_period=p.half_day_period, note=p.note,
                            applied_by=p.applied_by or p.employee_id, source=p.source))
    q    = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == p.employee_id, CheckIn.date == p.date)))
    crec = q.scalars().first()
    if not crec:
        crec = CheckIn(employee_id=p.employee_id, employee_name=device.employee_name,
                       hostname=device.hostname or "", date=p.date, auto_status="leave"); db.add(crec)
    crec.final_status=p.leave_type; crec.overridden=True
    crec.override_by=p.applied_by or p.employee_id; crec.override_note=f"Leave: {p.leave_type}"
    await db.commit()
    # -- Notify Teams --------------------------------------
    if NOTIFIER_AVAILABLE:
        notify_leave_applied(
            employee_name=device.employee_name,
            leave_type=p.leave_type,
            dates=[p.date],
            applied_by=p.applied_by or p.employee_id,
            note=p.note,
            webhook=_SERVER_WEBHOOK,
            level=LEVEL_ALL,
            server_url=_SERVER_URL,
        )
    return {"status": "ok", "leave_type": p.leave_type, "date": p.date}

@app.delete("/api/leave")
async def delete_leave(p: DeleteLeavePayload, request: Request,
                       db: AsyncSession = Depends(get_db)):
    caller = get_caller_id(request) or p.employee_id
    if caller != p.employee_id:
        await require_role(request, db, "manager", caller_id=caller)
    lq  = await db.execute(select(LeaveRequest).where(and_(
        LeaveRequest.employee_id == p.employee_id, LeaveRequest.date == p.date)))
    rec = lq.scalars().first()
    if rec: await db.delete(rec)
    q    = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == p.employee_id, CheckIn.date == p.date)))
    crec = q.scalars().first()
    if crec and crec.overridden:
        crec.final_status=crec.auto_status; crec.overridden=False
        crec.override_by=None; crec.override_note=None
    await db.commit()
    return {"status": "deleted"}

@app.get("/api/leave/{employee_id}")
async def get_leaves(employee_id: str, month: str=None, db: AsyncSession=Depends(get_db)):
    if not month: month=date.today().strftime("%Y-%m")
    q = await db.execute(select(LeaveRequest).where(and_(
        LeaveRequest.employee_id==employee_id, LeaveRequest.date.like(f"{month}%"),
    )).order_by(LeaveRequest.date))
    return {"employee_id": employee_id, "month": month, "leaves": [
        {"date": l.date, "leave_type": l.leave_type,
         "label": get_leave_meta(l.leave_type)["label"],
         "emoji": get_leave_meta(l.leave_type)["emoji"],
         "icon":  get_leave_meta(l.leave_type)["icon"],
         "half_day_period": l.half_day_period, "note": l.note, "source": l.source}
        for l in q.scalars().all()]}

# -- 7. PUBLIC HOLIDAYS ------------------------------------
@app.post("/api/holidays")
async def add_holiday(p: PublicHolidayPayload, request: Request,
                      db: AsyncSession=Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(PublicHoliday).where(PublicHoliday.date==p.date))
    rec = q.scalars().first()
    if rec: rec.name=p.name; rec.optional=p.optional
    else:
        db.add(PublicHoliday(date=p.date, name=p.name, country=p.country,
                              region=p.region, optional=p.optional))
    await db.commit()
    # Auto-apply to all employees (non-optional only)
    if not p.optional:
        dq = await db.execute(select(Device))
        for device in dq.scalars().all():
            lq  = await db.execute(select(LeaveRequest).where(and_(
                LeaveRequest.employee_id==device.employee_id, LeaveRequest.date==p.date)))
            if not lq.scalars().first():
                db.add(LeaveRequest(employee_id=device.employee_id, employee_name=device.employee_name,
                                    date=p.date, leave_type="public_holiday", note=p.name,
                                    applied_by="system", source="public_holiday_auto"))
                cq  = await db.execute(select(CheckIn).where(and_(
                    CheckIn.employee_id==device.employee_id, CheckIn.date==p.date)))
                crec = cq.scalars().first()
                if not crec:
                    crec = CheckIn(employee_id=device.employee_id, employee_name=device.employee_name,
                                   hostname=device.hostname or "", date=p.date, auto_status="public_holiday")
                    db.add(crec)
                crec.final_status="public_holiday"; crec.overridden=True
                crec.override_by="system"; crec.override_note=f"Public holiday: {p.name}"
        await db.commit()
        logger.info(f"Auto-applied public holiday '{p.name}' on {p.date} to all employees")
    return {"status": "ok", "date": p.date, "name": p.name, "auto_applied": not p.optional}

@app.delete("/api/holidays/{holiday_date}")
async def delete_holiday(holiday_date: str, request: Request,
                         db: AsyncSession=Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(PublicHoliday).where(PublicHoliday.date==holiday_date))
    rec = q.scalars().first()
    if rec: await db.delete(rec); await db.commit()
    return {"status": "deleted"}

@app.get("/api/holidays")
async def get_holidays(year: int=None, db: AsyncSession=Depends(get_db)):
    if not year: year=date.today().year
    q = await db.execute(select(PublicHoliday).where(
        PublicHoliday.date.like(f"{year}%")).order_by(PublicHoliday.date))
    return {"holidays": [{"date": h.date, "name": h.name,
                          "optional": h.optional, "country": h.country}
                         for h in q.scalars().all()]}

# -- 8. MISSED DAY -----------------------------------------
@app.post("/api/missed")
async def record_missed(p: MissedDayPayload, db: AsyncSession=Depends(get_db)):
    device = await db.get(Device, p.hostname)
    if not device: raise HTTPException(404, "Not registered")
    source   = "offline_queue_flushed" if p.has_cached_data else "missed_prompt_no_data"
    is_leave = p.status in LEAVE_TYPES or p.leave_type
    lt       = p.leave_type or p.status
    # Block re-submission — only offline_queue_flushed may overwrite
    if source != "offline_queue_flushed":
        ex_ci = await db.execute(select(CheckIn).where(and_(
            CheckIn.employee_id==device.employee_id, CheckIn.date==p.date)))
        if ex_ci.scalars().first():
            return {"status": "already_recorded", "date": p.date,
                    "detail": "A record already exists for this date."}
        ex_lv = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id==device.employee_id, LeaveRequest.date==p.date)))
        if ex_lv.scalars().first():
            return {"status": "already_recorded", "date": p.date,
                    "detail": "Leave already recorded for this date."}
    if is_leave:
        lq  = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id==device.employee_id, LeaveRequest.date==p.date)))
        if not lq.scalars().first():
            db.add(LeaveRequest(employee_id=device.employee_id, employee_name=device.employee_name,
                                date=p.date, leave_type=lt, applied_by=device.employee_id, source=source))
    else:
        open_seg = await get_open_segment(device.employee_id, p.date, db)
        if open_seg: await close_segment(open_seg, db)
        seg = await open_new_segment(
            device.employee_id, device.employee_name, p.hostname, p.date,
            p.status, p.status, "high" if p.has_cached_data else "user_declared", source,
            None, p.lan_ip, bool(p.vpn_tunnel_ip), p.vpn_tunnel_ip,
            p.dns_servers or [], p.dns_domains or [], p.is_ethernet, None, False, None, db)
        await close_segment(seg, db)
    q    = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id==device.employee_id, CheckIn.date==p.date)))
    crec = q.scalars().first()
    if not crec:
        crec = CheckIn(employee_id=device.employee_id, employee_name=device.employee_name,
                       hostname=p.hostname, date=p.date, auto_status="manual"); db.add(crec)
    crec.final_status=lt if is_leave else p.status; crec.overridden=True
    crec.override_by=device.employee_id; crec.override_note=f"Missed day ({source})"
    crec.confidence="high" if p.has_cached_data else "user_declared"
    await db.commit()
    return {"status": "recorded", "date": p.date, "source": source}

# -- 9. DASHBOARD APIS -------------------------------------
@app.get("/api/today")
async def get_today(team: str=None, request: Request=None,
                    db: AsyncSession=Depends(get_db)):
    q = await db.execute(select(CheckIn).where(
        CheckIn.date==today_str()).order_by(desc(CheckIn.timestamp)))
    caller_id = get_caller_id(request) if request else None
    managed = await get_managed_teams(caller_id, db) if caller_id else None
    result = []
    for r in q.scalars().all():
        s = await get_day_summary(r.employee_id, r.date, db)
        dq = await db.execute(select(Device).where(Device.employee_id==r.employee_id))
        dev = dq.scalars().first()
        if team and (not dev or dev.team != team): continue
        if managed is not None and dev and dev.team not in managed: continue
        # Check public holiday once per loop (cached after first call)
        if not hasattr(get_today, '_ph_cache') or get_today._ph_cache[0] != today_str():
            ph_chk = await db.execute(select(PublicHoliday).where(
                PublicHoliday.date==today_str()))
            ph_rec_t = ph_chk.scalars().first()
            get_today._ph_cache = (today_str(), ph_rec_t)
        else:
            ph_rec_t = get_today._ph_cache[1]
        # On public holiday: override display_status unless employee came in (wfo)
        dom_today = dominant_status_from_segments(s.get("segments",[])) or r.final_status or r.auto_status
        if ph_rec_t and dom_today != "wfo":
            display_today = "public_holiday"
        else:
            display_today = s["display_status"]
        result.append({"employee_id": r.employee_id, "employee_name": r.employee_name,
                       "hostname": r.hostname, "team": dev.team if dev else None,
                       "status": r.final_status or r.auto_status,
                       "dominant_status": dom_today,
                       "display_status": display_today, "split_label": s["split_label"],
                       "is_split": s["is_split"], "segments": s["segments"], "leaves": s["leaves"],
                       "is_public_holiday": ph_rec_t is not None,
                       "lan_ip": r.lan_ip, "vpn_active": r.vpn_active, "vpn_tunnel_ip": r.vpn_tunnel_ip,
                       "dns_servers": json.loads(r.dns_servers or "[]"), "is_ethernet": r.is_ethernet,
                       "confidence": r.confidence, "flagged": r.flagged, "flag_reason": r.flag_reason,
                       "overridden": r.overridden, "override_note": r.override_note, "platform": r.platform,
                       "timestamp": (s["segments"][-1]["started_at"] if s.get("segments")
                                     else (r.timestamp.isoformat()+"Z") if r.timestamp else None)})
    return {"date": today_str(), "total": len(result), "checkins": result}

@app.get("/api/today/team")
async def get_today_team(team: str, request: Request=None, db: AsyncSession=Depends(get_db)):
    """All members of a team with today's status - includes those not yet checked in."""
    # Get all devices in this team
    dq = await db.execute(select(Device).where(Device.team==team).order_by(Device.employee_name))
    devices = dq.scalars().all()

    # Get today's checkins for this team
    cq = await db.execute(select(CheckIn).where(CheckIn.date==today_str()))
    checkins = {r.employee_id: r for r in cq.scalars().all()}

    # Get leave for today
    lq = await db.execute(select(LeaveRequest).where(LeaveRequest.date==today_str()))
    leaves = {l.employee_id: l for l in lq.scalars().all()}

    caller_id_tt = get_caller_id(request) if request else None
    managed_tt = await get_managed_teams(caller_id_tt, db) if caller_id_tt else None
    result = []
    for dev in devices:
        eid = dev.employee_id
        if managed_tt is not None and dev.team not in managed_tt: continue
        r   = checkins.get(eid)
        lv  = leaves.get(eid)
        if r:
            s = await get_day_summary(eid, today_str(), db)
            status = s["display_status"] or r.final_status or r.auto_status
            split_label = s["split_label"]
            confidence  = r.confidence
            timestamp   = (r.timestamp.isoformat()+"Z") if r.timestamp else None
        elif lv:
            status      = lv.leave_type
            split_label = None
            confidence  = None
            timestamp   = None
        else:
            status      = "not_checked_in"
            split_label = None
            confidence  = None
            timestamp   = None
        result.append({
            "employee_id":   eid,
            "employee_name": dev.employee_name,
            "team":          dev.team,
            "status":        status,
            "split_label":   split_label,
            "confidence":    confidence,
            "timestamp":     timestamp,
        })
    return {"date": today_str(), "team": team, "members": result}

@app.get("/api/me")
async def get_me(request: Request, db: AsyncSession=Depends(get_db)):
    """Return device info for the calling employee - used by client to get team."""
    eid = get_caller_id(request)
    if not eid: raise HTTPException(400, "X-Employee-Id header required")
    dq  = await db.execute(select(Device).where(Device.employee_id==eid))
    dev = dq.scalars().first()
    if not dev: raise HTTPException(404, "Not registered")
    rq  = await db.execute(select(Role).where(Role.employee_id==eid))
    role = rq.scalars().first()
    return {"employee_id": dev.employee_id, "employee_name": dev.employee_name,
            "team": dev.team, "platform": dev.platform,
            "role": role.role if role else "employee"}

@app.get("/api/stats")
async def get_stats(team: str=None, request: Request=None,
                    db: AsyncSession=Depends(get_db)):
    today = today_str()
    q = await db.execute(select(CheckIn).where(CheckIn.date==today))
    caller_id = get_caller_id(request) if request else None
    managed = await get_managed_teams(caller_id, db) if caller_id else None
    records = q.scalars().all()
    # Check if today is a public holiday
    ph_today_q = await db.execute(select(PublicHoliday).where(PublicHoliday.date==today))
    ph_today   = ph_today_q.scalars().first()
    is_ph_today = ph_today is not None
    wfo = wfh = ambiguous = flagged = 0
    total_filtered = 0
    for r in records:
        dq2 = await db.execute(select(Device).where(Device.employee_id==r.employee_id))
        dev2 = dq2.scalars().first()
        if team and (not dev2 or dev2.team != team): continue
        if managed is not None and dev2 and dev2.team not in managed: continue
        total_filtered += 1
        s = await get_day_summary(r.employee_id, today, db)
        segs = s.get("segments", [])
        dom = dominant_status_from_segments(segs) or r.final_status or ""
        if dom == "wfo":
            wfo += 1  # WFO always counts even on holiday (came in)
        elif dom == "wfh" and not is_ph_today:
            wfh += 1  # WFH only counts on non-holiday days
        elif (not dom or dom == "vpn_ambiguous") and not is_ph_today:
            ambiguous += 1
        if r.flagged: flagged += 1
    return {"date": today, "is_public_holiday": is_ph_today,
            "wfo": wfo, "wfh": wfh,
            "ambiguous": ambiguous, "flagged": flagged,
            "total": total_filtered}

@app.get("/api/week")
async def get_week(team: str=None, request: Request=None,
                   db: AsyncSession=Depends(get_db)):
    results = []
    caller_id_w = get_caller_id(request) if request else None
    managed_w = await get_managed_teams(caller_id_w, db) if caller_id_w else None
    for i in range(6,-1,-1):
        d = (date.today()-timedelta(days=i)).isoformat()
        q = await db.execute(select(CheckIn).where(CheckIn.date==d))
        recs = q.scalars().all()
        # Check if this day is a public holiday
        ph_d_q = await db.execute(select(PublicHoliday).where(PublicHoliday.date==d))
        is_ph  = ph_d_q.scalars().first() is not None
        day_wfo = day_wfh = 0
        for r in recs:
            dq3 = await db.execute(select(Device).where(Device.employee_id==r.employee_id))
            dev3 = dq3.scalars().first()
            if team and (not dev3 or dev3.team != team): continue
            if managed_w is not None and dev3 and dev3.team not in managed_w: continue
            s = await get_day_summary(r.employee_id, d, db)
            segs = s.get("segments", [])
            dom = dominant_status_from_segments(segs) or r.final_status or ""
            if dom == "wfo":
                day_wfo += 1       # WFO counts even on holiday
            elif not is_ph:
                day_wfh += 1       # WFH only on non-holiday days
        results.append({"date": d, "wfo": day_wfo, "wfh": day_wfh,
                        "is_public_holiday": is_ph,
                        "ambiguous": sum(1 for r in recs if not r.final_status)})
    return {"week": results}

@app.get("/api/history/{employee_id}")
async def get_history(employee_id: str, month: str=None,
                      request: Request=None, db: AsyncSession=Depends(get_db)):
    if not month: month=date.today().strftime("%Y-%m")
    # Access check: manager can only view employees in their managed teams
    caller_id = get_caller_id(request) if request else None
    if caller_id and caller_id != employee_id:
        managed = await get_managed_teams(caller_id, db)
        if managed is not None:
            dq = await db.execute(select(Device).where(Device.employee_id==employee_id))
            dev = dq.scalars().first()
            if not dev or dev.team not in managed:
                return {"month": month, "records": [], "public_holiday_dates": [],
                        "personal_leave_dates": []}  # silently return empty
    q = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id==employee_id, CheckIn.date.like(f"{month}%")
    )).order_by(CheckIn.date))
    enriched = []
    for r in q.scalars().all():
        s = await get_day_summary(employee_id, r.date, db)
        # For split days: if ANY segment was WFO, dominant status = wfo
        # (WFO always takes priority for compliance - came in = gets credit)
        segs_h = s.get("segments", [])
        dominant = dominant_status_from_segments(segs_h) or s["display_status"]
        if s["is_split"] and s["segments"]:
            if any(seg["status"] == "wfo" for seg in s["segments"]):
                dominant = "wfo"
            else:
                dominant = "wfh"
        enriched.append({"date": r.date,
                         "status": dominant,
                         "display_status": s["display_status"],  # keep 'split' for UI display
                         "dominant_status": dominant,             # used for compliance counting
                         "split_label": s["split_label"],
                         "is_split": s["is_split"], "segments": s["segments"], "leaves": s["leaves"],
                         "confidence": r.confidence, "flagged": r.flagged, "overridden": r.overridden})
    # Also return public holidays and personal leave dates for this month
    # so frontend can subtract them from working days count
    phq = await db.execute(select(PublicHoliday).where(
        PublicHoliday.date.like(f"{month}%")))
    ph_dates = [h.date for h in phq.scalars().all()]
    # Personal leave dates (optional holidays, personal leave)
    lq = await db.execute(select(LeaveRequest).where(and_(
        LeaveRequest.employee_id==employee_id,
        LeaveRequest.date.like(f"{month}%"))))
    leave_dates = [l.date for l in lq.scalars().all()]
    return {"employee_id": employee_id, "month": month, "records": enriched,
            "public_holiday_dates": ph_dates,
            "personal_leave_dates": leave_dates}

@app.get("/api/anomalies")
async def get_anomalies(db: AsyncSession=Depends(get_db)):
    q = await db.execute(select(AnomalyLog).where(
        AnomalyLog.resolved==False).order_by(desc(AnomalyLog.detected_at)))
    return {"anomalies": [{"id": r.id, "employee_id": r.employee_id,
                           "employee_name": r.employee_name, "type": r.anomaly_type,
                           "description": r.description, "severity": r.severity,
                           "detected_at": r.detected_at.isoformat()+"Z"}
                          for r in q.scalars().all()]}

@app.get("/api/team")
async def get_team(request: Request=None, db: AsyncSession=Depends(get_db)):
    caller_id = get_caller_id(request) if request else None
    managed = await get_managed_teams(caller_id, db) if caller_id else None
    dq = await db.execute(select(Device).order_by(Device.employee_name))
    result = []
    for d in dq.scalars().all():
        if managed is not None and d.team not in managed: continue
        role = await get_role(d.employee_id, db)
        result.append({"hostname": d.hostname, "employee_name": d.employee_name,
                       "employee_id": d.employee_id, "team": d.team,
                       "platform": d.platform, "role": role})
    return {"team": result}

@app.get("/api/leave-types")
async def get_leave_types():
    return {"leave_types": [{"type": k, **v} for k,v in LEAVE_TYPES.items()]}

@app.get("/api/compliance")
async def get_compliance(month: str=None, team: str=None, request: Request=None,
                         db: AsyncSession=Depends(get_db)):
    if not month: month=date.today().strftime("%Y-%m")
    yr, mo = int(month[:4]), int(month[5:7])
    today  = date.today()

    # -- Get public holidays this month --
    phq      = await db.execute(select(PublicHoliday).where(
        PublicHoliday.date.like(f"{month}%")))
    ph_dates = {h.date for h in phq.scalars().all()}

    # -- Build Mon-Fri weeks for this month ------------------
    # A "completed week" = every Mon-Fri day in that week is either
    # in the past OR it's the last day of the month.
    # Incomplete current week is NOT penalised.
    days_in_month = (date(yr, mo % 12 + 1, 1) - timedelta(days=1)).day if mo < 12 else 31
    all_weekdays  = []
    for day in range(1, days_in_month + 1):
        try:
            d = date(yr, mo, day)
        except ValueError:
            break
        if d.weekday() < 5:  # Mon-Fri only
            all_weekdays.append(d.isoformat())

    # Group weekdays into Mon-Fri calendar weeks
    weeks = []
    current_week = []
    for ds in all_weekdays:
        d = date.fromisoformat(ds)
        if current_week and date.fromisoformat(current_week[-1]).weekday() > d.weekday():
            weeks.append(current_week)
            current_week = [ds]
        else:
            current_week.append(ds)
    if current_week:
        weeks.append(current_week)

    # A week is "completed" if its last day is before today
    completed_weeks = [w for w in weeks if date.fromisoformat(w[-1]) < today]

    caller_id = get_caller_id(request)
    managed = await get_managed_teams(caller_id, db) if caller_id else None

    dq = await db.execute(select(Device).order_by(Device.employee_name))
    result = []

    for d in dq.scalars().all():
        # Filter 1: managed_teams restriction (server-enforced)
        if managed is not None and d.team not in managed: continue
        # Filter 2: explicit team param from UI filter dropdown
        if team and d.team != team: continue
        q       = await db.execute(select(CheckIn).where(and_(
            CheckIn.employee_id == d.employee_id,
            CheckIn.date.like(f"{month}%")
        )))
        records  = q.scalars().all()

        # -- Build per-date status map using DaySegment -------
        # Rule: if ANY segment that day was WFO -> day counts as WFO
        # (employee came in, even for part of day - gets RTO credit)
        # This correctly handles split days (WFH morning -> WFO afternoon)
        seg_q    = await db.execute(select(DaySegment).where(and_(
            DaySegment.employee_id == d.employee_id,
            DaySegment.date.like(f"{month}%")
        )))
        segments  = seg_q.scalars().all()

        # Build date -> dominant_status map
        # Priority: wfo > wfh > leave (WFO always wins for compliance)
        date_status: dict = {}
        for seg in segments:
            ds  = seg.date
            st  = seg.final_status or seg.status
            cur = date_status.get(ds)
            if cur is None:
                date_status[ds] = st
            elif st == "wfo":
                date_status[ds] = "wfo"   # WFO overrides anything
            # wfh keeps existing unless wfo found

        # Also layer in leave records for days with no segments
        leave_q  = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id == d.employee_id,
            LeaveRequest.date.like(f"{month}%")
        )))
        for lr in leave_q.scalars().all():
            if lr.date not in date_status:
                date_status[lr.date] = lr.leave_type

        # Legacy checkin fallback for days with no segments
        rec_map  = {r.date: r.final_status for r in records}
        for date_key, status in rec_map.items():
            if date_key not in date_status and status:
                date_status[date_key] = status

        # Exclude public holiday dates from wfo/wfh counts — a check-in
        # that fired on a public holiday (agent running that morning) should
        # not inflate the WFO/WFH numbers. Also exclude personal leave days
        # from WFH count (leave is already counted separately).
        leave_dates_set = {lr.date for lr in
                           (await db.execute(select(LeaveRequest).where(and_(
                                LeaveRequest.employee_id == d.employee_id,
                                LeaveRequest.date.like(f"{month}%")
                           )))).scalars().all()}
        wfo   = sum(1 for ds, s in date_status.items()
                    if s == "wfo" and ds not in ph_dates)
        wfh   = sum(1 for ds, s in date_status.items()
                    if s == "wfh"
                    and ds not in ph_dates
                    and ds not in leave_dates_set)
        leave = sum(1 for date_key, s in date_status.items()
                    if (s in LEAVE_TYPES or date_key in ph_dates)
                    and date_key not in ph_dates)  # ph_dates counted separately below
        ph_count_emp = sum(1 for ds in ph_dates
                           if date(yr, mo, 1) <= date.fromisoformat(ds) <=
                              date(yr, mo, (date(yr, mo % 12 + 1, 1) - timedelta(days=1)).day
                                    if mo < 12 else 31))

        # -- Weekly compliance check --------------------------
        # Rule: each completed week target = max(0, 3 - leave_days_that_week)
        # A week PASSES if wfo_days >= adjusted_target
        weekly_results = []
        for week in completed_weeks:
            week_wfo   = sum(1 for ds in week
                             if date_status.get(ds) == "wfo" and ds not in ph_dates)
            week_wfh   = sum(1 for ds in week
                             if date_status.get(ds) == "wfh"
                             and ds not in ph_dates
                             and ds not in leave_dates_set)
            week_leave = sum(1 for ds in week
                             if date_status.get(ds) in LEAVE_TYPES or ds in ph_dates)
            week_any   = week_wfo + week_wfh + week_leave

            # -- Grace period rule -----------------------------
            # Case 1: ZERO activity → no data, no penalty.
            # Covers weeks before app install / onboarding gaps.
            if week_any == 0:
                adjusted_target = max(0, 3 - week_leave)
                weekly_results.append({
                    "week_start":    week[0],
                    "week_end":      week[-1],
                    "wfo":           0,
                    "leave":         0,
                    "target":        adjusted_target,
                    "passed":        True,
                    "no_data":       True,
                })
                continue

            # Case 2: PARTIAL WEEK install grace period.
            # If the employee had activity on fewer days than the number
            # of working days in the week, they likely installed mid-week.
            # Rule: active_days < min(3, working_days_in_week) → grace period.
            # Examples:
            #   Full week (5 days), installed Mon → active could be 1-5 → evaluated normally
            #   Full week (5 days), installed Wed → active ≤ 3 → grace period
            #   Short week (3 days, bank hol Mon/Tue) → active < 3 → grace
            # "Active days" = days with any check-in OR leave (excludes public holidays)
            working_days_in_week = sum(1 for ds in week if ds not in ph_dates)
            grace_threshold = min(3, working_days_in_week)
            # active_days = days with actual data (not just public holidays)
            active_days = sum(1 for ds in week
                              if date_status.get(ds) and ds not in ph_dates)
            if active_days < grace_threshold:
                adjusted_target = max(0, 3 - week_leave)
                weekly_results.append({
                    "week_start":    week[0],
                    "week_end":      week[-1],
                    "wfo":           week_wfo,
                    "leave":         week_leave,
                    "target":        adjusted_target,
                    "passed":        True,   # partial week = grace, no penalty
                    "no_data":       True,   # shown as "no data" in UI
                    "partial_week":  True,   # distinguishes from zero-data grace
                })
                continue

            adjusted_target = max(0, 3 - week_leave)
            passed = week_wfo >= adjusted_target
            weekly_results.append({
                "week_start":    week[0],
                "week_end":      week[-1],
                "wfo":           week_wfo,
                "leave":         week_leave,
                "target":        adjusted_target,
                "passed":        passed,
                "no_data":       False,
            })

        weeks_passed = sum(1 for w in weekly_results if w["passed"])
        weeks_total  = len(weekly_results)
        all_weeks_ok = weeks_total == 0 or weeks_passed == weeks_total

        # -- RAG determination --------------------------------
        # No data at all -> grey, don't penalise
        # Green : >=12 WFO days in month (primary target met)
        # Amber : <12 WFO days BUT all completed weeks passed
        # Red   : <12 WFO days AND any completed week missed its adjusted target
        # Grey only if truly no data at all - no checkins, no leave, no segments
        total_activity = len(records) + len(date_status)
        working = wfo + wfh

        if total_activity == 0:
            rag    = "grey"
            status = "No data yet"
        elif wfo >= 12:
            rag    = "green"
            status = "Monthly target met"
        elif all_weeks_ok:
            rag    = "amber"
            status = f"{wfo}/12 days - weekly on track"
        else:
            missed = weeks_total - weeks_passed
            rag    = "red"
            status = f"{wfo}/12 days - {missed} week{'s' if missed!=1 else ''} missed"

        pct = round((wfo / working) * 100) if working else 0

        result.append({
            "employee_id":   d.employee_id,
            "employee_name": d.employee_name,
            "team":          d.team,
            "wfo":           wfo,
            "wfh":           wfh,
            "leave":         leave,
            "working_days":  working,
            "wfo_pct":       pct,
            "rag":           rag,
            "status":        status,
            "weekly":        weekly_results,
            "weeks_passed":  weeks_passed,
            "weeks_total":   weeks_total,
        })

    return {"month": month, "team": result}

@app.get("/api/export")
async def export_csv(month: str=None, request: Request=None,
                     db: AsyncSession=Depends(get_db)):
    """Manager/admin monthly attendance export — team-wise, compliance-focused.
    Columns: Team | Employee | ID | Days WFO | Days WFH | Days Leave | Days Absent
             | RTO% | Wk1 | Wk2 | Wk3 | Wk4 | Wk5 | Notes
    """
    if not month: month = date.today().strftime("%Y-%m")
    if request:
        caller_id = get_caller_id(request)
        if caller_id:
            role = await get_role(caller_id, db)
            if role == "employee":
                raise HTTPException(403, "Manager or admin role required")

    # Get all working days in month
    yr, mo = int(month[:4]), int(month[5:7])
    ph_q = await db.execute(select(PublicHoliday))
    ph_dates = {r.date for r in ph_q.scalars().all()}
    working_days = []
    for day_num in range(1, 32):
        try:
            d = date(yr, mo, day_num)
        except ValueError:
            break
        if d.weekday() < 5 and d.isoformat() not in ph_dates:
            working_days.append(d.isoformat())

    # Group into calendar weeks
    weeks = []
    cur = []
    for ds in working_days:
        if cur and date.fromisoformat(cur[-1]).weekday() > date.fromisoformat(ds).weekday():
            weeks.append(cur); cur = [ds]
        else:
            cur.append(ds)
    if cur: weeks.append(cur)

    # Get all devices grouped by team
    dq = await db.execute(select(Device).order_by(Device.team, Device.employee_name))
    devices = dq.scalars().all()

    today = date.today()

    buf = io.StringIO()
    w = csv.writer(buf)

    # Header
    week_headers = [f"Wk {wk[0][5:]}" for wk in weeks]  # e.g. "Wk 05-04"
    w.writerow(["Team", "Employee", "Employee ID",
                "Days WFO", "Days WFH", "Days Leave", "Days Absent",
                "RTO %"] + week_headers + ["Notes"])

    current_team = None
    team_totals = {}
    caller_id_exp = get_caller_id(request) if request else None
    managed_exp = await get_managed_teams(caller_id_exp, db) if caller_id_exp else None

    for dev in devices:
        team = dev.team or "Unassigned"
        if managed_exp is not None and dev.team not in managed_exp: continue

        # Team separator row
        if team != current_team:
            if current_team is not None:
                # Write team subtotal
                tt = team_totals[current_team]
                total_working = tt["wfo"] + tt["wfh"] + tt["leave"] + tt["absent"]
                rto = round((tt["wfo"] / max(tt["wfo"] + tt["wfh"], 1)) * 100) if (tt["wfo"] + tt["wfh"]) > 0 else 0
                w.writerow(["", f"--- {current_team} TOTAL ---", "",
                            tt["wfo"], tt["wfh"], tt["leave"], tt["absent"],
                            f"{rto}%"] + [""] * len(weeks) + [""])
                w.writerow([])  # blank row between teams
            current_team = team
            team_totals[team] = {"wfo": 0, "wfh": 0, "leave": 0, "absent": 0}

        # Get employee's history for this month
        hq = await db.execute(select(CheckIn).where(and_(
            CheckIn.employee_id == dev.employee_id,
            CheckIn.date.like(f"{month}%")
        )))
        records = {r.date: r for r in hq.scalars().all()}

        lq = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id == dev.employee_id,
            LeaveRequest.date.like(f"{month}%")
        )))
        leaves = {l.date for l in lq.scalars().all()}

        # Count per day using dominant status
        wfo = wfh = leave = absent = 0
        week_results = []

        for wk in weeks:
            # Only count completed weeks
            wk_end = date.fromisoformat(wk[-1])
            if wk_end >= today:
                week_results.append("-")
                continue
            wk_wfo = wk_wfh = 0
            for ds in wk:
                r = records.get(ds)
                if ds in leaves:
                    pass  # leave — don't count toward wfo/wfh
                elif r:
                    s = await get_day_summary(dev.employee_id, ds, db)
                    dom = dominant_status_from_segments(s.get("segments", [])) or r.final_status or ""
                    if dom == "wfo": wk_wfo += 1
                    elif dom == "wfh": wk_wfh += 1
                # else: absent
            target = max(0, 3 - sum(1 for ds in wk if ds in leaves))
            passed = wk_wfo >= target
            week_results.append("P" if passed else ("F" if target > 0 else "-"))

        # Monthly totals
        past_days = [ds for ds in working_days if date.fromisoformat(ds) < today]
        for ds in past_days:
            r = records.get(ds)
            if ds in leaves:
                leave += 1
            elif r:
                s = await get_day_summary(dev.employee_id, ds, db)
                dom = dominant_status_from_segments(s.get("segments", [])) or r.final_status or ""
                if dom == "wfo": wfo += 1
                elif dom == "wfh": wfh += 1
                else: wfh += 1
            else:
                absent += 1

        total_counted = wfo + wfh
        rto_pct = round((wfo / total_counted) * 100) if total_counted > 0 else 0

        # Notes
        notes = []
        if absent > 2: notes.append(f"{absent} untracked days")
        rag = "On track" if rto_pct >= 60 else ("At risk" if rto_pct >= 40 else "Below target")
        if wfo + wfh > 0: notes.append(rag)

        w.writerow([team, dev.employee_name, dev.employee_id,
                    wfo, wfh, leave, absent,
                    f"{rto_pct}%"] + week_results + ["; ".join(notes)])

        # Accumulate team totals
        team_totals[team]["wfo"] += wfo
        team_totals[team]["wfh"] += wfh
        team_totals[team]["leave"] += leave
        team_totals[team]["absent"] += absent

    # Last team subtotal
    if current_team and current_team in team_totals:
        tt = team_totals[current_team]
        rto = round((tt["wfo"] / max(tt["wfo"] + tt["wfh"], 1)) * 100) if (tt["wfo"] + tt["wfh"]) > 0 else 0
        w.writerow(["", f"--- {current_team} TOTAL ---", "",
                    tt["wfo"], tt["wfh"], tt["leave"], tt["absent"],
                    f"{rto}%"] + [""] * len(weeks) + [""])

    buf.seek(0)
    return StreamingResponse(buf, media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=rto_attendance_{month}.csv"})

@app.get("/api/team-leave")
async def get_team_leave(month: str=None, team: str=None,
                         request: Request=None, db: AsyncSession=Depends(get_db)):
    if not month: month=date.today().strftime("%Y-%m")
    caller_id_tl = get_caller_id(request) if request else None
    managed_tl = await get_managed_teams(caller_id_tl, db) if caller_id_tl else None
    stmt = select(LeaveRequest).where(LeaveRequest.date.like(f"{month}%"))
    if team:
        subq = select(Device.employee_id).where(Device.team == team)
        stmt = stmt.where(LeaveRequest.employee_id.in_(subq))
    elif managed_tl is not None:
        # Restrict to managed teams
        subq = select(Device.employee_id).where(Device.team.in_(managed_tl))
        stmt = stmt.where(LeaveRequest.employee_id.in_(subq))
    stmt = stmt.order_by(LeaveRequest.date)
    q = await db.execute(stmt)
    return {"month": month, "leaves": [
        {"date": l.date, "employee_id": l.employee_id, "employee_name": l.employee_name,
         "leave_type": l.leave_type, "label": get_leave_meta(l.leave_type)["label"],
         "emoji": get_leave_meta(l.leave_type)["emoji"], "half_day_period": l.half_day_period}
        for l in q.scalars().all()]}

# -- 10. TEAM CONFIG --------------------------------------
class TeamPayload(BaseModel):
    name: str
    created_by: Optional[str] = None

@app.get("/api/teams")
async def get_teams(db: AsyncSession = Depends(get_db)):
    q = await db.execute(select(TeamConfig).order_by(TeamConfig.name))
    return {"teams": [{"id": t.id, "name": t.name} for t in q.scalars().all()]}

@app.post("/api/teams")
async def add_team(p: TeamPayload, request: Request,
                   db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "manager", caller_id=p.created_by)
    q = await db.execute(select(TeamConfig).where(TeamConfig.name == p.name))
    if q.scalars().first():
        return {"status": "exists", "name": p.name}
    db.add(TeamConfig(name=p.name, created_by=p.created_by))
    await db.commit()
    return {"status": "created", "name": p.name}

@app.delete("/api/teams/{team_id}")
async def delete_team(team_id: int, request: Request,
                      db: AsyncSession = Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(TeamConfig).where(TeamConfig.id == team_id))
    rec = q.scalars().first()
    if rec: await db.delete(rec); await db.commit()
    return {"status": "deleted"}

# -- 11. HTML PAGES ----------------------------------------
@app.get("/register/{hostname}", response_class=HTMLResponse)
async def register_page(hostname: str, request: Request):
    tmpl = templates.get_template("register.html")
    html = tmpl.render(request=request, hostname=hostname)
    return HTMLResponse(html)


@app.get("/confirm/{hostname}", response_class=HTMLResponse)
async def vpn_confirm_page(hostname: str, request: Request,
                            db: AsyncSession = Depends(get_db)):
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

@app.get("/missed/{hostname}", response_class=HTMLResponse)
async def missed_day_page(hostname: str, request: Request,
                           db: AsyncSession = Depends(get_db)):
    """
    Bulk missed-day page. Accepts comma-separated dates via ?dates=2026-05-19,2026-05-20
    or a single date via ?dates=2026-05-19 (backwards compat).
    Also accepts legacy /missed/{hostname}/{date} format via redirect.
    """
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

@app.get("/missed/{hostname}/{missed_date}", response_class=HTMLResponse)
async def missed_day_page_legacy(hostname: str, missed_date: str,
                                  request: Request):
    """Legacy single-date URL - redirects to bulk page."""
    from fastapi.responses import RedirectResponse
    cached = request.query_params.get("cached", "false")
    lan    = request.query_params.get("lan", "")
    cls    = request.query_params.get("class", "")
    url    = f"/missed/{hostname}?dates={missed_date}"
    if cls: url += f"&class_{missed_date}={cls}"
    if lan: url += f"&lan_{missed_date}={lan}"
    return RedirectResponse(url)

@app.get("/", response_class=FileResponse)
@app.get("/dashboard", response_class=FileResponse)
async def root():
    return FileResponse(os.path.join(os.path.dirname(__file__),"rto-ui.html"), media_type="text/html")

@app.get("/admin", response_class=FileResponse)
async def admin_page():
    return FileResponse(os.path.join(os.path.dirname(__file__),"admin.html"), media_type="text/html")

@app.get("/api/admin/tables")
async def admin_tables(request: Request, db: AsyncSession = Depends(get_db)):
    """Return row counts for all tables."""
    await require_role(request, db, "admin")
    counts = {}
    for name, model in [
        ("devices", Device), ("checkins", CheckIn), ("day_segments", DaySegment),
        ("leave_requests", LeaveRequest), ("public_holidays", PublicHoliday),
        ("roles", Role), ("anomalies", AnomalyLog), ("team_configs", TeamConfig),
    ]:
        q = await db.execute(select(func.count()).select_from(model))
        counts[name] = q.scalar()
    return counts

@app.get("/api/admin/table/{table_name}")
async def admin_table_data(
    table_name: str, request: Request,
    page: int = 0, limit: int = 50, search: str = "",
    db: AsyncSession = Depends(get_db)
):
    """Return paginated rows from any table."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices":        Device,
        "checkins":       CheckIn,
        "day_segments":   DaySegment,
        "leave_requests": LeaveRequest,
        "public_holidays":PublicHoliday,
        "roles":          Role,
        "anomalies":      AnomalyLog,
        "team_configs":   TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")

    from sqlalchemy import inspect as sa_inspect, or_, cast, Text
    mapper   = sa_inspect(model)
    cols     = [c.key for c in mapper.mapper.column_attrs]

    q = select(model)
    if search:
        filters = []
        for col in cols:
            try:
                filters.append(cast(getattr(model, col), Text).ilike(f"%{search}%"))
            except Exception:
                pass
        if filters:
            q = q.where(or_(*filters))

    total_q = await db.execute(select(func.count()).select_from(q.subquery()))
    total   = total_q.scalar()
    rows_q  = await db.execute(q.offset(page * limit).limit(limit))
    rows    = rows_q.scalars().all()

    def row_to_dict(r):
        d = {}
        for col in cols:
            val = getattr(r, col, None)
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            d[col] = val
        return d

    return {"table": table_name, "columns": cols, "rows": [row_to_dict(r) for r in rows],
            "total": total, "page": page, "limit": limit}

@app.patch("/api/admin/table/{table_name}/{row_id}")
async def admin_edit_row(
    table_name: str, row_id: str,
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Edit a single row's fields - admin only."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices": Device, "checkins": CheckIn, "day_segments": DaySegment,
        "leave_requests": LeaveRequest, "public_holidays": PublicHoliday,
        "roles": Role, "anomalies": AnomalyLog, "team_configs": TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")

    from sqlalchemy import inspect as sa_inspect, String, Boolean, Integer, Float, Text, DateTime
    mapper  = sa_inspect(model)
    pk      = mapper.mapper.primary_key[0].key
    q       = await db.execute(select(model).where(getattr(model, pk) == row_id))
    row     = q.scalars().first()
    if not row: raise HTTPException(404, "Row not found")

    body = await request.json()
    col_types = {
        c.key: type(mapper.mapper.columns[c.key].type).__name__
        for c in mapper.mapper.column_attrs
    }

    for field, value in body.items():
        if field == pk: continue  # never edit PK
        if not hasattr(row, field): continue
        col_type = col_types.get(field, "String")
        # Coerce types
        try:
            if value is None or value == "":
                coerced = None
            elif col_type == "Boolean":
                coerced = str(value).lower() in ("true", "1", "yes")
            elif col_type == "Integer":
                coerced = int(value)
            elif col_type == "Float":
                coerced = float(value)
            elif col_type == "DateTime":
                from datetime import datetime as _dt
                coerced = _dt.fromisoformat(str(value).replace("Z", "+00:00"))
            else:
                coerced = str(value)
            setattr(row, field, coerced)
        except Exception as e:
            raise HTTPException(400, f"Invalid value for {field} ({col_type}): {e}")

    await db.commit()
    return {"updated": True}

@app.get("/api/admin/schema/{table_name}")
async def admin_schema(
    table_name: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return column types for a table - used by edit UI."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices": Device, "checkins": CheckIn, "day_segments": DaySegment,
        "leave_requests": LeaveRequest, "public_holidays": PublicHoliday,
        "roles": Role, "anomalies": AnomalyLog, "team_configs": TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(model)
    schema = {}
    for c in mapper.mapper.column_attrs:
        col = mapper.mapper.columns[c.key]
        schema[c.key] = {
            "type":     type(col.type).__name__,
            "pk":       col.primary_key,
            "nullable": col.nullable,
        }
    return schema

@app.delete("/api/admin/table/{table_name}/{row_id}")
async def admin_delete_row(
    table_name: str, row_id: str,
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Delete a row by primary key - admin only.
    Deleting a device cascades to all related records."""
    await require_role(request, db, "admin")
    TABLE_MAP = {
        "devices": Device, "checkins": CheckIn, "day_segments": DaySegment,
        "leave_requests": LeaveRequest, "public_holidays": PublicHoliday,
        "roles": Role, "anomalies": AnomalyLog, "team_configs": TeamConfig,
    }
    model = TABLE_MAP.get(table_name)
    if not model: raise HTTPException(404, "Unknown table")
    from sqlalchemy import inspect as sa_inspect
    pk  = sa_inspect(model).mapper.primary_key[0].key
    q   = await db.execute(select(model).where(getattr(model, pk) == row_id))
    row = q.scalars().first()
    if not row: raise HTTPException(404, "Row not found")

    # Cascade delete: if deleting a device, wipe all related records first
    if table_name == "devices":
        emp_id = row.employee_id
        from sqlalchemy import delete as sa_delete
        try:
            for related_model in [DaySegment, CheckIn, LeaveRequest, AnomalyLog, Role]:
                await db.execute(
                    sa_delete(related_model).where(
                        getattr(related_model, "employee_id") == emp_id
                    )
                )
            await db.flush()
            logger.info(f"Cascade delete complete for employee {emp_id}")
        except Exception as e:
            logger.error(f"Cascade delete error for {emp_id}: {e}")
            await db.rollback()
            raise HTTPException(500, f"Cascade delete failed: {str(e)}")

    await db.delete(row)
    await db.commit()
    return {"deleted": True, "cascade": table_name == "devices"}

@app.get("/health")
async def health():
    return {"status": "ok", "service": APP_TITLE, "port": PORT, "version": "2.0"}

# -- STATIC FILES - must be mounted AFTER all routes -------
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")