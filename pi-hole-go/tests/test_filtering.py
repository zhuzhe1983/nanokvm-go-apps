import copy
import gzip
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from filtering.common import DEFAULTS, UserError, write, write_bytes
from filtering.engine import Engine
from filtering.ftl import configuration
from filtering.network import Network
from filtering.rules import build_database, domain, parse_rules, seed
from install import check_home
from server import Server


class FilteringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.core = Mock(running=False)
        self.net = Mock()
        self.engine = Engine(self.root, self.net, self.core)
        self.old_payload = b'||ads.example.com^\n'
        write_bytes(self.root / 'rules.txt.gz', gzip.compress(self.old_payload))
        build_database(self.root / 'ftl/gravity.db', self.old_payload, self.engine.settings)
        self.engine.route = Mock(return_value=('10.1.2.1', ('1.1.1.1',)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_integrity_and_official_schema(self):
        rules = list(parse_rules(seed()))
        self.assertGreater(len(rules), 50000)
        settings = {**DEFAULTS, 'allowlist': ['ok.example.com'], 'denylist': ['bad.example.com']}
        build_database(self.root / 'check.db', self.old_payload, settings)
        with sqlite3.connect(self.root / 'check.db') as db:
            self.assertEqual(db.execute("SELECT value FROM info WHERE property='version'").fetchone()[0], '21')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM vw_regex_allowlist').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM vw_regex_denylist').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM info WHERE property='gravity_count'").fetchone()[0], '1')

    def test_embedded_dnsmasq_cannot_own_firmware_pid_or_dhcp(self):
        cfg = configuration(self.root, ['1.1.1.1'])
        self.assertEqual(cfg['misc']['dnsmasq_lines'], ['pid-file=' + str(self.root / 'ftl/dnsmasq.pid')])
        self.assertFalse(cfg['dhcp']['active'])
        self.assertFalse(cfg['ntp.sync']['active'])
        self.assertFalse(cfg['ntp.ipv4']['active'])
        self.assertFalse(cfg['ntp.ipv6']['active'])
        self.assertFalse(cfg['misc']['etc_dnsmasq_d'])
        self.assertEqual(cfg['webserver']['port'], '127.0.0.1:5471')
        self.assertEqual(cfg['database']['maxDBdays'], 0)

    def test_untrusted_rule_content_is_rejected(self):
        for bad in [b'', b'<html>login</html>', b'@@||allow.example.com^', b'||ads.example.com^$script', b'0.0.0.0 ads.example.com', b'127.0.0.1', b'foo.com\nnot-a-domain']:
            with self.subTest(bad=bad), self.assertRaises(UserError):
                list(parse_rules(bad))
        self.assertEqual(domain('EXAMPLE.COM.'), 'example.com')
        self.assertTrue(domain('例子.中国').startswith('xn--'))

    def test_bad_download_never_touches_working_rules_or_network(self):
        before = (self.root / 'ftl/gravity.db').read_bytes()
        with self.assertRaises(UserError):
            self.engine.swap_rules(b'<html>unavailable</html>', self.engine.settings)
        self.assertEqual((self.root / 'ftl/gravity.db').read_bytes(), before)
        self.net.cleanup.assert_not_called()
        self.assertFalse((self.root / 'ftl/gravity.candidate').exists())

    def test_failed_core_restart_restores_database_settings_and_cache(self):
        before = (self.root / 'ftl/gravity.db').read_bytes()
        self.engine.settings['enabled'] = True
        original = copy.deepcopy(self.engine.settings)
        self.core.start.side_effect = [UserError('startup failed'), None]
        with self.assertRaisesRegex(UserError, 'startup failed'):
            self.engine.swap_rules(b'||new.example.com^', {**original, 'allowlist': ['new.example.com']})
        self.assertEqual((self.root / 'ftl/gravity.db').read_bytes(), before)
        self.assertEqual(gzip.decompress((self.root / 'rules.txt.gz').read_bytes()), self.old_payload)
        self.assertEqual(self.engine.settings, original)
        self.assertEqual(self.core.start.call_count, 2)

    def test_failed_settings_write_rolls_back_new_cache(self):
        original = copy.deepcopy(self.engine.settings)
        with patch('filtering.engine.write', side_effect=[OSError('disk full'), None]):
            with self.assertRaises(OSError):
                self.engine.swap_rules(b'||new.example.com^', original)
        self.assertEqual(gzip.decompress((self.root / 'rules.txt.gz').read_bytes()), self.old_payload)
        self.assertEqual(self.engine.settings, original)

    def test_core_failure_never_installs_dns_redirect(self):
        self.engine.settings['enabled'] = True
        self.core.start.side_effect = UserError('not ready')
        with self.assertRaises(UserError):
            self.engine.reconcile()
        self.net.apply.assert_not_called()
        self.core.stop.assert_called()
        self.assertFalse(self.engine.status()['effective'])

    def test_restart_does_not_report_ready_before_firewall_is_installed(self):
        self.engine.settings['enabled'] = True
        self.engine.active_route = self.engine.route()
        def starting(*_):
            self.core.running = True
            self.assertFalse(self.engine.status()['effective'])
        self.core.start.side_effect = starting
        self.engine.reconcile()
        self.net.apply.assert_called_once()
        self.assertTrue(self.engine.status()['effective'])

    def test_disabling_and_usb_unplug_remove_hooks(self):
        self.engine.settings['enabled'] = True
        self.engine.route.return_value = None
        self.engine.active_route = ('10.1.2.1', ('1.1.1.1',))
        self.engine.reconcile()
        self.net.cleanup.assert_called(); self.core.stop.assert_called()
        self.engine.change('enabled', False)
        self.assertFalse(self.engine.settings['enabled'])

    def test_config_rejects_loops_and_conflicting_lists(self):
        config = dict(upstreams=['1.1.1.1'], auto_update=True, allowlist=[], denylist=[])
        for ips in [['127.0.0.1'], ['0.0.0.0'], ['224.0.0.1'], [1234], []]:
            with self.assertRaises(UserError):
                self.engine.configure({**config, 'upstreams': ips})
        with self.assertRaises(UserError):
            self.engine.configure({**config, 'allowlist': ['same.example'], 'denylist': ['same.example']})

    def test_status_omits_credentials(self):
        text = json.dumps(self.engine.status())
        for secret in self.engine.keys.values():
            self.assertNotIn(secret, text)

    def test_clash_upstream_and_direct_route(self):
        engine = Engine(self.root, self.net, self.core)
        with patch('filtering.engine.addresses', return_value={'usb0': '10.1.2.1'}), patch('filtering.engine.read', return_value={'enabled': True, 'sharing': True}):
            self.assertEqual(engine.route()[1], ('10.1.2.1#1053',))
        with patch('filtering.engine.addresses', return_value={'usb0': '10.1.2.1'}), patch('filtering.engine.read', return_value={'enabled': False, 'sharing': True}):
            self.assertEqual(engine.route()[1], tuple(DEFAULTS['upstreams']))

    def test_firewall_only_captures_usb_and_never_changes_system_routes(self):
        calls = []
        def run(args, check=True):
            calls.append(args)
            return Mock(returncode=1 if '-C' in args or '-D' in args else 0, stdout='')
        with patch('filtering.network.command', side_effect=run):
            net = Network();net.apply();net.cleanup()
        self.assertTrue(any('REDIRECT' in a and '5354' in a for a in calls))
        for args in calls:
            self.assertIn(args[0], ('iptables', 'ip6tables'))
            for op in ['-F', '-X', '-N']:
                if op in args:
                    self.assertTrue(args[args.index(op) + 1].startswith('PG_'))
            if 'PREROUTING' in args:
                self.assertIn('usb0', args)
            self.assertNotIn('REJECT', args)

    def test_installer_refuses_existing_pihole(self):
        target = self.root / 'ftl'; home = self.root / 'existing'
        home.mkdir()
        with self.assertRaises(RuntimeError):
            check_home(home, target)
        home.rmdir();home.symlink_to(self.root / 'someone-else')
        with self.assertRaises(RuntimeError):
            check_home(home, target)
        home.unlink();home.symlink_to(target)
        check_home(home, target)


class HTTPTests(unittest.TestCase):
    def test_auth_origin_host_and_secret_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(Path(tmp), Mock(), Mock(running=False))
            server = Server(('127.0.0.1', 0), engine)
            worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
            client = http.client.HTTPConnection(*server.server_address)
            try:
                with patch('server.addresses', return_value={}):
                    client.request('GET', '/api/status')
                    response=client.getresponse();self.assertEqual(response.status, 401);response.read()
                    headers={'X-Pi-Hole-Go': '1', 'Content-Type': 'application/json'}
                    client.request('POST', '/api/login', json.dumps({'pin': engine.keys['pin']}), headers)
                    response=client.getresponse();self.assertEqual(response.status,200)
                    cookie=response.getheader('Set-Cookie');response.read()
                    self.assertIn('HttpOnly; Secure; SameSite=Strict',cookie)
                    auth={'Cookie':cookie.split(';')[0], **headers}
                    client.request('GET','/api/status',headers=auth)
                    response=client.getresponse();self.assertEqual(response.status,200)
                    data=response.read().decode()
                    self.assertNotIn(engine.keys['api'],data);self.assertNotIn(engine.keys['pin'],data)
                    client.request('POST','/api/change',json.dumps({'action':'enabled','value':True}),{**auth,'Origin':'https://attacker.example'})
                    response=client.getresponse();self.assertEqual(response.status,403);response.read()
                    client.request('GET','/',headers={'Host':'attacker.example'})
                    response=client.getresponse();self.assertEqual(response.status,403);response.read()
            finally:
                client.close();server.shutdown();server.server_close();worker.join(3)


if __name__ == '__main__':
    unittest.main()
