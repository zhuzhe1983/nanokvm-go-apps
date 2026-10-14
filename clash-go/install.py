#!/usr/bin/env python3
"""Install verified ARMv7 core and a background service; leave first-use toggles off."""
import gzip
import hashlib
import os
from pathlib import Path
import platform
import subprocess

from gateway.common import APP, ROOT, DEFAULTS, private_keys, read, write


def main():
    if os.geteuid() != 0 or platform.machine() not in ("armv7l", "armv8l"):
        raise SystemExit("This release requires root on NanoKVM Go ARM32.")
    for path in ("/dev/net/tun", "/etc/kvm/server.crt", "/etc/kvm/server.key"):
        if not Path(path).exists():
            raise SystemExit("Missing required device capability: " + path)
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    (ROOT / "bin").mkdir(exist_ok=True, mode=0o700)
    manifest = read(APP / "vendor/manifest.json")
    targets = []
    for name, entry in (("mihomo", manifest), ("mihomo-oix", manifest["oix"])):
        archive = APP / "vendor" / entry["asset"]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != entry["sha256"]:
            raise SystemExit(name + " archive checksum mismatch")
        target = ROOT / "bin" / (name + ".new")
        with gzip.open(archive, "rb") as source, target.open("wb") as dest:
            while chunk := source.read(1024 * 1024):
                dest.write(chunk)
        target.chmod(0o700)
        result = subprocess.run([str(target), "-v"], text=True, capture_output=True, timeout=10)
        if result.returncode or entry["version"] not in result.stdout:
            target.unlink(missing_ok=True)
            raise SystemExit(name + " is incompatible with this firmware")
        print(result.stdout.strip())
        targets.append((target, ROOT / "bin" / name))
    subprocess.run(["systemctl", "stop", "clash-go.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for target, dest in targets:
        target.replace(dest)
    if not (ROOT / "settings.json").exists():
        write(ROOT / "settings.json", DEFAULTS)
    private_keys(ROOT, shorten_pin=True)
    unit = f'''[Unit]
Description=Clash Go USB network controller
After=network.target

[Service]
Type=simple
WorkingDirectory={APP}
ExecStart=/usr/bin/python3 {APP}/server.py
Restart=on-failure
RestartSec=3
UMask=0077
Nice=10
OOMScoreAdjust=200
MemoryMax=112M
LimitNOFILE=4096
Environment=PYTHONDONTWRITEBYTECODE=1
KillMode=control-group
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
'''
    Path("/etc/systemd/system/clash-go.service").write_text(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "clash-go.service"], check=True)
    print("Clash Go installed. HTTPS configuration port: 8443. First-use switches are off.")


if __name__ == "__main__":
    main()
