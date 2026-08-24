"""
backtest.py - measures whether the forecast model is actually any good.

Every constant in analytics.py (EWMA alpha, the Markov clamp range, the
variance threshold for "stable" vs "erratic") was chosen because it sounded
reasonable, not because it was checked against real outcomes. This module
closes that gap: for each employee, walk forward through their own history,
predict each day using only the data that would genuinely have been
available *before* that day, then compare to what actually happened.

Two things come out of this:
  - Brier score (mean squared error between predicted probability and the
    0/1 outcome) — lower is better, 0 is a perfect forecaster, 0.25 is what
    you get from a coin-flip default.
  - A naive baseline (always predict the employee's own long-run WFO rate,
    with no day-of-week or Markov structure at all) computed the same way,
    so "our model scores 0.18" has something to be compared against —
    beating a trivial baseline is the actual bar, not scoring low in
    isolation.

This intentionally does NOT try to backtest _compliance_forecast_weeks or
the team-rhythm functions — those are downstream of the same per-weekday
rates this already exercises, and evaluating them separately would just be
re-testing the same numbers through more code.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Optional

from analytics import compute_forecast, MODEL_VERSION


def brier_score(predicted_prob: float, actual_wfo: bool) -> float:
    outcome = 1.0 if actual_wfo else 0.0
    return (predicted_prob - outcome) ** 2


def evaluate_employee_forecast(
    employee_id:      str,
    employee_name:    str,
    checkins:         list[dict],
    leaves:           list[dict],
    ph_dates:         set[str],
    eval_dates:       list[date],
    min_prior_weeks:  int = 2,
) -> dict:
    """
    Pure function, no DB access — takes an employee's full checkin/leave
    history plus a list of dates to evaluate, and for each one:
      1. Truncates history to only what predates that date (no peeking)
      2. Asks compute_forecast what it would have predicted for that date
      3. Compares to the real recorded outcome on that date

    Returns per-date results plus aggregate Brier scores for the model and
    for the naive baseline. Dates where the model had insufficient history,
    or where the "prediction" was actually a certain fact (leave/holiday —
    nothing genuinely uncertain to score), are skipped and counted
    separately rather than silently dropped.
    """
    checkins_by_date = {c["date"]: c for c in checkins}
    results = []
    skipped_insufficient = 0
    skipped_certain = 0

    for eval_date in sorted(eval_dates):
        ds = eval_date.isoformat()
        actual = checkins_by_date.get(ds)
        if not actual or actual.get("status") not in ("wfo", "wfh"):
            continue  # no genuine WFO/WFH ground truth for this date

        # No peeking: only data strictly before eval_date is visible to the model.
        prior_checkins = [c for c in checkins if c["date"] < ds]
        prior_leaves   = [l for l in leaves if l["date"] < ds]

        result = compute_forecast(
            employee_id=employee_id,
            employee_name=employee_name,
            checkins=prior_checkins,
            leaves=prior_leaves,
            ph_dates=ph_dates,
            today=eval_date,
            forecast_days=1,
        )

        if result.get("insufficient_data"):
            skipped_insufficient += 1
            continue

        forecast_today = next((f for f in result["forecast"] if f["date"] == ds), None)
        if not forecast_today or forecast_today.get("certain"):
            skipped_certain += 1
            continue

        actual_wfo = actual["status"] == "wfo"
        predicted_prob = forecast_today["probability"]

        # Naive baseline: this employee's own long-run WFO rate over
        # everything visible up to this point, with no day-of-week or
        # Markov structure — the bar the real model actually has to clear.
        prior_wfo_statuses = [c["status"] for c in prior_checkins if c["status"] in ("wfo", "wfh")]
        baseline_prob = (
            sum(1 for s in prior_wfo_statuses if s == "wfo") / len(prior_wfo_statuses)
            if prior_wfo_statuses else 0.40
        )

        results.append({
            "date":            ds,
            "dow_name":        eval_date.strftime("%A"),
            "actual":          "wfo" if actual_wfo else "wfh",
            "predicted_prob":  predicted_prob,
            "confidence":      forecast_today.get("confidence"),
            "model_brier":     round(brier_score(predicted_prob, actual_wfo), 4),
            "baseline_brier":  round(brier_score(baseline_prob, actual_wfo), 4),
        })

    n = len(results)
    return {
        "employee_id":         employee_id,
        "employee_name":       employee_name,
        "n_evaluated":         n,
        "skipped_insufficient_data": skipped_insufficient,
        "skipped_certain_days":      skipped_certain,
        "model_brier_mean":    round(sum(r["model_brier"] for r in results) / n, 4) if n else None,
        "baseline_brier_mean": round(sum(r["baseline_brier"] for r in results) / n, 4) if n else None,
        "beats_baseline":      (
            sum(r["model_brier"] for r in results) < sum(r["baseline_brier"] for r in results)
            if n else None
        ),
        "days":                results,
    }


def _calibration_bins(all_days: list[dict], n_bins: int = 5) -> list[dict]:
    """
    Buckets predictions by probability decile and compares mean predicted
    vs. mean actual per bucket — the standard way to check whether "70%"
    really means "true about 70% of the time" rather than just sounding
    plausible. A well-calibrated model has predicted ≈ actual in every bin.
    """
    bins = [[] for _ in range(n_bins)]
    for d in all_days:
        idx = min(n_bins - 1, int(d["predicted_prob"] * n_bins))
        bins[idx].append(d)

    out = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        lo, hi = i / n_bins, (i + 1) / n_bins
        mean_pred = sum(d["predicted_prob"] for d in bucket) / len(bucket)
        mean_actual = sum(1 for d in bucket if d["actual"] == "wfo") / len(bucket)
        out.append({
            "range": f"{lo:.0%}-{hi:.0%}",
            "n": len(bucket),
            "mean_predicted": round(mean_pred, 3),
            "mean_actual": round(mean_actual, 3),
        })
    return out


async def run_backtest(db, min_history_weeks: int = 2) -> dict:
    """
    Full-fleet backtest: pulls every registered employee's real history
    from the DB and evaluates each of their own recorded workdays the same
    way evaluate_employee_forecast does. Aggregates to a fleet-wide Brier
    score and calibration table so "is the model good" has one number to
    look at, with per-employee detail available for anyone who wants to
    dig into a specific case.
    """
    from sqlalchemy import select
    from database import Device, CheckIn, LeaveRequest, PublicHoliday

    ph_q = await db.execute(select(PublicHoliday))
    ph_dates = {h.date for h in ph_q.scalars().all()}

    dq = await db.execute(select(Device))
    devices = dq.scalars().all()

    per_employee = []
    for device in devices:
        eid = device.employee_id
        ci_q = await db.execute(select(CheckIn).where(CheckIn.employee_id == eid).order_by(CheckIn.date))
        raw_checkins = ci_q.scalars().all()
        if len(raw_checkins) < min_history_weeks * 3:
            continue  # not enough history for this employee to be worth evaluating at all

        checkins = [{"date": r.date, "status": r.final_status or r.auto_status or "wfh"}
                    for r in raw_checkins]
        lv_q = await db.execute(select(LeaveRequest).where(LeaveRequest.employee_id == eid))
        leaves = [{"date": l.date, "leave_type": l.leave_type} for l in lv_q.scalars().all()]

        eval_dates = [date.fromisoformat(c["date"]) for c in checkins]
        result = evaluate_employee_forecast(
            eid, device.employee_name, checkins, leaves, ph_dates, eval_dates,
            min_prior_weeks=min_history_weeks,
        )
        if result["n_evaluated"] > 0:
            per_employee.append(result)

    all_days = [d for r in per_employee for d in r["days"]]
    n_total = len(all_days)

    return {
        "model_version": MODEL_VERSION,
        "employees_evaluated": len(per_employee),
        "total_days_evaluated": n_total,
        "fleet_model_brier_mean": (
            round(sum(d["model_brier"] for d in all_days) / n_total, 4) if n_total else None
        ),
        "fleet_baseline_brier_mean": (
            round(sum(d["baseline_brier"] for d in all_days) / n_total, 4) if n_total else None
        ),
        "calibration": _calibration_bins(all_days),
        "per_employee": [
            {k: v for k, v in r.items() if k != "days"}  # summary only; full detail is a lot of JSON
            for r in per_employee
        ],
    }
