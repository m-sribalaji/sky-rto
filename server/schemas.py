"""
All the request-body shapes the API accepts, in one place. Grouping these
separately from the route handlers makes it easy to see the whole "API
contract" at a glance without wading through business logic.
"""
from pydantic import BaseModel
from typing import Optional


class RegisterPayload(BaseModel):
    hostname: str; employee_name: str; employee_id: str
    team: Optional[str]=None; platform: Optional[str]=None
    nonce: Optional[str]=None

class CheckInPayload(BaseModel):
    hostname: str; lan_ip: Optional[str]=None; vpn_tunnel_ip: Optional[str]=None
    ssid: Optional[str]=None; is_ethernet: bool=False
    dns_servers: Optional[list]=None; dns_domains: Optional[list]=None
    platform: Optional[str]=None; date: Optional[str]=None
    force_update: bool=False; source: Optional[str]="auto_detected"
    queued_at: Optional[str]=None  # ISO timestamp from offline queue — used as started_at

class ConfirmPayload(BaseModel):
    hostname: str; declared_status: str

class OverridePayload(BaseModel):
    employee_id: str; date: str; new_status: str
    override_by: str; note: Optional[str]=None

class LeavePayload(BaseModel):
    employee_id: str; date: str; leave_type: str
    half_day_period: Optional[str]=None; note: Optional[str]=None
    applied_by: Optional[str]=None; source: Optional[str]="self"

class DeleteLeavePayload(BaseModel):
    employee_id: str; date: str

class PublicHolidayPayload(BaseModel):
    date: str; name: str; country: str="GB"
    region: Optional[str]=None; optional: bool=False

class MissedDayPayload(BaseModel):
    hostname: str; date: str; status: str
    leave_type: Optional[str]=None; source: str="missed_prompt_no_data"
    lan_ip: Optional[str]=None; dns_servers: Optional[list]=None
    dns_domains: Optional[list]=None; vpn_tunnel_ip: Optional[str]=None
    is_ethernet: bool=False; has_cached_data: bool=False

class RolePayload(BaseModel):
    employee_id: str; role: str; assigned_by: Optional[str]=None

class TeamPayload(BaseModel):
    name: str
    created_by: Optional[str] = None
