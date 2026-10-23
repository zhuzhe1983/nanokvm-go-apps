import copy
import gzip
import ipaddress
import json
from pathlib import Path
import threading
import time

from .common import APP, ROOT, DEFAULTS, UserError, private_keys, read, write, write_bytes
from .ftl import FTL
from .network import Network, addresses
from .rules import seed, download, build_database, manual_domains


class Engine:
    def __init__(self, root=ROOT, network=None, core=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / 'ftl').mkdir(exist_ok=True, mode=0o700)
        self.settings = {**copy.deepcopy(DEFAULTS), **read(root / 'settings.json', {})}
        self.keys = private_keys(root)
        self.net = network if network is not None else Network()
        self.core = core if core is not None else FTL(root, self.keys['api'])
        self.lock = threading.RLock()
        self.end = threading.Event()
        self.thread = None
        self.updater = None
        self.updating = False
        self.error = ''
        self.update_error = ''
        self.active_route = None
        self.stats = {'queries': 0, 'blocked': 0, 'percent': 0.0}
        self.interfaces = {}
        self.sharing = False
        self.mode = 'direct'
        self.update_retry = 0

    def route(self):
        self.interfaces = addresses()
        peer = read(Path('/var/lib/nanokvm-agent/clash-go/settings.json'), {})
        self.sharing = bool(peer.get('sharing'))
        self.mode = 'clash' if peer.get('enabled') else 'direct'
        usb = self.interfaces.get('usb0')
        if not usb:
            return None
        upstreams = [usb + '#1053'] if peer.get('enabled') else self.settings['upstreams']
        return usb, tuple(upstreams)

    def initialize_rules(self):
        db = self.root / 'ftl/gravity.db'
        cache = self.root / 'rules.txt.gz'
        if not cache.exists():
            write_bytes(cache, gzip.compress(seed(), mtime=0))
        if not db.exists():
            count = build_database(db, gzip.decompress(cache.read_bytes()), self.settings)
            self.settings['domain_count'] = count
            self.settings['updated_at'] = int(time.time())
            write(self.root / 'settings.json', self.settings)

    def boot(self):
        self.net.cleanup()
        self.initialize_rules()
        self.thread = threading.Thread(target=self.watch, daemon=True)
        self.thread.start()

    def reconcile(self):
        route = self.route()
        if not self.settings['enabled'] or not route:
            if self.active_route or self.core.running:
                self.net.cleanup();self.core.stop();self.active_route = None
            self.error = '等待 USB 虚拟网卡' if self.settings['enabled'] and not route else ''
            return
        if route != self.active_route or not self.core.running:
            self.net.cleanup()
            self.active_route = None
            try:
                self.core.start(list(route[1]), self.settings['pause_until'])
                self.net.apply()
                self.active_route = route
            except Exception:
                self.net.cleanup();self.core.stop();self.active_route = None
                raise
        self.error = ''

    def watch(self):
        while not self.end.is_set():
            refresh = False
            with self.lock:
                try:
                    self.reconcile()
                    if self.core.running:
                        value = self.core.request('/stats/summary').get('queries', {})
                        self.stats = {'queries': value.get('total', 0), 'blocked': value.get('blocked', 0), 'percent': value.get('percent_blocked', 0.0)}
                    if self.settings['pause_until'] and self.settings['pause_until'] <= time.time():
                        self.settings['pause_until'] = 0
                        write(self.root / 'settings.json', self.settings)
                    refresh = self.settings['auto_update'] and time.time() > max(self.settings['updated_at'] + 86400, self.update_retry)
                except Exception as error:
                    self.error = str(error) if isinstance(error, UserError) else '广告过滤未就绪，原 DNS 通路已保留'
                    self.net.cleanup();self.core.stop();self.active_route = None
                for log in (self.root / 'ftl').glob('*.log'):
                    try:
                        if log.stat().st_size > 1024 * 1024:
                            log.write_text('')
                    except OSError:
                        pass
            if refresh:
                self.update()
            self.end.wait(3)

    def status(self):
        # Polling the UI never waits for a list download or core restart.
        return {'version': '0.1.0', 'core_version': 'Pi-hole FTL 6.8',
                'enabled': self.settings['enabled'], 'running': bool(self.core.running),
                'effective': bool(self.core.running and self.active_route),
                'paused_seconds': max(0, int(self.settings['pause_until'] - time.time())),
                'stats': dict(self.stats) if self.core.running else {'queries': 0, 'blocked': 0, 'percent': 0.0},
                'domain_count': self.settings['domain_count'],
                'updated_at': self.settings['updated_at'], 'updating': self.updating,
                'error': self.error, 'update_error': self.update_error,
                'upstreams': list(self.settings['upstreams']), 'auto_update': self.settings['auto_update'],
                'allowlist': list(self.settings['allowlist']), 'denylist': list(self.settings['denylist']),
                'interfaces': dict(self.interfaces), 'sharing': self.sharing, 'route': self.mode,
                'list_name': 'HaGeZi Light', 'scope': 'usb'}

    def change(self, action, value):
        with self.lock:
            if action == 'enabled' and type(value) is bool:
                if value and self.core.running and self.settings['pause_until']:
                    self.core.request('/dns/blocking', {'blocking': True, 'timer': None})
                self.settings.update(enabled=value, pause_until=0)
            elif action == 'pause' and type(value) is int and value in (0, 60, 300, 1800):
                if not self.settings['enabled'] or not self.core.running:
                    raise UserError('请先开启广告拦截')
                self.core.request('/dns/blocking', {'blocking': not bool(value), 'timer': value or None})
                self.settings['pause_until'] = time.time() + value if value else 0
            else:
                raise UserError('不支持的设置')
            write(self.root / 'settings.json', self.settings)
            self.reconcile()

    def swap_rules(self, payload, settings):
        current = self.root / 'ftl/gravity.db'
        candidate = current.with_suffix('.candidate')
        backup = current.with_suffix('.previous')
        try:
            count = build_database(candidate, payload, settings)
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        before = copy.deepcopy(self.settings)
        cache = self.root / 'rules.txt.gz'
        previous_cache = cache.read_bytes()
        self.net.cleanup();self.core.stop();self.active_route = None
        moved = False
        try:
            current.replace(backup)
            moved = True
            candidate.replace(current)
            self.settings = {**settings, 'domain_count': count}
            self.reconcile()
            write_bytes(cache, gzip.compress(payload, mtime=0))
            write(self.root / 'settings.json', self.settings)
        except Exception:
            self.net.cleanup();self.core.stop();self.active_route = None
            if moved:
                backup.replace(current)
            self.settings = before
            write_bytes(cache, previous_cache)
            write(self.root / 'settings.json', before)
            self.reconcile()
            raise
        finally:
            candidate.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)

    def configure(self, body):
        if not isinstance(body, dict) or set(body) != {'upstreams', 'auto_update', 'allowlist', 'denylist'}:
            raise UserError('配置字段不完整')
        ips = body['upstreams']
        if not isinstance(ips, list) or not 1 <= len(ips) <= 3 or type(body['auto_update']) is not bool:
            raise UserError('填写 1～3 个上游 DNS IPv4 地址')
        try:
            for ip in ips:
                if not isinstance(ip, str):
                    raise ValueError()
                parsed = ipaddress.IPv4Address(ip)
                if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast or str(parsed) in self.interfaces.values():
                    raise ValueError()
        except (ValueError, TypeError):
            raise UserError('上游 DNS 地址无效或会形成解析循环') from None
        update = {**body, 'allowlist': manual_domains(body['allowlist']), 'denylist': manual_domains(body['denylist'])}
        if set(update['allowlist']) & set(update['denylist']):
            raise UserError('同一域名不能同时加入白名单和黑名单')
        with self.lock:
            self.swap_rules(gzip.decompress((self.root / 'rules.txt.gz').read_bytes()), {**self.settings, **update})

    def update(self):
        with self.lock:
            if self.updating:
                return False
            self.updating = True
        def worker():
            try:
                payload = download()
                with self.lock:
                    if not self.end.is_set():
                        self.swap_rules(payload, {**self.settings, 'updated_at': int(time.time())})
                        self.update_error = ''
            except Exception as error:
                self.update_error = str(error) if isinstance(error, UserError) else '更新失败，原规则继续生效'
            finally:
                self.update_retry = time.time() + 3600
                self.updating = False
        self.updater = threading.Thread(target=worker, daemon=True)
        self.updater.start()
        return True

    def close(self):
        self.end.set()
        if self.thread:
            self.thread.join(timeout=25)
        with self.lock:
            self.net.cleanup();self.core.stop();self.active_route = None
