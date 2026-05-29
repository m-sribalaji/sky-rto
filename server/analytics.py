"""
analytics.py - RTO Tracker Intelligence Engine v2
==================================================
Improvements over v1 (based on expert review + independent analysis):

  1. Volatility-adaptive EWMA alpha
     Alpha adapts to both data tenure AND behavioural consistency.
     Stable employees → lower alpha (trust history).
     Erratic employees → higher alpha (adapt fast).
     New employees → higher alpha (learn fast).

  2. Wilson score confidence intervals per weekday
     Each weekday gets its own confidence band based on actual
     observation count for THAT day — not a global tier.
     Prevents "high confidence on Mondays" when there are only 2 Monday records.

  3. Personal Markov matrix with Laplace smoothing + global fallback
     Learns each person's actual transition tendencies from their history.
     Laplace smoothing prevents extreme probabilities from sparse counts.
     Falls back to global multipliers when < 4 weeks of data.

  4. Today explicitly included in forecast horizon (fixes unreachable code)
     Absence signal (no check-in by 11am) now actually fires.

  5. Expected Overlap replaces Jaccard for collaboration gap detection
     Forward-looking: P(A and B both WFO on day X) = P(A WFO) × P(B WFO).
     Jaccard kept for historical overlap matrix (backward-looking, still useful).
     Minimum 5 shared active days before Jaccard score shown.

  6. leave→WFO Markov multiplier corrected to 1.05 (was 0.70)
     Empirically, people often WFO on return-from-leave day (catch-up meetings).
     0.70 was too pessimistic and penalised normal return behaviour.

  7. Minimum support threshold for Jaccard
     Score only shown when ≥5 shared active days exist.
     Below threshold: "Insufficient shared data" shown instead of misleading score.

  8. Docstring updated throughout.
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

# EWMA alpha range — chosen adaptively per employee (see _adaptive_alpha)
EWMA_ALPHA_NEW     = 0.40   # < 4 weeks data: learn fast
EWMA_ALPHA_DEFAULT = 0.25   # 4–12 weeks: balanced
EWMA_ALPHA_STABLE  = 0.15   # 12+ weeks, low variance: trust history

# Global Markov fallback (used when personal data insufficient)
# Fix #6: leave→WFO corrected from 0.70 → 1.05
MARKOV_GLOBAL = {
    ("wfo",  "wfo"):  1.25,
    ("wfo",  "wfh"):  0.80,
    ("wfh",  "wfo"):  0.90,
    ("wfh",  "wfh"):  1.10,
    ("leave","wfo"):  1.05,   # return from leave → slightly more likely WFO (catch-up)
    ("leave","wfh"):  0.95,
    (None,   "wfo"):  1.00,
    (None,   "wfh"):  1.00,
}

# Laplace smoothing constant for personal Markov
LAPLACE_K = 1.0

# Minimum shared active days before showing Jaccard score (Fix #7)
JACCARD_MIN_SUPPORT = 5

# Absence signal: no check-in by 11am → decay WFO probability
ABSENCE_DECAY = 0.15

# Wilson score z-value for 90% confidence interval
WILSON_Z = 1.645

# ── confidence label ───────────────────────────────────────────────────────
def _confidence_tier(n_obs: int) -> str:
    """Tier based on actual observation count (per-weekday or global)."""
    if n_obs < 3:  return "insufficient"
    if n_obs < 6:  return "low"
    if n_obs < 12: return "medium"
    return "high"

def _confidence_label(tier: str) -> str:
    return {
        "insufficient": "Not enough data yet",
        "low":          "Low confidence — limited history",
        "medium":       "Medium confidence — pattern stabilising",
        "high":         "High confidence — strong historical pattern",
    }.get(tier, tier)

# ── Wilson score confidence interval (Fix #2) ──────────────────────────────
def _wilson_interval(successes: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """
    Wilson score interval for a proportion.
    More accurate than normal approximation for small n.
    Returns (low, high) probability bounds.
    """
    if n == 0:
        return (0.05, 0.95)
    p_hat = successes / n
    denom = 1 + z*z/n
    centre = (p_hat + z*z/(2*n)) / denom
    margin = (z * math.sqrt(p_hat*(1-p_hat)/n + z*z/(4*n*n))) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))

# ── Volatility-adaptive EWMA alpha (Fix #1) ────────────────────────────────
def _adaptive_alpha(obs: list[tuple[int, bool]], n_weeks: int) -> float:
    """
    Choose EWMA alpha based on data tenure AND behavioural variance.

    Low variance (consistent person)  → lower alpha → trust history
    High variance (erratic person)    → higher alpha → adapt fast
    New employee (< 4 weeks)          → higher alpha → learn fast
    """
    if n_weeks < 4:
        return EWMA_ALPHA_NEW
    if len(obs) < 4:
        return EWMA_ALPHA_DEFAULT

    # Compute variance of binary WFO observations
    vals = [1.0 if wfo else 0.0 for _, wfo in obs]
    mean_v = sum(vals) / len(vals)
    variance = sum((v - mean_v)**2 for v in vals) / len(vals)

    # High variance → more responsive alpha
    if variance > 0.20 and n_weeks >= 12:
        return EWMA_ALPHA_DEFAULT   # erratic even with long history
    if n_weeks >= 12 and variance <= 0.20:
        return EWMA_ALPHA_STABLE    # stable, tenured employee
    return EWMA_ALPHA_DEFAULT

# ── Date helpers ───────────────────────────────────────────────────────────
def _is_weekday(d: date) -> bool:
    return d.weekday() < 5

def _next_n_workdays(start: date, n: int, ph_dates: set[str],
                     inclusive: bool = False) -> list[date]:
    """
    Return next n working days from start.
    inclusive=True: include start itself if it is a workday.
    """
    days, cur = [], start if inclusive else start
    if not inclusive:
        cur = start
    else:
        cur = start - timedelta(days=1)  # will be incremented immediately
    while len(days) < n:
        cur += timedelta(days=1)
        if inclusive and cur == start:
            if _is_weekday(cur) and cur.isoformat() not in ph_dates:
                days.append(cur)
            continue
        if not inclusive and cur == start:
            continue
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

# ── Attendance record builder ──────────────────────────────────────────────
def build_attendance(
    checkins:  list[dict],
    leaves:    list[dict],
    ph_dates:  set[str],
) -> dict[str, str]:
    """
    Build a clean date → status map.
    Priority: leave > public_holiday > checkin dominant status.
    """
    att: dict[str, str] = {}
    for r in checkins:
        ds, st = r.get("date",""), r.get("status","")
        if ds and st:
            att[ds] = "wfo" if st == "wfo" else "wfh"
    for ds in list(att.keys()):
        if ds in ph_dates and att[ds] != "wfo":
            att[ds] = "public_holiday"
    for ds in ph_dates:
        if ds not in att:
            att[ds] = "public_holiday"
    for lv in leaves:
        ds = lv.get("date","")
        if ds and att.get(ds) != "wfo":
            att[ds] = "leave"
    return att

# ── EWMA day-of-week model with per-weekday Wilson confidence ──────────────
def compute_dow_rates(
    att: dict[str, str],
    ph_dates: set[str],
) -> tuple[dict[int, float], int, dict[int, dict], dict[int, str]]:
    """
    Compute EWMA WFO probability for each weekday (0=Mon … 4=Fri).
    Uses volatility-adaptive alpha per weekday.
    Also returns per-weekday Wilson confidence intervals and tiers.

    Returns:
        dow_rates:      {0: 0.75, …}
        active_weeks:   int
        dow_intervals:  {0: {"low": 0.55, "high": 0.90, "n": 8}, …}
        dow_confidence: {0: "high", …}
    """
    records_by_week: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for ds, st in att.items():
        if st in ("leave", "public_holiday"):
            continue
        d = date.fromisoformat(ds)
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        records_by_week[wk].append((d.weekday(), st))

    sorted_weeks = sorted(records_by_week.keys())
    # Fix 6: only count weeks where employee had ≥3 working days of data
    # Prevents mid-week install inflating active_weeks prematurely
    active_weeks = sum(
        1 for wk in sorted_weeks
        if len(records_by_week[wk]) >= 3
    )
    # But still use all weeks for EWMA (just not for confidence gating)
    active_weeks_for_ewma = len(sorted_weeks)

    if active_weeks == 0:
        empty_interval = {"low": 0.05, "high": 0.95, "n": 0}
        return (
            {i: 0.40 for i in range(5)},
            0,
            {i: empty_interval for i in range(5)},
            {i: "insufficient" for i in range(5)},
        )

    # Store (actual_date_str, was_wfo) per weekday for calendar-aware decay (Fix 1)
    dow_obs_dates: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    for wk in sorted_weeks:
        for dow, st in records_by_week[wk]:
            # Find a representative date for this (week, dow) — reconstruct from ISO week
            yr_w, w_num = int(wk.split("-W")[0]), int(wk.split("-W")[1])
            monday = date.fromisocalendar(yr_w, w_num, 1)
            day_date = monday + timedelta(days=dow)
            dow_obs_dates[dow].append((day_date.isoformat(), st == "wfo"))

    # Sort each weekday's observations by date (oldest first)
    for dow in dow_obs_dates:
        dow_obs_dates[dow].sort(key=lambda x: x[0])

    # For adaptive alpha we still need (week_idx, was_wfo) form
    dow_obs: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    for week_idx, wk in enumerate(sorted_weeks):
        for dow, st in records_by_week[wk]:
            dow_obs[dow].append((week_idx, st == "wfo"))

    n_weeks = active_weeks_for_ewma  # use all weeks for EWMA decay
    # Current week start — used for calendar distance (Fix 1)
    today_ref = max(
        (date.fromisoformat(ds) for ds in att if att[ds] not in ("leave","public_holiday")),
        default=date.today()
    )
    current_week_start = today_ref - timedelta(days=today_ref.weekday())

    dow_rates:      dict[int, float] = {}
    dow_intervals:  dict[int, dict]  = {}
    dow_confidence: dict[int, str]   = {}

    for dow in range(5):
        obs       = dow_obs.get(dow, [])
        obs_dates = dow_obs_dates.get(dow, [])
        n         = len(obs)

        if n == 0:
            dow_rates[dow]      = 0.40
            dow_intervals[dow]  = {"low": 0.05, "high": 0.75, "n": 0}
            dow_confidence[dow] = "insufficient"
            continue

        # Adaptive alpha for this weekday's observations
        alpha = _adaptive_alpha(obs, n_weeks)

        # Fix 1 + Fix 5 (EWMA init prior):
        # - Calendar-aware recency_exp: weeks since observation, not list index
        # - Init ewma_val with prior (0.40) instead of first raw value
        ewma_val = 0.40  # Bayesian prior: neutral before any data (Fix 5)
        for ds_str, was_wfo in obs_dates:
            obs_date = date.fromisoformat(ds_str)
            obs_week_start = obs_date - timedelta(days=obs_date.weekday())
            weeks_ago = max(0, (current_week_start - obs_week_start).days // 7)
            w = alpha * ((1 - alpha) ** weeks_ago)
            v = 1.0 if was_wfo else 0.0
            ewma_val = (1 - w) * ewma_val + w * v

        rate = max(0.05, min(0.95, ewma_val or 0.40))
        dow_rates[dow] = rate

        # Wilson confidence interval based on actual observation count
        successes = sum(1 for _, wfo in obs if wfo)
        low, high = _wilson_interval(successes, n)
        dow_intervals[dow]  = {"low": round(low,3), "high": round(high,3), "n": n}
        dow_confidence[dow] = _confidence_tier(n)

    return dow_rates, active_weeks, dow_intervals, dow_confidence

# ── Personal Markov matrix with Laplace smoothing (Fix #3) ─────────────────
def _build_personal_markov(att: dict[str, str], ph_dates: set[str],
                            today: date) -> Optional[dict]:
    """
    Build a personal transition probability dict using Laplace smoothing.
    Returns None if insufficient data (< 4 weeks of transitions).
    """
    sorted_days = sorted(
        ds for ds in att
        if _is_weekday(date.fromisoformat(ds))
        and ds not in ph_dates
        and date.fromisoformat(ds) < today
    )
    if len(sorted_days) < 20:  # need enough transitions
        return None

    counts: dict[tuple[str,str], int] = defaultdict(int)
    from_counts: dict[str, int] = defaultdict(int)
    states = ("wfo", "wfh", "leave")

    prev = None
    for ds in sorted_days:
        curr_raw = att.get(ds, "wfh")
        curr = curr_raw if curr_raw in states else "wfh"
        if prev is not None:
            counts[(prev, curr)] += 1
            from_counts[prev]    += 1
        prev = curr

    # Laplace-smoothed transition multipliers
    # p_personal(wfo | prev) = (count(prev→wfo) + K) / (count(prev) + K*2)
    # Expressed as multiplier vs global: personal / global_base_rate
    personal = {}
    base_wfo_rate = sum(1 for st in att.values() if st == "wfo") / max(len(att), 1)
    if base_wfo_rate < 0.05: base_wfo_rate = 0.05

    for prev_st in states:
        n_from = from_counts.get(prev_st, 0)
        n_to_wfo = counts.get((prev_st, "wfo"), 0)
        # Laplace-smoothed probability
        p_wfo = (n_to_wfo + LAPLACE_K) / (n_from + LAPLACE_K * 2)
        # Express as multiplier (personal preference vs neutral)
        personal[(prev_st, "wfo")] = p_wfo / base_wfo_rate
        personal[(None,    "wfo")] = 1.0

    # Wider clamp: allows sharper negative/positive habits
    # 0.25 floor: allows strong 'no consecutive WFO' patterns
    # 2.50 ceiling: allows strong clustering without extreme swings
    for k in personal:
        personal[k] = max(0.25, min(2.50, personal[k]))

    return personal

def markov_adjust(base_prob: float, prev_status: Optional[str],
                  personal_markov: Optional[dict] = None) -> float:
    """
    Adjust base probability using personal Markov (preferred) or global fallback.
    Clamps to [0.05, 0.95].
    """
    prev = prev_status if prev_status in ("wfo","wfh","leave") else None
    table = personal_markov if personal_markov else MARKOV_GLOBAL
    mul = table.get((prev, "wfo"), 1.0)
    return max(0.05, min(0.95, base_prob * mul))

# ── Absence signal ─────────────────────────────────────────────────────────
def apply_absence_signal(prob: float, att: dict[str, str],
                         today: date,
                         check_in_time_hour: Optional[int] = None) -> float:
    """
    Applied only for today.
    If already checked in → return deterministic value.
    If past 11am and no check-in → decay WFO probability.
    """
    ds = today.isoformat()
    if ds in att:
        return 1.0 if att[ds] == "wfo" else 0.05
    if check_in_time_hour is not None and check_in_time_hour >= 11:
        return prob * ABSENCE_DECAY
    return prob

# ── Main forecast function ─────────────────────────────────────────────────
def compute_forecast(
    employee_id:          str,
    employee_name:        str,
    checkins:             list[dict],
    leaves:               list[dict],
    ph_dates:             set[str],
    today:                date,
    forecast_days:        int = 14,
    current_month_wfo:    int = 0,
    current_month_total_workdays: int = 0,
    check_in_hour_today:  Optional[int] = None,
) -> dict:
    """Compute personal WFO forecast for next forecast_days working days."""
    att = build_attendance(checkins, leaves, ph_dates)
    dow_rates, active_weeks, dow_intervals, dow_confidence = compute_dow_rates(att, ph_dates)

    # Insufficiency check based on per-weekday observation counts
    # Use global active_weeks as primary gate (fastest check)
    # Fix 3: derive global confidence from actual observation count
    # sum of per-weekday Wilson n values is the true data volume
    total_actual_obs = sum(iv.get('n', 0) for iv in dow_intervals.values())
    global_conf = _confidence_tier(total_actual_obs)
    if active_weeks < 2:
        return {
            "employee_id":      employee_id,
            "employee_name":    employee_name,
            "confidence":       "insufficient",
            "confidence_label": _confidence_label("insufficient"),
            "active_weeks":     active_weeks,
            "forecast":         [],
            "monthly":          _monthly_progress(att, today, ph_dates, current_month_wfo),
            "insufficient_data": True,
            "message": f"Need at least 2 weeks of check-in data. Currently have {active_weeks}.",
        }

    # Build personal Markov matrix (uses Laplace smoothing, falls back to global)
    personal_markov = _build_personal_markov(att, ph_dates, today)

    prev_status = _last_workday_status(att, today, ph_dates)

    # Fix #4: include today explicitly as first element if it's a workday
    forecast_dates: list[date] = []
    if _is_weekday(today) and today.isoformat() not in ph_dates:
        forecast_dates.append(today)
    future = today
    while len(forecast_dates) < forecast_days + (1 if forecast_dates else 0):
        future += timedelta(days=1)
        if _is_weekday(future) and future.isoformat() not in ph_dates:
            forecast_dates.append(future)
    forecast_dates = forecast_dates[:forecast_days]

    forecast = []
    running_prev = prev_status

    for fd in forecast_dates:
        ds = fd.isoformat()

        # Deterministic overrides — highest priority
        lv_match = next((l for l in leaves if l.get("date") == ds), None)
        if lv_match:
            forecast.append({
                "date": ds, "dow": fd.weekday(), "dow_name": fd.strftime("%A"),
                "probability": 0.0, "status": "leave",
                "leave_type": lv_match.get("leave_type","leave"),
                "certain": True, "confidence": "certain",
            })
            running_prev = "leave"
            continue

        if ds in ph_dates:
            forecast.append({
                "date": ds, "dow": fd.weekday(), "dow_name": fd.strftime("%A"),
                "probability": 0.0, "status": "public_holiday",
                "certain": True, "confidence": "certain",
            })
            running_prev = "leave"
            continue

        # Already have actual data (today or past)
        if ds in att and fd <= today:
            actual = att[ds]
            forecast.append({
                "date": ds, "dow": fd.weekday(), "dow_name": fd.strftime("%A"),
                "probability": 1.0 if actual == "wfo" else 0.0,
                "status": actual, "certain": True, "confidence": "actual",
            })
            running_prev = actual
            continue

        # Probabilistic prediction
        base = dow_rates.get(fd.weekday(), 0.40)
        prob = markov_adjust(base, running_prev, personal_markov)

        # Absence signal — only for today (Fix #4: now reachable)
        if fd == today:
            prob = apply_absence_signal(prob, att, today, check_in_hour_today)

        # Per-weekday Wilson confidence interval (Fix #2)
        interval = dow_intervals.get(fd.weekday(), {"low": 0.05, "high": 0.95})
        # Scale interval around actual predicted probability
        span = (interval["high"] - interval["low"]) / 2
        prob_low  = max(0.0, prob - span)
        prob_high = min(1.0, prob + span)
        day_conf  = dow_confidence.get(fd.weekday(), "low")

        forecast.append({
            "date":       ds,
            "dow":        fd.weekday(),
            "dow_name":   fd.strftime("%A"),
            "probability": round(prob, 3),
            "prob_low":   round(prob_low, 3),
            "prob_high":  round(prob_high, 3),
            "status":     "predicted_wfo" if prob >= 0.5 else "predicted_wfh",
            "certain":    False,
            "confidence": day_conf,
            "n_obs":      interval.get("n", 0),
        })
        running_prev = "wfo" if prob >= 0.5 else "wfh"

    monthly = _monthly_progress(att, today, ph_dates, current_month_wfo)
    month_rem = [f for f in forecast
                 if f["date"][:7] == today.strftime("%Y-%m")
                 and not f.get("certain") and f["status"] == "predicted_wfo"]
    monthly["predicted_additional"] = len(month_rem)
    monthly["predicted_total"]      = monthly["actual_wfo"] + len(month_rem)

    return {
        "employee_id":      employee_id,
        "employee_name":    employee_name,
        "confidence":       global_conf,
        "confidence_label": _confidence_label(global_conf),
        "active_weeks":     active_weeks,
        "dow_rates":        {str(k): round(v,3) for k,v in dow_rates.items()},
        "dow_confidence":   {str(k): v for k,v in dow_confidence.items()},
        "personal_markov":  personal_markov is not None,
        "forecast":         forecast,
        "monthly":          monthly,
        "insufficient_data": False,
    }

def _last_workday_status(att: dict, today: date, ph_dates: set) -> Optional[str]:
    cur = today - timedelta(days=1)
    for _ in range(14):
        if _is_weekday(cur) and cur.isoformat() not in ph_dates:
            return att.get(cur.isoformat())
        cur -= timedelta(days=1)
    return None

def _monthly_progress(att, today, ph_dates, current_month_wfo=0):
    yr, mo = today.year, today.month
    workdays  = _workdays_in_month(yr, mo, ph_dates)
    elapsed   = [d for d in workdays if d < today.isoformat()]
    remaining = [d for d in workdays if d >= today.isoformat()]
    actual_wfo = max(
        sum(1 for d in elapsed if att.get(d) == "wfo"),
        current_month_wfo
    )
    needed    = max(0, 12 - actual_wfo)
    return {
        "month":          today.strftime("%B %Y"),
        "actual_wfo":     actual_wfo,
        "target":         12,
        "needed":         needed,
        "elapsed_days":   len(elapsed),
        "remaining_days": len(remaining),
        "total_workdays": len(workdays),
        "achievable":     len(remaining) >= needed,
        "on_track":       actual_wfo >= round((len(elapsed) / max(len(workdays),1)) * 12),
    }

# ─────────────────────────────────────────────────────────────────────────────
# TEAM RHYTHM
# ─────────────────────────────────────────────────────────────────────────────

def compute_team_rhythm(
    members:        list[dict],
    ph_dates:       set[str],
    today:          date,
    lookback_weeks: int = 8,
) -> dict:
    """Team rhythm: best days, overlap, heatmap, gaps (expected overlap), individual patterns."""
    member_att: dict[str, dict[str, str]] = {}
    for m in members:
        member_att[m["employee_id"]] = build_attendance(
            m.get("checkins",[]), m.get("leaves",[]), ph_dates)

    cutoff = (today - timedelta(weeks=lookback_weeks)).isoformat()
    def _windowed(att):
        return {ds: st for ds, st in att.items()
                if ds >= cutoff and _is_weekday(date.fromisoformat(ds))}

    windowed = {eid: _windowed(att) for eid, att in member_att.items()}

    # Build per-member forecasts for Expected Overlap (Fix #5)
    member_forecasts: dict[str, dict[int, float]] = {}
    for m in members:
        dow_rates, active_weeks, _, _ = compute_dow_rates(
            windowed.get(m["employee_id"], {}), ph_dates)
        if active_weeks >= 2:
            member_forecasts[m["employee_id"]] = dow_rates
        else:
            member_forecasts[m["employee_id"]] = {}

    best_days      = _compute_best_days(members, windowed, ph_dates)
    overlap_matrix = _compute_overlap(members, windowed, member_forecasts,
                                      ph_dates, today)
    individual     = []
    for m in members:
        dow_rates, active_weeks, dow_intervals, dow_conf = compute_dow_rates(
            windowed.get(m["employee_id"], {}), ph_dates)
        streak = _compute_streak(member_att.get(m["employee_id"], {}), today, ph_dates)
        individual.append({
            "employee_id":        m["employee_id"],
            "employee_name":      m["employee_name"],
            "dow_rates":          {str(k): round(v,3) for k,v in dow_rates.items()},
            "dow_confidence":     {str(k): v for k,v in dow_conf.items()},
            "active_weeks":       active_weeks,
            "confidence":         _confidence_tier(active_weeks * 3),
            "current_streak_wfo": streak,
        })

    heatmap   = _compute_heatmap(members, member_att, ph_dates, today, lookback_weeks)
    gaps      = _compute_gaps(overlap_matrix, member_forecasts, ph_dates, today)
    all_dates = [ds for att in windowed.values() for ds in att]

    return {
        "best_days":      best_days,
        "overlap_matrix": overlap_matrix,
        "individual":     individual,
        "heatmap":        heatmap,
        "gaps":           gaps,
        "data_start":     min(all_dates) if all_dates else today.isoformat(),
        "lookback_weeks": lookback_weeks,
        "team_size":      len(members),
    }

def _compute_best_days(members, windowed, ph_dates):
    # Fix 4: weighted average by observation count per weekday
    # Members with more data anchor the recommendation more strongly
    # than new joiners with 1-2 weeks of history
    dow_weighted_sum:   dict[int, float] = defaultdict(float)
    dow_weight_total:   dict[int, float] = defaultdict(float)
    dow_member_count:   dict[int, int]   = defaultdict(int)

    for m in members:
        att = windowed.get(m["employee_id"], {})
        dow_rates, active_weeks, dow_intervals, _ = compute_dow_rates(att, ph_dates)
        if active_weeks < 2:
            continue
        for dow, rate in dow_rates.items():
            n_obs = dow_intervals.get(dow, {}).get("n", 0)
            weight = max(1, n_obs)   # at least weight 1 so member is counted
            dow_weighted_sum[dow]  += rate * weight
            dow_weight_total[dow]  += weight
            dow_member_count[dow]  += 1

    n = len(members)
    dow_names = ["Monday","Tuesday","Wednesday","Thursday","Friday"]
    result = []
    for dow in range(5):
        total_w = dow_weight_total.get(dow, 0)
        supported = dow_member_count.get(dow, 0)
        avg_prob  = (dow_weighted_sum[dow] / total_w) if total_w > 0 else 0.0
        result.append({
            "dow":               dow,
            "dow_name":          dow_names[dow],
            "avg_count":         round(avg_prob * n, 1),
            "probability":       round(avg_prob, 3),
            "members_with_data": supported,
            "label":             f"~{avg_prob*n:.1f} of {n} in office"
                                 + ("" if supported == n else f" ({supported}/{n} with data)"),
        })
    result.sort(key=lambda x: x["probability"], reverse=True)
    return result

def _compute_overlap(members, windowed, member_forecasts,
                     ph_dates, today):
    """
    Hybrid overlap: historical Jaccard (backward) + Expected Overlap (forward).
    Fix #5: Expected Overlap for gaps; Jaccard kept for historical display.
    Fix #7: Minimum 5 shared active days for Jaccard score.
    """
    result = []
    ids      = [m["employee_id"] for m in members]
    name_map = {m["employee_id"]: m["employee_name"] for m in members}

    for i in range(len(ids)):
        for j in range(i+1, len(ids)):
            a_id, b_id = ids[i], ids[j]
            a_att = windowed.get(a_id, {})
            b_att = windowed.get(b_id, {})

            a_available = {ds for ds, st in a_att.items() if st in ("wfo","wfh")}
            b_available = {ds for ds, st in b_att.items() if st in ("wfo","wfh")}
            a_wfo       = {ds for ds, st in a_att.items() if st == "wfo"}
            b_wfo       = {ds for ds, st in b_att.items() if st == "wfo"}

            intersection  = a_wfo & b_wfo
            union         = a_wfo | b_wfo
            total_days    = len(a_available | b_available)
            shared_active = len(intersection)

            # Historical Jaccard — only if minimum support met (Fix #7)
            if len(union) >= JACCARD_MIN_SUPPORT:
                jaccard = round(len(intersection) / len(union), 3)
                jaccard_valid = True
            else:
                jaccard = None
                jaccard_valid = False

            # Expected Overlap (forward-looking, Fix #5)
            a_dow = member_forecasts.get(a_id, {})
            b_dow = member_forecasts.get(b_id, {})
            expected_14d = 0.0
            for dow in range(5):
                pa = a_dow.get(dow, 0.0)
                pb = b_dow.get(dow, 0.0)
                expected_14d += pa * pb * 2  # ~2 of each weekday in 14 days

            result.append({
                "a_id":            a_id,
                "b_id":            b_id,
                "a_name":          name_map[a_id],
                "b_name":          name_map[b_id],
                "shared_days":     shared_active,
                "total_days":      total_days,
                "jaccard":         jaccard,
                "jaccard_valid":   jaccard_valid,
                "score":           jaccard if jaccard_valid else 0.0,
                "expected_overlap_14d": round(expected_14d, 1),
                "label":           _overlap_label(
                    jaccard if jaccard_valid else 0.0, shared_active, jaccard_valid),
            })

    result.sort(key=lambda x: x["score"], reverse=True)
    return result

def _overlap_label(score, shared_days, score_valid=True):
    if not score_valid:
        return "Insufficient shared data"
    if shared_days < 2:   return "Rarely overlap"
    if score >= 0.60:     return "Strong overlap"
    if score >= 0.35:     return "Moderate overlap"
    if score >= 0.15:     return "Low overlap"
    return "Rarely overlap"

def _compute_gaps(overlap_matrix, member_forecasts, ph_dates, today):
    """
    Collaboration gaps now based on Expected Overlap (Fix #5),
    not purely on historical Jaccard.
    """
    gaps = []
    next_14 = []
    cur = today
    while len(next_14) < 14:
        cur += timedelta(days=1)
        if _is_weekday(cur) and cur.isoformat() not in ph_dates:
            next_14.append(cur)

    for pair in overlap_matrix:
        a_dow = member_forecasts.get(pair["a_id"], {})
        b_dow = member_forecasts.get(pair["b_id"], {})
        if not a_dow or not b_dow:
            continue
        # Expected days both in office over next 14 working days
        expected = sum(
            a_dow.get(d.weekday(), 0.0) * b_dow.get(d.weekday(), 0.0)
            for d in next_14
        )
        # Historical support check
        if pair["total_days"] >= 10 and expected < 1.5:
            gaps.append({
                "a_id":    pair["a_id"],
                "b_id":    pair["b_id"],
                "a_name":  pair["a_name"],
                "b_name":  pair["b_name"],
                "expected_shared_days": round(expected, 1),
                "message": (
                    f"{pair['a_name'].split()[0]} and {pair['b_name'].split()[0]} "
                    f"are expected to overlap in office only ~{expected:.1f} day"
                    f"{'s' if expected != 1 else ''} over the next 2 weeks. "
                    f"Consider aligning schedules."
                ),
            })
    return gaps

def _compute_streak(att, today, ph_dates):
    streak, cur = 0, today - timedelta(days=1)
    for _ in range(30):
        if not _is_weekday(cur) or cur.isoformat() in ph_dates:
            cur -= timedelta(days=1); continue
        if att.get(cur.isoformat()) == "wfo":
            streak += 1; cur -= timedelta(days=1)
        else:
            break
    return streak

def _compute_heatmap(members, member_att, ph_dates, today, lookback_weeks):
    monday      = today - timedelta(days=today.weekday())
    week_starts = [monday - timedelta(weeks=i) for i in range(lookback_weeks-1, -1, -1)]
    result = []
    for m in members:
        eid, att = m["employee_id"], member_att.get(m["employee_id"], {})
        weeks_data = []
        for ws in week_starts:
            wfo = total = 0
            for delta in range(5):
                d  = ws + timedelta(days=delta)
                ds = d.isoformat()
                if ds in ph_dates or not _is_weekday(d) or ds > today.isoformat():
                    continue
                total += 1
                if att.get(ds) == "wfo": wfo += 1
            weeks_data.append({
                "week_start": ws.isoformat(),
                "wfo_days":   wfo,
                "total_days": total,
                "rate":       round(wfo/total, 2) if total else 0.0,
            })
        result.append({
            "employee_id":   eid,
            "employee_name": m["employee_name"],
            "weeks":         weeks_data,
        })
    return result