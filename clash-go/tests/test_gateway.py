import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import socket
import ssl
import subprocess
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.common import DEFAULTS, UserError, private_keys, subscription_url
from gateway.engine import Engine, core_config
from gateway.network import Network
from server import Server


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_settings_do_not_claim_free_or_premium_nodes(self):
        engine = Engine(self.root)
        engine.net = Mock()
        with self.assertRaisesRegex(UserError, "订阅"):
            engine.change("enabled", True)
        self.assertFalse(engine.settings["enabled"])
        self.assertFalse(engine.settings["sharing"])
        self.assertEqual(engine.nodes(), ([], ""))

    def test_core_routes_are_scoped_and_api_is_loopback(self):
        cfg = core_config(DEFAULTS, "test-secret", self.root, "10.9.8.1")
        self.assertFalse(cfg["tun"]["auto-route"])
        self.assertFalse(cfg["tun"]["auto-redirect"])
        self.assertEqual(cfg["external-controller"], "127.0.0.1:9090")
        self.assertEqual(cfg["bind-address"], "127.0.0.1")
        self.assertEqual(cfg["dns"]["listen"], "10.9.8.1:1053")
        self.assertFalse(cfg["ipv6"])
        self.assertEqual(cfg["proxy-groups"][0]["proxies"], ["REJECT"])

    def test_subscription_rejects_plain_http_and_url_credentials(self):
        for value in ("http://example.com/x", "https://user:pass@example.com/x", "file:///tmp/a", "https://example.com/a\nsecret"):
            with self.assertRaises(UserError):
                subscription_url(value)
        value = "https://example.com/sub?token=private-value"
        self.assertEqual(subscription_url(value), value)
        keys = private_keys(self.root)
        self.assertRegex(keys["pin"], r"^[0-9]{6}$")
        self.assertEqual((self.root / "keys.json").stat().st_mode & 0o777, 0o600)

    def test_pin_migration_preserves_api_and_is_stable(self):
        from gateway.common import write
        previous = {"pin": "ABCDEFGHIJ", "api": "preserve-this-api-key"}
        write(self.root / "keys.json", previous)
        self.assertEqual(private_keys(self.root), previous)
        with patch("gateway.common.secrets.randbelow", return_value=1234):
            migrated = private_keys(self.root, shorten_pin=True)
        self.assertEqual(migrated["pin"], "001234")
        self.assertEqual(migrated["api"], previous["api"])
        self.assertEqual(private_keys(self.root, shorten_pin=True), migrated)
        self.assertEqual(private_keys(self.root), migrated)

    def test_proxy_disable_preserves_sharing_and_sharing_disable_disables_proxy(self):
        engine = Engine(self.root); engine.net = Mock()
        engine.settings.update(enabled=True, sharing=True)
        engine.change("enabled", False)
        engine.net.apply.assert_called_with(True, False)
        engine.settings["enabled"] = True
        engine.change("sharing", False)
        engine.net.apply.assert_called_with(False, False)

    def test_network_failure_restores_settings(self):
        engine = Engine(self.root); engine.net = Mock()
        engine.net.apply.side_effect = [UserError("failure"), None]
        with self.assertRaises(UserError):
            engine.change("sharing", True)
        self.assertFalse(engine.settings["sharing"])
        engine.net.apply.assert_called_with(False, False)

    def test_firewall_does_not_flush_foreign_chains_and_has_dead_tun_route(self):
        calls = []
        def record(args, **kwargs):
            calls.append(args)
            return Mock(returncode=0, stdout="")
        from gateway.common import write
        write(self.root / "network-before.json", {"ip_forward": 0})
        with patch("gateway.network.command", side_effect=record), patch("gateway.network.addresses", return_value={"usb0":"10.9.8.1/24"}):
            Network(self.root).apply(True, True)
        self.assertTrue(any("unreachable" in args for args in calls))
        self.assertTrue(any("rule" in args and "add" in args and "usb0" in args for args in calls))
        for args in calls:
            if "-F" in args:
                self.assertTrue(args[args.index("-F") + 1].startswith("CG_"))
            if args[:3] == ["ip", "route", "flush"]:
                self.assertEqual(args[-1], "2023")

    def test_public_status_never_contains_keys_or_subscription(self):
        engine = Engine(self.root)
        engine.settings["subscription_url"] = "https://example.com/?token=private-sub"
        with patch("gateway.engine.addresses", return_value={}), patch("gateway.engine.wifi_status", return_value={}):
            status = json.dumps(engine.status())
        for secret in ("private-sub", engine.keys["pin"], engine.keys["api"]):
            self.assertNotIn(secret, status)

    def test_dns_hook_preserves_pihole_precedence(self):
        pg = '-A PREROUTING -i usb0 -j PG_DNS'
        for existing, expected in ((pg, '2'), ('', '1'), ('-A PREROUTING -j FOREIGN\n' + pg, '3')):
            calls = []
            def ipt(table, *args, **kwargs):
                calls.append((table, *args))
                return Mock(returncode=0 if '-S' in args else 1, stdout='-P PREROUTING ACCEPT\n' + existing)
            net = Network(self.root)
            net.ipt = ipt
            net.hook('nat', 'PREROUTING', ['-i', 'usb0', '-j', 'CG_DNS'])
            self.assertEqual(calls[-1], ('nat', '-I', 'PREROUTING', expected, '-i', 'usb0', '-j', 'CG_DNS'))

    def test_controller_readiness_waits_for_tun(self):
        engine = Engine(self.root)
        engine.api = Mock(return_value={"type": "Selector"})
        process = Mock(); process.poll.return_value = None
        with patch("gateway.engine.addresses", return_value={}), \
             patch("gateway.engine.subprocess.run", return_value=Mock(returncode=0)), \
             patch("gateway.engine.subprocess.Popen", return_value=process), \
             patch("gateway.engine.Path.exists", side_effect=[False, True]), \
             patch("gateway.engine.time.sleep") as wait:
            engine.start_core()
        self.assertEqual(engine.api.call_count, 2)
        wait.assert_called_once_with(.1)

    def test_restart_rolls_back_unconfirmed_wifi(self):
        from gateway.common import write
        write(self.root / "wifi-pending.json", {"old": "0", "new": "3"})
        engine = Engine(self.root); engine.net = Mock()
        with patch("gateway.engine.command", return_value=Mock(stdout="OK")) as cmd:
            engine.boot()
        self.assertEqual([call.args[0] for call in cmd.call_args_list], [
            ["wpa_cli", "-i", "wlan0", "remove_network", "3"],
            ["wpa_cli", "-i", "wlan0", "select_network", "0"]])
        self.assertFalse((self.root / "wifi-pending.json").exists())

    def test_wifi_confirm_must_match_new_network(self):
        engine = Engine(self.root); engine.wifi_pending = {"old": "0", "new": "2"}
        with patch("gateway.engine.wifi_status", return_value={"wpa_state":"COMPLETED", "id":"0"}):
            with self.assertRaises(UserError):
                engine.finish_wifi(True)
        self.assertIsNotNone(engine.wifi_pending)

    def test_http_auth_origin_and_cookie_attributes(self):
        engine = Mock()
        engine.keys = {"api":"test-api", "pin":"ABCDEFGHIJ"}
        engine.status.return_value = {"enabled":False}
        with patch("server.addresses", return_value={}):
            server = Server(("127.0.0.1", 0), engine)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            try:
                def request(method, path, data=None, headers=None):
                    c=http.client.HTTPConnection("127.0.0.1", server.server_port)
                    try:
                        c.request(method,path,json.dumps(data) if data else None,headers or {})
                        r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
                    finally:c.close()
                self.assertEqual(request("GET","/api/status")[0],401)
                self.assertEqual(request("POST","/api/change",{"action":"sharing","value":True})[0],403)
                self.assertEqual(request("POST","/api/login",{"pin":"ABCDEFGHIJ"},{"X-Clash-Go":"1","Origin":"https://evil.example"})[0],403)
                status, headers, _ = request("POST","/api/login",{"pin":"ABCDEFGHIJ"},{"X-Clash-Go":"1"})
                self.assertEqual(status,200)
                self.assertIn("HttpOnly; Secure; SameSite=Strict",headers["Set-Cookie"])
                cookie=headers["Set-Cookie"].split(";",1)[0]
                self.assertEqual(request("GET","/api/status",headers={"Cookie":cookie})[0],200)
                self.assertEqual(request("POST","/api/change",{"action":"sharing","value":True},{"Cookie":cookie,"X-Clash-Go":"1"})[0],200)
                engine.change.assert_called_once_with("sharing",True)
            finally:
                server.shutdown();server.server_close();worker.join()

    def test_idle_tls_connection_does_not_block_https_and_expires(self):
        cert, key = self.root / "cert.pem", self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                        "-days", "1"], check=True, capture_output=True, timeout=15)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        client_context = ssl.create_default_context(cafile=str(cert))
        engine = Mock(); engine.keys = {"api": "test-api", "pin": "ABCDEFGHIJ"}
        with patch("server.addresses", return_value={}):
            server = Server(("127.0.0.1", 0), engine, context, client_timeout=2)
            worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
            idle = socket.create_connection(server.server_address, timeout=4)
            connection = http.client.HTTPSConnection("localhost", server.server_port,
                                                      context=client_context, timeout=1)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertIn(b"Clash Go", response.read())
                # No data was sent by this peer; the handshake deadline closes it.
                self.assertEqual(idle.recv(1), b"")
            finally:
                idle.close(); connection.close()
                server.shutdown(); server.server_close(); worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
