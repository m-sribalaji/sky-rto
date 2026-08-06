"""
queue.py - the offline fallback. If the server's unreachable (VPN down,
laptop asleep, whatever) we stash the check-in payload on disk instead of
losing it, then flush it the next time we can reach the server. Also owns
the "write this file owner-only" helper since the queue file holds the same
kind of sensitive payload data as config.json.
"""

import os
import json
from datetime import datetime
from pathlib import Path

from .config import logger, QUEUE_FILE, load_config, _get_auth_headers
from .api import api_post


def _write_secure_file(path: Path, content: str):
    """Write text and lock down permissions to owner-only (0600).
    Used for any local file that could aid signal fabrication or credential
    theft if readable by other local users (pending_queue.json holds queued
    check-in payloads; config.json holds the device token)."""
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

def queue_checkin(payload: dict):
    from datetime import datetime as _dt
    date_str = payload.get("date", "")
    try:
        if _dt.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
            logger.info(f"Weekend date {date_str} - not queuing"); return
    except Exception: pass

    queue = []
    if QUEUE_FILE.exists():
        try: queue = json.loads(QUEUE_FILE.read_text())
        except Exception: queue = []
    queue = [q for q in queue if q.get("date") != payload.get("date")]
    payload["queued_at"] = datetime.utcnow().isoformat() + "Z"
    queue.append(payload)
    _write_secure_file(QUEUE_FILE, json.dumps(queue, indent=2))
    logger.info(f"Queued check-in for {payload.get('date')} (server unreachable)")

def flush_queue(server: str, cfg: dict = None) -> tuple:
    """Flush offline queue. Returns (count, [(payload, response)]) so caller
    can send per-record WFH/WFO Teams cards for each synced check-in."""
    cfg = cfg or load_config()
    if not QUEUE_FILE.exists(): return 0, []
    try: queue = json.loads(QUEUE_FILE.read_text())
    except Exception: return 0, []
    if not queue: return 0, []

    synced = []; failed = []; synced_results = []
    from datetime import datetime as _dt
    for payload in queue:
        date_str = payload.get("date", "")
        try:
            if _dt.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
                synced.append(payload); continue
        except Exception: pass
        resp = api_post(f"{server}/api/checkin", payload, auth_headers=_get_auth_headers(cfg), sign_key=cfg.get("device_token"))
        if resp and resp.get("action") in ("ok", "already_checked_in",
                                            "confirm_needed", "override_locked",
                                            "leave_recorded"):
            synced.append(payload)
            synced_results.append((payload, resp))
            logger.info(f"Flushed queued: {payload.get('date')} -> {resp.get('action')}")
        else:
            failed.append(payload)
            logger.warning(f"[WARN] Failed to flush: {payload.get('date')}")

    if failed: _write_secure_file(QUEUE_FILE, json.dumps(failed, indent=2))
    else: QUEUE_FILE.unlink(missing_ok=True)
    if synced: logger.info(f"Flushed {len(synced)} queued check-in(s)")
    return len(synced), synced_results
