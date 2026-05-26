# detection.py - Multi-signal classification engine
# Uses config.py as single source of truth for all network ranges.
from dataclasses import dataclass
from config import (
    ip_in_any, dns_is_office, classify_lan,
    OFFICE_LAN_NETS, HOME_LAN_NETS, VPN_TUNNEL_NETS,
)

CONF_HIGH = "high"
CONF_MED  = "medium"
CONF_LOW  = "low"

@dataclass
class DetectionResult:
    auto_status: str        # wfo | wfh | vpn_ambiguous
    confidence:  str        # high | medium | low
    vpn_active:  bool
    flagged:     bool
    flag_reason: str | None
    detail:      str

def classify(
    public_ip:     str | None,
    lan_ip:        str | None,
    vpn_tunnel_ip: str | None,
    ssid:          str | None,      # kept for backwards compat, not used
    is_ethernet:   bool,
    dns_servers:   list | None = None,
    dns_domains:   list | None = None,
) -> DetectionResult:
    """
    Multi-signal classification - priority order:

    1. Office LAN subnet (10.126.0.0/16)     -> WFO definitive
    2. Office DNS server or domain            -> WFO definitive
       (unless home LAN + VPN -> WFH, VPN just routes DNS)
    3. VPN tunnel active:
       a. + office LAN or DNS               -> WFO (in office on VPN)
       b. + home LAN (192.168/172.16)       -> WFH confirmed
       c. + private_unknown 10.x.x.x        -> WFH (benefit of doubt)
       d. no other signals                  -> vpn_ambiguous (prompt user)
    4. Ethernet active + not home LAN        -> WFO medium confidence
    5. Home LAN, no VPN                      -> WFH definitive
    6. Private unknown (10.x home ISP)       -> WFH medium (benefit of doubt)
    7. No signals at all                     -> WFH medium (safer default)
    """
    vpn_active = bool(vpn_tunnel_ip)
    lan_class  = classify_lan(lan_ip)
    dns_office = dns_is_office(dns_servers or [], dns_domains or [])

    lan_is_office          = (lan_class == "office")
    lan_is_home            = (lan_class == "home")
    lan_is_private_unknown = (lan_class == "private_unknown")

    # -- 1. Office LAN -------------------------------------
    if lan_is_office:
        return DetectionResult(
            auto_status = "wfo",
            confidence  = CONF_HIGH,
            vpn_active  = vpn_active,
            flagged     = False,
            flag_reason = None,
            detail      = f"Office LAN {lan_ip} - definitive WFO.",
        )

    # -- 2. Office DNS -------------------------------------
    if dns_office:
        # Home LAN + office DNS = physical location is home, DNS is VPN-routed.
        # No vpn_active check — home LAN is sufficient proof regardless of
        # whether the VPN tunnel IP was detected (Windows adapter names vary).
        if lan_is_home or lan_is_private_unknown:
            return DetectionResult(
                auto_status = "wfh",
                confidence  = CONF_MED,
                vpn_active  = vpn_active,
                flagged     = False,
                flag_reason = None,
                detail      = (
                    f"Home/remote LAN {lan_ip} + office DNS - "
                    f"DNS is VPN-routed, physical location is home. WFH."
                ),
            )
        return DetectionResult(
            auto_status = "wfo",
            confidence  = CONF_HIGH,
            vpn_active  = vpn_active,
            flagged     = False,
            flag_reason = None,
            detail      = (
                f"Office DNS detected "
                f"(servers={dns_servers}, domains={dns_domains}) - WFO."
            ),
        )

    # -- 3. VPN tunnel active ------------------------------
    if vpn_active:
        # 3a. VPN + office signals -> in office
        if lan_is_office or dns_office:
            return DetectionResult(
                auto_status = "wfo",
                confidence  = CONF_HIGH,
                vpn_active  = True,
                flagged     = False,
                flag_reason = None,
                detail      = "VPN on + office signals - in office.",
            )
        # 3b. VPN + known home LAN -> WFH
        if lan_is_home:
            return DetectionResult(
                auto_status = "wfh",
                confidence  = CONF_MED,
                vpn_active  = True,
                flagged     = False,
                flag_reason = None,
                detail      = f"VPN on + home LAN {lan_ip} - WFH.",
            )
        # 3c. VPN + private unknown (10.x home ISP) -> WFH benefit of doubt
        if lan_is_private_unknown:
            return DetectionResult(
                auto_status = "wfh",
                confidence  = CONF_MED,
                vpn_active  = True,
                flagged     = False,
                flag_reason = None,
                detail      = (
                    f"VPN on + private LAN {lan_ip} (non-office 10.x range) - "
                    f"likely home ISP using 10.x addressing. WFH."
                ),
            )
        # 3d. VPN + no other signals -> prompt user
        return DetectionResult(
            auto_status = "vpn_ambiguous",
            confidence  = CONF_LOW,
            vpn_active  = True,
            flagged     = False,
            flag_reason = None,
            detail      = "VPN active, no location signals - user confirmation needed.",
        )

    # -- 4. Ethernet + not home ----------------------------
    if is_ethernet and not lan_is_home and not lan_is_private_unknown:
        return DetectionResult(
            auto_status = "wfo",
            confidence  = CONF_MED,
            vpn_active  = False,
            flagged     = False,
            flag_reason = None,
            detail      = "Ethernet/dock + not on home subnet - likely in office.",
        )

    # -- 5. Home LAN, no VPN -------------------------------
    if lan_is_home:
        return DetectionResult(
            auto_status = "wfh",
            confidence  = CONF_HIGH,
            vpn_active  = False,
            flagged     = False,
            flag_reason = None,
            detail      = f"Home LAN {lan_ip}, no VPN - WFH.",
        )

    # -- 6. Private unknown (home ISP on 10.x), no VPN ----
    if lan_is_private_unknown:
        return DetectionResult(
            auto_status = "wfh",
            confidence  = CONF_MED,
            vpn_active  = False,
            flagged     = False,
            flag_reason = None,
            detail      = (
                f"Private LAN {lan_ip} not matching known office range - "
                f"likely home ISP. WFH."
            ),
        )

    # -- 7. No signals at all - safe default ---------------
    # Default to WFH rather than WFO - safer for compliance
    # (better to under-count WFO than over-count)
    return DetectionResult(
        auto_status = "wfh",
        confidence  = CONF_LOW,
        vpn_active  = False,
        flagged     = True,
        flag_reason = "No network signals detected - defaulted to WFH. Review manually.",
        detail      = "No network signals - defaulted to WFH (low confidence).",
    )


def score_declaration(declared: str, result: DetectionResult,
                       lan_ip: str | None) -> tuple:
    """Score a user's VPN self-declaration against detected signals."""
    lan_class = classify_lan(lan_ip or "")
    if declared == "wfo" and lan_class == "home":
        return (
            True,
            f"Declared WFO but LAN {lan_ip} is a home subnet - flagged for review.",
            CONF_LOW,
        )
    if declared == "wfo" and lan_class == "private_unknown":
        return (
            True,
            f"Declared WFO but LAN {lan_ip} is an unrecognised private range - flagged.",
            CONF_LOW,
        )
    if declared == "wfo":
        return (False, None, CONF_MED)
    return (False, None, CONF_MED)