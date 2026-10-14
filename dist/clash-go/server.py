#!/usr/bin/env python3
"""Local HTTPS control panel. Credentials never appear in request URLs or logs."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import secrets
import signal
import socket
import ssl
import threading
import time

from gateway.common import APP, UserError
from gateway.engine import Engine
from gateway.network import addresses, wifi_scan


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine, tls_context=None, client_timeout=5):
        super().__init__(address, Handler)
        self.engine = engine
        self.sessions = {}
        self.attempts = {}
        self.tls_context = tls_context
        self.client_timeout = client_timeout

    def finish_request(self, request, client_address):
        # This runs in the request worker, not the shared accept loop. A browser
        # may preconnect without sending a ClientHello, or use plain HTTP here.
        request.settimeout(self.client_timeout)
        if self.tls_context is None:
            return super().finish_request(request, client_address)
        try:
            with self.tls_context.wrap_socket(request, server_side=True) as secure:
                super().finish_request(secure, client_address)
        except (ssl.SSLError, TimeoutError, ConnectionError):
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value, cookie=None, content_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass

    def trusted_host(self):
        host = self.headers.get("Host", "").lower().split(":", 1)[0]
        allowed = {"localhost", "127.0.0.1", socket.gethostname().lower(), socket.gethostname().lower() + ".local"}
        allowed.update(v.split("/")[0] for v in addresses().values() if v)
        return host in allowed

    def authenticated(self):
        bearer = self.headers.get("Authorization", "")
        if hmac.compare_digest(bearer, "Bearer " + self.server.engine.keys["api"]):
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            session = cookie["clash_go"].value
            return self.server.sessions.get(session, 0) > time.monotonic()
        except (KeyError, ValueError):
            return False

    def do_GET(self):
        if not self.trusted_host():
            return self.reply(403, {"error": "访问地址不匹配"})
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in files:
            name, mime = files[self.path]
            return self.reply(200, (APP / "web" / name).read_bytes(), content_type=mime)
        if not self.authenticated():
            return self.reply(401, {"error": "请输入设备小屏上的配置码"})
        try:
            if self.path == "/api/status":
                return self.reply(200, self.server.engine.status())
            if self.path == "/api/wifi":
                return self.reply(200, {"networks": wifi_scan()})
            return self.reply(404, {"error": "接口不存在"})
        except Exception:
            return self.reply(503, {"error": "暂时无法读取设备网络状态"})

    def do_POST(self):
        if not self.trusted_host():
            return self.reply(403, {"error": "访问地址不匹配"})
        origin = self.headers.get("Origin")
        if origin and origin != "https://" + self.headers.get("Host", ""):
            return self.reply(403, {"error": "请在本机配置页面操作"})
        if self.headers.get("X-Clash-Go") != "1":
            return self.reply(403, {"error": "请求缺少校验头"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16384:
                raise ValueError()
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            return self.reply(400, {"error": "请求格式错误"})
        engine = self.server.engine
        if self.path == "/api/login":
            now = time.monotonic()
            ip = self.client_address[0]
            attempts = [t for t in self.server.attempts.get(ip, []) if now - t < 60]
            if len(attempts) >= 5:
                return self.reply(429, {"error": "尝试过多，请一分钟后重试"})
            self.server.attempts[ip] = attempts + [now]
            pin = body.get("pin", "")
            if not isinstance(pin, str) or not hmac.compare_digest(pin.strip().upper(), engine.keys["pin"]):
                return self.reply(401, {"error": "配置码不正确，请查看设备小屏"})
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v > now}
            if len(self.server.sessions) >= 32:
                self.server.sessions.pop(next(iter(self.server.sessions)))
            token = secrets.token_urlsafe(32)
            self.server.sessions[token] = now + 8 * 3600
            return self.reply(200, {"ok": True}, f"clash_go={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=28800")
        if not self.authenticated():
            return self.reply(401, {"error": "请先登录配置页面"})
        try:
            if self.path == "/api/change":
                engine.change(body.get("action"), body.get("value"))
            elif self.path == "/api/latency":
                started = engine.measure_latency(body.get("names"), body.get("force", False))
                return self.reply(202, {"started": started})
            elif self.path == "/api/wifi":
                engine.connect_wifi(body.get("ssid"), body.get("password"))
            elif self.path == "/api/wifi-confirm":
                if type(body.get("keep")) is not bool:
                    raise UserError("确认格式错误")
                engine.finish_wifi(body["keep"])
            elif self.path == "/api/logout":
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                if "clash_go" in cookie:
                    self.server.sessions.pop(cookie["clash_go"].value, None)
                return self.reply(200, {"ok": True}, "clash_go=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0")
            else:
                return self.reply(404, {"error": "接口不存在"})
            return self.reply(200, {"ok": True})
        except UserError as error:
            return self.reply(400, {"error": str(error)})
        except Exception:
            return self.reply(500, {"error": "操作未完成，请查看运行状态"})


def main():
    os.umask(0o077)
    engine = Engine()
    server = None
    try:
        engine.boot()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain("/etc/kvm/server.crt", "/etc/kvm/server.key")
        server = Server(("0.0.0.0", 8443), engine, tls_context=context)
        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=.3)
    finally:
        if server:
            server.server_close()
        engine.close()


if __name__ == "__main__":
    main()
