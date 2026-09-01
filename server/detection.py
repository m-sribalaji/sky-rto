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

    1. Office LAN subnet (10.126.0.0/16)     -> WFO (flagged, see note)
    2. Office DNS server or domain            -> WFO (flagged, see note)
       (unless home LAN + VPN -> WFH, VPN just routes DNS)
    3. VPN tunnel active:
       a. + office LAN or DNS               -> WFO (flagged, see note)
       b. + home LAN (192.168/172.16)       -> WFH confirmed
       c. + private_unknown 10.x.x.x        -> WFH (benefit of doubt)
       d. no other signals                  -> vpn_ambiguous (prompt user)
    4. Ethernet active + not home LAN        -> WFO (flagged, see note)
    5. Home LAN, no VPN                      -> WFH definitive
    6. Private unknown (10.x home ISP)       -> WFH medium (benefit of doubt)
    7. No signals at all                     -> WFH medium (safer default)

    NOTE on WFO confidence (2026-09 security review): every WFO path here
    is derived entirely from values the client itself reports (lan_ip,
    dns_servers, is_ethernet) - none of them are independently verifiable
    by the server. A security review found that on this deployment,
    verify_client_signals' cross-check against the connecting public_ip
    (conn_is_sky) has no real discriminating power, because Sky's VPN
    egresses through the same address space as genuine office traffic -
    meaning a fabricated lan_ip claim from home is accepted identically to
    a real one. Until there's an independently-verifiable signal (e.g. a
    network-level distinction from IT, still pending), every WFO
    determination from these client-reported signals is capped at MEDIUM
    confidence and flagged for manager visibility, rather than treated as
    high-confidence/definitive. This doesn't stop a false claim - nothing
    purely server-side can - but it stops it from being silent.
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
            confidence  = CONF_MED,
            vpn_active  = vpn_active,
            flagged     = True,
            flag_reason = (
                f"WFO based on self-reported office LAN {lan_ip} - not "
                f"independently verifiable on this network. Needs review."
            ),
            detail      = f"Office LAN {lan_ip} claimed - unverified WFO.",
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
            confidence  = CONF_MED,
            vpn_active  = vpn_active,
            flagged     = True,
            flag_reason = (
                f"WFO based on self-reported office DNS "
                f"(servers={dns_servers}, domains={dns_domains}) - not "
                f"independently verifiable on this network. Needs review."
            ),
            detail      = (
                f"Office DNS claimed "
                f"(servers={dns_servers}, domains={dns_domains}) - unverified WFO."
            ),
        )

    # -- 3. VPN tunnel active ------------------------------
    if vpn_active:
        # 3a. VPN + office signals -> in office
        if lan_is_office or dns_office:
            return DetectionResult(
                auto_status = "wfo",
                confidence  = CONF_MED,
                vpn_active  = True,
                flagged     = True,
                flag_reason = (
                    "WFO based on self-reported office LAN/DNS while on VPN - "
                    "not independently verifiable on this network. Needs review."
                ),
                detail      = "VPN on + self-reported office signals - unverified WFO.",
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
            flagged     = True,
            flag_reason = (
                "WFO based on self-reported ethernet/non-home LAN - not "
                "independently verifiable on this network. Needs review."
            ),
            detail      = "Ethernet/dock + not on home subnet - unverified WFO.",
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


def verify_client_signals(
    public_ip:     str,
    lan_ip:        str | None,
    vpn_tunnel_ip: str | None,
    is_ethernet:   bool,
    dns_servers:   list | None,
    dns_domains:   list | None,
) -> DetectionResult:
    """
    The one place that turns "here's what the client claims about its
    network" into "here's what we actually trust". Used by both the live
    /api/checkin path and the /api/missed backfill path (when someone
    submits cached signals from a day they were offline) — anywhere a
    client hands us lan_ip/dns/vpn info, it should go through here rather
    than being taken at face value.

    The check that matters: a real office machine's requests — whether
    they're actually on the office LAN, or connected in over Sky's VPN —
    arrive from Sky's own network (public_ip starts with 10.x, or is
    localhost during dev; that's the same address space a genuine VPN
    tunnel egresses through). So ANY claim that implies "I'm at the office"
    — an office LAN IP, office DNS servers, or a VPN tunnel address —
    needs that same real-network origin to back it up. This used to only
    check the LAN IP claim, which meant dns_servers and vpn_tunnel_ip were
    trusted purely on the client's word — someone at home could claim
    dns_servers=["10.126.63.5"] (no LAN IP needed at all) and get a fully
    "verified" WFO record with zero real corroboration. All three claims
    go through the same check now.
    """
    claimed_lan     = lan_ip or ""
    lan_is_office    = claimed_lan.startswith("10.126.") or claimed_lan.startswith("10.128.")
    dns_claims_office = dns_is_office(dns_servers or [], dns_domains or [])
    vpn_claimed       = bool(vpn_tunnel_ip)
    conn_is_sky      = public_ip.startswith("10.") or public_ip in ("127.0.0.1", "::1")

    fabricated_sig = (lan_is_office or dns_claims_office or vpn_claimed) and not conn_is_sky

    if fabricated_sig:
        claims = []
        if lan_is_office:      claims.append(f"office LAN {claimed_lan}")
        if dns_claims_office:  claims.append(f"office DNS {dns_servers}")
        if vpn_claimed:        claims.append(f"VPN tunnel {vpn_tunnel_ip}")
        claim_desc = " + ".join(claims)
        return DetectionResult(
            auto_status="wfh", confidence="high",
            vpn_active=False, flagged=True,
            flag_reason=f"Signal fabrication: claimed {claim_desc} "
                        f"but connected from {public_ip}. Recorded as WFH.",
            detail=f"Fabricated office signals detected from {public_ip}.",
        )
    return classify(public_ip=public_ip, lan_ip=lan_ip,
                     vpn_tunnel_ip=vpn_tunnel_ip, ssid=None,
                     is_ethernet=is_ethernet,
                     dns_servers=dns_servers, dns_domains=dns_domains)


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