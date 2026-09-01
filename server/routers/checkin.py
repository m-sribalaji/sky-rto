"""
The core attendance loop: the agent posts a check-in, the user can confirm
an ambiguous one, and a manager can override a day's status by hand. This
is the most security-sensitive router in the app (signal fabrication
checks, WFO-on-leave detection) so it's kept tight and on its own.
"""
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from database import get_db, Device, CheckIn, AnomalyLog, DaySegment, LeaveRequest, PublicHoliday
from detection import verify_client_signals
from segments import (
    dominant_status_from_segments, handle_checkin, get_open_segment,
    close_segment, open_new_segment, LEAVE_TYPES,
)
from deps import (
    get_client_ip, get_device_token, get_caller_id, today_str, is_weekend, require_role,
    verify_device_auth, verify_request_signature,
    NOTIFIER_AVAILABLE, notify_override_applied, LEVEL_ALL,
    _SERVER_WEBHOOK, _SERVER_URL, sync_employee_teams_card,
)
from schemas import CheckInPayload, ConfirmPayload, OverridePayload

router = APIRouter()
logger = logging.getLogger("rto")

# -- 3. CHECK-IN -------------------------------------------
@router.post("/api/checkin")
async def checkin(p: CheckInPayload, request: Request, db: AsyncSession = Depends(get_db)):
    device = await db.get(Device, p.hostname)
    if not device: return {"action": "register_first"}
    token = get_device_token(request)
    await verify_device_auth(device, token, db)
    # This is the endpoint that produces "verified, high-confidence"
    # attendance records, so it's the one place a signature is required on
    # top of the bearer token — see deps.verify_request_signature for what
    # that does and doesn't protect against. request.body() is safe to
    # await here even though `p` was already parsed from it: Starlette
    # caches the raw bytes on first read, so this returns the exact same
    # bytes the client signed, not a re-serialized (and therefore
    # signature-mismatching) copy.
    raw_body = await request.body()
    verify_request_signature(request, raw_body, device)
    today     = p.date if p.date else today_str()
    # Skip weekends
    if is_weekend(today):
        logger.info(f"Weekend check-in skipped: {today} ({device.employee_id})")
        return {"action": "weekend_skip", "date": today}
    public_ip = get_client_ip(request)

    # Cross-check what the client claims about its network against where the
    # request actually came from — see detection.verify_client_signals for
    # why (short version: catches a hand-edited offline queue file, or a
    # hand-crafted request, claiming office LAN/DNS/VPN signals that don't
    # match where the request actually originated from).
    result = verify_client_signals(
        public_ip=public_ip, lan_ip=p.lan_ip, vpn_tunnel_ip=p.vpn_tunnel_ip,
        is_ethernet=p.is_ethernet, dns_servers=p.dns_servers, dns_domains=p.dns_domains)
    if result.flagged and (result.flag_reason or "").startswith("Signal fabrication:"):
        # result.flag_reason already names exactly which claim(s) were
        # fabricated (LAN/DNS/VPN) — log that instead of assuming it was
        # always the LAN IP, which stopped being true once the DNS/VPN
        # claims got the same cross-check.
        # NOTE: no AnomalyLog write here — the generic `if result.flagged`
        # block further down (right after _upsert_checkin) already writes
        # one unconditionally for every flagged result, fabrication
        # included. This branch exists only to log a more specific warning
        # line than that generic block does.
        logger.warning(f"[SECURITY] {device.employee_id}: {result.flag_reason}")
    elif result.flagged and result.auto_status == "wfo":
        # Any other flagged WFO result — currently the "unverified,
        # client-reported-signal-only" downgrade from the 2026-09 security
        # review (detection.classify()'s WFO paths). No AnomalyLog write
        # needed here either, same reason as above — the generic block
        # further down already covers it.
        logger.info(f"[FLAGGED] {device.employee_id}: {result.flag_reason}")

    # Backdated WFO claims (date param != the real current date) replay
    # whatever network signals the client currently has under a different
    # day's date. Security review (2026-09) found this indistinguishable
    # from a genuine same-day check-in unless separately flagged — the
    # /api/missed endpoint already treats unverified backdated WFO this
    # way, this brings /api/checkin's own `date` override in line with it.
    real_today   = today_str()
    is_backdated = bool(p.date) and p.date != real_today
    if is_backdated and result.auto_status == "wfo":
        result.flagged     = True
        backdate_reason = (
            f"Backdated WFO submission — claims {today} but was submitted "
            f"on {real_today}. A same-day network signal can't verify a "
            f"different day's attendance; needs manager review."
        )
        result.flag_reason = (
            f"{result.flag_reason} {backdate_reason}" if result.flag_reason
            else backdate_reason
        )
        logger.warning(f"[SECURITY] {device.employee_id}: {backdate_reason}")
        db.add(AnomalyLog(
            employee_id   = device.employee_id,
            employee_name = device.employee_name,
            anomaly_type  = "backdated_wfo_checkin",
            description   = (
                f"{device.employee_name} ({device.employee_id}) submitted a "
                f"backdated WFO check-in for {today} (submitted {real_today}). "
                f"LAN: {p.lan_ip or 'unknown'}."
            ),
            severity = "high",
        ))

    logger.info(f"CheckIn: {device.employee_id} lan={p.lan_ip} conn={public_ip} -> {result.auto_status}({result.confidence}){' [FLAGGED]' if result.flagged else ''}")
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
            await sync_employee_teams_card(
                device.employee_id, device.employee_name, device.team,
                f"Office signals detected while marked as {alert_reason} — flagged for review.", db)
        return {**seg_result, "detail": result.detail}

    await _upsert_checkin(device, today, public_ip, p, result, db)
    if result.flagged and result.flag_reason:
        # Distinguish real signal fabrication (client claimed office
        # signals from a connection that doesn't back that up — high
        # severity, real evidence of a lie) from the broader "unverified,
        # client-reported-only WFO" case (medium — could be genuine, just
        # not independently verifiable on this network). Previously
        # everything flagged landed under one generic "lan_mismatch"/high
        # label regardless of cause (security review, 2026-09).
        is_fabrication = (result.flag_reason or "").startswith("Signal fabrication:")
        anomaly_type = "signal_fabrication" if is_fabrication else "unverified_wfo_checkin"
        severity     = "high" if is_fabrication else "medium"
        db.add(AnomalyLog(employee_id=device.employee_id, employee_name=device.employee_name,
                          anomaly_type=anomaly_type, description=result.flag_reason, severity=severity))
        await db.commit()

    # Every check-in reaches a persistent Teams card for this person — see
    # deps.sync_employee_teams_card. Runs after the DB write is already
    # committed, and never raises, so a Teams outage can't affect whether
    # the check-in itself succeeded.
    status_text = "in the office" if result.auto_status == "wfo" else "working from home"
    event_text = f"Checked in — {status_text}" + (f" (flagged: {result.flag_reason})" if result.flagged else "")
    await sync_employee_teams_card(device.employee_id, device.employee_name, device.team, event_text, db)

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
    rec.platform=p.platform; rec.auto_status=result.auto_status
    # final_status must reflect WFO priority across all segments for the day.
    # A split day (WFH→WFO) must store "wfo" not the last raw detection.
    # Re-derive from all segments so CheckIn.final_status is always the dominant status.
    segs_q = await db.execute(select(DaySegment).where(and_(
        DaySegment.employee_id == device.employee_id,
        DaySegment.date == today,
    )))
    all_segs = segs_q.scalars().all()
    if all_segs:
        seg_data = [{"status": s.final_status or s.status} for s in all_segs]
        rec.final_status = dominant_status_from_segments(seg_data) or result.auto_status
    else:
        rec.final_status = result.auto_status
    rec.confidence=result.confidence; rec.flagged=result.flagged; rec.flag_reason=result.flag_reason
    await db.commit()

# -- 4. CONFIRM --------------------------------------------
@router.post("/api/confirm")
async def confirm(p: ConfirmPayload, request: Request, db: AsyncSession = Depends(get_db)):
    device = await db.get(Device, p.hostname)
    if not device: raise HTTPException(404, "Not registered")
    token = get_device_token(request)
    await verify_device_auth(device, token, db)
    # Same signature requirement as /api/checkin — this endpoint also
    # writes an attendance status, so a bearer token alone (which, per
    # security review 2026-09, travels in plaintext over this deployment's
    # unencrypted HTTP) shouldn't be sufficient on its own to flip it.
    raw_body = await request.body()
    verify_request_signature(request, raw_body, device)
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
@router.post("/api/override")
async def override(p: OverridePayload, request: Request,
                   db: AsyncSession = Depends(get_db)):
    caller_id = await require_role(request, db, "manager", caller_id=p.override_by)
    # Same signature requirement as /api/checkin — a manager's bearer
    # token alone (plaintext-HTTP-exposed per security review 2026-09)
    # shouldn't be sufficient by itself to rewrite someone else's
    # attendance history. Signed with the CALLER's own device/token, not
    # the target employee's — require_role already verified caller_id
    # owns a real device and a valid token.
    caller_dq = await db.execute(select(Device).where(Device.employee_id == caller_id))
    caller_device = caller_dq.scalars().first()
    if not caller_device: raise HTTPException(401, "Caller device not found.")
    raw_body = await request.body()
    verify_request_signature(request, raw_body, caller_device)
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
    await sync_employee_teams_card(
        p.employee_id, device.employee_name, device.team,
        f"Manager override: {old_status} → {p.new_status} (by {p.override_by})" + (f" — {p.note}" if p.note else ""),
        db)
    return {"status": "overridden", "employee_id": p.employee_id, "date": p.date}
