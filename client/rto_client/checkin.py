"""
checkin.py - the main orchestrator. run_checkin() is what actually fires on
every trigger (unlock, poll timer, manual run): gather signals, classify,
decide if anything changed, talk to the server, handle registration/queueing/
notifications. It necessarily calls into just about every other module in
this package — that's expected, it's the thing tying the whole flow together.
run_reset() and run_retry() are the two small CLI-invoked helpers that live
next to it since they're just thin wrappers around the same state.
"""

import json
import time
from datetime import date, datetime

from .config import (
    logger, load_config, save_config, get_hostname, get_platform,
    _get_auth_headers, _get_notify_cfg, _sync_device_auth,
    QUEUE_FILE, CONFIG_DIR,
    NOTIFIER_AVAILABLE,
    notify_registration_needed, notify_registration_complete,
    post_employee_reply,
)
from .network import get_lan_ip, get_vpn_tunnel_ip, get_dns_info, get_is_ethernet
from .classify import classify_locally, location_changed
from .queue import queue_checkin, flush_queue
from .api import api_post, api_get, server_reachable, open_browser, _get_reg_url, _desktop_notify
from .lock import acquire_lock, release_lock
from .missed import check_missed_yesterday


# ── MAIN CHECK-IN ─────────────────────────────────────────────────────────────
def run_checkin(force: bool = False):
    cfg      = load_config()
    hostname = get_hostname()
    server   = cfg["server_url"].rstrip("/")
    today    = date.today().isoformat()

    logger.info(f"Check-in triggered | {hostname} | {today} | force={force}")

    lock = acquire_lock()
    if not lock:
        logger.info("Another instance is running - skipping"); return

    try:
        lan_ip               = get_lan_ip()
        vpn_tun              = get_vpn_tunnel_ip()
        dns_servers, dns_dom = get_dns_info()
        ethernet             = get_is_ethernet()
        local_class = classify_locally(lan_ip, vpn_tun, dns_servers, dns_dom, ethernet)

        logger.info(
            f"Signals: lan={lan_ip} vpn={vpn_tun} "
            f"dns={dns_servers} domains={dns_dom} eth={ethernet} "
            f"-> class={local_class}"
        )

        if server_reachable(server):
            _sync_device_auth(server, hostname, cfg)

        changed = location_changed(cfg, local_class)
        if not force and not changed:
            if QUEUE_FILE.exists() and server_reachable(server):
                flushed, flushed_results = flush_queue(server, cfg)
                if flushed > 0:
                    # Teams notifications for these are handled server-side now
                    # (see deps.sync_employee_teams_card) — each flushed record
                    # is really just a normal /api/checkin call once it lands,
                    # so the server already updates the person's card and posts
                    # the reply for it. Posting our own card here too would just
                    # double up the same event.
                    logger.info(f"Flushed {flushed} queued offline record(s)")
            logger.info(f"Same location ({local_class}), same day - skipping"); return

        payload = {
            "hostname":      hostname,
            "lan_ip":        lan_ip,
            "vpn_tunnel_ip": vpn_tun,
            "ssid":          None,
            "is_ethernet":   ethernet,
            "dns_servers":   dns_servers,
            "dns_domains":   dns_dom,
            "platform":      get_platform(),
            "date":          today,
        }

        if not server_reachable(server):
            logger.warning(f"[WARN] Server unreachable at {server}")
            if local_class in ("wfo", "wfh"):
                queue_len = queue_checkin(payload)
                cfg["last_checkin_date"]   = today
                cfg["last_status"]         = local_class
                cfg["last_detected_class"] = local_class
                save_config(cfg)
                _desktop_notify("RTO Tracker",
                    f"Server offline. {local_class.upper()} check-in saved locally "
                    f"and will sync automatically when VPN connects.")
                # The server being unreachable (internal VPN-only endpoint)
                # doesn't mean the internet is down — the Teams webhook is
                # a normal external HTTPS endpoint, so it's still reachable
                # from here even when the RTO server isn't. Ping it
                # directly so this doesn't go silent until whenever the
                # queue eventually flushes; the eventual real sync still
                # updates the persistent card itself (see queue-flush
                # comment below), this is just the "saved offline" ping.
                employee_id = cfg.get("employee_id")
                if NOTIFIER_AVAILABLE and employee_id:
                    post_employee_reply(
                        employee_id,
                        f"Server unreachable — {local_class.upper()} check-in saved "
                        f"locally ({queue_len} record{'s' if queue_len != 1 else ''} "
                        f"queued). Will sync automatically once VPN connects.",
                        cfg.get("teams_webhook"),
                    )
            else:
                _desktop_notify("RTO Tracker",
                       "Server unreachable. Connect Sky VPN for attendance tracking.")
            return

        flushed, flushed_results = flush_queue(server, cfg)
        if flushed > 0:
            # Teams notification for these happens server-side (see the
            # comment above) — nothing to send from here.
            logger.info(f"Flushed {flushed} offline record(s)")

        device = _sync_device_auth(server, hostname, cfg)
        if not device or not device.get("registered"):
            logger.info("Not registered - opening registration page")
            last_reg_ts  = cfg.get("last_reg_attempt_ts") or 0
            now_ts       = time.time()
            hour_elapsed = (now_ts - float(last_reg_ts)) > 3600

            reg_nonce = None
            if hour_elapsed:
                reg_url, reg_nonce = _get_reg_url(server, hostname)
                if NOTIFIER_AVAILABLE:
                    wh, lvl, _ = _get_notify_cfg(cfg)
                    notify_registration_needed(hostname, reg_url, webhook=wh, level=lvl)
                else:
                    _desktop_notify("RTO Tracker", "Please register your device.")
                open_browser(reg_url)
                cfg["last_reg_attempt_ts"] = now_ts
                cfg["_pending_reg_nonce"]  = reg_nonce  # survive across poll cycles
                save_config(cfg)
            else:
                logger.info("Registration browser opened recently - waiting")
                reg_nonce = cfg.get("_pending_reg_nonce")

            logger.info("Waiting up to 3 minutes for registration...")
            for attempt in range(36):
                time.sleep(5)
                check = api_get(f"{server}/api/device/{hostname}")
                if check and check.get("registered"):
                    emp_name = check.get("employee_name", hostname)
                    logger.info(f"Registration completed: {emp_name}")
                    cfg.pop("last_reg_attempt_ts", None)
                    cfg.pop("last_reg_attempt_date", None)
                    cfg.pop("last_token_recovery_ts", None)
                    cfg.pop("_pending_reg_nonce", None)
                    cfg["employee_name"] = emp_name
                    cfg["employee_id"] = check.get("employee_id")
                    # /api/device deliberately never returns api_token (it's a
                    # public read-only endpoint) — retrieve it via the nonce
                    # that gated this registration instead. The browser form
                    # submit stashes the token against this same nonce
                    # server-side (see /api/register).
                    if reg_nonce:
                        tok_resp = api_get(f"{server}/api/reg-nonce-status/{reg_nonce}")
                        if tok_resp and tok_resp.get("ready") and tok_resp.get("api_token"):
                            cfg["device_token"] = tok_resp["api_token"]
                        else:
                            logger.warning(
                                "[WARN] Registration detected but token not "
                                "yet claimed against nonce - will pick up "
                                "via token-refresh bootstrap next cycle"
                            )
                    save_config(cfg)
                    if NOTIFIER_AVAILABLE:
                        wh, lvl, srv = _get_notify_cfg(cfg)
                        notify_registration_complete(
                            emp_name, hostname, check.get("team", ""),
                            srv, webhook=wh, level=lvl)
                    else:
                        _desktop_notify("RTO Tracker", f"Welcome, {emp_name}! Device registered.")
                    device = check; break
            else:
                logger.info("Registration not completed in 3 minutes - retry on next unlock")
                return

        # Weekend skip (attendance only — registration always runs above)
        from datetime import datetime as _dt
        if _dt.strptime(today, "%Y-%m-%d").weekday() >= 5:
            logger.info(f"Weekend ({today}) - skipping attendance check-in"); return

        # Missed day check (once per day)
        check_missed_yesterday(server, hostname, cfg, today)

        response = api_post(f"{server}/api/checkin", payload, auth_headers=_get_auth_headers(cfg), sign_key=cfg.get("device_token"))
        if not response:
            logger.error("[FAIL] Check-in POST failed - queuing for retry")
            if local_class in ("wfo", "wfh"):
                queue_len = queue_checkin(payload)
                employee_id = cfg.get("employee_id")
                if NOTIFIER_AVAILABLE and employee_id:
                    post_employee_reply(
                        employee_id,
                        f"Check-in failed to reach server — {local_class.upper()} "
                        f"saved locally ({queue_len} record{'s' if queue_len != 1 else ''} "
                        f"queued). Will retry automatically.",
                        cfg.get("teams_webhook"),
                    )
            return

        action = response.get("action")
        logger.info(f"Server: action={action} detail={response.get('detail','')}")

        if action == "ok":
            status     = response.get("status")
            confidence = response.get("confidence", "")
            logger.info(f"[OK] Checked in: {status} (conf: {confidence})")
            cfg["last_checkin_date"]   = today
            cfg["last_status"]         = status
            cfg["last_detected_class"] = local_class
            if not cfg.get("employee_name"):
                dev_check = api_get(f"{server}/api/device/{hostname}")
                if dev_check and dev_check.get("employee_name"):
                    cfg["employee_name"] = dev_check["employee_name"]
                    cfg["employee_id"] = dev_check.get("employee_id")
                    cfg["device_token"] = dev_check.get("api_token")
            save_config(cfg)
            # Teams card + notification for this check-in is handled
            # server-side now (deps.sync_employee_teams_card, called from
            # inside /api/checkin) — the server already knows the full
            # current status and owns the one persistent card per person,
            # so posting our own here would just double it up.

        elif action == "already_checked_in":
            existing_status = response.get("status")
            if local_class in ("wfo", "wfh") and existing_status != local_class and force:
                force_payload = {**payload, "force_update": True}
                resp2 = api_post(f"{server}/api/checkin", force_payload, auth_headers=_get_auth_headers(cfg), sign_key=cfg.get("device_token"))
                if resp2 and resp2.get("action") == "ok":
                    status = resp2.get("status")
                    logger.info(f"[OK] Status updated: {status}")
                    cfg["last_checkin_date"]   = today
                    cfg["last_status"]         = status
                    cfg["last_detected_class"] = local_class
                    save_config(cfg); return
            logger.info(f"Already checked in today: {existing_status}")
            cfg["last_checkin_date"]   = today
            cfg["last_status"]         = existing_status
            cfg["last_detected_class"] = local_class
            save_config(cfg)

        elif action == "confirm_needed":
            logger.info("VPN ambiguous - opening confirmation page")
            open_browser(f"{server}/confirm/{hostname}")

        elif action == "register_first":
            url, _ = _get_reg_url(server, hostname)
            open_browser(url)

        elif action == "override_locked":
            locked_status = response.get("status", "unknown")
            logger.info(f"Day locked as {locked_status} - check-in skipped")
            cfg["last_checkin_date"] = today
            cfg["last_status"]       = locked_status
            save_config(cfg)

        elif action == "leave_recorded":
            logger.info("Leave recorded for today - check-in skipped")
            cfg["last_checkin_date"] = today
            cfg["last_status"]       = "leave"
            save_config(cfg)

        elif action == "weekend_skip":
            logger.info("Weekend - server skipped")

        else:
            logger.warning(f"[WARN] Unknown action: {action}")

    finally:
        release_lock(lock)

# ── RESET ─────────────────────────────────────────────────────────────────────
def run_reset():
    """Clear ALL local caches then return (caller runs run_checkin after)."""
    cleared = []
    for f in [CONFIG_DIR / ".last_watcher_run",
              CONFIG_DIR / ".checkin.lock",
              CONFIG_DIR / ".last_missed_check"]:
        if f.exists():
            try: f.unlink(); cleared.append(f.name)
            except Exception: pass

    cfg = load_config()
    cfg["last_checkin_date"]   = None
    cfg["last_status"]         = None
    cfg["last_detected_class"] = None
    cfg.pop("last_reg_attempt_ts", None)
    cfg.pop("last_reg_attempt_date", None)
    cfg.pop("last_token_recovery_ts", None)
    cfg.pop("_pending_reg_nonce", None)
    save_config(cfg)
    cleared.append("config cache")
    logger.info(f"Reset complete - cleared: {', '.join(cleared)}")
    print(f"\n  [OK] Reset complete - cleared: {', '.join(cleared)}")
    print(f"  Running fresh check-in...\n")

# ── RETRY QUEUE ───────────────────────────────────────────────────────────────
def run_retry():
    cfg    = load_config()
    hostname = get_hostname()
    server = cfg["server_url"].rstrip("/")
    if not QUEUE_FILE.exists():
        print("  No queued check-ins to retry."); return
    try: queue = json.loads(QUEUE_FILE.read_text())
    except Exception: print("  [FAIL] Queue file unreadable."); return
    if not queue: print("  Queue is empty."); return

    print(f"\n  Found {len(queue)} queued check-in(s):")
    for q in queue:
        print(f"    {q.get('date')} - locally classified as "
              f"{classify_locally(q.get('lan_ip'), q.get('vpn_tunnel_ip'), q.get('dns_servers',[]), q.get('dns_domains',[]), q.get('is_ethernet',False))}")

    if not server_reachable(server):
        print(f"\n    [WARN] Server unreachable at {server}")
        print(f"  Connect VPN and try again.\n"); return

    _sync_device_auth(server, hostname, cfg)
    flushed, _ = flush_queue(server, cfg)
    print(f"\n    [OK] Synced {flushed} record(s) to server.")
    remaining = json.loads(QUEUE_FILE.read_text()) if QUEUE_FILE.exists() else []
    if remaining:
        print(f"    [FAIL] {len(remaining)} record(s) still failed - check logs.")
    print()
