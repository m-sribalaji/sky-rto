"""
notifier.py - RTO Tracker Teams + Desktop notification module

This used to be duplicated as server/notifier.py and client/notifier.py,
two byte-identical files that would inevitably drift apart. It now lives
here in shared/ so both the server and the PyInstaller-built client agent
import the same code. See server/main.py and client/checkin_core.py for
how each side adds this directory to sys.path, and build.sh for how the
client binary bundles it.

Supports two notification channels:
  1. Microsoft Teams  - via Incoming Webhook (no API/admin needed)
  2. Desktop          - via platform-native notification (existing notify())

Teams messages use Adaptive Cards for rich formatting.
All messages are emoji-free and use clean text + card styling.

Configuration (in ~/.rto_tracker/config.json):
  "teams_webhook": "https://outlook.office.com/webhook/..."   # optional
  "teams_notify_level": "all" | "important" | "errors"       # default: all
"""

import json
import re
import logging
import platform
import subprocess
import threading
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("notifier")

# Masks the last two octets of any IPv4 address before it goes out in a
# Teams message — cards land in a channel that's visible to more people
# than the compliance data itself normally is, so no reason to broadcast
# someone's exact home/office IP there. First two octets are usually enough
# for a manager to recognise "yeah that's the office range" without
# exposing the specific address. Works as a blanket regex swap rather than
# a strict IP parser, so it also catches IPs embedded in free-text
# descriptions (flag_reason strings etc.), not just dedicated IP fields.
_IP_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}\b")

def _redact_ip(value):
    if value is None:
        return value
    return _IP_RE.sub(r"\1.\2.X.X", str(value))

# -- Notification levels -----------------------------------
LEVEL_ALL       = "all"        # every event
LEVEL_IMPORTANT = "important"  # check-ins, misses, VPN issues (no queue syncs)
LEVEL_ERRORS    = "errors"     # only failures and warnings

_LEVEL_RANK = {LEVEL_ALL: 0, LEVEL_IMPORTANT: 1, LEVEL_ERRORS: 2}

# -- Event types + their minimum level --------------------
_EVENT_LEVELS = {
    # Check-in results
    "checkin_wfo":          LEVEL_ALL,
    "checkin_wfh":          LEVEL_ALL,
    "checkin_split":        LEVEL_ALL,
    "checkin_already":      LEVEL_ALL,
    # Issues requiring action
    "vpn_ambiguous":        LEVEL_IMPORTANT,
    "server_unreachable":   LEVEL_IMPORTANT,
    "missed_day":           LEVEL_IMPORTANT,
    "registration_needed":  LEVEL_IMPORTANT,
    # Background ops
    "queue_flushed":        LEVEL_ALL,
    "queue_saved":          LEVEL_ALL,
    # Manager actions (sent server-side)
    "override_applied":     LEVEL_IMPORTANT,
    "leave_applied":        LEVEL_IMPORTANT,
    # Errors
    "error":                LEVEL_ERRORS,
    "wfo_on_leave":         LEVEL_IMPORTANT,
    "registration_complete": LEVEL_IMPORTANT,
}

# -- Card accent colours -----------------------------------
_COLOURS = {
    "checkin_wfo":          "good",     # green
    "checkin_wfh":          "accent",   # blue
    "checkin_split":        "accent",
    "checkin_already":      "default",
    "vpn_ambiguous":        "warning",
    "server_unreachable":   "attention", # red
    "missed_day":           "warning",
    "registration_needed":  "accent",
    "queue_flushed":        "good",
    "queue_saved":          "default",
    "override_applied":     "warning",
    "leave_applied":        "accent",
    "error":                "attention",
    "wfo_on_leave":         "warning",
    "registration_complete": "good",
}

# -- Icons (text-based, no emojis) ------------------------
_ICONS = {
    "checkin_wfo":          "[OK]",
    "checkin_wfh":          "[OK]",
    "checkin_split":        "[OK]",
    "checkin_already":      "[OK]",
    "vpn_ambiguous":        "[WARN]",
    "server_unreachable":   "[WARN]",
    "missed_day":           "[WARN]",
    "registration_needed":  "[INFO]",
    "queue_flushed":        "[OK]",
    "queue_saved":          "[INFO]",
    "override_applied":     "[WARN]",
    "leave_applied":        "[INFO]",
    "error":                "[FAIL]",
}


# ---------------------------------------------------------
# TEAMS ADAPTIVE CARD BUILDER
# ---------------------------------------------------------

def _build_card(event: str, title: str, body: str,
                facts: Optional[list] = None,
                action_url: Optional[str] = None,
                action_label: str = "Open RTO Tracker") -> dict:
    """Build a Teams Adaptive Card payload."""
    colour  = _COLOURS.get(event, "default")
    icon    = _ICONS.get(event, "[RTO]")
    ts      = datetime.now().strftime("%a %d %b %Y  %H:%M")

    # Header block
    header_text = f"**{icon}  {title}**"

    body_blocks = [
        {
            "type": "TextBlock",
            "text": header_text,
            "weight": "Bolder",
            "size": "Medium",
            "color": colour,
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": _redact_ip(body),
            "wrap": True,
            "spacing": "Small",
        },
    ]

    # Optional fact table (key-value pairs)
    if facts:
        body_blocks.append({
            "type": "FactSet",
            "spacing": "Small",
            "facts": [{"title": k, "value": _redact_ip(v)} for k, v in facts],
        })

    # Timestamp
    body_blocks.append({
        "type": "TextBlock",
        "text": ts,
        "size": "Small",
        "color": "default",
        "isSubtle": True,
        "spacing": "Medium",
    })

    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body_blocks,
                "actions": ([{
                    "type": "Action.OpenUrl",
                    "title": action_label,
                    "url": action_url,
                }] if action_url else []),
            }
        }]
    }
    return card


# ---------------------------------------------------------
# SEND FUNCTIONS
# ---------------------------------------------------------

def _send_teams(webhook_url: str, payload: dict) -> bool:
    """POST an Adaptive Card to a Teams webhook. Returns True on success."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            webhook_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
            method="POST",
        )
        # SSL: resolve cert bundle — works both as .py and inside PyInstaller binary
        import ssl as _ssl
        ssl_ctx = None
        try:
            import certifi
            ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        if ssl_ctx is None:
            try:
                ssl_ctx = _ssl.create_default_context()
            except Exception:
                ssl_ctx = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            status = resp.status
            # 200 = classic O365 Connector success.
            # 202 = Power Automate Workflow webhook success (request accepted, run triggered async).
            if status in (200, 202):
                logger.info(f"[OK] Teams notification sent (HTTP {status})")
                return True
            body_txt = ""
            try:
                body_txt = resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                body_txt = f"response body unavailable: {e}"
            logger.warning(f"[WARN] Teams webhook returned HTTP {status}: {body_txt}")
            return False
    except urllib.error.HTTPError as e:
        # 202 responses with empty bodies can sometimes surface here depending on
        # the urllib version's handling of non-2xx-with-empty-body edge cases —
        # handle explicitly rather than falling through to the generic except.
        if e.code in (200, 202):
            logger.info(f"[OK] Teams notification sent (HTTP {e.code})")
            return True
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        logger.warning(f"[WARN] Teams webhook returned HTTP {e.code}: {body_txt}")
        return False
    except urllib.error.URLError as e:
        logger.warning(f"[WARN] Teams webhook unreachable: {e}")
        return False
    except Exception as e:
        logger.error(f"[FAIL] Teams notification failed: {e}")
        return False


def _send_teams_json(webhook_url: str, payload: dict) -> dict | None:
    """
    Same as _send_teams, but for calls where we need something back —
    specifically, the Power Automate flow handing us the Teams message ID
    it just created so we can remember it for next time. Returns the
    parsed JSON body on success, None on any failure (network, non-2xx,
    or a response that isn't valid JSON).
    """
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity", "Connection": "close"},
            method="POST",
        )
        import ssl as _ssl
        ssl_ctx = None
        try:
            import certifi
            ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        if ssl_ctx is None:
            try:
                ssl_ctx = _ssl.create_default_context()
            except Exception:
                ssl_ctx = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
            if resp.status not in (200, 202):
                logger.warning(f"[WARN] Teams webhook returned HTTP {resp.status}")
                return None
            raw = resp.read().decode("utf-8", errors="ignore")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"[WARN] Teams webhook call failed: {e}")
        return None


# ---------------------------------------------------------
# PERSISTENT PER-EMPLOYEE CARD (create-once, update-forever)
# ---------------------------------------------------------
# The old model: every event (check-in, override, leave...) posts a brand
# new Teams message. At 140+ people checking in twice a day, that's a
# firehose. The new model: each employee has exactly ONE card, which gets
# edited in place every time their status changes — plus a threaded reply
# under that card so a change is still visible in the channel feed, just
# without spawning a new top-level post every time.
#
# This only works through a Power Automate flow (not the old Incoming
# Webhook connector, which can only ever post new messages) — the flow
# needs to branch on an `action` field in the payload:
#
#   action="create_or_update_card": if `message_id` is present, edit that
#   message with the new `card`; if it's null, post `card` as a new
#   message and respond with {"message_id": "<the new id>"} so we can
#   remember it. Either way, respond synchronously (a Response action in
#   the flow) — that's what lets us read the id straight off the HTTP
#   response instead of needing a separate callback into our own API.
#
#   action="reply": post `text` as a threaded reply under `message_id`.
#
# Card content and history (who has which message_id) live in OUR
# database, not the flow — the flow is deliberately kept dumb (create-or-
# update, reply), all the actual logic of "what does this person's card
# say right now" happens in Python where it's easy to test and change.

# Maps a status word to a pill's colour. Adaptive Cards don't have a
# native "pill/chip" component, so pills are built as small bordered
# Containers with a semantic `style` (good/attention/warning/accent/
# default) — that's the same styling vocabulary the rest of this file
# already uses for card accent colours, just applied per-pill instead of
# per-card.
_PILL_STYLES = {
    "wfo":              "good",
    "wfh":               "accent",
    "vpn_ambiguous":     "warning",
    "leave":             "accent",
    "public_holiday":    "accent",
    "flagged":           "attention",
    "vpn_on":            "accent",
    "confidence_high":   "good",
    "confidence_medium": "warning",
    "confidence_low":    "attention",
    "default":           "default",
}

def build_status_pills(status: str, vpn_active: bool, confidence: str,
                        flagged: bool, flag_reason: str | None,
                        leave_label: str | None = None) -> list[dict]:
    """
    Turns a person's current attendance state into the list of pills their
    card should show right now — status, VPN, confidence, and a flag pill
    if something looks off. Same inputs the dashboard's own status chips
    are built from, just rendered for Teams instead of the browser.
    """
    pills = []
    if leave_label:
        pills.append({"text": leave_label, "style": _PILL_STYLES["leave"]})
    elif status == "wfo":
        pills.append({"text": "In office", "style": _PILL_STYLES["wfo"]})
    elif status == "wfh":
        pills.append({"text": "Working from home", "style": _PILL_STYLES["wfh"]})
    elif status:
        pills.append({"text": status.replace("_", " ").title(), "style": _PILL_STYLES["default"]})

    if vpn_active:
        pills.append({"text": "VPN on", "style": _PILL_STYLES["vpn_on"]})

    if confidence:
        pills.append({"text": f"{confidence.capitalize()} confidence",
                       "style": _PILL_STYLES.get(f"confidence_{confidence}", "default")})

    if flagged:
        pills.append({"text": _redact_ip(flag_reason) or "Flagged", "style": _PILL_STYLES["flagged"]})

    return pills

def build_employee_card(employee_name: str, employee_id: str, team: str | None,
                         pills: list[dict], last_updated: str) -> dict:
    """The one persistent card per employee — see module docstring above."""
    pill_columns = [{
        "type": "Column", "width": "auto",
        "items": [{
            "type": "Container", "style": p["style"], "bleed": False,
            "spacing": "None",
            "items": [{"type": "TextBlock", "text": p["text"], "size": "Small",
                       "weight": "Bolder", "wrap": False}],
        }],
    } for p in pills]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "ColumnSet",
                "columns": [
                    {"type": "Column", "width": "auto", "items": [{
                        "type": "TextBlock", "text": employee_name, "weight": "Bolder", "size": "Medium",
                    }]},
                    {"type": "Column", "width": "stretch", "items": [{
                        "type": "TextBlock", "text": f"{employee_id}" + (f" · {team}" if team else ""),
                        "size": "Small", "isSubtle": True, "horizontalAlignment": "Right",
                    }]},
                ],
            },
            {"type": "ColumnSet", "spacing": "Medium", "wrap": True, "columns": pill_columns} if pill_columns
                else {"type": "TextBlock", "text": "No status yet", "isSubtle": True},
            {"type": "TextBlock", "text": f"Last updated {last_updated} · This card updates automatically",
             "size": "Small", "isSubtle": True, "spacing": "Medium", "wrap": True},
        ],
    }

def upsert_employee_card(employee_id: str, employee_name: str, team: str | None,
                          pills: list[dict], existing_message_id: str | None,
                          webhook: str | None) -> str | None:
    """
    Create the employee's card if they don't have one yet, or edit their
    existing one in place. Returns the message_id to persist (same as
    existing_message_id if this was an edit, a new one if this was a
    create) — or None if the send failed, in which case the caller should
    NOT overwrite whatever message_id it already had stored.
    """
    if not webhook:
        return existing_message_id
    from datetime import datetime as _dt
    last_updated = _dt.now().strftime("%a %d %b, %H:%M")
    card = build_employee_card(employee_name, employee_id, team, pills, last_updated)
    payload = {
        "action": "create_or_update_card",
        "employee_id": employee_id,
        "message_id": existing_message_id,
        "card": card,
    }
    result = _send_teams_json(webhook, payload)
    if result is None:
        logger.warning(f"[WARN] Card upsert failed for {employee_id} - keeping old message_id")
        return existing_message_id
    new_id = result.get("message_id") or existing_message_id
    if not new_id:
        logger.warning(f"[WARN] Flow didn't return a message_id for {employee_id}'s new card")
    return new_id

def post_employee_reply(employee_id: str, text: str, webhook: str | None) -> bool:
    """Post a threaded reply under an employee's card — this is the actual
    'ping' people see; the card edit above is silent on its own.

    Keyed on employee_id, not a message_id from the server: the flow
    doesn't return a Response (that needs Power Automate Premium), so the
    server never actually learns the real message_id. The flow already
    looks up each employee's card id itself via its own Excel table, so
    employee_id is all it needs on the reply side too."""
    if not webhook or not employee_id:
        return False
    payload = {"action": "reply", "employee_id": employee_id, "text": _redact_ip(text)}
    return _send_teams_json(webhook, payload) is not None


def _send_desktop(title: str, body: str):
    """Send a native desktop notification."""
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{body}" with title "{title}"'
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=5)
        elif system == "Windows":
            ps = (
                f"[Windows.UI.Notifications.ToastNotificationManager,"
                f"Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
                f"$t=[Windows.UI.Notifications.ToastTemplateType]"
                f"::ToastText02; $x=[Windows.UI.Notifications"
                f".ToastNotificationManager]::GetTemplateContent($t);"
                f"$x.GetElementsByTagName('text')[0].AppendChild("
                f"$x.CreateTextNode('{title}')) | Out-Null;"
                f"$x.GetElementsByTagName('text')[1].AppendChild("
                f"$x.CreateTextNode('{body}')) | Out-Null;"
                f"$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
                f"[Windows.UI.Notifications.ToastNotificationManager]"
                f"::CreateToastNotifier('RTO Tracker').Show($n);"
            )
            subprocess.run(
                ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
                capture_output=True, timeout=10,
                creationflags=0x08000000,
            )
        else:
            subprocess.run(
                ["notify-send", title, body],
                capture_output=True, timeout=5,
            )
    except Exception as e:
        logger.debug(f"Desktop notification failed: {e}")


# ---------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------

def notify(
    event:        str,
    title:        str,
    body:         str,
    facts:        Optional[list]  = None,
    action_url:   Optional[str]   = None,
    action_label: str             = "Open RTO Tracker",
    webhook_url:  Optional[str]   = None,
    notify_level: str             = LEVEL_ALL,
    desktop:      bool            = True,
    teams:        bool            = True,
    async_send:   bool            = False,
):
    """
    Send a notification to desktop and/or Teams.

    Args:
        event:        Event type key (e.g. "checkin_wfo"). Controls colour/icon.
        title:        Short title line.
        body:         Main message text.
        facts:        Optional list of (key, value) tuples shown in a fact table.
        action_url:   Optional URL for a button on the Teams card.
        webhook_url:  Teams webhook URL. If None, Teams notification is skipped.
        notify_level: Minimum level filter ("all" | "important" | "errors").
        desktop:      Whether to send desktop notification.
        teams:        Whether to send Teams notification.
        async_send:   Send Teams notification in background thread (non-blocking).
    """
    # Level filter
    event_rank = _LEVEL_RANK.get(
        _EVENT_LEVELS.get(event, LEVEL_ALL), 0
    )
    min_rank = _LEVEL_RANK.get(notify_level, 0)
    if event_rank < min_rank:
        logger.info(f"Notification suppressed by level filter: {event}")
        return

    # Desktop
    if desktop:
        _send_desktop(f"RTO Tracker - {title}", body)

    # Teams
    if teams and webhook_url:
        card = _build_card(event, title, body, facts, action_url, action_label)
        if async_send:
            t = threading.Thread(
                target=_send_teams,
                args=(webhook_url, card),
                daemon=True,
                name="teams-notify",
            )
            t.start()
        else:
            _send_teams(webhook_url, card)


# ---------------------------------------------------------
# CONVENIENCE WRAPPERS
# ---------------------------------------------------------

def notify_checkin_wfo(employee_name: str, lan_ip: str,
                       confidence: str = "",
                       webhook: str = None, level: str = LEVEL_ALL,
                       server_url: str = None):
    notify(
        event="checkin_wfo",
        title="Checked in - In Office",
        body=f"{employee_name} is in the office today.",
        facts=[
            ("Status",     "In Office (WFO)"),
            ("LAN IP",     lan_ip or "unknown"),
            ("Confidence", confidence.capitalize() if confidence else "High"),
        ],
        action_url=server_url,
        webhook_url=webhook,
        notify_level=level,
    )


def notify_checkin_wfh(employee_name: str, lan_ip: str,
                       confidence: str = "",
                       vpn: bool = False,
                       webhook: str = None, level: str = LEVEL_ALL,
                       server_url: str = None):
    notify(
        event="checkin_wfh",
        title="Checked in - Working from Home",
        body=f"{employee_name} is working from home today.",
        facts=[
            ("Status", "Work from Home (WFH)"),
            ("VPN",    "Connected" if vpn else "Not connected"),
            ("LAN IP", lan_ip or "unknown"),
        ],
        action_url=server_url,
        webhook_url=webhook,
        notify_level=level,
    )


def notify_vpn_ambiguous(employee_name: str, lan_ip: str,
                         confirm_url: str,
                         webhook: str = None, level: str = LEVEL_ALL):
    notify(
        event="vpn_ambiguous",
        title="Location Confirmation Needed",
        body=(
            f"VPN is active but location could not be auto-detected "
            f"for {employee_name}. Please confirm whether you are in "
            f"the office or working remotely."
        ),
        facts=[
            ("VPN",    "Connected"),
            ("LAN IP", lan_ip or "unknown"),
            ("Action", "Click the button below to confirm"),
        ],
        action_url=confirm_url,
        action_label="Confirm My Location",
        webhook_url=webhook,
        notify_level=level,
    )


def notify_server_unreachable(server_url: str,
                              webhook: str = None, level: str = LEVEL_ALL):
    notify(
        event="server_unreachable",
        title="RTO Server Unreachable",
        body=(
            f"The RTO Tracker server at {server_url} could not be reached. "
            f"Your check-in has been queued locally and will sync automatically "
            f"once the VPN is connected."
        ),
        facts=[
            ("Server",  server_url),
            ("Action",  "Connect Sky VPN to sync"),
            ("Status",  "Check-in saved locally"),
        ],
        webhook_url=webhook,
        notify_level=level,
    )


def notify_missed_days(employee_name: str, dates: list,
                       missed_url: str,
                       webhook: str = None, level: str = LEVEL_ALL):
    n = len(dates)
    date_list = ", ".join(dates) if n <= 3 else f"{dates[0]} ... {dates[-1]}"
    notify(
        event="missed_day",
        title=f"Missing Attendance - {n} Day{'s' if n > 1 else ''}",
        body=(
            f"{employee_name} has {n} day{'s' if n > 1 else ''} with no "
            f"attendance record. Please fill in what happened."
        ),
        facts=[
            ("Missing dates", date_list),
            ("Action",        "Click below to fill in attendance"),
        ],
        action_url=missed_url,
        action_label="Fill In Attendance",
        webhook_url=webhook,
        notify_level=level,
    )


def notify_queue_flushed(employee_name: str, count: int,
                         webhook: str = None, level: str = LEVEL_ALL,
                         server_url: str = None):
    notify(
        event="queue_flushed",
        title=f"{count} Queued Check-in{'s' if count > 1 else ''} Synced",
        body=(
            f"{count} offline check-in{'s' if count > 1 else ''} for "
            f"{employee_name} have been synced to the server."
        ),
        facts=[("Records synced", str(count))],
        action_url=server_url,
        webhook_url=webhook,
        notify_level=level,
    )


def notify_queue_saved(employee_name: str, status: str,
                       webhook: str = None, level: str = LEVEL_ALL):
    notify(
        event="queue_saved",
        title="Check-in Saved Offline",
        body=(
            f"Server is unreachable. {employee_name}'s {status.upper()} "
            f"check-in has been saved locally and will sync when VPN connects."
        ),
        facts=[
            ("Status", status.upper()),
            ("Queued", "Yes - will auto-sync"),
        ],
        webhook_url=webhook,
        notify_level=level,
    )


def notify_registration_needed(hostname: str, register_url: str,
                                webhook: str = None, level: str = LEVEL_ALL):
    notify(
        event="registration_needed",
        title="Device Registration Required",
        body=(
            f"Device {hostname} is not yet registered with RTO Tracker. "
            f"Please register to start tracking attendance."
        ),
        facts=[
            ("Hostname", hostname),
            ("Action",   "Click below to register"),
        ],
        action_url=register_url,
        webhook_url=webhook,
        notify_level=level,
    )


def notify_registration_complete(employee_name: str, hostname: str, team: str,
                                  server_url: str,
                                  webhook: str = None, level: str = LEVEL_ALL):
    notify(
        event="registration_complete",
        title="[REGISTERED] Device Registered",
        body=(
            f"Welcome, {employee_name}! Your device has been registered for "
            f"RTO tracking. Check-ins will happen automatically hereafter!"
        ),
        facts=[
            ("Name",     employee_name),
            ("Hostname", hostname),
            ("Team",     team or "-"),
        ],
        action_url=server_url,
        webhook_url=webhook,
        notify_level=level,
    )


def notify_override_applied(target_name: str, target_id: str,
                             date: str, old_status: str, new_status: str,
                             override_by: str, note: str,
                             webhook: str = None, level: str = LEVEL_ALL,
                             server_url: str = None):
    """Server-side: called when a manager applies an override."""
    notify(
        event="override_applied",
        title="Attendance Override Applied",
        body=(
            f"Manager {override_by} has updated the attendance record "
            f"for {target_name} on {date}."
        ),
        facts=[
            ("Employee",   f"{target_name} ({target_id})"),
            ("Date",       date),
            ("Changed",    f"{old_status.upper()} to {new_status.upper()}"),
            ("Reason",     note or "No reason provided"),
            ("Applied by", override_by),
        ],
        action_url=server_url,
        webhook_url=webhook,
        notify_level=level,
    )


def notify_wfo_on_leave(employee_name: str, employee_id: str,
                        date: str, leave_type: str,
                        lan_ip: str = None,
                        webhook: str = None, level: str = LEVEL_ALL,
                        server_url: str = None):
    """Alert: employee has WFO office signals while marked as on leave/holiday."""
    notify(
        event="wfo_on_leave",
        title="Office Detected on Leave Day",
        body=(
            f"{employee_name} ({employee_id}) has office network signals on {date} "
            f"but is recorded as {leave_type}. Please review."
        ),
        facts=[
            ("Employee",   f"{employee_name} ({employee_id})"),
            ("Date",       date),
            ("Recorded as", leave_type),
            ("LAN IP",     lan_ip or "unknown"),
            ("Action",     "Review and update attendance if needed"),
        ],
        action_url=server_url,
        action_label="Review Attendance",
        webhook_url=webhook,
        notify_level=level,
    )


def notify_leave_applied(employee_name: str, leave_type: str,
                         dates: list, applied_by: str,
                         note: str = None,
                         webhook: str = None, level: str = LEVEL_ALL,
                         server_url: str = None):
    """Server-side: called when leave is applied for an employee."""
    n = len(dates)
    date_str = (dates[0] if n == 1
                else f"{dates[0]} to {dates[-1]} ({n} days)")
    notify(
        event="leave_applied",
        title="Leave Recorded",
        body=(
            f"{leave_type.replace('_', ' ').title()} has been recorded "
            f"for {employee_name}."
        ),
        facts=[
            ("Employee",   employee_name),
            ("Leave type", leave_type.replace("_", " ").title()),
            ("Date(s)",    date_str),
            ("Applied by", applied_by),
            *([("Note", note)] if note else []),
        ],
        action_url=server_url,
        webhook_url=webhook,
        notify_level=level,
    )