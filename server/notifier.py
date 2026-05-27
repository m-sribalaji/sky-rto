"""
notifier.py - RTO Tracker Teams + Desktop notification module

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
            "text": body,
            "wrap": True,
            "spacing": "Small",
        },
    ]

    # Optional fact table (key-value pairs)
    if facts:
        body_blocks.append({
            "type": "FactSet",
            "spacing": "Small",
            "facts": [{"title": k, "value": v} for k, v in facts],
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
            if status == 200:
                logger.info("[OK] Teams notification sent")
                return True
            body_txt = ""
            try:
                body_txt = resp.read().decode("utf-8", errors="ignore")
            except Exception as e:
                body_txt = f"response body unavailable: {e}"
            logger.warning(f"[WARN] Teams webhook returned HTTP {status}: {body_txt}")
            return False
    except urllib.error.URLError as e:
        logger.warning(f"[WARN] Teams webhook unreachable: {e}")
        return False
    except Exception as e:
        logger.error(f"[FAIL] Teams notification failed: {e}")
        return False


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
