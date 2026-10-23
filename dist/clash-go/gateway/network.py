"""Only USB forwarding is managed. Never flush global firewall or default routes."""
import ipaddress
import json
from pathlib import Path
import subprocess

from .common import ROOT, UserError, read, write

TABLE = "2023"
PRIORITY = "12023"
TUN = "cg-tun"


def command(args, check=True, timeout=8, input=None):
    try:
        result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise UserError("系统网络命令不可用或超时") from None
    if check and result.returncode:
        raise UserError("网络配置未完成，请检查设备网络能力")
    return result


def addresses():
    value = json.loads(command(["ip", "-j", "-4", "addr"]).stdout)
    return {item["ifname"]: next((a["local"] + "/" + str(a["prefixlen"])
            for a in item.get("addr_info", []) if a["family"] == "inet"), "") for item in value}


def wifi_status():
    text = command(["wpa_cli", "-i", "wlan0", "status"], check=False).stdout
    data = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return {key: data.get(key, "") for key in ("ssid", "wpa_state", "ip_address", "id")}


def wifi_scan():
    command(["wpa_cli", "-i", "wlan0", "scan"], check=False)
    lines = command(["wpa_cli", "-i", "wlan0", "scan_results"]).stdout.splitlines()[1:]
    found = {}
    for line in lines:
        row = line.split("\t", 4)
        if len(row) == 5 and row[4]:
            found.setdefault(row[4], {"ssid": row[4], "signal": row[2], "secure": "WPA" in row[3] or "WEP" in row[3]})
    return list(found.values())[:40]


def wpa_set(index, field, value):
    # Passwords travel over stdin, never process argv, logs or shell expansion.
    output = command(["wpa_cli", "-i", "wlan0"], input=f"set_network {index} {field} {value}\nquit\n").stdout
    if "FAIL" in output or "OK" not in output:
        raise UserError("Wi-Fi 参数未被接受")


class Network:
    def __init__(self, root=ROOT):
        self.root = root

    def ipt(self, table, *args, six=False, check=True):
        return command(["ip6tables" if six else "iptables", "-w", "3", "-t", table, *args], check=check)

    def chain(self, table, name, rules, six=False):
        self.ipt(table, "-N", name, six=six, check=False)
        self.ipt(table, "-F", name, six=six)
        for rule in rules:
            self.ipt(table, "-A", name, *rule, six=six)

    def hook(self, table, parent, rule, six=False):
        if self.ipt(table, "-C", parent, *rule, six=six, check=False).returncode:
            position = "1"
            # Pi-hole Go filters USB DNS before forwarding allowed queries to
            # Mihomo. Preserve that order when proxy/sharing is toggled again.
            if not six and table == "nat" and parent == "PREROUTING" and rule == ["-i", "usb0", "-j", "CG_DNS"]:
                existing = [line.split() for line in self.ipt(table, "-S", parent).stdout.splitlines()
                            if line.startswith("-A ")]
                for index, entry in enumerate(existing):
                    if entry == ["-A", parent, "-i", "usb0", "-j", "PG_DNS"]:
                        position = str(index + 2)
                        break
            self.ipt(table, "-I", parent, position, *rule, six=six)

    def cleanup(self):
        for table, parent, name, rule, six in self.hooks():
            for _ in range(8):
                if self.ipt(table, "-D", parent, *rule, six=six, check=False).returncode:
                    break
            self.ipt(table, "-F", name, six=six, check=False)
            self.ipt(table, "-X", name, six=six, check=False)
        # Exact task-owned rule; never flush other routing tables/rules.
        command(["ip", "rule", "del", "priority", PRIORITY, "iif", "usb0", "lookup", TABLE], check=False)
        command(["ip", "route", "flush", "table", TABLE], check=False)
        previous = read(self.root / "network-before.json")
        if previous:
            command(["sysctl", "-w", "net.ipv4.ip_forward=" + str(previous["ip_forward"])])
            (self.root / "network-before.json").unlink(missing_ok=True)

    @staticmethod
    def hooks():
        return [
            ("filter", "FORWARD", "CG_OUT", ["-i", "usb0", "-j", "CG_OUT"], False),
            ("filter", "FORWARD", "CG_BACK", ["-o", "usb0", "-j", "CG_BACK"], False),
            ("nat", "POSTROUTING", "CG_NAT", ["-j", "CG_NAT"], False),
            ("nat", "PREROUTING", "CG_DNS", ["-i", "usb0", "-j", "CG_DNS"], False),
            ("filter", "FORWARD", "CG_V6", ["-i", "usb0", "-j", "CG_V6"], True)]

    def apply(self, sharing, proxy):
        if not sharing:
            self.cleanup()
            return
        # Block USB first during transitions to prevent accidental direct fallback.
        # Go+ firmware does not include the iptables REJECT kernel module.
        self.chain("filter", "CG_OUT", [["-j", "DROP"]])
        self.hook("filter", "FORWARD", ["-i", "usb0", "-j", "CG_OUT"])
        addr = addresses().get("usb0")
        if not addr:
            raise UserError("请先在 NanoKVM 设置中开启 NCM 虚拟网卡")
        subnet = str(ipaddress.ip_interface(addr).network)
        if not (self.root / "network-before.json").exists():
            before = int(Path("/proc/sys/net/ipv4/ip_forward").read_text())
            write(self.root / "network-before.json", {"ip_forward": before})
        command(["sysctl", "-w", "net.ipv4.ip_forward=1"])
        self.chain("filter", "CG_BACK", [["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"], ["-j", "DROP"]])
        self.chain("filter", "CG_V6", [["-j", "DROP"]], six=True)
        self.chain("nat", "CG_NAT", [["-s", subnet, "-o", "wlan0", "-j", "MASQUERADE"]])
        dns = [["-p", proto, "--dport", "53", "-j", "REDIRECT", "--to-ports", "1053"] for proto in ("udp", "tcp")] if proxy else []
        self.chain("nat", "CG_DNS", dns)
        for table, parent, _, rule, six in self.hooks():
            self.hook(table, parent, rule, six=six)
        command(["ip", "rule", "del", "priority", PRIORITY, "iif", "usb0", "lookup", TABLE], check=False)
        command(["ip", "route", "flush", "table", TABLE], check=False)
        if proxy:
            # A dead TUN must fail closed instead of falling through to Wi-Fi.
            command(["ip", "route", "add", "unreachable", "default", "table", TABLE, "metric", "32760"])
            command(["ip", "route", "add", "default", "dev", TUN, "table", TABLE, "metric", "10"])
            command(["ip", "rule", "add", "priority", PRIORITY, "iif", "usb0", "lookup", TABLE])
        self.chain("filter", "CG_OUT", [["-s", subnet, "-o", TUN if proxy else "wlan0", "-j", "ACCEPT"], ["-j", "DROP"]])
