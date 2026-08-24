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

# ── model version ─────────────────────────────────────────────────────────
# Bumped whenever the forecast math itself changes (not for UI/copy-only
# changes). Stamped onto every forecast response so a stored/exported
# forecast stays interpretable after the algorithm moves on, and so
# backtest.py can report results per version rather than conflating
# different eras of the model together.
MODEL_VERSION = "3.0.0"  # 3.0: recent-level + damped weekday offset, calibration shrinkage,
                         #      responsiveness-gated quota pressure (see PREDICTION MODEL below)

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

# Compliance targets — named here instead of scattered as literal 3s and
# 12s through _monthly_progress / _compliance_forecast_weeks, so there's
# exactly one place to change if policy ever does.
WEEKLY_WFO_TARGET  = 3
MONTHLY_WFO_TARGET = 12

# Empirical-Bayes shrinkage strength for pooling toward the team baseline —
# how many "pseudo-observations" of team-wide behaviour a personal rate is
# weighed against. Someone with this many (or more) of their own real
# observations for a weekday is barely nudged toward the team; someone
# with 1-2 is pulled most of the way there instead of relying on a single
# data point or an arbitrary "new employee" alpha heuristic.
TEAM_POOL_STRENGTH = 15

# ── PREDICTION MODEL (v3) ──────────────────────────────────────────────────
# v2 predicted from a per-weekday EWMA ("this person is WFO 67% of Tuesdays").
# Backtested against real recorded history, that scored WORSE than predicting
# a flat 0.5 every day. Two reasons, both confirmed by measurement:
#
#   1. People change. Someone can be near-100% office for a month and then
#      shift to mostly-home. A per-weekday average over 6-12 weeks blends
#      both regimes together and describes a person who no longer exists.
#      Measured on real data, a plain "what have they done lately" rate beat
#      every per-weekday variant tried — including per-weekday averages,
#      per-weekday EWMAs at six different alphas, and last-N majority votes.
#
#   2. Confident predictions are expensive when the signal is weak. Brier
#      score punishes a wrong 0.9 far harder than a wrong 0.6, so when
#      behaviour is genuinely near-random at daily granularity (which it
#      often is), the honest move is to say so rather than pick a side.
#
# So v3 decomposes into level + offset, the standard shape for a series with
# a drifting baseline and a weak periodic component:
#
#   level  — recent WFO rate over the last RECENT_LEVEL_WINDOW working days.
#            Tracks regime shifts automatically; this is the dominant term.
#   offset — how much this particular weekday deviates from that person's
#            own overall rate, in log-odds, damped hard by
#            WEEKDAY_OFFSET_DAMPING. Weekday genuinely does carry a little
#            signal for some people, but nothing like enough to lead with.
#   shrink — final pull toward 0.5 by FORECAST_SHRINK, which is calibration,
#            not timidity: it stops the model claiming more certainty than
#            the underlying signal supports.
#
# These constants were chosen from a backtest sweep on real recorded data,
# picking conservative values within the flat part of the curve rather than
# the exact grid-search optimum — the sample is small enough that the single
# best cell is not meaningfully better than its neighbours, and tuning to it
# would be fitting noise. Re-run GET /api/admin/backtest as more history
# accumulates; if these want moving, that endpoint is how you'll know.
RECENT_LEVEL_WINDOW    = 8     # working days feeding the level term
WEEKDAY_OFFSET_DAMPING = 0.35  # how much weekday deviation is allowed to matter
FORECAST_SHRINK        = 0.55  # 1.0 = no shrinkage, 0 = always predict 0.5

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
        "low":          "Low confidence — recent days don't follow a clear pattern",
        "medium":       "Medium confidence — pattern is somewhat variable",
        "high":         "High confidence — consistent, predictable pattern",
    }.get(tier, tier)

_TIER_ORDER = ["insufficient", "low", "medium", "high"]

def _predictability_tier(skill: float) -> str:
    """
    Turn a measured skill score (see compute_predictability) into a tier.
    Thresholds are deliberately demanding: beating a flat "predict this
    person's own base rate" benchmark by a few percent is not a pattern
    worth calling strong.
    """
    if skill >= 0.30: return "high"
    if skill >= 0.15: return "medium"
    if skill >= 0.05: return "low"
    return "insufficient"

def _combined_confidence(data_tier: str, skill_tier: str) -> str:
    """
    Report the WEAKER of "how much history exists" and "how well that
    history actually predicts this person" — because both have to hold for
    a confident forecast to be honest.

    Data volume alone used to drive this, which produced the exact claim
    this model was rebuilt to stop making: an employee with two months of
    records was labelled "High confidence — strong historical pattern"
    while the model's own measured skill on them was ~0.1, barely above
    guessing. Plenty of data about someone whose behaviour just changed is
    still plenty of data — it just doesn't license confidence about
    tomorrow.
    """
    return _TIER_ORDER[min(_TIER_ORDER.index(data_tier), _TIER_ORDER.index(skill_tier))]

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

# ── Calendar-aware EWMA (fixes double-decay) ───────────────────────────────
def _ewma_gap_decay(obs_dates: list[tuple[str, bool]], alpha: float,
                    current_week_start: date, prior: float = 0.40) -> float:
    """
    Blend a sequence of (date_str, was_wfo) observations — sorted oldest to
    newest — into a single EWMA estimate, decaying by the actual calendar
    gap between consecutive observations rather than by list position, so
    a missed week counts as a bigger jump than a consecutive one.

    The previous approach computed each observation's weight from its
    distance-from-today directly (`alpha * (1-alpha)**weeks_ago`) and THEN
    still ran it through the same sequential blend — which erodes earlier
    contributions a second time on every later blend step. Concretely, an
    observation 3 weeks old ended up with roughly HALF the influence its
    own assigned weight implied, once you unroll the recursion. This
    version decays exactly once per elapsed week: the running estimate is
    aged by (1-alpha)**gap before each new observation is blended in, and
    by one final gap-to-now step at the end so a long-stale estimate still
    drifts back toward the neutral prior instead of freezing at whatever
    the last observation happened to be.
    """
    if not obs_dates:
        return prior
    ewma_val = prior
    prev_week_start: Optional[date] = None
    for ds_str, was_wfo in obs_dates:
        obs_date = date.fromisoformat(ds_str)
        obs_week_start = obs_date - timedelta(days=obs_date.weekday())
        gap_weeks = 1 if prev_week_start is None else max(1, (obs_week_start - prev_week_start).days // 7)
        decay = (1 - alpha) ** gap_weeks
        v = 1.0 if was_wfo else 0.0
        ewma_val = decay * ewma_val + (1 - decay) * v
        prev_week_start = obs_week_start
    gap_to_now = max(0, (current_week_start - prev_week_start).days // 7)
    if gap_to_now > 0:
        decay = (1 - alpha) ** gap_to_now
        ewma_val = decay * ewma_val + (1 - decay) * prior
    return ewma_val

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
    today: Optional[date] = None,
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
    # Current week start — used for calendar distance in the EWMA decay.
    # Must come from the caller's actual `today`, not be inferred from the
    # data itself — inferring it from the latest attendance date silently
    # broke backtesting (which deliberately evaluates a past "today" with
    # later data truncated away) and would also drift for anyone whose
    # most recent check-in wasn't literally today (a day off, a gap).
    today_ref = today if today is not None else max(
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

        ewma_val = _ewma_gap_decay(obs_dates, alpha, current_week_start)
        rate = max(0.05, min(0.95, ewma_val or 0.40))
        dow_rates[dow] = rate

        # Wilson confidence interval based on actual observation count
        successes = sum(1 for _, wfo in obs if wfo)
        low, high = _wilson_interval(successes, n)
        dow_intervals[dow]  = {"low": round(low,3), "high": round(high,3), "n": n}
        dow_confidence[dow] = _confidence_tier(n)

    return dow_rates, active_weeks, dow_intervals, dow_confidence

# ── Stable display baseline — slow EWMA, immune to single deviations ────────
def compute_dow_rates_stable(
    att: dict[str, str],
    ph_dates: set[str],
    today: Optional[date] = None,
) -> dict[int, float]:
    """
    Compute a STABLE WFO probability for each weekday — display only, not prediction.

    Key difference from compute_dow_rates:
    - Caps to the most recent 12 weeks of data (ancient patterns don't bleed in).
    - Uses the same adaptive alpha logic as compute_dow_rates, so new employees
      still get accurate rates (not stuck near the 0.40 neutral prior).
    - The 12-week window is the actual stability mechanism — not a slow fixed alpha.
    - Single off-day deviations still cause some movement, but historical anchor
      from 12 weeks of data dampens wild swings.
    """
    # Must come from the caller, not real wall-clock date.today() — this
    # function used to hardcode date.today() internally, which silently
    # ignored whatever `today` compute_forecast was actually called with.
    # Invisible in normal use (real "today" calls happen to match), but
    # it meant backtesting a past date transparently leaked in real
    # present-day data past the simulated cutoff.
    today_ref = today if today is not None else date.today()
    cutoff_12w = (today_ref - timedelta(weeks=12)).isoformat()
    current_week_start = today_ref - timedelta(days=today_ref.weekday())

    dow_obs_dates: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    for ds, st in att.items():
        if ds < cutoff_12w:
            continue
        if st in ("leave", "public_holiday"):
            continue
        d = date.fromisoformat(ds)
        dow_obs_dates[d.weekday()].append((ds, st == "wfo"))

    for dow in dow_obs_dates:
        dow_obs_dates[dow].sort(key=lambda x: x[0])  # oldest first

    # Count active weeks in the 12-week window (for adaptive alpha)
    active_week_keys: set[str] = set()
    for ds, st in att.items():
        if ds < cutoff_12w or st in ("leave", "public_holiday"):
            continue
        d = date.fromisoformat(ds)
        active_week_keys.add(f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}")
    n_weeks_stable = len(active_week_keys)

    dow_rates: dict[int, float] = {}
    for dow in range(5):
        obs = dow_obs_dates.get(dow, [])
        if not obs:
            dow_rates[dow] = 0.40
            continue
        # Use same adaptive alpha as compute_dow_rates so bars reflect real patterns.
        # Convert to (int, bool) format expected by _adaptive_alpha.
        obs_for_alpha = [(i, v) for i, (_, v) in enumerate(obs)]
        alpha = _adaptive_alpha(obs_for_alpha, n_weeks_stable)
        ewma_val = _ewma_gap_decay(obs, alpha, current_week_start)
        dow_rates[dow] = round(max(0.05, min(0.95, ewma_val)), 3)
    return dow_rates

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

# ── Log-odds helpers ────────────────────────────────────────────────────────
# Combining independently-estimated effects (day-of-week base rate, Markov
# transition, quota pressure) by multiplying raw probabilities together has
# no principled ceiling — a 0.90 base times a 2.5x multiplier is 2.25,
# which only "worked" before because of an ad hoc clamp afterward, and it
# distorts the tails exactly where the clamp bites hardest. Adding in
# log-odds (logit) space instead and converting back with sigmoid is the
# textbook-correct way to combine a base rate with a multiplicative effect
# — the same mechanism logistic regression uses to combine feature effects.
def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))

def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    z = math.exp(x)
    return z / (1 + z)

def markov_adjust(base_prob: float, prev_status: Optional[str],
                  personal_markov: Optional[dict] = None) -> float:
    """
    Adjust base probability using personal Markov (preferred) or global
    fallback. Combines in log-odds space (see helpers above) rather than
    multiplying raw probabilities. Clamps to [0.05, 0.95].
    """
    prev = prev_status if prev_status in ("wfo","wfh","leave") else None
    table = personal_markov if personal_markov else MARKOV_GLOBAL
    mul = table.get((prev, "wfo"), 1.0)
    combined_logit = _logit(base_prob) + math.log(max(mul, 1e-6))
    return max(0.05, min(0.95, _sigmoid(combined_logit)))

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

# ── Team-pooled shrinkage (practical partial pooling) ──────────────────────
def compute_team_pool_rates(pooled_checkins: list[dict]) -> dict[int, float]:
    """
    A stable, long-run day-of-week WFO rate computed across an entire
    team's *combined* history, ignoring who each record belongs to — the
    prior that individual forecasts shrink toward when they don't have
    much personal data yet (see _shrink_to_team).

    Deliberately not an EWMA: a pooling prior should represent "what's
    typical on this team," not chase recent movement the way a personal
    forecast does — that's the personal EWMA's job, not this one's.
    """
    totals     = defaultdict(int)
    wfo_counts = defaultdict(int)
    for c in pooled_checkins:
        st = c.get("status")
        if st not in ("wfo", "wfh"):
            continue
        try:
            dow = date.fromisoformat(c["date"]).weekday()
        except Exception:
            continue
        if dow >= 5:
            continue
        totals[dow] += 1
        if st == "wfo":
            wfo_counts[dow] += 1
    # Laplace-smoothed so a thin team pool can't produce a hard 0 or 1
    return {dow: round((wfo_counts.get(dow, 0) + 1) / (totals.get(dow, 0) + 2), 3)
            for dow in range(5)}

def _shrink_to_team(dow_rates_stable: dict[int, float], dow_intervals: dict[int, dict],
                    team_dow_rates: Optional[dict[int, float]]) -> dict[int, float]:
    """
    Empirical-Bayes shrinkage: blend each weekday's personal rate toward
    the team-wide rate in proportion to how little personal data exists
    for that specific weekday. This is a lightweight, practical version of
    the same idea behind hierarchical/mixed-effects models (pool
    statistical strength across many small groups) without requiring an
    actual model fit — someone with TEAM_POOL_STRENGTH+ observed
    Wednesdays is barely nudged; someone with 1 is pulled most of the way
    to the team baseline instead of relying on a single data point.
    No-ops (returns input unchanged) when no team data was supplied — callers
    without team context (e.g. a lone-employee test) still get a sane result.
    """
    if not team_dow_rates:
        return dow_rates_stable
    shrunk = {}
    for dow, personal_rate in dow_rates_stable.items():
        n = dow_intervals.get(dow, {}).get("n", 0)
        team_rate = team_dow_rates.get(dow, 0.40)
        shrunk[dow] = round((n * personal_rate + TEAM_POOL_STRENGTH * team_rate) / (n + TEAM_POOL_STRENGTH), 3)
    return shrunk

# ── Quota pressure ──────────────────────────────────────────────────────────
def _quota_pressure_for_day(fd: date, ds: str, att: dict[str, str],
                            ph_dates: set[str], weekly_target: int,
                            predicted_wfo_dates: Optional[set[str]] = None) -> tuple[int, int]:
    """
    (days_needed_remaining, days_left_inclusive) for fd's own ISO week.

    predicted_wfo_dates: dates earlier in THIS SAME forecast run that were
    already predicted WFO, counted toward the week's target the same way a
    real recorded day is. Without this, every day in a week with zero
    actual data recomputes "0 WFO so far" from scratch and each one
    independently concludes the week can only be salvaged by coming in —
    so a week needing 3 office days can end up with all 5 remaining days
    predicted WFO, each one technically "correct" in isolation but
    collectively absurd. Once this day's own prediction lands, the
    caller is expected to add ds to this set before evaluating the next
    day, so later days in the same week see the earlier ones as already
    "accounted for" and pressure eases off accordingly.
    """
    predicted_wfo_dates = predicted_wfo_dates or set()
    monday        = fd - timedelta(days=fd.weekday())
    week_day_strs = [(monday + timedelta(days=i)).isoformat() for i in range(5)
                      if (monday + timedelta(days=i)).isoformat() not in ph_dates]
    leave_count        = sum(1 for wd in week_day_strs if att.get(wd) in ("leave", "public_holiday"))
    working_days_in_wk = len(week_day_strs) - leave_count
    week_target = math.ceil(working_days_in_wk * weekly_target / 5) if working_days_in_wk < 5 else weekly_target
    wfo_so_far  = sum(1 for wd in week_day_strs
                      if wd < ds and (att.get(wd) == "wfo" or wd in predicted_wfo_dates))
    days_needed_remaining = max(0, week_target - wfo_so_far)
    days_left_inclusive = sum(1 for wd in week_day_strs
                              if wd >= ds and att.get(wd) not in ("leave", "public_holiday"))
    return days_needed_remaining, days_left_inclusive

def compute_recent_level(att: dict[str, str], today: date, ph_dates: set[str],
                          window: int = RECENT_LEVEL_WINDOW) -> tuple[float, int]:
    """
    This person's WFO rate over their most recent `window` recorded working
    days — the level term of the v3 model (see PREDICTION MODEL above).

    Deliberately a short flat window rather than a long decayed average:
    the whole job of this term is to notice when someone's pattern has
    genuinely shifted and to follow it, which a slow average is designed
    not to do. Returns (rate, n_observations) so callers can tell a rate
    backed by 8 days from one backed by 2.
    """
    days = sorted(
        (ds for ds, st in att.items()
         if st in ("wfo", "wfh") and ds < today.isoformat()
         and ds not in ph_dates and _is_weekday(date.fromisoformat(ds))),
        reverse=True,
    )[:window]
    if not days:
        return 0.40, 0
    wfo = sum(1 for ds in days if att[ds] == "wfo")
    return wfo / len(days), len(days)


def compute_weekday_offsets(att: dict[str, str], today: date, ph_dates: set[str],
                             damping: float = WEEKDAY_OFFSET_DAMPING) -> dict[int, float]:
    """
    Per-weekday log-odds deviation from this person's own overall WFO rate,
    damped — the offset term of the v3 model.

    Expressed as a deviation rather than an absolute per-weekday rate on
    purpose: it has to compose with a level term that moves. "Fridays run
    hotter than my average" stays true when the average drops, whereas
    "Fridays are 86%" silently becomes wrong the moment behaviour shifts.
    """
    working = {ds: st for ds, st in att.items()
               if st in ("wfo", "wfh") and ds < today.isoformat()
               and ds not in ph_dates and _is_weekday(date.fromisoformat(ds))}
    if not working:
        return {dow: 0.0 for dow in range(5)}

    overall = sum(1 for st in working.values() if st == "wfo") / len(working)
    overall = min(max(overall, 0.05), 0.95)

    by_dow: dict[int, list[str]] = defaultdict(list)
    for ds, st in working.items():
        by_dow[date.fromisoformat(ds).weekday()].append(st)

    offsets = {}
    for dow in range(5):
        obs = by_dow.get(dow, [])
        if len(obs) < 2:
            offsets[dow] = 0.0   # too thin to claim a weekday effect at all
            continue
        # Laplace-smoothed so one or two observations can't imply a 0%/100% weekday
        rate = (sum(1 for s in obs if s == "wfo") + 1) / (len(obs) + 2)
        offsets[dow] = (_logit(rate) - _logit(overall)) * damping
    return offsets


def compute_prediction_base(att: dict[str, str], today: date, ph_dates: set[str]) -> dict[int, float]:
    """
    The per-weekday probabilities the forecast actually predicts from:
    recent level, adjusted by that weekday's damped offset. Distinct from
    compute_dow_rates_stable, which stays a pure historical description for
    the "My WFO Pattern" display — this one is the thing that has to be
    right about tomorrow, and it's what the backtest scores.
    """
    level, _n = compute_recent_level(att, today, ph_dates)
    level = min(max(level, 0.05), 0.95)
    offsets = compute_weekday_offsets(att, today, ph_dates)
    base_logit = _logit(level)
    return {dow: max(0.05, min(0.95, _sigmoid(base_logit + offsets.get(dow, 0.0))))
            for dow in range(5)}


def compute_predictability(att: dict[str, str], today: date, ph_dates: set[str],
                            lookback: int = 15) -> float:
    """
    How much genuine predictive signal this specific person has, measured
    against their own recent history. Returns 0..1, feeding the shrinkage
    that decides how confident the forecast is allowed to be.

    A fixed shrinkage constant can't be right for everyone. Someone with a
    rigid Mon/Wed/Fri routine deserves confident predictions; someone whose
    days are effectively coin flips deserves ~0.5 and no pretence
    otherwise. Measured against a flat "predict this person's own base
    rate" strategy, the only benchmark that matters: if the signals can't
    beat that, they're fitting noise, and the honest response is to stop
    claiming to know. Deliberately self-scoring — each person's own
    outcomes decide how much the model is trusted about them.
    """
    days = sorted(ds for ds, st in att.items()
                  if st in ("wfo", "wfh") and ds < today.isoformat()
                  and ds not in ph_dates and _is_weekday(date.fromisoformat(ds)))
    if len(days) < 12:
        return 0.5   # not enough history to judge either way — stay middling

    eval_days = days[-lookback:]
    err_model = err_base = 0.0
    n = 0
    for ds in eval_days:
        prior = {k: v for k, v in att.items() if k < ds}
        working_prior = [v for k, v in prior.items() if v in ("wfo", "wfh")]
        if len(working_prior) < 6:
            continue
        d_obj = date.fromisoformat(ds)
        base_rate = sum(1 for v in working_prior if v == "wfo") / len(working_prior)
        pred = compute_prediction_base(prior, d_obj, ph_dates).get(d_obj.weekday(), base_rate)
        actual = 1.0 if att[ds] == "wfo" else 0.0
        err_model += (pred - actual) ** 2
        err_base  += (base_rate - actual) ** 2
        n += 1

    if n == 0 or err_base <= 1e-9:
        return 0.5
    # Skill score: >0 means the signals genuinely beat the person's own base
    # rate, <=0 means they're adding noise and should be damped toward it.
    skill = 1.0 - (err_model / err_base)
    return max(0.0, min(1.0, skill))


def compute_compliance_responsiveness(att: dict[str, str], ph_dates: set[str], today: date,
                                       weekly_target: int = WEEKLY_WFO_TARGET,
                                       alpha: float = 0.35) -> float:
    """
    Does this person ACTUALLY chase the weekly office target when they fall
    behind? Returns 0..1, where 1 = reliably hits target every week and 0 =
    the target has no observable effect on their behaviour.

    This exists because assuming target-seeking behaviour is a much stronger
    claim than it looks. Quota pressure used to be applied to everyone at
    full strength, which meant the model would confidently predict "in
    office" for someone who is behind — overriding that person's own
    observed habit — even when their history plainly shows they miss the
    target and carry on as normal. That's the model asserting a behaviour
    the evidence contradicts, and it produced worse-than-coin-flip
    predictions for exactly the people whose recent pattern had shifted.

    Measured over completed weeks only (the in-progress week can't be
    scored yet), EWMA-weighted so a person who used to hit target but has
    missed the last three weeks is scored on who they are now, not who
    they were two months ago. Laplace-ish neutral 0.5 start means someone
    with almost no history gets partial pressure rather than either
    extreme.
    """
    week_days: dict[str, list[str]] = defaultdict(list)
    for ds, st in att.items():
        if ds >= today.isoformat():
            continue  # only completed days
        d = date.fromisoformat(ds)
        if not _is_weekday(d) or ds in ph_dates:
            continue
        wk = (d - timedelta(days=d.weekday())).isoformat()
        week_days[wk].append(st)

    current_week = (today - timedelta(days=today.weekday())).isoformat()
    scored = 0.5  # neutral prior — no evidence either way
    seen = 0
    for wk in sorted(week_days):
        if wk >= current_week:
            continue  # in-progress week isn't finished, can't be scored
        statuses = week_days[wk]
        working = [s for s in statuses if s in ("wfo", "wfh")]
        if len(working) < 3:
            continue  # partial week (holiday/leave-heavy or mid-install) — not a fair test
        target = math.ceil(len(working) * weekly_target / 5) if len(working) < 5 else weekly_target
        hit = 1.0 if sum(1 for s in working if s == "wfo") >= target else 0.0
        scored = (1 - alpha) * scored + alpha * hit
        seen += 1

    if seen == 0:
        return 0.5
    return max(0.0, min(1.0, scored))


def _quota_pressure_adjust(prob: float, days_needed_remaining: int, days_left_inclusive: int,
                           responsiveness: float = 1.0) -> float:
    """
    Nudges probability toward "will hit quota" behaviour as a week gets
    tight — someone 2 days behind with 2 workdays left may well come in
    regardless of what a normal Tuesday looks like for them. That's real,
    learnable, target-seeking behaviour the day-of-week/Markov signals
    can't see, since neither knows anything about the weekly target.

    Blends in log-odds space, weighted by how binding the quota is right
    now (needed / left) AND by how much this specific person has
    historically responded to that pressure (see
    compute_compliance_responsiveness). The responsiveness term is what
    keeps this honest: pressure can only override someone's observed habit
    to the extent their own history shows the target actually moves them.
    For a reliable target-chaser it behaves as before; for someone who
    routinely misses target it stays near-silent and habit governs, which
    is what the evidence supports.
    """
    if days_left_inclusive <= 0:
        return prob
    quota_rate = max(0.0, min(1.0, days_needed_remaining / days_left_inclusive))
    weight = quota_rate * max(0.0, min(1.0, responsiveness))
    if weight <= 0:
        return prob
    quota_logit = _logit(0.97 if quota_rate >= 0.999 else max(quota_rate, 0.03))
    combined_logit = (1 - weight) * _logit(prob) + weight * quota_logit
    return max(0.05, min(0.97, _sigmoid(combined_logit)))

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
    team_checkins:        Optional[list[dict]] = None,
) -> dict:
    """
    Compute personal WFO forecast for next forecast_days working days.

    team_checkins (optional): every teammate's checkin history, pooled
    (see compute_team_pool_rates) into a team-wide day-of-week baseline
    that this person's own rate gets shrunk toward in proportion to how
    little personal data they have — new joiners inherit something close
    to "what's typical here" instead of an arbitrary fast-learning alpha
    heuristic. Omit it and this behaves exactly as before (no shrinkage).
    """
    att = build_attendance(checkins, leaves, ph_dates)
    dow_rates, active_weeks, dow_intervals, dow_confidence = compute_dow_rates(att, ph_dates, today)
    # Stable display baseline — slow alpha, max 12 weeks, won't swing on a single day
    dow_rates_stable = compute_dow_rates_stable(att, ph_dates, today)
    if team_checkins:
        team_dow_rates   = compute_team_pool_rates(team_checkins)
        dow_rates_stable = _shrink_to_team(dow_rates_stable, dow_intervals, team_dow_rates)

    # Insufficiency check based on per-weekday observation counts
    # Use global active_weeks as primary gate (fastest check)
    # Fix 3: derive global confidence from actual observation count
    # sum of per-weekday Wilson n values is the true data volume
    total_actual_obs = sum(iv.get('n', 0) for iv in dow_intervals.values())
    global_conf = _confidence_tier(total_actual_obs)
    if active_weeks < 2:
        return {
            "model_version":     MODEL_VERSION,
            "employee_id":       employee_id,
            "employee_name":     employee_name,
            "confidence":        "insufficient",
            "confidence_label":  _confidence_label("insufficient"),
            "active_weeks":      active_weeks,
            "forecast":          [],
            "dow_rates_stable":  {str(i): 0.40 for i in range(5)},
            "compliance_weeks":  [],
            "wfh_budget":        0,
            "projected_month_total": 0,
            "monthly":           _monthly_progress(att, today, ph_dates, current_month_wfo),
            "insufficient_data": True,
            "message": f"Need at least 2 weeks of check-in data. Currently have {active_weeks}.",
        }

    # Build personal Markov matrix (uses Laplace smoothing, falls back to global)
    personal_markov = _build_personal_markov(att, ph_dates, today)
    # How much this person's own history says the weekly target actually
    # moves them — gates quota pressure so it can't assert compliance
    # behaviour the evidence contradicts.
    responsiveness = compute_compliance_responsiveness(att, ph_dates, today)
    # v3 prediction base: recent level + damped weekday offset. dow_rates_stable
    # remains the historical *description* shown in "My WFO Pattern"; this is
    # what the forecast actually predicts from. See PREDICTION MODEL at the top.
    pred_base = compute_prediction_base(att, today, ph_dates)
    # How confident this person's own track record earns the forecast being.
    # Blends the global floor with measured per-person skill, so a rigid
    # routine gets sharp predictions and an erratic one gets honest hedging
    # instead of noise dressed up as insight.
    predictability = compute_predictability(att, today, ph_dates)
    # sqrt curve, not linear: skill has to be earned before confidence is
    # granted, but someone with a genuinely rigid routine still reaches
    # near-full sharpness. Chosen by backtest sweep across four profiles
    # (this employee's real history, a rigid Mon/Wed/Fri pattern, a pure
    # coin-flip, and a noisy semi-structured one) — it won or tied on all
    # four, where a linear curve under-sharpened real patterns and a raw
    # pass-through over-trusted noise.
    shrink = max(0.10, min(0.90, (predictability ** 0.5) * 0.9))
    # What gets shown to the user: the weaker of "how much history exists"
    # and "how well that history actually predicts this person".
    reported_conf = _combined_confidence(global_conf, _predictability_tier(predictability))

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
    predicted_wfo_dates: set[str] = set()  # see _quota_pressure_for_day

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

        # v3 base: recent level + damped weekday offset (see PREDICTION MODEL).
        # The same pred_base feeds the Compliance Outlook week view below, so
        # the two panels can't disagree about the same date.
        base = pred_base.get(fd.weekday(), 0.40)
        prob = markov_adjust(base, running_prev, personal_markov)

        # Quota pressure, gated by how much this person's own history shows
        # the weekly target actually moves them — see
        # compute_compliance_responsiveness for why that gate exists.
        days_needed_remaining, days_left_inclusive = _quota_pressure_for_day(
            fd, ds, att, ph_dates, WEEKLY_WFO_TARGET, predicted_wfo_dates)
        prob = _quota_pressure_adjust(prob, days_needed_remaining, days_left_inclusive,
                                       responsiveness)

        # Calibration shrinkage — the last step, applied to whatever the
        # signals produced. Daily attendance is only weakly predictable even
        # with good history, and an overconfident wrong call costs far more
        # than a hedged one; this keeps stated confidence in line with what
        # the data actually supports rather than what the arithmetic emitted.
        prob = 0.5 + shrink * (prob - 0.5)

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
        if running_prev == "wfo":
            predicted_wfo_dates.add(ds)

    monthly = _monthly_progress(att, today, ph_dates, current_month_wfo)
    compliance_meta = _compliance_forecast_weeks(
        att, today, ph_dates, pred_base, current_month_wfo,
        dow_confidence=dow_confidence, personal_markov=personal_markov,
        responsiveness=responsiveness, shrink=shrink)
    month_rem = [f for f in forecast
                 if f["date"][:7] == today.strftime("%Y-%m")
                 and not f.get("certain") and f["status"] == "predicted_wfo"]
    monthly["predicted_additional"] = len(month_rem)
    monthly["predicted_total"]      = monthly["actual_wfo"] + len(month_rem)

    return {
        "model_version":         MODEL_VERSION,
        "employee_id":           employee_id,
        "employee_name":         employee_name,
        "confidence":            reported_conf,
        "confidence_label":      _confidence_label(reported_conf),
        "data_confidence":       global_conf,      # how much history exists
        "predictability":        round(predictability, 3),  # measured skill, 0..1
        "active_weeks":          active_weeks,
        "dow_rates":             {str(k): round(v,3) for k,v in dow_rates.items()},
        "dow_rates_stable":      {str(k): v for k, v in dow_rates_stable.items()},
        "dow_confidence":        {str(k): v for k,v in dow_confidence.items()},
        "personal_markov":       personal_markov is not None,
        "forecast":              forecast,
        "monthly":               monthly,
        "compliance_weeks":      compliance_meta["compliance_weeks"],
        "wfh_budget":            compliance_meta["wfh_budget"],
        "projected_month_total": compliance_meta["projected_month_total"],
        "insufficient_data":     False,
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
    needed    = max(0, MONTHLY_WFO_TARGET - actual_wfo)
    return {
        "month":          today.strftime("%B %Y"),
        "actual_wfo":     actual_wfo,
        "target":         MONTHLY_WFO_TARGET,
        "needed":         needed,
        "elapsed_days":   len(elapsed),
        "remaining_days": len(remaining),
        "total_workdays": len(workdays),
        "achievable":     len(remaining) >= needed,
        "on_track":       actual_wfo >= round((len(elapsed) / max(len(workdays),1)) * 12),
    }

# ── Per-week compliance forecast ──────────────────────────────────────────────
def _compliance_forecast_weeks(
    att: dict[str, str],
    today: date,
    ph_dates: set[str],
    pred_base: dict[int, float],
    current_month_wfo: int,
    dow_confidence: dict[int, str] | None = None,
    monthly_target: int = MONTHLY_WFO_TARGET,
    weekly_target: int = WEEKLY_WFO_TARGET,
    personal_markov: Optional[dict] = None,
    responsiveness: float = 1.0,
    shrink: float = FORECAST_SHRINK,
) -> dict:
    """
    Compute per-remaining-week compliance outlook for the current month.
    Uses stable (slow-moving) dow rates so projections don't chase behavior.

    Every day here is either a FACT (an actual check-in, approved leave, or
    override already on record — zero uncertainty) or a FORECAST (a future
    day where we're guessing based on that person's past pattern for that
    weekday). We never blur the two: forecasts always carry the honest
    Wilson-score confidence tier for that weekday (dow_confidence), so
    someone with 2 weeks of history doesn't get shown the same false
    certainty as someone with 6 months of history. There is no such thing
    as a 100%-certain prediction of a future day — people get sick, plans
    change — so we don't pretend otherwise anywhere in this output.

    Returns:
        compliance_weeks  — list of week dicts for the UI
        wfh_budget        — total WFH days you can take and still hit monthly target
        total_needed      — WFO days still needed this month
        projected_month_total — actual + projected WFO (may exceed target)
    """
    dow_confidence = dow_confidence or {}
    yr, mo = today.year, today.month
    workdays  = _workdays_in_month(yr, mo, ph_dates)
    elapsed   = [d for d in workdays if d < today.isoformat()]
    remaining = [d for d in workdays if d >= today.isoformat()]

    actual_wfo = max(
        sum(1 for d in elapsed if att.get(d) == "wfo"),
        current_month_wfo,
    )
    total_needed = max(0, monthly_target - actual_wfo)
    wfh_budget   = max(0, len(remaining) - total_needed)

    # Group remaining workdays by ISO week
    weeks_map: dict[str, list[str]] = defaultdict(list)
    for ds in remaining:
        d = date.fromisoformat(ds)
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        weeks_map[wk].append(ds)

    compliance_weeks = []
    projected_total_additional = 0.0
    # Carried across the whole remaining range (not reset per week) so the
    # Markov transition sees real day-to-day continuity, same as the main
    # per-day forecast loop in compute_forecast.
    running_prev = _last_workday_status(att, today, ph_dates)
    predicted_wfo_dates: set[str] = set()  # see _quota_pressure_for_day

    for wk_key in sorted(weeks_map.keys()):
        days_in_week = weeks_map[wk_key]
        day_details  = []
        week_proj    = 0.0
        leave_count  = 0

        for ds in days_in_week:
            d_obj         = date.fromisoformat(ds)
            dow           = d_obj.weekday()
            is_leave      = att.get(ds) in ("leave", "public_holiday")
            # Today counts as actual if a check-in was already recorded (wfo/wfh).
            # Only use the in-progress "today" state when no check-in exists yet.
            is_past_actual = ds <= today.isoformat() and att.get(ds) in ("wfo", "wfh")
            is_today      = ds == today.isoformat() and att.get(ds) not in ("wfo", "wfh")
            actual_status  = att.get(ds) if ds <= today.isoformat() else None

            # is_fact: this day is already decided — a real check-in, leave,
            # or holiday — not a guess. Everything else is a forecast and
            # must be labelled with how much we actually trust it.
            is_fact = is_leave or is_past_actual

            if is_leave:
                rate             = 0.0
                leave_count     += 1
                predicted_status = "leave"
                confidence_tier  = "certain"
                running_prev     = "leave"
            elif is_past_actual:
                rate              = 1.0 if actual_status == "wfo" else 0.0
                week_proj        += rate
                predicted_status  = actual_status
                confidence_tier   = "certain"
                running_prev      = actual_status
            else:
                # Identical pipeline to the per-day forecast in
                # compute_forecast — same pred_base, same Markov, same
                # responsiveness-gated quota pressure, same shrinkage — so
                # the two views can't disagree about the same date.
                base = pred_base.get(dow, 0.40)
                rate = markov_adjust(base, running_prev, personal_markov)
                days_needed_remaining, days_left_inclusive = _quota_pressure_for_day(
                    d_obj, ds, att, ph_dates, weekly_target, predicted_wfo_dates)
                rate = _quota_pressure_adjust(rate, days_needed_remaining, days_left_inclusive,
                                               responsiveness)
                rate = 0.5 + shrink * (rate - 0.5)
                week_proj        += rate
                predicted_status  = "wfo" if rate >= 0.5 else "wfh"
                confidence_tier   = dow_confidence.get(dow, "insufficient")
                running_prev      = predicted_status
                if predicted_status == "wfo":
                    predicted_wfo_dates.add(ds)

            day_details.append({
                "date":              ds,
                "dow":               dow,
                "dow_name":          d_obj.strftime("%A")[:3],
                "rate":              round(rate, 3),
                "is_leave":          is_leave,
                "is_today":          is_today,
                "is_actual":         is_past_actual,
                "is_fact":           is_fact,
                "actual_status":     actual_status,
                # What we're telling the person will happen on this day.
                # "certain" only ever means it's already a recorded fact —
                # never used for a genuine future prediction.
                "predicted_status":  predicted_status,
                "confidence_tier":   confidence_tier,
                "confidence_label":  "Recorded" if confidence_tier == "certain" else _confidence_label(confidence_tier),
            })

        working_days = len(days_in_week) - leave_count
        week_proj_r  = round(week_proj, 1)

        # Scale weekly target for partial weeks (e.g. 2 days → ceil(2*3/5)=2)
        week_tgt = math.ceil(working_days * weekly_target / 5) if working_days < 5 else weekly_target
        risk     = week_proj < week_tgt and working_days > 0

        # How many WFH days this person can still take this week and still
        # hit their weekly office target — the number people actually want
        # to know, not the raw projection.
        week_wfh_budget = max(0, working_days - week_tgt)

        # Plain-English summary of the week, built straight from the day
        # list above so it can never say something the data doesn't back up.
        office_facts, office_forecast, home_facts, home_forecast = [], [], [], []
        for dd in day_details:
            if dd["is_leave"]:
                continue
            bucket = office_facts if dd["predicted_status"] == "wfo" and dd["is_fact"] else \
                     office_forecast if dd["predicted_status"] == "wfo" else \
                     home_facts if dd["is_fact"] else home_forecast
            bucket.append(dd["dow_name"])

        summary_parts = []
        if office_facts:
            summary_parts.append(f"In office {', '.join(office_facts)}")
        if office_forecast:
            summary_parts.append(f"likely office {', '.join(office_forecast)}")
        if home_facts:
            summary_parts.append(f"WFH {', '.join(home_facts)}")
        if home_forecast:
            summary_parts.append(f"likely WFH {', '.join(home_forecast)}")
        week_summary = " · ".join(summary_parts) if summary_parts else "No working days this week"

        projected_total_additional += week_proj

        try:
            is_current = (
                date.fromisoformat(days_in_week[0]).isocalendar()[1] == today.isocalendar()[1]
                and date.fromisoformat(days_in_week[0]).isocalendar()[0] == today.isocalendar()[0]
            )
        except Exception:
            is_current = False

        compliance_weeks.append({
            "week_key":        wk_key,
            "week_start":      days_in_week[0],
            "week_end":        days_in_week[-1],
            "working_days":    working_days,
            "leave_days":      leave_count,
            "week_target":     week_tgt,
            "week_wfh_budget": week_wfh_budget,
            "week_summary":    week_summary,
            "projected_wfo":   week_proj_r,
            "risk":            risk,
            "is_current_week": is_current,
            "days":            day_details,
        })

    return {
        "compliance_weeks":      compliance_weeks,
        "wfh_budget":            wfh_budget,
        "total_needed":          total_needed,
        "remaining_days":        len(remaining),
        "projected_month_total": round(actual_wfo + projected_total_additional, 1),
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
            windowed.get(m["employee_id"], {}), ph_dates, today)
        if active_weeks >= 2:
            member_forecasts[m["employee_id"]] = dow_rates
        else:
            member_forecasts[m["employee_id"]] = {}

    best_days      = _compute_best_days(members, windowed, ph_dates, today)
    overlap_matrix = _compute_overlap(members, windowed, member_forecasts,
                                      ph_dates, today)
    individual     = []
    for m in members:
        dow_rates, active_weeks, dow_intervals, dow_conf = compute_dow_rates(
            windowed.get(m["employee_id"], {}), ph_dates, today)
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

def _compute_best_days(members, windowed, ph_dates, today):
    # Fix 4: weighted average by observation count per weekday
    # Members with more data anchor the recommendation more strongly
    # than new joiners with 1-2 weeks of history
    dow_weighted_sum:   dict[int, float] = defaultdict(float)
    dow_weight_total:   dict[int, float] = defaultdict(float)
    dow_member_count:   dict[int, int]   = defaultdict(int)

    for m in members:
        att = windowed.get(m["employee_id"], {})
        dow_rates, active_weeks, dow_intervals, _ = compute_dow_rates(att, ph_dates, today)
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