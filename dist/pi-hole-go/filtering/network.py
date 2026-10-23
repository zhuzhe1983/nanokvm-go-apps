"""USB DNS capture only; no DHCP, main routes or host resolver changes."""
import json
import socket
import struct
import secrets
import subprocess

from .common import UserError

DNS_PORT = 5354


def command(args, check=True):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        raise UserError('无法操作设备 DNS 转发规则') from None
    if check and result.returncode:
        raise UserError('设备 DNS 转发规则设置失败')
    return result


def addresses():
    rows = json.loads(command(['ip', '-j', '-4', 'addr']).stdout)
    return {r['ifname']: next((v['local'] for v in r.get('addr_info', []) if v['family'] == 'inet'), '') for r in rows}


def dns_ready(host, port=DNS_PORT):
    ident = secrets.randbelow(65536)
    query = struct.pack('!6H', ident, 0x100, 1, 0, 0, 0) + b'\x02pi\x04hole\0' + struct.pack('!HH', 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect((host, port)); s.send(query)
            answer = s.recv(4096)
        rid, flags = struct.unpack('!HH', answer[:4])
        return rid == ident and bool(flags & 0x8000) and flags & 15 == 0
    except (OSError, struct.error):
        return False


class Network:
    def ipt(self, table, *args, six=False, check=True):
        return command(['ip6tables' if six else 'iptables', '-w', '3', '-t', table, *args], check)

    def hooks(self):
        return [('nat', 'PREROUTING', ['-i', 'usb0', '-j', 'PG_DNS'], False),
                ('filter', 'INPUT', ['-p', 'udp', '--dport', str(DNS_PORT), '-j', 'PG_IN'], False),
                ('filter', 'INPUT', ['-p', 'tcp', '--dport', str(DNS_PORT), '-j', 'PG_IN'], False),
                ('filter', 'FORWARD', ['-i', 'usb0', '-j', 'PG_V6'], True)]

    def apply(self):
        # Only called after FTL answers DNS. Existing rules stay until rebuilt.
        for table, name, six in [('nat', 'PG_DNS', False), ('filter', 'PG_IN', False), ('filter', 'PG_V6', True)]:
            self.ipt(table, '-N', name, six=six, check=False)
            self.ipt(table, '-F', name, six=six)
        for proto in ['udp', 'tcp']:
            self.ipt('nat', '-A', 'PG_DNS', '-p', proto, '--dport', '53', '-j', 'REDIRECT', '--to-ports', str(DNS_PORT))
            self.ipt('filter', '-A', 'PG_V6', '-p', proto, '--dport', '53', '-j', 'DROP', six=True)
        for interface in ['lo', 'usb0']:
            self.ipt('filter', '-A', 'PG_IN', '-i', interface, '-j', 'ACCEPT')
        self.ipt('filter', '-A', 'PG_IN', '-j', 'DROP')
        for table, parent, rule, six in self.hooks():
            if self.ipt(table, '-C', parent, *rule, six=six, check=False).returncode:
                self.ipt(table, '-I', parent, '1', *rule, six=six)

    def cleanup(self):
        for table, parent, rule, six in self.hooks():
            for _ in range(8):
                if self.ipt(table, '-D', parent, *rule, six=six, check=False).returncode:
                    break
        for table, name, six in [('nat', 'PG_DNS', False), ('filter', 'PG_IN', False), ('filter', 'PG_V6', True)]:
            self.ipt(table, '-F', name, six=six, check=False)
            self.ipt(table, '-X', name, six=six, check=False)
