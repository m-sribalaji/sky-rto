"""
classify.py - turns the raw network signals from network.py into a
wfo/wfh/vpn_ambiguous/unknown verdict, plus the day-to-day "did anything
change since the last check-in" logic. Pure decision logic, no subprocess
calls, no I/O beyond one logger line — easy to reason about (and test) on
its own away from all the ipconfig-parsing noise.
"""

from datetime import date

from .config import logger
from .network import _ip_in, _OFFICE_LAN_NETS, _OFFICE_DNS_NETS, _OFFICE_DNS_DOMS, _VPN_TUNNEL_NETS, _HOME_LAN_NETS, _ALL_PRIVATE


def _classify_lan(lan_ip):
    if not lan_ip: return "unknown"
    if _ip_in(lan_ip, _OFFICE_LAN_NETS):  return "office"
    if _ip_in(lan_ip, _VPN_TUNNEL_NETS):  return "vpn_tunnel"
    if _ip_in(lan_ip, _HOME_LAN_NETS):    return "home"
    if _ip_in(lan_ip, _ALL_PRIVATE):      return "private_unknown"
    return "unknown"

def _dns_is_office(dns_servers, dns_domains):
    for s in (dns_servers or []):
        if _ip_in(s, _OFFICE_DNS_NETS): return True
    for d in (dns_domains or []):
        if any(d.lower().endswith(od) for od in _OFFICE_DNS_DOMS): return True
    return False

def classify_locally(lan_ip, vpn_tunnel_ip, dns_servers, dns_domains, is_ethernet):
    vpn_active             = bool(vpn_tunnel_ip)
    lan_class              = _classify_lan(lan_ip)
    dns_office             = _dns_is_office(dns_servers, dns_domains)
    lan_is_office          = (lan_class == "office")
    lan_is_home            = (lan_class == "home")
    lan_is_private_unknown = (lan_class == "private_unknown")

    if lan_is_office: return "wfo"
    if dns_office:
        if lan_is_home or lan_is_private_unknown: return "wfh"
        return "wfo"
    if vpn_active:
        if lan_is_office or dns_office:  return "wfo"
        if lan_is_home:                  return "wfh"
        if lan_is_private_unknown:       return "wfh"
        return "vpn_ambiguous"
    if is_ethernet and not lan_is_home and not lan_is_private_unknown:
        return "wfo"
    if lan_is_home:            return "wfh"
    if lan_is_private_unknown: return "wfh"
    return "unknown"

# ── LOCATION CHANGE DETECTION ────────────────────────────────────────────────
def location_changed(cfg: dict, current_class: str) -> bool:
    last_status = cfg.get("last_status")
    today       = date.today().isoformat()

    if cfg.get("last_checkin_date") != today: return True
    if not last_status: return True
    if current_class == "vpn_ambiguous": return False
    if last_status == "wfh" and current_class == "wfo":
        logger.info("Location change: WFH -> WFO detected"); return True
    if last_status == "wfo" and current_class == "wfh":
        logger.info("Location change: WFO -> WFH detected"); return True
    if current_class == "unknown" and last_status in ("wfo", "wfh"):
        return False
    return False
