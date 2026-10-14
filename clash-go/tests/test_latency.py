from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.latency import Latency, probe
from gateway.common import UserError
from gateway.engine import Engine


class LatencyTests(unittest.TestCase):
    def test_correct_provider_path_encoding_and_delay(self):
        response = Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'{"delay":274}'
        opener=Mock();opener.open.return_value=response
        with patch('gateway.latency.urllib.request.build_opener',return_value=opener):
            self.assertEqual(probe('private','oixCloud','香港 / A?'),{'state':'ok','ms':274})
        req=opener.open.call_args.args[0]
        self.assertIn('/providers/proxies/oixCloud/%E9%A6%99%E6%B8%AF%20%2F%20A%3F/healthcheck?',req.full_url)
        self.assertNotIn('private',req.full_url)
        self.assertIn('timeout=8000',req.full_url)

    def test_timeout_failure_and_invalid_delay_are_distinct(self):
        opener=Mock()
        with patch('gateway.latency.urllib.request.build_opener',return_value=opener):
            for code,expected in [(504,'timeout'),(503,'failed'),(404,'failed')]:
                opener.open.side_effect=urllib.error.HTTPError('http://localhost',code,'private diagnostic',{},None)
                self.assertEqual(probe('key','account','node'),{'state':expected,'ms':None})
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            opener.open.side_effect=None;opener.open.return_value=response
            for payload in [b'{"delay":0}',b'{"delay":true}',b'{"delay":"200"}',b'[]']:
                response.read.return_value=payload
                self.assertEqual(probe('key','account','node')['state'],'failed')

    def test_cache_and_force_refresh_do_not_repeat_network_requests(self):
        fn=Mock(return_value={'state':'ok','ms':42});latency=Latency(fn)
        self.assertTrue(latency.request(['a'],'account'));latency.worker.join(1)
        self.assertEqual(latency.snapshot(['a'])[0]['a']['ms'],42)
        self.assertFalse(latency.request(['a'],'account'))
        fn.assert_called_once_with('account','a')
        self.assertTrue(latency.request(['a'],'account',force=True));latency.worker.join(1)
        self.assertEqual(fn.call_count,2)
        with patch('gateway.latency.time.time',return_value=10**12):
            self.assertTrue(latency.request(['a'],'account'));latency.worker.join(1)
        self.assertEqual(fn.call_count,3)

    def test_reset_discards_old_account_results_and_limits_inflight_work(self):
        release=threading.Event();two_started=threading.Event();lock=threading.Lock();calls=[]
        def fn(provider,name):
            with lock:
                calls.append(name)
                if len(calls)==2:two_started.set()
            release.wait(2)
            return {'state':'ok','ms':100}
        latency=Latency(fn)
        try:
            self.assertTrue(latency.request(['a','b','c'],'account'))
            self.assertTrue(two_started.wait(1))
            self.assertFalse(latency.request(['d'],'account',force=True))
            latency.reset()
            self.assertFalse(latency.request(['a'],'oixCloud'))
            self.assertEqual(latency.snapshot(['a'])[0]['a']['state'],'untested')
        finally:
            release.set();latency.worker.join(2)
        self.assertEqual(len(calls),2)
        self.assertFalse(latency.snapshot(['a'])[1])
        self.assertEqual(latency.snapshot(['a'])[0]['a']['state'],'untested')

    def test_engine_checks_membership_and_preserves_selected_node_and_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=Engine(Path(folder));engine.nodes=Mock(return_value=(['a','b','c'],'b'))
            engine.net=Mock();engine.settings.update(source='oix',selected='b',sharing=True,enabled=True)
            engine.latency=Mock();engine.latency.request.return_value=True
            with self.assertRaises(UserError):engine.measure_latency(['other'])
            with self.assertRaises(UserError):engine.measure_latency(['a']*4)
            with self.assertRaises(UserError):engine.measure_latency(['a'],force='true')
            self.assertTrue(engine.measure_latency(['a','c'],True))
            engine.latency.request.assert_called_once_with(['a','c'],'oixCloud',True)
            engine.net.apply.assert_not_called()
            self.assertEqual(engine.settings['selected'],'b');self.assertTrue(engine.settings['enabled'])


if __name__=='__main__':unittest.main()
