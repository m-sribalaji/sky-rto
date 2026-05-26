# segments.py - Split day segment management
import json
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, desc
from database import DaySegment, LeaveRequest, PublicHoliday
import logging

logger = logging.getLogger("segments")

LEAVE_TYPES = {
    "annual":           {"label": "Annual Leave",         "emoji": "", "icon": "palm-tree"},
    "casual":           {"label": "Casual Leave",         "emoji": "", "icon": "calendar-days"},
    "sick":             {"label": "Sick Leave",           "emoji": "", "icon": "stethoscope"},
    "public_holiday":   {"label": "Public Holiday",       "emoji": "", "icon": "sparkles"},
    "optional_holiday": {"label": "Optional Holiday",     "emoji": "", "icon": "calendar-plus"},
    "half_day_am":      {"label": "Half Day AM",          "emoji": "", "icon": "sunrise",   "half": True},
    "half_day_pm":      {"label": "Half Day PM",          "emoji": "", "icon": "sunset",    "half": True},
    "wfh_unplanned":    {"label": "WFH (Retroactive)",    "emoji": "", "icon": "home"},
    "wfo_unplanned":    {"label": "WFO (Retroactive)",    "emoji": "", "icon": "building"},
    "other":            {"label": "Other",                "emoji": "", "icon": "file-text"},
}

def get_leave_meta(leave_type: str) -> dict:
    return LEAVE_TYPES.get(leave_type, {
        "label": leave_type.replace("_"," ").title(),
        "emoji": "", "icon": "file-text",
    })

def dominant_status_from_segments(segments: list) -> str:
    """WFO priority: if ANY segment that day was WFO, the day = WFO.
    Single source of truth used by stats, week, today, history endpoints."""
    if any(s.get("status") == "wfo" for s in segments):
        return "wfo"
    if any(s.get("status") == "wfh" for s in segments):
        return "wfh"
    return segments[0].get("status") if segments else None


async def get_open_segment(employee_id: str, date: str, db: AsyncSession):
    q = await db.execute(
        select(DaySegment).where(and_(
            DaySegment.employee_id == employee_id,
            DaySegment.date == date,
            DaySegment.ended_at == None,
        )).order_by(desc(DaySegment.segment_number))
    )
    return q.scalars().first()

async def get_all_segments(employee_id: str, date: str, db: AsyncSession):
    q = await db.execute(
        select(DaySegment).where(and_(
            DaySegment.employee_id == employee_id,
            DaySegment.date == date,
        )).order_by(DaySegment.segment_number)
    )
    return q.scalars().all()

async def close_segment(seg: DaySegment, db: AsyncSession) -> float:
    now = datetime.now(timezone.utc)
    seg.ended_at = now
    st = seg.started_at.replace(tzinfo=timezone.utc) if seg.started_at.tzinfo is None else seg.started_at
    seg.duration_minutes = round((now - st).total_seconds() / 60, 1)
    await db.commit()
    return seg.duration_minutes

async def open_new_segment(
    employee_id, employee_name, hostname, date,
    status, final_status, confidence, source,
    public_ip, lan_ip, vpn_active, vpn_tunnel_ip,
    dns_servers, dns_domains, is_ethernet, platform,
    flagged, flag_reason, db: AsyncSession,
    queued_at: str = None,
) -> DaySegment:
    existing = await get_all_segments(employee_id, date, db)
    # Use queued_at as started_at if provided — preserves real transition time
    # when checkin was queued offline (VPN off) and synced later
    if queued_at:
        try:
            started = datetime.fromisoformat(queued_at.replace("Z", "+00:00"))
        except Exception:
            started = datetime.now(timezone.utc)
    else:
        started = datetime.now(timezone.utc)
    seg = DaySegment(
        employee_id=employee_id, employee_name=employee_name,
        hostname=hostname, date=date, segment_number=len(existing)+1,
        status=status, final_status=final_status,
        confidence=confidence, source=source,
        started_at=started,
        public_ip=public_ip, lan_ip=lan_ip,
        vpn_active=vpn_active, vpn_tunnel_ip=vpn_tunnel_ip,
        dns_servers=json.dumps(dns_servers or []),
        dns_domains=json.dumps(dns_domains or []),
        is_ethernet=is_ethernet, platform=platform,
        flagged=flagged, flag_reason=flag_reason,
    )
    db.add(seg)
    await db.commit()
    await db.refresh(seg)
    return seg

async def handle_checkin(
    employee_id, employee_name, hostname, date,
    new_status, confidence, source,
    public_ip, lan_ip, vpn_active, vpn_tunnel_ip,
    dns_servers, dns_domains, is_ethernet, platform,
    flagged, flag_reason, db: AsyncSession,
    queued_at: str = None,
) -> dict:
    if new_status == "vpn_ambiguous":
        return {"action": "confirm_needed", "lan_ip": lan_ip, "public_ip": public_ip}

    # -- Override lock: only block auto check-in if manager overrode
    # a day that already had real network segments. If the override was
    # applied to a leave day (no real segments), let auto check-in through -
    # real signals should win (employee may or may not have come in).
    existing_segs = await get_all_segments(employee_id, date, db)
    real_segs = [s for s in existing_segs
                 if s.overridden and s.source != "manager_override"]
    manager_only_override = (
        any(s.overridden for s in existing_segs) and
        all(s.source == "manager_override" for s in existing_segs)
    )
    if real_segs:
        locked_status = next(s.final_status or s.status for s in existing_segs if s.overridden)
        logger.info(f"Override lock: {employee_id} {date} locked as {locked_status} - skipping auto check-in")
        return {"action": "override_locked", "status": locked_status}

    if not manager_only_override:
        lq = await db.execute(
            select(LeaveRequest).where(and_(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.date == date,
            ))
        )
        if lq.scalars().first():
            logger.info(f"Leave lock: {employee_id} {date} has leave - skipping auto check-in")
            return {"action": "leave_recorded", "status": "leave"}

    open_seg = await get_open_segment(employee_id, date, db)

    if not open_seg:
        seg = await open_new_segment(
            employee_id, employee_name, hostname, date,
            new_status, new_status, confidence, source,
            public_ip, lan_ip, vpn_active, vpn_tunnel_ip,
            dns_servers, dns_domains, is_ethernet, platform,
            flagged, flag_reason, db, queued_at=queued_at)
        return {"action": "ok", "status": new_status, "confidence": confidence,
                "segment_number": seg.segment_number, "split": False}

    if open_seg.final_status == new_status:
        return {"action": "already_checked_in", "status": new_status,
                "segment_number": open_seg.segment_number, "split": False}

    duration = await close_segment(open_seg, db)
    new_seg  = await open_new_segment(
        employee_id, employee_name, hostname, date,
        new_status, new_status, confidence, source,
        public_ip, lan_ip, vpn_active, vpn_tunnel_ip,
        dns_servers, dns_domains, is_ethernet, platform,
        flagged, flag_reason, db, queued_at=queued_at)
    logger.info(f"Split day: {employee_id} {date} "
                f"seg{open_seg.segment_number}({open_seg.final_status},{duration:.0f}m)"
                f"→seg{new_seg.segment_number}({new_status})")
    return {"action": "ok", "status": new_status, "confidence": confidence,
            "segment_number": new_seg.segment_number, "split": True,
            "previous_status": open_seg.final_status,
            "previous_duration_minutes": duration}

async def get_day_summary(employee_id: str, date: str, db: AsyncSession) -> dict:
    segments = await get_all_segments(employee_id, date, db)
    lq = await db.execute(select(LeaveRequest).where(and_(
        LeaveRequest.employee_id == employee_id, LeaveRequest.date == date)))
    leaves = lq.scalars().all()
    phq    = await db.execute(select(PublicHoliday).where(PublicHoliday.date == date))
    holidays = phq.scalars().all()

    seg_data, total_min = [], 0
    for s in segments:
        if not s.ended_at:
            now = datetime.now(timezone.utc)
            st  = s.started_at.replace(tzinfo=timezone.utc) if s.started_at.tzinfo is None else s.started_at
            dur = (now - st).total_seconds() / 60
        else:
            dur = s.duration_minutes or 0
        total_min += dur
        seg_data.append({"segment_number": s.segment_number,
                         "status": s.final_status or s.status,
                         "started_at": s.started_at.isoformat()+"Z",
                         "ended_at": (s.ended_at.isoformat()+"Z") if s.ended_at else None,
                         "duration_minutes": round(dur,1),
                         "confidence": s.confidence, "flagged": s.flagged, "source": s.source})

    leave_data   = [{"leave_type": l.leave_type, "label": get_leave_meta(l.leave_type)["label"],
                     "emoji": get_leave_meta(l.leave_type)["emoji"],
                     "icon": get_leave_meta(l.leave_type)["icon"],
                     "half_day_period": l.half_day_period, "note": l.note, "source": l.source}
                    for l in leaves]
    holiday_data = [{"name": h.name, "optional": h.optional} for h in holidays]

    if holidays and not segments and not leaves: display_status = "public_holiday"
    elif leaves and not segments:                display_status = leaves[0].leave_type
    elif segments:
        # Build merged first, then decide display_status from merged result
        pass  # handled below after merge
    else:
        display_status = None

    split_label = None
    if seg_data:
        # ── Same-status merge ─────────────────────────────
        merged = []
        for s in seg_data:
            if merged and merged[-1]["status"] == s["status"]:
                merged[-1]["ended_at"] = s["ended_at"]
                merged[-1]["duration_minutes"] = round(
                    merged[-1]["duration_minutes"] + s["duration_minutes"], 1)
            else:
                merged.append(dict(s))

        # ── display_status from merged segments ──────────
        if not (holidays and not leaves):
            if len(merged) > 1:
                display_status = "split"
            else:
                display_status = merged[0]["status"]

        # ── Build transition label ────────────────────────
        # Format: WFH 01:19 -> WFO 13:44 -> WFH 20:34
        # All segments show status + their start time
        if len(merged) == 1:
            pass
        else:
            parts = []
            for i, s in enumerate(merged):
                status_label = s["status"].upper()
                # All segments get a time — including the first
                try:
                    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                    started_iso = s["started_at"]
                    started_iso = started_iso.replace("Z", "+00:00")
                    dt_utc  = _dt.fromisoformat(started_iso)
                    if dt_utc.tzinfo is None:
                        dt_utc = dt_utc.replace(tzinfo=_tz.utc)
                    dt_ist  = dt_utc.astimezone(_tz(offset=_td(hours=5, minutes=30)))
                    time_part = dt_ist.strftime("%H:%M")
                except Exception:
                    time_part = "?"
                parts.append(f"{status_label} {time_part}")
            split_label = " → ".join(parts)

    return {"date": date, "employee_id": employee_id, "display_status": display_status,
            "split_label": split_label, "is_split": split_label is not None,
            "segments": seg_data, "leaves": leave_data, "holidays": holiday_data,
            "total_work_minutes": round(total_min, 1)}