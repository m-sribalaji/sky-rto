"""
analytics.py - RTO Tracker Intelligence Engine
================================================
Provides two capabilities:

  1. Personal WFO Forecast (Insights tab)
     Model: EWMA day-of-week base rate + Markov day-transition adjustment
            + deterministic overrides (leave / public holiday / already checked in)
     Outputs: per-day probability for next 14 working days, confidence tier,
              monthly progress, needed-days calculation

  2. Team Rhythm Analysis (Team Rhythm tab)
     Model: Jaccard similarity on leave-adjusted WFO attendance sets (overlap),
            EWMA per day-of-week for best-meeting-days,
            leave-aware pattern computation per person
     Outputs: overlap heatmap, best meeting days, individual rhythm rows,
              collaboration gap flags

Both models degrade gracefully when data is scarce:
  < 2 weeks  → "Insufficient data" returned, no prediction shown
  2–4 weeks  → Low confidence, wide probability bands shown
  4–8 weeks  → Medium confidence
  8+ weeks   → High confidence

No external ML libraries required — pure Python / stdlib math.
"""

from __future__ import annotations
import math
import logging
from datetime import date, timedelta
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("analytics")

# ── constants ──────────────────────────────────────────────────────────────
LEAVE_TYPES = {
    "annual","casual","sick","public_holiday","optional_holiday",
    "half_day_am","half_day_pm","wfh_unplanned","wfo_unplanned","other",
}

# EWMA decay: most-recent week has weight ~0.5, 4 weeks ago ~0.06
EWMA_ALPHA  = 0.45

# Markov transition multipliers (applied to base day-of-week rate)
# Based on observed human behaviour: consecutive WFO days cluster
MARKOV = {
    ("wfo",  "wfo"):  1.25,   # WFO yesterday → more likely WFO today
    ("wfo",  "wfh"):  0.80,
    ("wfh",  "wfo"):  0.90,
    ("wfh",  "wfh"):  1.10,
    ("leave","wfo"):  0.70,   # Back from leave → less likely WFO first day
    ("leave","wfh"):  1.20,
    (None,   "wfo"):  1.00,   # No prior info → neutral
    (None,   "wfh"):  1.00,
}

# Absence threshold: if no check-in by 11am local time, decay today's WFO prob
ABSENCE_DECAY = 0.15   # multiplier applied when unexpectedly absent at 11am

# Confidence tiers based on weeks of data with non-zero activity
def _confidence_tier(active_weeks: int) -> str:
    if active_weeks < 2:  return "insufficient"
    if active_weeks < 4:  return "low"
    if active_weeks < 8:  return "medium"
    return "high"

def _confidence_label(tier: str) -> str:
    return {
        "insufficient": "Not enough data yet",
        "low":          "Low confidence — based on limited history",
        "medium":       "Medium confidence — pattern stabilising",
        "high":         "High confidence — strong historical pattern",
    }.get(tier, tier)

# ── date helpers ───────────────────────────────────────────────────────────
def _is_weekday(d: date) -> bool:
    return d.weekday() < 5

def _next_n_workdays(start: date, n: int, ph_dates: set[str]) -> list[date]:
    """Return next n working days from start (exclusive), skipping weekends and public holidays."""
    days, cur = [], start
    while len(days) < n:
        cur += timedelta(days=1)
        if _is_weekday(cur) and cur.isoformat() not in ph_dates:
            days.append(cur)
    return days

def _workdays_in_month(yr: int, mo: int, ph_dates: set[str]) -> list[str]:
    try:
        last = (date(yr, mo % 12 + 1, 1) - timedelta(days=1)).day if mo < 12 else 31
    except Exception:
        last = 31
    result = []
    for day in range(1, last + 1):
        try:
            d = date(yr, mo, day)
        except ValueError:
            break
        if _is_weekday(d) and d.isoformat() not in ph_dates:
            result.append(d.isoformat())
    return result

# ── attendance record builder ──────────────────────────────────────────────
def build_attendance(
    checkins:  list[dict],   # [{date, status}] — dominant status per day
    leaves:    list[dict],   # [{date, leave_type}]
    ph_dates:  set[str],
) -> dict[str, str]:
    """
    Build a clean date → status map:
      'wfo' | 'wfh' | 'leave' | 'public_holiday'

    Priority: leave > public_holiday > checkin dominant status
    Leave-adjusted: public holidays override wfh checkins.
    """
    att: dict[str, str] = {}

    # Layer 1: check-in dominant status
    for r in checkins:
        ds, st = r.get("date",""), r.get("status","")
        if ds and st:
            att[ds] = "wfo" if st == "wfo" else "wfh"

    # Layer 2: public holidays override wfh (not wfo — someone may have come in)
    for ds in list(att.keys()):
        if ds in ph_dates and att[ds] != "wfo":
            att[ds] = "public_holiday"
    for ds in ph_dates:
        if ds not in att:
            att[ds] = "public_holiday"

    # Layer 3: explicit leave records override everything except wfo
    for lv in leaves:
        ds = lv.get("date","")
        if ds and att.get(ds) != "wfo":
            att[ds] = "leave"

    return att

# ── EWMA day-of-week model ─────────────────────────────────────────────────
def compute_dow_rates(
    att: dict[str, str],
    ph_dates: set[str],
    alpha: float = EWMA_ALPHA,
) -> tuple[dict[int, float], int]:
    """
    Compute EWMA WFO probability for each weekday (0=Mon … 4=Fri).
    Excludes leave days and public holidays from the denominator
    (so a sick week doesn't suppress someone's Monday rate).

    Returns:
        dow_rates: {0: 0.75, 1: 0.50, …}
        active_weeks: number of calendar weeks with any non-leave activity
    """
    # Group dates by (ISO week, weekday)
    # Sort chronologically so EWMA weights recent data higher
    records_by_week: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for ds, st in att.items():
        if st in ("leave", "public_holiday"):
            continue
        d = date.fromisoformat(ds)
        week_key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        records_by_week[week_key].append((d.weekday(), st))

    sorted_weeks = sorted(records_by_week.keys())
    active_weeks = len(sorted_weeks)

    if active_weeks == 0:
        return {i: 0.5 for i in range(5)}, 0

    # Per weekday: list of (week_index, was_wfo) sorted oldest first
    dow_obs: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    for week_idx, wk in enumerate(sorted_weeks):
        for dow, st in records_by_week[wk]:
            dow_obs[dow].append((week_idx, st == "wfo"))

    dow_rates: dict[int, float] = {}
    n_weeks = len(sorted_weeks)

    for dow in range(5):
        obs = dow_obs.get(dow, [])
        if not obs:
            # No data for this weekday → neutral prior
            dow_rates[dow] = 0.40
            continue

        # EWMA: weight = alpha * (1-alpha)^(n_weeks - week_idx - 1)
        # i.e. most recent week gets highest weight
        ewma_val = None
        for week_idx, was_wfo in obs:
            recency_exp = (n_weeks - week_idx - 1)
            w = alpha * ((1 - alpha) ** recency_exp)
            v = 1.0 if was_wfo else 0.0
            ewma_val = v if ewma_val is None else (1 - w) * ewma_val + w * v

        dow_rates[dow] = max(0.05, min(0.95, ewma_val or 0.40))

    return dow_rates, active_weeks

# ── Markov transition adjustment ───────────────────────────────────────────
def markov_adjust(base_prob: float, prev_status: Optional[str]) -> float:
    """
    Adjust base day-of-week probability using the previous working day's status.
    Clamps to [0.05, 0.95].
    """
    prev = prev_status if prev_status in ("wfo","wfh","leave") else None
    mul = MARKOV.get((prev, "wfo"), 1.0)
    return max(0.05, min(0.95, base_prob * mul))

# ── absence signal ─────────────────────────────────────────────────────────
def apply_absence_signal(
    prob: float,
    today: date,
    target_date: date,
    att: dict[str, str],
    check_in_time_hour: Optional[int] = None,
) -> float:
    """
    If target_date is today and it's past 11am and no check-in recorded,
    decay WFO probability significantly (unexpected absence signal).
    """
    if target_date != today:
        return prob
    ds = today.isoformat()
    if ds in att:
        # Already checked in → deterministic
        return 1.0 if att[ds] == "wfo" else 0.05
    # No check-in yet today
    if check_in_time_hour is not None and check_in_time_hour >= 11:
        return prob * ABSENCE_DECAY
    return prob

# ── main forecast function ─────────────────────────────────────────────────
def compute_forecast(
    employee_id:        str,
    employee_name:      str,
    checkins:           list[dict],
    leaves:             list[dict],
    ph_dates:           set[str],
    today:              date,
    forecast_days:      int = 14,
    current_month_wfo:  int = 0,
    current_month_total_workdays: int = 0,
    check_in_hour_today: Optional[int] = None,
) -> dict:
    """
    Compute a personal WFO forecast for the next `forecast_days` working days.

    Returns a dict suitable for the /api/insights/{employee_id} endpoint.
    """
    att = build_attendance(checkins, leaves, ph_dates)
    dow_rates, active_weeks = compute_dow_rates(att, ph_dates)
    confidence = _confidence_tier(active_weeks)

    if confidence == "insufficient":
        return {
            "employee_id":   employee_id,
            "employee_name": employee_name,
            "confidence":    confidence,
            "confidence_label": _confidence_label(confidence),
            "active_weeks":  active_weeks,
            "forecast":      [],
            "monthly":       _monthly_progress(att, today, ph_dates, current_month_wfo),
            "insufficient_data": True,
            "message": f"Need at least 2 weeks of check-in data. Currently have {active_weeks}.",
        }

    # Get last working day status for Markov adjustment
    prev_status = _last_workday_status(att, today, ph_dates)

    # Generate forecast
    future_days = _next_n_workdays(today, forecast_days, ph_dates)
    forecast = []
    running_prev = prev_status

    for fd in future_days:
        ds = fd.isoformat()

        # --- Deterministic overrides (highest priority) ---
        # Check leave
        lv_match = next((l for l in leaves if l.get("date") == ds), None)
        if lv_match:
            forecast.append({
                "date":          ds,
                "dow":           fd.weekday(),
                "dow_name":      fd.strftime("%A"),
                "probability":   0.0,
                "status":        "leave",
                "leave_type":    lv_match.get("leave_type","leave"),
                "certain":       True,
                "confidence":    "certain",
            })
            running_prev = "leave"
            continue

        # Check public holiday
        if ds in ph_dates:
            forecast.append({
                "date":        ds,
                "dow":         fd.weekday(),
                "dow_name":    fd.strftime("%A"),
                "probability": 0.0,
                "status":      "public_holiday",
                "certain":     True,
                "confidence":  "certain",
            })
            running_prev = "leave"
            continue

        # Already have actual data for this day (today or recently)
        if ds in att and fd <= today:
            actual = att[ds]
            forecast.append({
                "date":        ds,
                "dow":         fd.weekday(),
                "dow_name":    fd.strftime("%A"),
                "probability": 1.0 if actual == "wfo" else 0.0,
                "status":      actual,
                "certain":     True,
                "confidence":  "actual",
            })
            running_prev = actual
            continue

        # --- Probabilistic prediction ---
        base  = dow_rates.get(fd.weekday(), 0.40)
        prob  = markov_adjust(base, running_prev)

        # Absence signal for today
        if fd == today:
            prob = apply_absence_signal(prob, today, fd, att, check_in_hour_today)

        # Confidence band
        band  = _confidence_band(prob, confidence)

        forecast.append({
            "date":          ds,
            "dow":           fd.weekday(),
            "dow_name":      fd.strftime("%A"),
            "probability":   round(prob, 3),
            "prob_low":      round(band[0], 3),
            "prob_high":     round(band[1], 3),
            "status":        "predicted_wfo" if prob >= 0.5 else "predicted_wfh",
            "certain":       False,
            "confidence":    confidence,
        })
        running_prev = "wfo" if prob >= 0.5 else "wfh"

    # Monthly projection
    monthly = _monthly_progress(att, today, ph_dates, current_month_wfo)
    # Add predicted WFO days remaining this month
    month_remaining_forecast = [
        f for f in forecast
        if f["date"][:7] == today.strftime("%Y-%m")
        and not f.get("certain") and f["status"] == "predicted_wfo"
    ]
    monthly["predicted_additional"] = len(month_remaining_forecast)
    monthly["predicted_total"]      = monthly["actual_wfo"] + len(month_remaining_forecast)

    return {
        "employee_id":      employee_id,
        "employee_name":    employee_name,
        "confidence":       confidence,
        "confidence_label": _confidence_label(confidence),
        "active_weeks":     active_weeks,
        "dow_rates":        {str(k): round(v,3) for k,v in dow_rates.items()},
        "forecast":         forecast,
        "monthly":          monthly,
        "insufficient_data": False,
    }

def _last_workday_status(att: dict, today: date, ph_dates: set) -> Optional[str]:
    """Find status of the last completed working day before today."""
    cur = today - timedelta(days=1)
    for _ in range(14):
        if _is_weekday(cur) and cur.isoformat() not in ph_dates:
            return att.get(cur.isoformat())
        cur -= timedelta(days=1)
    return None

def _confidence_band(prob: float, tier: str) -> tuple[float, float]:
    """Return (low, high) band based on confidence tier."""
    spread = {"low": 0.25, "medium": 0.15, "high": 0.08}.get(tier, 0.20)
    return (max(0.0, prob - spread), min(1.0, prob + spread))

def _monthly_progress(
    att: dict[str, str],
    today: date,
    ph_dates: set[str],
    current_month_wfo: int = 0,
) -> dict:
    """Compute actual monthly WFO progress and days needed."""
    yr, mo = today.year, today.month
    workdays = _workdays_in_month(yr, mo, ph_dates)
    elapsed   = [d for d in workdays if d < today.isoformat()]
    remaining = [d for d in workdays if d >= today.isoformat()]

    actual_wfo = sum(1 for d in elapsed if att.get(d) == "wfo")
    # Also count current_month_wfo passed in from server (more accurate)
    actual_wfo = max(actual_wfo, current_month_wfo)

    needed = max(0, 12 - actual_wfo)
    achievable = len(remaining) >= needed

    return {
        "month":          today.strftime("%B %Y"),
        "actual_wfo":     actual_wfo,
        "target":         12,
        "needed":         needed,
        "elapsed_days":   len(elapsed),
        "remaining_days": len(remaining),
        "total_workdays": len(workdays),
        "achievable":     achievable,
        "on_track":       actual_wfo >= round((len(elapsed) / max(len(workdays),1)) * 12),
    }

# ─────────────────────────────────────────────────────────────────────────────
# TEAM RHYTHM
# ─────────────────────────────────────────────────────────────────────────────

def compute_team_rhythm(
    members:   list[dict],   # [{employee_id, employee_name, checkins:[{date,status}], leaves:[{date,...}]}]
    ph_dates:  set[str],
    today:     date,
    lookback_weeks: int = 8,
) -> dict:
    """
    Compute team rhythm analytics.

    Returns:
        best_days:      list of {dow, dow_name, avg_count, probability}
        overlap_matrix: list of {a_id, b_id, a_name, b_name, score, shared_days, total_days}
        individual:     list of {employee_id, dow_rates, active_weeks, confidence, streak}
        heatmap:        list of {employee_id, weeks: [{week_start, wfo_days, total_days}]}
        gaps:           list of {a_id, b_id, a_name, b_name, message} for low-overlap pairs
        data_start:     earliest date with any data
        lookback_weeks: actual weeks used
    """
    # Build attendance for each member
    member_att: dict[str, dict[str, str]] = {}
    for m in members:
        att = build_attendance(
            m.get("checkins", []),
            m.get("leaves",   []),
            ph_dates,
        )
        member_att[m["employee_id"]] = att

    # Determine lookback window
    cutoff = today - timedelta(weeks=lookback_weeks)
    cutoff_str = cutoff.isoformat()

    # Filter to lookback window and weekdays only
    def _windowed(att: dict) -> dict:
        return {
            ds: st for ds, st in att.items()
            if ds >= cutoff_str and _is_weekday(date.fromisoformat(ds))
        }

    windowed: dict[str, dict[str, str]] = {
        eid: _windowed(att) for eid, att in member_att.items()
    }

    # ── Best days for team meetings ───────────────────────────────────────
    best_days = _compute_best_days(members, windowed, ph_dates, today)

    # ── Overlap matrix (Jaccard) ──────────────────────────────────────────
    overlap_matrix = _compute_overlap(members, windowed)

    # ── Individual patterns ───────────────────────────────────────────────
    individual = []
    for m in members:
        dow_rates, active_weeks = compute_dow_rates(
            windowed.get(m["employee_id"], {}), ph_dates
        )
        streak = _compute_streak(member_att.get(m["employee_id"], {}), today, ph_dates)
        individual.append({
            "employee_id":   m["employee_id"],
            "employee_name": m["employee_name"],
            "dow_rates":     {str(k): round(v,3) for k,v in dow_rates.items()},
            "active_weeks":  active_weeks,
            "confidence":    _confidence_tier(active_weeks),
            "current_streak_wfo": streak,
        })

    # ── Heatmap (last 8 weeks per person) ─────────────────────────────────
    heatmap = _compute_heatmap(members, member_att, ph_dates, today, lookback_weeks)

    # ── Collaboration gaps ────────────────────────────────────────────────
    gaps = _compute_gaps(overlap_matrix)

    # Earliest data date
    all_dates = [ds for att in windowed.values() for ds in att]
    data_start = min(all_dates) if all_dates else today.isoformat()

    return {
        "best_days":      best_days,
        "overlap_matrix": overlap_matrix,
        "individual":     individual,
        "heatmap":        heatmap,
        "gaps":           gaps,
        "data_start":     data_start,
        "lookback_weeks": lookback_weeks,
        "team_size":      len(members),
    }

def _compute_best_days(
    members: list[dict],
    windowed: dict[str, dict[str, str]],
    ph_dates: set[str],
    today: date,
) -> list[dict]:
    """Compute per-weekday average WFO count using EWMA across team members."""
    dow_counts: dict[int, list[float]] = defaultdict(list)

    for m in members:
        eid = m["employee_id"]
        att = windowed.get(eid, {})
        dow_rates, active_weeks = compute_dow_rates(att, ph_dates)
        if active_weeks < 2:
            continue
        for dow, rate in dow_rates.items():
            dow_counts[dow].append(rate)

    n = len(members)
    result = []
    dow_names = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    for dow in range(5):
        rates = dow_counts.get(dow, [])
        avg_prob   = (sum(rates) / len(rates)) if rates else 0.0
        avg_count  = avg_prob * n
        result.append({
            "dow":          dow,
            "dow_name":     dow_names[dow],
            "avg_count":    round(avg_count, 1),
            "probability":  round(avg_prob, 3),
            "label":        f"~{avg_count:.1f} of {n} in office",
        })

    result.sort(key=lambda x: x["probability"], reverse=True)
    return result

def _compute_overlap(
    members: list[dict],
    windowed: dict[str, dict[str, str]],
) -> list[dict]:
    """
    Compute pairwise Jaccard similarity on leave-adjusted WFO sets.
    Only considers days where both members had non-leave, non-holiday activity.
    """
    result = []
    ids = [m["employee_id"] for m in members]
    name_map = {m["employee_id"]: m["employee_name"] for m in members}

    for i in range(len(ids)):
        for j in range(i+1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            a_att = windowed.get(a_id, {})
            b_att = windowed.get(b_id, {})

            # Leave-adjusted: only days where both were working (not on leave/holiday)
            a_available = {ds for ds, st in a_att.items() if st in ("wfo","wfh")}
            b_available = {ds for ds, st in b_att.items() if st in ("wfo","wfh")}

            a_wfo = {ds for ds, st in a_att.items() if st == "wfo"}
            b_wfo = {ds for ds, st in b_att.items() if st == "wfo"}

            intersection = a_wfo & b_wfo   # both WFO same day
            union        = a_wfo | b_wfo   # either WFO

            jaccard = len(intersection) / len(union) if union else 0.0
            total_days = len(a_available | b_available)

            result.append({
                "a_id":        a_id,
                "b_id":        b_id,
                "a_name":      name_map[a_id],
                "b_name":      name_map[b_id],
                "shared_days": len(intersection),
                "total_days":  total_days,
                "score":       round(jaccard, 3),
                "label":       _overlap_label(jaccard, len(intersection)),
            })

    result.sort(key=lambda x: x["score"], reverse=True)
    return result

def _overlap_label(score: float, shared_days: int) -> str:
    if shared_days < 2:   return "Rarely overlap"
    if score >= 0.60:     return "Strong overlap"
    if score >= 0.35:     return "Moderate overlap"
    if score >= 0.15:     return "Low overlap"
    return "Rarely overlap"

def _compute_streak(att: dict[str, str], today: date, ph_dates: set[str]) -> int:
    """Count consecutive WFO days ending on or before today."""
    streak, cur = 0, today - timedelta(days=1)
    for _ in range(30):
        if not _is_weekday(cur) or cur.isoformat() in ph_dates:
            cur -= timedelta(days=1)
            continue
        if att.get(cur.isoformat()) == "wfo":
            streak += 1
            cur -= timedelta(days=1)
        else:
            break
    return streak

def _compute_heatmap(
    members:        list[dict],
    member_att:     dict[str, dict[str, str]],
    ph_dates:       set[str],
    today:          date,
    lookback_weeks: int,
) -> list[dict]:
    """
    Build per-person weekly WFO heatmap for the last `lookback_weeks` weeks.
    Each cell: {week_start, wfo_days, total_workdays, rate}
    """
    # Build list of week start dates (Mondays)
    week_starts = []
    monday = today - timedelta(days=today.weekday())  # most recent Monday
    for i in range(lookback_weeks - 1, -1, -1):
        week_starts.append(monday - timedelta(weeks=i))

    result = []
    for m in members:
        eid  = m["employee_id"]
        att  = member_att.get(eid, {})
        weeks_data = []
        for ws in week_starts:
            wfo   = 0
            total = 0
            for delta in range(5):  # Mon-Fri
                d = ws + timedelta(days=delta)
                ds = d.isoformat()
                if ds in ph_dates or not _is_weekday(d):
                    continue
                if ds > today.isoformat():
                    continue
                total += 1
                if att.get(ds) == "wfo":
                    wfo += 1
            weeks_data.append({
                "week_start": ws.isoformat(),
                "wfo_days":   wfo,
                "total_days": total,
                "rate":       round(wfo / total, 2) if total > 0 else 0.0,
            })
        result.append({
            "employee_id":   eid,
            "employee_name": m["employee_name"],
            "weeks":         weeks_data,
        })
    return result

def _compute_gaps(overlap_matrix: list[dict]) -> list[dict]:
    """Flag pairs with low overlap who could benefit from schedule alignment."""
    gaps = []
    for pair in overlap_matrix:
        if pair["score"] < 0.15 and pair["shared_days"] < 3 and pair["total_days"] >= 10:
            gaps.append({
                "a_id":    pair["a_id"],
                "b_id":    pair["b_id"],
                "a_name":  pair["a_name"],
                "b_name":  pair["b_name"],
                "message": (
                    f"{pair['a_name'].split()[0]} and {pair['b_name'].split()[0]} "
                    f"have only been in office together {pair['shared_days']} "
                    f"time{'s' if pair['shared_days']!=1 else ''} recently. "
                    f"Consider aligning schedules."
                ),
            })
    return gaps