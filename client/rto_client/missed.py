"""
missed.py - the "hey, you forgot to check in yesterday" sweep. Runs once a
day, looks back a week for weekdays with no attendance record (skipping
public holidays), tries to auto-resolve anything we still have a cached
payload for, and otherwise pops the browser open to a bulk missed-days page.
Pulled out on its own since it leans on nearly every other module (api,
queue, classify) and would otherwise bloat run_checkin further.
"""

import json
from datetime import datetime, timedelta

from .config import logger, MISSED_DAY_FILE, QUEUE_FILE, NOTIFIER_AVAILABLE, notify_missed_days, _get_notify_cfg, _get_auth_headers, _sync_device_auth
from .api import api_get, api_post, server_reachable, open_browser, _desktop_notify
from .classify import classify_locally
from .queue import _write_secure_file


def check_missed_yesterday(server: str, hostname: str, cfg: dict, today: str):
    try:
        last_checked = MISSED_DAY_FILE.read_text().strip() if MISSED_DAY_FILE.exists() else ""
        if last_checked == today: return
    except Exception: pass
    try: MISSED_DAY_FILE.write_text(today)
    except Exception: pass

    if not server_reachable(server): return
    device = _sync_device_auth(server, hostname, cfg)
    if not device or not device.get("registered"): return

    emp_id = device.get("employee_id", "")
    ph_dates = set()
    ph_data = api_get(f"{server}/api/holidays?year={today[:4]}")
    if ph_data:
        ph_dates = {h["date"] for h in ph_data.get("holidays", [])}

    days_to_check = []
    for i in range(7, 0, -1):
        dt = datetime.now() - timedelta(days=i)
        if dt.weekday() >= 5: continue
        ds = dt.strftime("%Y-%m-%d")
        if ds in ph_dates: continue
        if ds >= today: continue
        days_to_check.append(ds)

    if not days_to_check: return

    months_needed = list(dict.fromkeys(d[:7] for d in days_to_check))
    records_by_date = {}
    for month in months_needed:
        history = api_get(f"{server}/api/history/{emp_id}?month={month}",
                          auth_headers=_get_auth_headers(cfg))
        if history:
            for r in history.get("records", []):
                records_by_date[r["date"]] = r

    missing_to_prompt = []
    for day in days_to_check:
        if day in records_by_date: continue
        cached_payload = None
        try:
            if QUEUE_FILE.exists():
                queue_data = json.loads(QUEUE_FILE.read_text())
                cached_payload = next(
                    (q for q in queue_data if q.get("date") == day), None)
        except Exception: pass

        if cached_payload:
            local_class = classify_locally(
                cached_payload.get("lan_ip"),
                cached_payload.get("vpn_tunnel_ip"),
                cached_payload.get("dns_servers", []),
                cached_payload.get("dns_domains", []),
                cached_payload.get("is_ethernet", False),
            )
            if server_reachable(server):
                resp = api_post(f"{server}/api/checkin", cached_payload, auth_headers=_get_auth_headers(cfg))
                if resp and resp.get("action") in ("ok", "already_checked_in"):
                    logger.info(f"Soft miss auto-resolved: {day}")
                    try:
                        qd = json.loads(QUEUE_FILE.read_text())
                        qd = [q for q in qd if q.get("date") != day]
                        if qd: _write_secure_file(QUEUE_FILE, json.dumps(qd, indent=2))
                        else: QUEUE_FILE.unlink(missing_ok=True)
                    except Exception: pass
                    continue
            missing_to_prompt.append({
                "date": day, "cached_class": local_class,
                "cached_lan": cached_payload.get("lan_ip", ""),
            })
        else:
            missing_to_prompt.append({"date": day, "cached_class": "", "cached_lan": ""})

    if not missing_to_prompt:
        logger.info("Missed day check: all days auto-resolved"); return

    dates_str = ",".join(d["date"] for d in missing_to_prompt)
    missed_url = f"{server}/missed/{hostname}?dates={dates_str}"
    for d in missing_to_prompt:
        if d["cached_class"]: missed_url += f"&class_{d['date']}={d['cached_class']}"
        if d["cached_lan"]:   missed_url += f"&lan_{d['date']}={d['cached_lan']}"

    n = len(missing_to_prompt)
    if n == 1:
        _desktop_notify("RTO Tracker",
               f"No record for {missing_to_prompt[0]['date']}. Please fill in your attendance.")
    else:
        _desktop_notify("RTO Tracker",
               f"{n} days with no attendance record. Please fill them in.")

    logger.info(f"Opening bulk missed page for {n} day(s): {dates_str}")
    open_browser(missed_url)
    if NOTIFIER_AVAILABLE:
        wh, lvl, _ = _get_notify_cfg(cfg)
        notify_missed_days(cfg.get("employee_name", hostname),
                           [d["date"] for d in missing_to_prompt],
                           missed_url, webhook=wh, level=lvl)
