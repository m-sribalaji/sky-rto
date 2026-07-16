# config.py - RTO Tracker Network Configuration
# Single source of truth for all IP/DNS classification rules.
# All ranges use proper CIDR notation - no string prefix matching.
import ipaddress

# -- OFFICE NETWORK ----------------------------------------
# Sky office LAN - definitive WFO signal
OFFICE_LAN_CIDRS    = [
    "10.126.0.0/16",       # Main Sky office LAN (confirmed)
]

# Sky office public egress IPs (seen from outside)
OFFICE_PUBLIC_CIDRS = [
    "144.125.6.0/24",
]

# -- OFFICE DNS --------------------------------------------
# DNS server IPs that appear when physically in Sky office
# or when VPN routes DNS through Sky infrastructure
OFFICE_DNS_CIDRS    = [
    "10.126.63.0/24",      # Primary Sky office DNS (confirmed via scutil)
    "10.5.0.0/16",         # Sky VPN DNS range
    "10.20.0.0/16",        # Sky VPN DNS range (secondary)
]

# DNS search domains that appear on Sky network
OFFICE_DNS_DOMAINS  = [
    "bskyb.com",
    "sssl.bskyb.com",
]

# -- VPN RANGES --------------------------------------------
# Tunnel IPs assigned by Sky Cisco AnyConnect
VPN_TUNNEL_CIDRS    = [
    "10.109.0.0/16",       # Primary Sky VPN tunnel range (confirmed)
    "10.23.0.0/16",        # Secondary VPN range
    "10.8.0.0/16",         # OpenVPN-style (fallback)
]

# -- HOME / PRIVATE LAN ------------------------------------
# RFC-1918 private ranges that indicate home/remote network.
# NOTE: 10.0.0.0/8 is intentionally NOT listed here because
# Sky office also uses 10.x.x.x - we rely on OFFICE_LAN_CIDRS
# for positive office detection first, then treat remaining
# 10.x.x.x as ambiguous (not definitively home).
HOME_LAN_CIDRS      = [
    "192.168.0.0/16",      # Most home routers (BT, Virgin, Sky broadband)
    "172.16.0.0/12",       # Docker / corporate VMs / some ISPs
]

# ISP ranges that are definitely home (not office)
# Add your home ISP range here if needed
ISP_HOME_CIDRS      = [
    # "100.64.0.0/10",     # CGNAT - some ISPs (uncomment if needed)
]

# -- APP ---------------------------------------------------
APP_TITLE    = "RTO Tracker"
APP_ORG      = "Sky"
PORT         = 9999
import os
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "sqlite+aiosqlite:////app/data/rto.db"
)

# -- PARSED NETWORKS ---------------------------------------
def _parse(cidrs):
    nets = []
    for c in cidrs:
        try:
            nets.append(ipaddress.ip_network(c, strict=False))
        except ValueError as e:
            import logging
            logging.getLogger("config").warning(f"Invalid CIDR {c!r}: {e}")
    return nets

OFFICE_LAN_NETS    = _parse(OFFICE_LAN_CIDRS)
OFFICE_PUBLIC_NETS = _parse(OFFICE_PUBLIC_CIDRS)
OFFICE_DNS_NETS    = _parse(OFFICE_DNS_CIDRS)
VPN_TUNNEL_NETS    = _parse(VPN_TUNNEL_CIDRS)
HOME_LAN_NETS      = _parse(HOME_LAN_CIDRS + ISP_HOME_CIDRS)

# All private/VPN ranges combined - used to exclude from "unknown" fallback
ALL_PRIVATE_NETS   = _parse([
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"
])

# -- HELPERS -----------------------------------------------
def ip_in_any(ip_str: str, networks: list) -> bool:
    """Check if an IP string falls within any of the given networks."""
    if not ip_str:
        return False
    try:
        addr = ipaddress.ip_address(ip_str.strip().split("%")[0])  # strip IPv6 zone id
        return any(addr in net for net in networks)
    except ValueError:
        return False

def is_vpn_tunnel_ip(ip_str: str) -> bool:
    """Check if an IP looks like a VPN tunnel address."""
    return ip_in_any(ip_str, VPN_TUNNEL_NETS)

def dns_is_office(dns_servers: list, dns_domains: list) -> bool:
    """Return True if DNS servers or search domains indicate Sky office network."""
    for dns in (dns_servers or []):
        if ip_in_any(dns, OFFICE_DNS_NETS):
            return True
    for domain in (dns_domains or []):
        if any(domain.lower().endswith(d) for d in OFFICE_DNS_DOMAINS):
            return True
    return False

def classify_lan(lan_ip: str) -> str:
    """
    Classify a LAN IP into a location category.
    Returns: 'office' | 'home' | 'vpn_tunnel' | 'private_unknown' | 'unknown'
    """
    if not lan_ip:
        return "unknown"
    if ip_in_any(lan_ip, OFFICE_LAN_NETS):
        return "office"
    if is_vpn_tunnel_ip(lan_ip):
        return "vpn_tunnel"
    if ip_in_any(lan_ip, HOME_LAN_NETS):
        return "home"
    if ip_in_any(lan_ip, ALL_PRIVATE_NETS):
        # Private IP but not in a known range - could be home ISP using 10.x
        return "private_unknown"
    return "unknown"