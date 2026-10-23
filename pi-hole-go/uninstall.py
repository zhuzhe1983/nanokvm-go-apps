#!/usr/bin/env python3
"""Stop filtering and unregister the service; keep private settings for recovery."""
from pathlib import Path
import subprocess
from filtering.common import ROOT
from filtering.network import Network

if __name__ == '__main__':
    subprocess.run(['systemctl', 'disable', '--now', 'pi-hole-go.service'], check=True)
    Network().cleanup()
    Path('/etc/systemd/system/pi-hole-go.service').unlink(missing_ok=True)
    home = Path('/etc/pihole')
    if home.is_symlink() and home.resolve() == (ROOT / 'ftl').resolve():
        home.unlink()
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('Pi-hole Go stopped. Private settings and app files were retained.')
