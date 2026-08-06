"""
network.py - all the OS-poking to figure out what network we're on: LAN IP,
VPN tunnel IP, DNS servers/search domains, ethernet-vs-wifi. Every function
here forks into a macOS path and a Windows path because there's no portable
way to get this stuff — it's all shelling out to ipconfig/ifconfig/scutil/netsh.
Kept separate from the classifier so the "how do we detect signals" code
doesn't get tangled up with the "what do these signals mean" code.
"""

import socket
import subprocess
import ipaddress as _ipaddress

from .config import logger, IS_MAC, IS_WIN, _NO_WIN


def _nets(cidrs):
    nets = []
    for c in cidrs:
        try: nets.append(_ipaddress.ip_network(c, strict=False))
        except ValueError: pass
    return nets

def _ip_in(ip, nets):
    if not ip: return False
    try:
        addr = _ipaddress.ip_address(ip.strip().split("%")[0])
        return any(addr in n for n in nets)
    except ValueError: return False

_OFFICE_LAN_NETS = _nets(["10.126.0.0/16"])
_OFFICE_DNS_NETS = _nets(["10.126.63.0/24", "10.5.0.0/16", "10.20.0.0/16"])
_OFFICE_DNS_DOMS = ("bskyb.com", "sssl.bskyb.com")
_VPN_TUNNEL_NETS = _nets(["10.109.0.0/16", "10.23.0.0/16", "10.8.0.0/16"])
_HOME_LAN_NETS   = _nets(["192.168.0.0/16", "172.16.0.0/12"])
_ALL_PRIVATE     = _nets(["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"])

def get_lan_ip():
    if IS_WIN:
        try:
            flags = _NO_WIN
            out = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5, creationflags=flags
            ).stdout
            for line in out.splitlines():
                s = line.strip()
                if "IPv4 Address" in s and ":" in s:
                    ip = s.split(":")[-1].strip().rstrip("(Preferred)").strip()
                    if ip and not _ip_in(ip, _VPN_TUNNEL_NETS):
                        return ip
        except Exception:
            pass
    else:
        try:
            for iface in ["en0", "en1", "en2", "en3"]:
                r = subprocess.run(["ipconfig", "getifaddr", iface],
                    capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    ip = r.stdout.strip()
                    if not _ip_in(ip, _VPN_TUNNEL_NETS):
                        return ip
        except Exception:
            pass
    # UDP route probe fallback
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not _ip_in(ip, _VPN_TUNNEL_NETS):
            return ip
    except Exception:
        pass
    return None

def get_vpn_tunnel_ip():
    if IS_MAC: return _vpn_macos()
    if IS_WIN: return _vpn_windows()
    return None

def _vpn_macos():
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        current, ifaces = None, {}
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("utun") and "flags" in s:
                current = s.split(":")[0]; ifaces[current] = None
            if current and current in ifaces:
                if s.startswith("inet ") and "-->" in s:
                    ifaces[current] = s.split()[1]
                elif s.startswith("inet ") and "netmask" in s:
                    ifaces[current] = s.split()[1]
        for ip in ifaces.values():
            if ip and not ip.startswith("fe80") and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None

def _vpn_windows():
    try:
        flags = _NO_WIN
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             timeout=5, creationflags=flags).stdout
        VPN_KEYWORDS = [
            "Cisco AnyConnect", "AnyConnect", "Cisco Secure",
            "Cisco VPN", "VPN Adapter", "Virtual Private",
            "Tunnel Adapter", "PPP Adapter",
        ]
        in_vpn = False; current_ip = None
        for line in out.splitlines():
            stripped = line.strip()
            if line and not line.startswith(" ") and not line.startswith("\t"):
                in_vpn = any(k.lower() in line.lower() for k in VPN_KEYWORDS)
                current_ip = None
            elif in_vpn and "IPv4 Address" in line and ":" in line:
                ip = line.split(":")[-1].strip().rstrip("(Preferred)").strip()
                if ip: current_ip = ip
            elif in_vpn and stripped == "" and current_ip:
                return current_ip
        if in_vpn and current_ip:
            return current_ip
        # Fallback: scan all interfaces for IPs in VPN ranges
        VPN_NETS = [_ipaddress.ip_network(c, strict=False) for c in
                    ["10.109.0.0/16", "10.23.0.0/16", "10.8.0.0/16"]]
        for line in out.splitlines():
            if "IPv4 Address" in line and ":" in line:
                ip_str = line.split(":")[-1].strip().rstrip("(Preferred)").strip()
                try:
                    addr = _ipaddress.ip_address(ip_str)
                    if any(addr in net for net in VPN_NETS):
                        return ip_str
                except ValueError:
                    pass
    except Exception:
        pass
    return None

def get_dns_info():
    if IS_MAC: return _dns_macos()
    if IS_WIN: return _dns_windows()
    return [], []

def _dns_macos():
    servers, domains = [], []
    try:
        out = subprocess.run(["scutil", "--dns"],
            capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("resolver #2"): break
            if "nameserver[" in line and ":" in line:
                ns = line.split(":")[-1].strip()
                if ns and ns not in servers: servers.append(ns)
            if "search domain[" in line and ":" in line:
                d = line.split(":")[-1].strip()
                if d and d not in domains: domains.append(d)
    except Exception as e:
        logger.warning(f"[WARN] DNS detect failed: {e}")
    return servers, domains

def _dns_windows():
    servers, domains = [], []
    try:
        flags = _NO_WIN
        out = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True,
                             timeout=5, creationflags=flags).stdout
        for line in out.splitlines():
            s = line.strip()
            if "DNS Servers" in s and ":" in s:
                ip = s.split(":")[-1].strip()
                if ip and ip not in servers: servers.append(ip)
            elif "Connection-specific DNS Suffix" in s and ":" in s:
                d = s.split(":")[-1].strip()
                if d and d not in domains: domains.append(d)
    except Exception as e:
        logger.warning(f"[WARN] DNS detect failed: {e}")
    return servers, domains

def get_is_ethernet():
    if IS_MAC: return _eth_macos()
    if IS_WIN: return _eth_windows()
    return False

def _eth_macos():
    try:
        for iface in ["en1", "en2", "en3", "eth0"]:
            r = subprocess.run(["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                ip = r.stdout.strip()
                if not _ip_in(ip, _VPN_TUNNEL_NETS):
                    return True
    except Exception:
        pass
    return False

def _eth_windows():
    try:
        flags = _NO_WIN
        out = subprocess.run(["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=5, creationflags=flags).stdout
        for line in out.splitlines():
            if "Connected" in line and ("Ethernet" in line or "Local Area" in line):
                return True
    except Exception:
        pass
    return False
