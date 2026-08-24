"""
The "smart" endpoints — WFO forecasting for an individual and team rhythm
analysis (who tends to overlap with whom, best meeting days). These lean on
analytics.py for the actual math; this file is just access control plus
shaping the DB rows into whatever analytics.py expects.
"""
from datetime import date, timedelta
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from database import get_db, Device, CheckIn, DaySegment, LeaveRequest, PublicHoliday
from segments import dominant_status_from_segments
from deps import get_caller_context, get_caller_id, get_managed_teams, require_role
from analytics import compute_forecast, compute_team_rhythm, _workdays_in_month
from backtest import run_backtest
from narrator import get_narrative

_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

router = APIRouter()

@router.get("/api/insights/{employee_id}")
async def get_insights(employee_id: str, request: Request = None,
                       db: AsyncSession = Depends(get_db)):
    """
    Personal WFO forecast for the next 14 working days + monthly progress.
    Accessible by the employee themselves, their manager, or admin.
    """
    caller_device, caller_role = await get_caller_context(request, db)
    caller_id = get_caller_id(request) if request else None
    # Access control:
    #   admin    → anyone
    #   manager  → anyone in their managed teams
    #   employee → own record OR any member of their own team
    if caller_id and caller_id != employee_id:
        if caller_role == "employee":
            # Employees may view teammates' insights
            dq_target = await db.execute(select(Device).where(Device.employee_id == employee_id))
            target_dev = dq_target.scalars().first()
            if not target_dev or not caller_device or caller_device.team != target_dev.team:
                raise HTTPException(403, "Employees can only view insights for their own team members")
        else:
            managed = await get_managed_teams(caller_id, db)
            if managed is not None:
                dq = await db.execute(select(Device).where(Device.employee_id == employee_id))
                dev_chk = dq.scalars().first()
                if not dev_chk or dev_chk.team not in managed:
                    raise HTTPException(403, "Access denied")

    dq = await db.execute(select(Device).where(Device.employee_id == employee_id))
    device = dq.scalars().first()
    if not device:
        raise HTTPException(404, "Employee not registered")

    today = date.today()
    month = today.strftime("%Y-%m")

    # Fetch last 12 weeks of checkins
    cutoff = (today - timedelta(weeks=12)).isoformat()
    ci_q = await db.execute(select(CheckIn).where(and_(
        CheckIn.employee_id == employee_id,
        CheckIn.date >= cutoff,
    )).order_by(CheckIn.date))
    raw_checkins = ci_q.scalars().all()

    # Correct stale final_status for split days:
    # CheckIn.final_status should always reflect dominant segment status (WFO priority).
    # Old records written before this fix may have the wrong status stored.
    corrected = False
    for r in raw_checkins:
        segs_q2 = await db.execute(select(DaySegment).where(and_(
            DaySegment.employee_id == employee_id,
            DaySegment.date == r.date,
        )))
        segs2 = segs_q2.scalars().all()
        if segs2:
            seg_data2 = [{"status": s.final_status or s.status} for s in segs2]
            correct_status = dominant_status_from_segments(seg_data2) or r.auto_status
            if r.final_status != correct_status:
                r.final_status = correct_status
                corrected = True
    if corrected:
        await db.commit()

    checkins = [{"date": r.date, "status": r.final_status or r.auto_status or "wfh"}
                for r in raw_checkins]

    # Fetch leaves
    lv_q = await db.execute(select(LeaveRequest).where(and_(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.date >= cutoff,
    )))
    leaves = [{"date": l.date, "leave_type": l.leave_type}
              for l in lv_q.scalars().all()]

    # Public holidays
    ph_q = await db.execute(select(PublicHoliday))
    ph_dates = {h.date for h in ph_q.scalars().all()}

    # Teammates' combined check-in history — pooled into a team-wide
    # day-of-week baseline that this person's own rate shrinks toward when
    # they don't have much personal data yet (see analytics._shrink_to_team).
    # Not required for the forecast to work — compute_forecast no-ops the
    # shrinkage if this comes back empty — just meaningfully better for
    # anyone new enough that their own history is thin.
    team_checkins: list[dict] = []
    if device.team:
        team_ci_q = await db.execute(select(CheckIn).where(and_(
            CheckIn.employee_id != employee_id,
            CheckIn.date >= cutoff,
        )).join(Device, Device.employee_id == CheckIn.employee_id).where(Device.team == device.team))
        team_checkins = [{"date": r.date, "status": r.final_status or r.auto_status or "wfh"}
                          for r in team_ci_q.scalars().all()]

    # Current month actual WFO count
    seg_q = await db.execute(select(DaySegment).where(and_(
        DaySegment.employee_id == employee_id,
        DaySegment.date.like(f"{month}%"),
    )))
    segs_month = seg_q.scalars().all()
    date_status_month: dict = {}
    for seg in segs_month:
        ds = seg.date
        st = seg.final_status or seg.status
        if date_status_month.get(ds) != "wfo":
            date_status_month[ds] = st
    current_month_wfo = sum(1 for ds, st in date_status_month.items()
                             if st == "wfo" and ds not in ph_dates)

    result = compute_forecast(
        employee_id         = employee_id,
        employee_name       = device.employee_name,
        checkins            = checkins,
        leaves              = leaves,
        ph_dates            = ph_dates,
        today               = today,
        forecast_days       = 14,
        current_month_wfo   = current_month_wfo,
        current_month_total_workdays = len(_workdays_in_month(today.year, today.month, ph_dates)),
        team_checkins       = team_checkins,
    )

    # Narratives paraphrase the numbers already in `result` — see
    # narrator.py's module docstring for why they never compute anything
    # themselves. Each section caches independently and only re-calls the
    # LLM when its own facts actually changed, so a WFH->WFO correction
    # refreshes just the affected section(s), not all three.
    result["narratives"] = {}
    if not result.get("insufficient_data"):
        m = result["monthly"]
        progress_facts = {
            "month": m["month"], "actual_wfo": m["actual_wfo"], "target": m["target"],
            "needed": m["needed"], "elapsed_days": m["elapsed_days"],
            "remaining_days": m["remaining_days"], "achievable": m["achievable"],
            "on_track": m["on_track"], "predicted_total": m.get("predicted_total"),
            "confidence_label": result["confidence_label"],
            "predictability": result["predictability"],
        }
        pattern_facts = {
            "dow_rates_percent": {_DOW_NAMES[int(k)]: round(v * 100) for k, v in result["dow_rates_stable"].items()},
            "active_weeks": result["active_weeks"],
            "confidence_label": result["confidence_label"],
            "predictability": result["predictability"],
        }
        outlook_facts = {
            "wfh_budget": result["wfh_budget"],
            "projected_month_total": result["projected_month_total"],
            "monthly_target": m["target"], "monthly_needed": m["needed"],
            "weeks": [{
                "week_start": w["week_start"], "week_end": w["week_end"],
                "week_target": w["week_target"], "projected_wfo": w["projected_wfo"],
                "at_risk": w["risk"], "is_current_week": w["is_current_week"],
                "summary": w["week_summary"],
            } for w in result["compliance_weeks"]],
        }
        result["narratives"]["progress"] = await get_narrative(
            db, "employee", employee_id, "progress", progress_facts)
        result["narratives"]["pattern"] = await get_narrative(
            db, "employee", employee_id, "pattern", pattern_facts)
        result["narratives"]["compliance_outlook"] = await get_narrative(
            db, "employee", employee_id, "compliance_outlook", outlook_facts)

    return result


@router.get("/api/insights-members")
async def get_insights_members(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Returns the list of employees whose insights the caller can view.
    - Employee: own team only
    - Manager:  managed teams only (or all if no restriction)
    - Admin:    everyone
    Used to populate the person-picker in the Insights UI.
    """
    caller_device, caller_role = await get_caller_context(request, db)
    caller_id = get_caller_id(request)

    if caller_role == "employee":
        # Scoped strictly to own team
        team = caller_device.team if caller_device else None
        if team:
            dq = await db.execute(
                select(Device).where(Device.team == team).order_by(Device.employee_name))
        else:
            dq = await db.execute(
                select(Device).where(Device.employee_id == caller_id))
        devices = dq.scalars().all()
    elif caller_role == "manager":
        managed = await get_managed_teams(caller_id, db)
        if managed is not None:
            dq = await db.execute(
                select(Device).where(Device.team.in_(managed))
                .order_by(Device.team, Device.employee_name))
        else:
            dq = await db.execute(
                select(Device).order_by(Device.team, Device.employee_name))
        devices = dq.scalars().all()
    else:  # admin
        dq = await db.execute(
            select(Device).order_by(Device.team, Device.employee_name))
        devices = dq.scalars().all()

    return {
        "members": [
            {"employee_id": d.employee_id, "employee_name": d.employee_name, "team": d.team or ""}
            for d in devices
        ]
    }


@router.get("/api/rhythm/{team}")
async def get_team_rhythm(team: str, request: Request = None,
                          lookback_weeks: int = 8,
                          db: AsyncSession = Depends(get_db)):
    """
    Team rhythm analysis: overlap matrix, best meeting days, heatmap.
    Accessible by all team members, managers, and admins.
    Employees can only query their own team.
    """
    caller_device, caller_role = await get_caller_context(request, db)
    if caller_role == "employee":
        if caller_device and caller_device.team != team:
            raise HTTPException(403, "Employees can only view their own team rhythm")

    # Get all team members
    dq = await db.execute(select(Device).where(Device.team == team).order_by(Device.employee_name))
    devices = dq.scalars().all()
    if not devices:
        raise HTTPException(404, f"No members found for team: {team}")

    today = date.today()
    cutoff = (today - timedelta(weeks=lookback_weeks + 2)).isoformat()

    # Public holidays
    ph_q = await db.execute(select(PublicHoliday))
    ph_dates = {h.date for h in ph_q.scalars().all()}

    members = []
    for dev in devices:
        ci_q = await db.execute(select(CheckIn).where(and_(
            CheckIn.employee_id == dev.employee_id,
            CheckIn.date >= cutoff,
        )).order_by(CheckIn.date))
        checkins = [{"date": r.date, "status": r.final_status or r.auto_status or "wfh"}
                    for r in ci_q.scalars().all()]

        lv_q = await db.execute(select(LeaveRequest).where(and_(
            LeaveRequest.employee_id == dev.employee_id,
            LeaveRequest.date >= cutoff,
        )))
        leaves = [{"date": l.date, "leave_type": l.leave_type}
                  for l in lv_q.scalars().all()]

        members.append({
            "employee_id":   dev.employee_id,
            "employee_name": dev.employee_name,
            "checkins":      checkins,
            "leaves":        leaves,
        })

    result = compute_team_rhythm(
        members        = members,
        ph_dates       = ph_dates,
        today          = today,
        lookback_weeks = lookback_weeks,
    )
    result["team"] = team

    rhythm_facts = {
        "team": team,
        "team_size": result["team_size"],
        "lookback_weeks": result["lookback_weeks"],
        "best_days": [{"day": bd["dow_name"], "percent_in_office": round(bd["probability"] * 100),
                       "members_with_data": bd["members_with_data"]}
                      for bd in result["best_days"][:3]],
        "collaboration_gaps": [g["message"] for g in result["gaps"]],
        "individual_summaries": [{
            "name": p["employee_name"], "confidence": p["confidence"],
            "active_weeks": p["active_weeks"], "current_wfo_streak": p["current_streak_wfo"],
        } for p in result["individual"]],
    }
    result["narrative"] = await get_narrative(db, "team", team, "team_rhythm", rhythm_facts)
    return result


@router.get("/api/admin/backtest")
async def get_backtest(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Replays everyone's real history through the forecast model — for each
    employee's own recorded workday, predicts it using only data that
    predated it, and compares to what actually happened. Reports Brier
    score against a naive baseline (each employee's own long-run WFO rate,
    no day-of-week or Markov structure) so "the model scores 0.18" has
    something to be judged against. See backtest.py for the full method.
    Admin-only: this walks every employee's full history in one request.
    """
    await require_role(request, db, "admin")
    return await run_backtest(db)
