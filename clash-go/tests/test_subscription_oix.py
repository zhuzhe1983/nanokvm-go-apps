import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.common import DEFAULTS, UserError, write, write_bytes
from gateway.engine import Engine, core_config
from gateway.subscription import download, HTTPSRedirect, LIMIT
from gateway import oix


class SubscriptionTests(unittest.TestCase):
    def test_https_download_accepts_full_profile_without_rewriting_protocol_options(self):
        payload = b'proxies:\n  - {name: test, type: anytls, password: private, server: example.com, port: 443}\n'
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        response.read.return_value = payload
        opener = Mock(); opener.open.return_value = response
        with patch('gateway.subscription.urllib.request.build_opener', return_value=opener):
            self.assertEqual(download('https://example.com/sub'), payload)
        req = opener.open.call_args.args[0]
        self.assertEqual(req.headers['User-agent'], 'clash')
        self.assertEqual(response.read.call_args.args, (LIMIT + 1,))

    def test_https_failure_does_not_leak_private_subscription(self):
        url = 'https://example.com/private-token'
        opener = Mock(); opener.open.side_effect = urllib.error.HTTPError(url, 403, url, {}, None)
        with patch('gateway.subscription.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(UserError, 'HTTP 403') as caught:
                download(url)
        self.assertNotIn('private-token', str(caught.exception))
        with self.assertRaises(UserError):
            HTTPSRedirect().redirect_request(urllib.request.Request(url), None, 302, '', {}, 'http://example.com/sub')

    def test_bad_download_is_not_committed(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder)); engine.net = Mock()
            with patch('gateway.engine.download', side_effect=UserError('下载失败')):
                with self.assertRaises(UserError):
                    engine.change('subscription', 'https://example.com/bad')
            self.assertFalse(engine.settings['subscription_url'])
            self.assertFalse((engine.root / 'providers/account.yaml').exists())

    def test_parsed_empty_nodes_restore_previous_cache_and_core(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder)); engine.net = Mock()
            engine.settings['subscription_url'] = 'https://example.com/old'
            path = engine.root / 'providers/account.yaml'; write_bytes(path, b'old payload')
            engine.start_core = Mock()
            engine.choose = Mock(side_effect=[UserError('无节点'), None])
            with patch('gateway.engine.download', return_value=b'proxies: []'):
                with self.assertRaises(UserError):
                    engine.change('subscription', 'https://example.com/new')
            self.assertEqual(engine.settings['subscription_url'], 'https://example.com/old')
            self.assertEqual(path.read_bytes(), b'old payload')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(engine.start_core.call_count, 2)

    def test_file_provider_has_no_unreliable_async_http_fetch(self):
        cfg = core_config(dict(DEFAULTS, subscription_url='https://example.com/private'), 'secret', Path('/tmp'), '10.1.1.1')
        self.assertEqual(cfg['proxy-providers']['account'], {'type':'file','path':'providers/account.yaml'})
        self.assertNotIn('https://example.com/private', json.dumps(cfg))


class OixTests(unittest.TestCase):
    def test_core_account_uses_stdin_and_whitelists_public_fields(self):
        reply = {'token':'own-token','account':{'plan':'Pro','traffic':'100 GB','token':'must-not-leak'},'rebound':True}
        with patch('gateway.oix.subprocess.run', return_value=Mock(returncode=0, stdout=json.dumps(reply))) as run:
            result = oix.account('/bin/core', 'Bearer pasted-token')
        self.assertNotIn('pasted-token', ' '.join(run.call_args.args[0]))
        self.assertEqual(json.loads(run.call_args.kwargs['input']), {'token':'pasted-token'})
        self.assertEqual(result['token'], 'own-token')
        self.assertNotIn('token', result['account'])
        self.assertTrue(result['rebound'])

    def test_rejected_token_does_not_surface_core_raw_output(self):
        with patch('gateway.oix.subprocess.run', return_value=Mock(returncode=2, stdout=json.dumps({'code':'rejected','error':'https://secret/token'}))):
            with self.assertRaises(UserError) as caught:
                oix.account('/bin/core','private')
        self.assertNotIn('https://secret',str(caught.exception))
        with self.assertRaises(UserError):
            oix.access_token('https://example.com/sub')

    def test_managed_provider_keeps_official_decryption_and_usb_scope(self):
        cfg = core_config(dict(DEFAULTS,source='oix'), 'secret', Path('/tmp'), '10.1.1.1')
        self.assertEqual(cfg['proxy-groups'][0]['use'], ['oixCloud'])
        self.assertEqual(cfg['proxy-providers']['oixCloud']['path'], 'providers/oixCloud')
        self.assertFalse(cfg['tun']['auto-route'])
        self.assertNotIn('age-secret-key', cfg['proxy-providers']['oixCloud']) # supplied by official core

    def test_failed_managed_load_restores_old_account_and_deletes_candidate_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(Path(folder)); engine.net = Mock()
            engine.settings.update(source='oix',oix={'token':'old'},oix_home='oix-old')
            old = engine.root / 'runtimes/oix-old';old.mkdir(parents=True)
            (old / 'private-cache').write_text('old')
            engine.start_core = Mock(side_effect=[UserError('加载失败'),None]);engine.choose = Mock()
            with patch('gateway.engine.oix.account', return_value={'token':'new','account':{}}):
                with self.assertRaises(UserError):
                    engine.change('oix_login','new')
            self.assertEqual(engine.settings['oix']['token'],'old')
            self.assertEqual(list((engine.root/'runtimes').iterdir()),[old])
            self.assertEqual(engine.start_core.call_count,2)

    def test_account_without_nodes_still_shows_plan_without_switching_working_subscription(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=Engine(Path(folder));engine.net=Mock()
            engine.settings.update(subscription_url='https://example.com/old')
            engine.start_core=Mock(side_effect=[UserError('托管节点未加载'),None]);engine.choose=Mock()
            with patch('gateway.engine.oix.account',return_value={'token':'new','account':{'plan':'Expired'}}):
                with self.assertRaisesRegex(UserError,'账号已登录'):
                    engine.change('oix_login','new')
            self.assertEqual(engine.settings['source'],'subscription')
            self.assertEqual(engine.settings['oix']['account']['plan'],'Expired')
            self.assertEqual(engine.settings['subscription_url'],'https://example.com/old')
            self.assertIn('未加载',engine.settings['oix']['node_error'])
            self.assertEqual(list((engine.root/'runtimes').iterdir()),[])

    def test_logout_is_local_and_disables_forwarding_only_when_account_active(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=Engine(Path(folder));engine.net=Mock()
            engine.settings.update(source='oix',oix={'token':'secret'},oix_home='oix-old',sharing=True,enabled=True)
            with patch('gateway.engine.oix.account') as account:
                engine.change('oix_logout',None)
            account.assert_not_called()
            self.assertFalse(engine.settings['sharing']);self.assertFalse(engine.settings['enabled'])
            self.assertEqual(engine.settings['oix'],{})

    def test_status_does_not_expose_token_or_private_account_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=Engine(Path(folder));engine.settings.update(source='oix',oix={'token':'private-token','account':{'plan':'Pro','token':'private-field'}})
            with patch('gateway.engine.addresses',return_value={}),patch('gateway.engine.wifi_status',return_value={}):
                status=engine.status()
            self.assertEqual(status['oix']['account']['plan'],'Pro')
            self.assertNotIn('private-',json.dumps(status))
            self.assertIn('oix',status['core_version'])


if __name__ == '__main__':
    unittest.main()
