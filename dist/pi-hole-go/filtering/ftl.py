"""Run the unmodified official ARMv7 FTL core on private application ports."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

from .common import UserError
from .network import DNS_PORT, dns_ready

API_PORT = 5471


def configuration(root, upstreams):
    home = root / 'ftl'
    return {
        'dns': {'upstreams': upstreams, 'interface': 'usb0', 'listeningMode': 'BIND',
                'port': DNS_PORT, 'queryLogging': False, 'ignoreLocalhost': False,
                'CNAMEdeepInspect': True},
        'dns.cache': {'size': 1000},
        'dns.blocking': {'active': True, 'mode': 'NULL'},
        'dhcp': {'active': False},
        'ntp.ipv4': {'active': False}, 'ntp.ipv6': {'active': False}, 'ntp.sync': {'active': False},
        'resolver': {'resolveIPv4': False, 'resolveIPv6': False, 'networkNames': False},
        'database': {'DBimport': False, 'maxDBdays': 0, 'forceDisk': True},
        'database.network': {'parseARPcache': False},
        'webserver': {'port': f'127.0.0.1:{API_PORT}', 'threads': 2, 'serve_all': False},
        'webserver.api': {'maxHistory': 3600, 'max_sessions': 4, 'cli_pw': False, 'allow_destructive': False},
        'webserver.session': {'restore': False},
        'files': {'database': str(home / 'history.db'), 'tmp_db': str(home / 'temporary.db'),
                  'gravity': str(home / 'gravity.db'), 'macvendor': str(home / 'macvendor.db')},
        'files.log': {'ftl': str(home / 'ftl.log'), 'dnsmasq': str(home / 'dns.log'), 'webserver': str(home / 'web.log')},
        'misc': {'privacylevel': 2, 'nice': 10, 'readOnly': True, 'etc_dnsmasq_d': False,
                 'dnsmasq_lines': ['pid-file=' + str(home / 'dnsmasq.pid')]},
        'misc.check': {'load': False},
    }


def save_config(root, upstreams):
    sections = configuration(root, upstreams)
    text = '\n'.join('[' + section + ']\n' + '\n'.join(key + ' = ' + json.dumps(value, ensure_ascii=False)
                    for key, value in values.items()) + '\n' for section, values in sections.items())
    target = root / 'ftl/pihole.toml'
    temp = target.with_suffix('.new')
    temp.write_text(text);temp.chmod(0o600);temp.replace(target)


class FTL:
    def __init__(self, root, password):
        self.root, self.password = root, password
        self.binary = root / 'bin/pihole-FTL'
        self.process = None
        self.sid = ''
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def request(self, path, body=None, retry=True):
        if path != '/auth' and not self.sid:
            result = self.request('/auth', {'password': self.password}, False)
            session = result.get('session', {})
            if not session.get('valid') or not session.get('sid'):
                raise UserError('Pi-hole 内核认证失败')
            self.sid = session['sid']
        request = urllib.request.Request(f'http://127.0.0.1:{API_PORT}/api' + path,
            json.dumps(body).encode() if body is not None else None,
            {'Content-Type': 'application/json', 'X-FTL-SID': self.sid})
        try:
            with self.opener.open(request, timeout=3) as response:
                return json.loads(response.read(512 * 1024))
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry:
                self.sid = ''
                return self.request(path, body, False)
        except (OSError, ValueError):
            pass
        raise UserError('Pi-hole 内核暂不可用')

    def start(self, upstreams, pause_until=0):
        self.stop()
        save_config(self.root, upstreams)
        env = {k: v for k, v in os.environ.items() if not k.startswith('FTLCONF_')}
        env['FTLCONF_webserver_api_password'] = self.password
        self.process = subprocess.Popen([str(self.binary), '-f'], env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not self.running:
                break
            try:
                if self.request('/dns/blocking').get('blocking') == 'enabled' and dns_ready('127.0.0.1'):
                    if pause_until > time.time():
                        self.request('/dns/blocking', {'blocking': False, 'timer': max(1, int(pause_until - time.time()))})
                    return
            except UserError:
                pass
            time.sleep(.2)
        self.stop()
        raise UserError('Pi-hole 内核未就绪，原 DNS 通路已保留')

    def stop(self):
        if self.running:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill();self.process.wait(timeout=3)
        self.process = None
        self.sid = ''
