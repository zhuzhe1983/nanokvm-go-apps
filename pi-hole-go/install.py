#!/usr/bin/env python3
"""Install the official FTL binary without replacing firmware DNS services."""
import gzip
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess

from filtering.common import APP, ROOT, private_keys, read
from filtering.engine import Engine


def check_home(home=Path('/etc/pihole'), target=ROOT / 'ftl'):
    if home.is_symlink():
        if home.resolve() != target.resolve():
            raise RuntimeError('/etc/pihole belongs to another installation; nothing was replaced.')
    elif home.exists():
        raise RuntimeError('/etc/pihole already exists; nothing was replaced.')


def main():
    if os.geteuid() != 0 or platform.machine() not in ('armv7l', 'armv8l'):
        raise SystemExit('This release requires root on NanoKVM Go ARM32.')
    for path in ('/etc/kvm/server.crt', '/etc/kvm/server.key', '/kvmcomm/apps/appbase.py'):
        if not Path(path).is_file():
            raise SystemExit('Missing firmware resource: ' + path)
    for command in ('ip', 'iptables', 'ip6tables', 'systemctl'):
        if not shutil.which(command):
            raise SystemExit('Missing command: ' + command)
    check_home()
    # An existing Pi-hole service must remain entirely untouched.
    result = subprocess.run(['systemctl', 'is-active', '--quiet', 'pihole-FTL.service'])
    if result.returncode == 0:
        raise SystemExit('A separate Pi-hole service is already running.')
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    (ROOT / 'bin').mkdir(exist_ok=True, mode=0o700)
    (ROOT / 'ftl').mkdir(exist_ok=True, mode=0o700)
    meta = read(APP / 'vendor/manifest.json')
    archive = (APP / 'vendor' / meta['asset']).read_bytes()
    if hashlib.sha256(archive).hexdigest() != meta['sha256']:
        raise SystemExit('FTL archive checksum mismatch')
    payload = gzip.decompress(archive)
    if hashlib.sha256(payload).hexdigest() != meta['binary_sha256']:
        raise SystemExit('FTL binary checksum mismatch')
    binary = ROOT / 'bin/pihole-FTL.new'
    binary.write_bytes(payload);binary.chmod(0o700)
    result = subprocess.run([str(binary), '-v'], capture_output=True, text=True, timeout=10)
    if result.returncode or result.stdout.strip() != meta['version']:
        binary.unlink(missing_ok=True)
        raise SystemExit('FTL is incompatible with this firmware')
    subprocess.run(['systemctl', 'stop', 'pi-hole-go.service'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    binary.replace(ROOT / 'bin/pihole-FTL')
    if not Path('/etc/pihole').is_symlink():
        Path('/etc/pihole').symlink_to(ROOT / 'ftl')
    private_keys()
    Engine().initialize_rules()
    unit = f'''[Unit]
Description=Pi-hole Go USB DNS filter
After=network.target clash-go.service

[Service]
Type=simple
WorkingDirectory={APP}
ExecStart=/usr/bin/python3 {APP}/server.py
ExecStopPost=/usr/bin/python3 {APP}/cleanup.py
Restart=on-failure
RestartSec=3
UMask=0077
Nice=10
OOMScoreAdjust=300
MemoryMax=96M
LimitNOFILE=2048
Environment=PYTHONDONTWRITEBYTECODE=1
KillMode=control-group
TimeoutStopSec=40

[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/pi-hole-go.service').write_text(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'pi-hole-go.service'], check=True)
    print('Pi-hole Go installed. HTTPS port: 8444. Enable filtering from the touch screen.')


if __name__ == '__main__':
    main()
