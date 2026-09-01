"""
Leave requests, public holidays, and the "missed day" backfill flow (when
someone forgot to check in and fills it in retroactively). These three are
grouped together because they all boil down to the same thing: writing a
day's status when it wasn't captured live by a normal check-in.
"""
import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from datetime import date

from database import get_db, Device, CheckIn, LeaveRequest, PublicHoliday, AnomalyLog
from segments import get_leave_meta, LEAVE_TYPES, get_open_segment, close_segment, open_new_segment
from detection import verify_client_signals
from deps import (
    get_device_token, get_client_ip, require_role, require_registered_caller, get_caller_context,
    get_managed_teams, limiter, verify_device_auth,
    NOTIFIER_AVAILABLE, notify_leave_applied, LEVEL_ALL, _SERVER_WEBHOOK, _SERVER_URL,
    sync_employee_teams_card,
)
from schemas import LeavePayload, DeleteLeavePayload, PublicHolidayPayload, MissedDayPayload

router = APIRouter()
logger = logging.getLogger("rto")

# -- 6. LEAVE ---------------------------------------------
@router.post("/api/leave")
async def apply_leave(p: LeavePayload, request: Request,
                      db: AsyncSession = Depends(get_db)):
    # Self-apply always allowed; applying for someone else requires manager role
    caller_device = await require_registered_caller(request, db)
    caller = caller_device.employee_id
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
    await sync_employee_teams_card(
        p.employee_id, device.employee_name, device.team,
        f"Leave applied: {p.leave_type} on {p.date}" + (f" — {p.note}" if p.note else ""), db)
    return {"status": "ok", "leave_type": p.leave_type, "date": p.date}

@router.delete("/api/leave")
async def delete_leave(p: DeleteLeavePayload, request: Request,
                       db: AsyncSession = Depends(get_db)):
    caller_device = await require_registered_caller(request, db)
    caller = caller_device.employee_id
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

@router.get("/api/leave/{employee_id}")
async def get_leaves(employee_id: str, month: str=None, request: Request=None,
                     db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
    if caller_device.employee_id != employee_id:
        if caller_role == "employee":
            raise HTTPException(403, "Access denied")
        managed = await get_managed_teams(caller_device.employee_id, db)
        if managed is not None:
            dq = await db.execute(select(Device).where(Device.employee_id == employee_id))
            dev = dq.scalars().first()
            if not dev or dev.team not in managed:
                raise HTTPException(403, "Access denied")
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
@router.post("/api/holidays")
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

@router.delete("/api/holidays/{holiday_date}")
async def delete_holiday(holiday_date: str, request: Request,
                         db: AsyncSession=Depends(get_db)):
    await require_role(request, db, "admin")
    q   = await db.execute(select(PublicHoliday).where(PublicHoliday.date==holiday_date))
    rec = q.scalars().first()
    if rec: await db.delete(rec); await db.commit()
    return {"status": "deleted"}

@router.get("/api/holidays")
async def get_holidays(year: int=None, db: AsyncSession=Depends(get_db)):
    if not year: year=date.today().year
    q = await db.execute(select(PublicHoliday).where(
        PublicHoliday.date.like(f"{year}%")).order_by(PublicHoliday.date))
    return {"holidays": [{"date": h.date, "name": h.name,
                          "optional": h.optional, "country": h.country}
                         for h in q.scalars().all()]}

# -- 8. MISSED DAY -----------------------------------------
@router.post("/api/missed")
@limiter.limit("30/hour")
async def record_missed(p: MissedDayPayload, request: Request, db: AsyncSession=Depends(get_db)):
    device = await db.get(Device, p.hostname)
    if not device: raise HTTPException(404, "Not registered")
    token = get_device_token(request)
    await verify_device_auth(device, token, db)
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
    final_status = p.status  # may get overridden below if claimed signals don't check out
    conf         = "high"    # leave/holiday days are always "certain", not a probability
    unverified_wfo = False   # set below only in the non-leave (wfo/wfh) branch
    if is_leave:
        lq  = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id==device.employee_id, LeaveRequest.date==p.date)))
        if not lq.scalars().first():
            db.add(LeaveRequest(employee_id=device.employee_id, employee_name=device.employee_name,
                                date=p.date, leave_type=lt, applied_by=device.employee_id, source=source))
    else:
        # has_cached_data=True is supposed to mean "the agent really was
        # offline that day but its network sensors still captured real
        # signals, queued locally, and are only reaching us now." That's a
        # legitimate claim — but until now we just took the has_cached_data
        # flag itself as proof and stamped confidence="high" on whatever
        # status the client sent, with no check at all. That's exactly the
        # gap that let someone hand-craft a request claiming office signals
        # for a day they were never near the office. So: if real signals
        # were actually supplied, run them through the same verification
        # live check-ins get (fabrication check + classify). If the client
        # says has_cached_data=True but hands over no signals to back that
        # up, that claim doesn't hold up either — treat it the same as an
        # honest "I have no evidence" self-declaration.
        conf = "user_declared"
        has_real_signals = bool(p.lan_ip) or bool(p.dns_servers)

        if p.has_cached_data and has_real_signals:
            verified = verify_client_signals(
                public_ip=get_client_ip(request), lan_ip=p.lan_ip,
                vpn_tunnel_ip=p.vpn_tunnel_ip, is_ethernet=p.is_ethernet,
                dns_servers=p.dns_servers, dns_domains=p.dns_domains)
            conf = verified.confidence
            if verified.auto_status != p.status:
                logger.warning(
                    f"[SECURITY] Missed-day claim mismatch: {device.employee_id} "
                    f"claimed {p.status} for {p.date} but supplied signals verify "
                    f"as {verified.auto_status}. Using verified status."
                )
                db.add(AnomalyLog(
                    employee_id=device.employee_id, employee_name=device.employee_name,
                    anomaly_type="missed_day_claim_mismatch",
                    description=(
                        f"{device.employee_name} ({device.employee_id}) submitted a "
                        f"missed-day claim of '{p.status}' for {p.date}, but the "
                        f"network signals they supplied actually verify as "
                        f"'{verified.auto_status}'. Recorded as {verified.auto_status}."
                    ),
                    severity="high",
                ))
                final_status = verified.auto_status
            elif verified.flagged:
                # Status matched, but verify_client_signals still flagged
                # something odd (e.g. fabricated office LAN) — keep the
                # claimed status but make sure it's still visible.
                db.add(AnomalyLog(
                    employee_id=device.employee_id, employee_name=device.employee_name,
                    anomaly_type="missed_day_signal_flagged",
                    description=f"Missed-day submission for {p.date}: {verified.flag_reason}",
                    severity="medium",
                ))

        open_seg = await get_open_segment(device.employee_id, p.date, db)
        if open_seg: await close_segment(open_seg, db)
        seg = await open_new_segment(
            device.employee_id, device.employee_name, p.hostname, p.date,
            final_status, final_status, conf, source,
            None, p.lan_ip, bool(p.vpn_tunnel_ip), p.vpn_tunnel_ip,
            p.dns_servers or [], p.dns_domains or [], p.is_ethernet, None, False, None, db)
        await close_segment(seg, db)

        # ── Unverified self-declared WFO: flag for manager visibility ──────
        # A retroactive WFO claim with no network evidence (no cached signals,
        # confidence=user_declared) can't be cross-checked the way live
        # check-ins are. Rather than silently accepting it at face value
        # forever, flag it as an anomaly (visible in the Anomalies panel)
        # and track a monthly count so a pattern of unverified WFO claims
        # is visible, not just each individual one.
        #
        # Security review (2026-09): this used to be purely cosmetic — the
        # anomaly got logged, but the CheckIn record itself was never
        # marked flagged, and severity only escalated to "high" after the
        # 4th claim in a month, so the first three were easy to miss in
        # practice. Now every unverified WFO claim is "high" immediately,
        # and the CheckIn row itself carries the flag so it's visible
        # anywhere that surfaces flagged records, not just the Anomalies
        # panel.
        unverified_wfo = final_status == "wfo" and conf == "user_declared"
        if unverified_wfo:
            month_prefix = p.date[:7]  # YYYY-MM
            cnt_q = await db.execute(select(func.count()).select_from(AnomalyLog).where(and_(
                AnomalyLog.employee_id == device.employee_id,
                AnomalyLog.anomaly_type == "unverified_wfo_declared",
                AnomalyLog.description.like(f"%{month_prefix}%"),
            )))
            month_count = (cnt_q.scalar() or 0) + 1
            db.add(AnomalyLog(
                employee_id=device.employee_id, employee_name=device.employee_name,
                anomaly_type="unverified_wfo_declared",
                description=(
                    f"{device.employee_name} ({device.employee_id}) self-declared "
                    f"WFO for {p.date} via missed-day form with no network signals "
                    f"to verify. This is their #{month_count} unverified WFO claim "
                    f"in {month_prefix}."
                ),
                severity="high",
            ))
            logger.warning(
                f"[SECURITY] Unverified WFO self-declaration: {device.employee_id} "
                f"for {p.date} (#{month_count} this month, no supporting signals)"
            )

    q    = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id==device.employee_id, CheckIn.date==p.date)))
    crec = q.scalars().first()
    if not crec:
        crec = CheckIn(employee_id=device.employee_id, employee_name=device.employee_name,
                       hostname=p.hostname, date=p.date, auto_status="manual"); db.add(crec)
    crec.final_status=lt if is_leave else final_status; crec.overridden=True
    crec.override_by=device.employee_id; crec.override_note=f"Missed day ({source})"
    crec.confidence=conf
    if unverified_wfo:
        crec.flagged = True
        crec.flag_reason = (
            f"Unverified self-declared WFO for {p.date} — no network "
            f"signals to corroborate; needs manager review."
        )
    await db.commit()
    await sync_employee_teams_card(
        device.employee_id, device.employee_name, device.team,
        f"Missed day backfilled: {p.date} → {lt if is_leave else final_status} ({source})", db)
    return {"status": "recorded", "date": p.date, "source": source}
