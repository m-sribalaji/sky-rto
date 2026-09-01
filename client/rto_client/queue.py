"""
queue.py - the offline fallback. If the server's unreachable (VPN down,
laptop asleep, whatever) we stash the check-in payload on disk instead of
losing it, then flush it the next time we can reach the server. Also owns
the "write this file owner-only" helper since the queue file holds the same
kind of sensitive payload data as config.json.

SIGNING (security review, 2026-09): each queued entry is now signed at
capture time — the moment the real network signals were observed — and
the resulting signature is stored alongside the payload as a sealed pair.
flush_queue() replays that exact pair unchanged; it does NOT recompute a
signature over whatever's currently on disk. Previously it did, which
meant editing the queue file any time before it happened to flush (which
could be hours later) produced a perfectly valid signature over the
edited content — the server had no way to tell a hand-edited entry from
a genuinely-captured one. Now, editing the file after capture makes the
stored signature stop matching the (changed) payload bytes, and the
server's existing signature check rejects it exactly like it would any
other tampered request.

This closes the "just edit pending_queue.json" attack path only once
paired with native_signer (client/native_signer) actually being live on
a device — while a device is still on the legacy plaintext device_token,
a local user could still read that token and manually recompute a
matching signature over their edit, same as they could before. See
native_signer/README.md for that module's build status.
"""

import os
import json
import base64
from datetime import datetime
from pathlib import Path

from .config import logger, QUEUE_FILE, load_config, _get_auth_headers
from .api import api_post, api_post_presigned, _sign_request, _sign_request_native


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

def queue_checkin(payload: dict, cfg: dict = None) -> int:
    """Stash payload for later sync. Returns the queue's new length so the
    caller can report 'N records queued' without a separate read.

    Signs NOW, at capture time, using whichever signing method the device
    currently has (native_signer if enrolled, else the legacy HMAC
    device_token) — see module docstring for why. The signed envelope
    (payload + headers) is what actually gets stored; flush_queue() just
    replays it later unmodified.
    """
    from datetime import datetime as _dt
    date_str = payload.get("date", "")
    try:
        if _dt.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
            logger.info(f"Weekend date {date_str} - not queuing"); return 0
    except Exception: pass

    cfg = cfg or load_config()
    payload["queued_at"] = datetime.utcnow().isoformat() + "Z"
    payload_bytes = json.dumps(payload).encode()

    sig_headers = None
    if cfg.get("native_public_key"):
        sig_headers = _sign_request_native(payload_bytes, queued=True)
        # IMPORTANT (found live, 2026-09): do NOT fall back to legacy HMAC
        # here if native signing fails (e.g. Keychain locked — happens
        # overnight while the Mac sleeps). Once a device has enrolled a
        # public_key, the server ONLY accepts ECDSA signatures for it
        # (deps.py's dual-path check is intentionally exclusive, not
        # either/or — accepting either would let anyone bypass ECDSA
        # entirely by just using the plaintext token, defeating the whole
        # point). An HMAC-signed entry from an enrolled device is
        # permanently, unrecoverably rejected by the server — worse than
        # not signing at all, since flush_queue used to just replay it
        # forever without ever retrying. Leaving sig_headers=None here
        # instead means flush_queue (below) retries live signing instead.
    elif cfg.get("device_token"):
        sig_headers = _sign_request(cfg["device_token"], payload_bytes, queued=True)

    entry = {
        "payload": payload,
        # Store the exact bytes that were signed (base64) rather than
        # re-deriving them from `payload` at flush time via
        # json.dumps(payload) again — Python dict key ordering is stable
        # within one process/version but this removes any risk of a
        # future re-serialization producing different bytes than what
        # was actually signed, which would just make every queued entry
        # fail its own signature check for an unrelated reason.
        "payload_b64": base64.b64encode(payload_bytes).decode(),
        "sig_headers": sig_headers,
    }

    queue = []
    if QUEUE_FILE.exists():
        try: queue = json.loads(QUEUE_FILE.read_text())
        except Exception: queue = []
    queue = [q for q in queue if q.get("payload", {}).get("date") != payload.get("date")]
    queue.append(entry)
    _write_secure_file(QUEUE_FILE, json.dumps(queue, indent=2))
    logger.info(f"Queued check-in for {payload.get('date')} (server unreachable, signed at capture time)")
    return len(queue)

def flush_queue(server: str, cfg: dict = None) -> tuple:
    """Flush offline queue. Returns (count, [(payload, response)]) so caller
    can send per-record WFH/WFO Teams cards for each synced check-in.

    Replays each entry's capture-time-signed bytes/headers unchanged —
    does not re-sign. Falls back to the old "sign now, from whatever's on
    disk" behavior only for legacy-format entries queued before this
    change (no sig_headers stored), so an in-flight queue file from an
    older agent version doesn't just get silently dropped on upgrade.
    """
    cfg = cfg or load_config()
    if not QUEUE_FILE.exists(): return 0, []
    try: queue = json.loads(QUEUE_FILE.read_text())
    except Exception: return 0, []
    if not queue: return 0, []

    synced = []; failed = []; synced_results = []
    from datetime import datetime as _dt
    for entry in queue:
        # Legacy format: a bare payload dict (pre-capture-time-signing).
        is_legacy = "payload" not in entry
        payload = entry if is_legacy else entry["payload"]
        date_str = payload.get("date", "")
        try:
            if _dt.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
                synced.append(payload); continue
        except Exception: pass

        if is_legacy:
            resp = api_post(f"{server}/api/checkin", payload, auth_headers=_get_auth_headers(cfg),
                            sign_key=cfg.get("device_token"),
                            use_native_signer=bool(cfg.get("native_public_key")), queued=True)
        else:
            sig_headers = entry.get("sig_headers")
            payload_bytes = base64.b64decode(entry["payload_b64"])
            if sig_headers:
                resp = api_post_presigned(f"{server}/api/checkin", payload_bytes, sig_headers,
                                          auth_headers=_get_auth_headers(cfg))
            elif cfg.get("native_public_key"):
                # Capture-time native signing failed (Keychain was locked,
                # most likely — this happens overnight while the Mac
                # sleeps). Rather than give up permanently, retry live
                # right now — flush time is typically right after
                # reconnecting/waking, when the Keychain is far more
                # likely to be accessible again. If it still fails, this
                # entry stays queued and gets retried again next cycle.
                sig_headers = _sign_request_native(payload_bytes, queued=True)
                if sig_headers:
                    resp = api_post_presigned(f"{server}/api/checkin", payload_bytes, sig_headers,
                                              auth_headers=_get_auth_headers(cfg))
                else:
                    logger.warning(f"[WARN] Queued entry for {date_str} still can't be signed (native_signer unavailable) - will retry next cycle")
                    failed.append(entry)
                    continue
            else:
                # No signing method available at all for this entry and
                # never was (device never enrolled, never had a token at
                # capture time) — nothing safe to replay.
                failed.append(entry)
                logger.warning(f"[WARN] Queued entry for {date_str} has no signing method available - skipping")
                continue

        if resp and resp.get("action") in ("ok", "already_checked_in",
                                            "confirm_needed", "override_locked",
                                            "leave_recorded"):
            synced.append(payload)
            synced_results.append((payload, resp))
            logger.info(f"Flushed queued: {payload.get('date')} -> {resp.get('action')}")
        else:
            failed.append(entry)
            logger.warning(f"[WARN] Failed to flush: {payload.get('date')}")

    if failed: _write_secure_file(QUEUE_FILE, json.dumps(failed, indent=2))
    else: QUEUE_FILE.unlink(missing_ok=True)
    if synced: logger.info(f"Flushed {len(synced)} queued check-in(s)")
    return len(synced), synced_results
