"""
Read-only reporting endpoints that power the dashboard UI: today's status
board, weekly/monthly rollups, compliance scoring, and the CSV export.
Nothing here writes attendance data — it's all queries and aggregation over
what checkin.py / leave.py already wrote.
"""
import json
import csv
import io
import logging
from datetime import date, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc, func
from fastapi.responses import StreamingResponse

from database import get_db, Device, CheckIn, DaySegment, LeaveRequest, PublicHoliday, AnomalyLog, Role
from segments import dominant_status_from_segments, get_day_summary, LEAVE_TYPES, get_leave_meta
from deps import today_str, get_caller_id, get_caller_context, get_managed_teams, get_role, require_role, require_registered_caller

router = APIRouter()
logger = logging.getLogger("rto")

# -- 9. DASHBOARD APIS -------------------------------------
@router.get("/api/today")
async def get_today(team: str=None, request: Request=None,
                    db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
    q = await db.execute(select(CheckIn).where(
        CheckIn.date==today_str()).order_by(desc(CheckIn.timestamp)))
    caller_id = get_caller_id(request) if request else None
    managed = await get_managed_teams(caller_id, db) if caller_id else None
    result = []
    for r in q.scalars().all():
        dq = await db.execute(select(Device).where(Device.employee_id==r.employee_id))
        dev = dq.scalars().first()
        if caller_role == "employee" and (not dev or dev.team != caller_device.team):
            continue
        s = await get_day_summary(r.employee_id, r.date, db)
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

@router.get("/api/today/team")
async def get_today_team(team: str, request: Request=None, db: AsyncSession=Depends(get_db)):
    """All members of a team with today's status - includes those not yet checked in."""
    caller_device, caller_role = await get_caller_context(request, db)
    if caller_role == "employee" and team != caller_device.team:
        raise HTTPException(403, "Employees can only view their own team")
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

@router.get("/api/me")
async def get_me(request: Request, db: AsyncSession=Depends(get_db)):
    """Return device info for the calling employee - used by client to get team."""
    dev = await require_registered_caller(request, db)
    rq  = await db.execute(select(Role).where(Role.employee_id==dev.employee_id))
    role = rq.scalars().first()
    return {"employee_id": dev.employee_id, "employee_name": dev.employee_name,
            "team": dev.team, "platform": dev.platform,
            "role": role.role if role else "employee"}

@router.get("/api/stats")
async def get_stats(team: str=None, request: Request=None,
                    db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
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
        if caller_role == "employee" and (not dev2 or dev2.team != caller_device.team): continue
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

@router.get("/api/week")
async def get_week(team: str=None, request: Request=None,
                   db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
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
            if caller_role == "employee" and (not dev3 or dev3.team != caller_device.team): continue
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

@router.get("/api/history/{employee_id}")
async def get_history(employee_id: str, month: str=None,
                      request: Request=None, db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
    if not month: month=date.today().strftime("%Y-%m")
    # Access check: manager can only view employees in their managed teams
    caller_id = get_caller_id(request) if request else None
    if caller_id and caller_id != employee_id:
        if caller_role == "employee":
            raise HTTPException(403, "Access denied")
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

@router.get("/api/anomalies")
async def get_anomalies(team: str=None, request: Request=None, db: AsyncSession=Depends(get_db)):
    caller_id = await require_role(request, db, "manager")
    q = await db.execute(select(AnomalyLog).where(
        AnomalyLog.resolved==False).order_by(desc(AnomalyLog.detected_at)))
    rows = q.scalars().all()

    # A manager restricted to specific teams shouldn't see anomalies for
    # people outside those teams — this endpoint never actually checked
    # that (it just required "manager or above" and returned everything
    # org-wide). Some anomalies aren't attributable to any employee at all
    # (e.g. token_enumeration is logged against a source IP, not a person)
    # — those stay visible to every manager/admin since there's no team to
    # scope them to.
    managed = await get_managed_teams(caller_id, db)
    if managed is not None or team:
        emp_ids = {r.employee_id for r in rows if r.employee_id and r.employee_id != "unknown"}
        dq = await db.execute(select(Device).where(Device.employee_id.in_(emp_ids)))
        team_by_emp = {d.employee_id: d.team for d in dq.scalars().all()}
        def _keep(r):
            emp_team = team_by_emp.get(r.employee_id)
            if emp_team is None:
                return True  # not attributable to a device/team — always visible to managers+
            if team and emp_team != team:
                return False
            if managed is not None and emp_team not in managed:
                return False
            return True
        rows = [r for r in rows if _keep(r)]

    return {"anomalies": [{"id": r.id, "employee_id": r.employee_id,
                           "employee_name": r.employee_name, "type": r.anomaly_type,
                           "description": r.description, "severity": r.severity,
                           "detected_at": r.detected_at.isoformat()+"Z"}
                          for r in rows]}

@router.patch("/api/anomalies/{anomaly_id}/resolve")
async def resolve_anomaly(anomaly_id: int, request: Request, db: AsyncSession=Depends(get_db)):
    """Mark an anomaly as reviewed so it drops off the active list.
    Manager or above — same access level as viewing them."""
    await require_role(request, db, "manager")
    row = await db.get(AnomalyLog, anomaly_id)
    if not row:
        raise HTTPException(404, "Anomaly not found")
    row.resolved = True
    await db.commit()
    return {"status": "resolved", "id": anomaly_id}

@router.get("/api/team")
async def get_team(request: Request=None, db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
    caller_id = get_caller_id(request) if request else None
    managed = await get_managed_teams(caller_id, db) if caller_id else None
    dq = await db.execute(select(Device).order_by(Device.employee_name))
    result = []
    for d in dq.scalars().all():
        if caller_role == "employee" and d.employee_id != caller_device.employee_id: continue
        if managed is not None and d.team not in managed: continue
        role = await get_role(d.employee_id, db)
        result.append({"hostname": d.hostname, "employee_name": d.employee_name,
                       "employee_id": d.employee_id, "team": d.team,
                       "platform": d.platform, "role": role})
    return {"team": result}

@router.get("/api/leave-types")
async def get_leave_types():
    return {"leave_types": [{"type": k, **v} for k,v in LEAVE_TYPES.items()]}

@router.get("/api/compliance")
async def get_compliance(month: str=None, team: str=None, request: Request=None,
                         db: AsyncSession=Depends(get_db)):
    await require_role(request, db, "manager")
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
        # Green  : 12+ WFO days — monthly target fully met
        # Amber  : below 12, month in progress, no weekly misses — on track
        # Orange : below 12, month in progress, but at least one week missed 3 days
        #          (heads-up: weekly pattern is off, but month not over yet)
        # Red    : month complete AND below 12 WFO days — real compliance failure
        # Grey   : no data at all
        total_activity = len(records) + len(date_status)
        working = wfo + wfh

        # Is the month fully completed?
        month_complete = bool(completed_weeks) and len(completed_weeks) == len(weeks)
        needed = max(0, 12 - wfo)
        missed_weeks = weeks_total - weeks_passed

        if total_activity == 0:
            rag    = "grey"
            status = "No data yet"
        elif wfo >= 12:
            rag    = "green"
            status = "Monthly target met"
        elif month_complete:
            # Month over, below 12 — hard red
            rag    = "red"
            status = f"{wfo}/12 days - monthly target missed"
        elif not all_weeks_ok:
            # Month ongoing but weekly pattern has gaps — orange warning
            rag    = "orange"
            status = f"{wfo}/12 days - {missed_weeks} week{'s' if missed_weeks!=1 else ''} below 3 days"
        else:
            # Month ongoing, all weeks fine — amber (behind but consistent)
            rag    = "amber"
            status = f"{wfo}/12 days - {needed} more WFO day{'s' if needed!=1 else ''} needed"

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

@router.get("/api/export")
async def export_csv(month: str=None, request: Request=None,
                     db: AsyncSession=Depends(get_db)):
    """Manager/admin monthly attendance export — team-wise, compliance-focused.
    Columns: Team | Employee | ID | Days WFO | Days WFH | Days Leave | Days Absent
             | RTO% | Wk1 | Wk2 | Wk3 | Wk4 | Wk5 | Notes
    """
    if not month: month = date.today().strftime("%Y-%m")
    caller_device = await require_registered_caller(request, db)
    role = await get_role(caller_device.employee_id, db)
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

        # Monthly totals (public holidays excluded from wfh count)
        past_days = [ds for ds in working_days if date.fromisoformat(ds) < today]
        for ds in past_days:
            r = records.get(ds)
            if ds in leaves:
                leave += 1
            elif ds in ph_dates:
                leave += 1  # public holiday counts same as leave
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
        # Use 12-day monthly target for notes
        if wfo >= 12: notes.append("Monthly target met")
        elif wfo + wfh > 0: notes.append(f"{wfo}/12 WFO days")

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

@router.get("/api/team-leave")
async def get_team_leave(month: str=None, team: str=None,
                         request: Request=None, db: AsyncSession=Depends(get_db)):
    caller_device, caller_role = await get_caller_context(request, db)
    if not month: month=date.today().strftime("%Y-%m")
    caller_id_tl = get_caller_id(request) if request else None
    managed_tl = await get_managed_teams(caller_id_tl, db) if caller_id_tl else None
    stmt = select(LeaveRequest).where(LeaveRequest.date.like(f"{month}%"))
    if caller_role == "employee":
        team = caller_device.team
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

