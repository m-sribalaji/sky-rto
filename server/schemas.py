"""
All the request-body shapes the API accepts, in one place. Grouping these
separately from the route handlers makes it easy to see the whole "API
contract" at a glance without wading through business logic.
"""
import re
from datetime import date as _date, timedelta as _timedelta
from pydantic import BaseModel, field_validator
from typing import Optional

# Sky's NTID format: exactly 3 letters then 3 digits, e.g. "abc123".
# Checked only at registration — that's the one place a person types their
# own ID in by hand, so it's the one place a typo can actually happen.
_NTID_RE = re.compile(r"^[A-Za-z]{3}\d{3}$")


# Every date string that reaches the database eventually gets parsed with
# date.fromisoformat() somewhere in analytics.py (dow rates, compliance
# forecasts, exports...) with no try/except around it. Before this existed,
# nothing stopped a bad or made-up date string ("2026-08-32", "hello", a
# date from the future) from being saved straight into a check-in or leave
# record — which meant it would silently sit there until some report tried
# to read it and crashed with an unhandled ValueError. This checks the
# string really is a calendar date up front, at the door, so garbage never
# gets in in the first place.
def _parse_calendar_date(v: str) -> str:
    try:
        _date.fromisoformat(v)
    except (ValueError, TypeError):
        raise ValueError(f"'{v}' is not a real calendar date (expected YYYY-MM-DD)")
    return v

# How far back someone can self-report a "missed" day for. The agent itself
# only ever looks 7 days back for gaps, but people go on leave, laptops die,
# etc. — 30 days is generous room for a genuine late catch-up without
# leaving the door open to backdating attendance from months or years ago.
MISSED_DAY_MAX_LOOKBACK_DAYS = 30

def _validate_missed_day_date(v: str) -> str:
    v = _parse_calendar_date(v)
    d = _date.fromisoformat(v)
    today = _date.today()
    if d > today:
        raise ValueError(f"'{v}' is in the future — can't record attendance for a day that hasn't happened yet")
    if d < today - _timedelta(days=MISSED_DAY_MAX_LOOKBACK_DAYS):
        raise ValueError(
            f"'{v}' is more than {MISSED_DAY_MAX_LOOKBACK_DAYS} days ago — "
            f"contact your manager/admin to backdate attendance that old"
        )
    return v

# The live /api/checkin endpoint also accepts an optional `date` field — it
# exists so the offline queue can replay a check-in under the date it was
# originally captured, if the agent couldn't reach the server that day.
# Without a bound here, that same field is a bigger hole than the missed-day
# one: someone could capture one genuine set of office signals, then edit
# pending_queue.json (or just call the API directly) and resubmit that same
# real, verifiable signal set under a different `date` each time — and
# because the signals are real, it sails through signal verification at
# confidence="high" with none of the "unverified" flagging /api/missed
# applies. Bounding it here closes that off while still letting a laptop
# that was genuinely offline for a week or two catch up normally.
CHECKIN_MAX_BACKDATE_DAYS = 14

def _validate_checkin_date(v: str) -> str:
    v = _parse_calendar_date(v)
    d = _date.fromisoformat(v)
    today = _date.today()
    if d > today:
        raise ValueError(f"'{v}' is in the future — can't check in for a day that hasn't happened yet")
    if d < today - _timedelta(days=CHECKIN_MAX_BACKDATE_DAYS):
        raise ValueError(
            f"'{v}' is more than {CHECKIN_MAX_BACKDATE_DAYS} days ago — "
            f"use the missed-day form to backfill attendance that old"
        )
    return v


class RegisterPayload(BaseModel):
    hostname: str; employee_name: str; employee_id: str
    team: Optional[str]=None; platform: Optional[str]=None
    nonce: Optional[str]=None

    @field_validator("employee_id")
    @classmethod
    def _check_ntid(cls, v):
        if not _NTID_RE.match(v or ""):
            raise ValueError("Please enter proper employee NTID (ex abc123)")
        return v

class CheckInPayload(BaseModel):
    hostname: str; lan_ip: Optional[str]=None; vpn_tunnel_ip: Optional[str]=None
    ssid: Optional[str]=None; is_ethernet: bool=False
    dns_servers: Optional[list]=None; dns_domains: Optional[list]=None
    platform: Optional[str]=None; date: Optional[str]=None
    force_update: bool=False; source: Optional[str]="auto_detected"
    queued_at: Optional[str]=None  # ISO timestamp from offline queue — used as started_at

    @field_validator("date")
    @classmethod
    def _check_date(cls, v):
        return v if v is None else _validate_checkin_date(v)

class ConfirmPayload(BaseModel):
    hostname: str; declared_status: str

class OverridePayload(BaseModel):
    employee_id: str; date: str; new_status: str
    override_by: str; note: Optional[str]=None

    @field_validator("date")
    @classmethod
    def _check_date(cls, v):
        return _parse_calendar_date(v)

class LeavePayload(BaseModel):
    employee_id: str; date: str; leave_type: str
    half_day_period: Optional[str]=None; note: Optional[str]=None
    applied_by: Optional[str]=None; source: Optional[str]="self"

    @field_validator("date")
    @classmethod
    def _check_date(cls, v):
        return _parse_calendar_date(v)

class DeleteLeavePayload(BaseModel):
    employee_id: str; date: str

    @field_validator("date")
    @classmethod
    def _check_date(cls, v):
        return _parse_calendar_date(v)

class PublicHolidayPayload(BaseModel):
    date: str; name: str; country: str="GB"
    region: Optional[str]=None; optional: bool=False

    @field_validator("date")
    @classmethod
    def _check_date(cls, v):
        return _parse_calendar_date(v)

class MissedDayPayload(BaseModel):
    hostname: str; date: str; status: str
    leave_type: Optional[str]=None; source: str="missed_prompt_no_data"
    lan_ip: Optional[str]=None; dns_servers: Optional[list]=None
    dns_domains: Optional[list]=None; vpn_tunnel_ip: Optional[str]=None
    is_ethernet: bool=False; has_cached_data: bool=False

    # This is the self-service, no-real-oversight path (any employee, their
    # own token, no manager involved) — so on top of just being a real
    # date, it also can't be in the future or absurdly far in the past.
    @field_validator("date")
    @classmethod
    def _check_date(cls, v):
        return _validate_missed_day_date(v)

class RolePayload(BaseModel):
    employee_id: str; role: str; assigned_by: Optional[str]=None

class TeamPayload(BaseModel):
    name: str
    created_by: Optional[str] = None
