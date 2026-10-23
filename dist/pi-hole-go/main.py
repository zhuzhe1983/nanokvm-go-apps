#!/usr/bin/env python3
"""Native touch panel; the independent service keeps filtering after exit."""
import json
import signal
import ssl
import threading
import urllib.error
import urllib.request

from appbase import app
from filtering.common import private_keys
from filtering.font import Font
from filtering.canvas import Canvas, BG, PANEL, CONTROL, ACCENT, MUTED, WHITE, SUCCESS, ERROR


class Client:
    def __init__(self):
        self.keys = private_keys()
        self.context = ssl.create_default_context(cafile='/etc/kvm/server.crt')
        self.status, self.error, self.busy = {}, '', False
        self.end = threading.Event()
        self.thread = threading.Thread(target=self.poll, daemon=True)
        self.thread.start()

    def request(self, path, body=None):
        req = urllib.request.Request('https://localhost:8444' + path,
                json.dumps(body).encode() if body is not None else None,
                {'Authorization': 'Bearer ' + self.keys['api'], 'Content-Type': 'application/json', 'X-Pi-Hole-Go': '1'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.context))
        try:
            with opener.open(req, timeout=60 if body is not None else 5) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            result = json.load(error)
        if result.get('error') and path != '/api/status':
            raise RuntimeError(result['error'])
        return result

    def poll(self):
        while not self.end.is_set():
            try:
                self.status = self.request('/api/status')
                self.error = ''
            except Exception:
                self.status = {}
                self.error = '过滤服务未连接'
            self.end.wait(2)

    def action(self, name, value):
        if self.busy:
            return
        self.busy = True
        def work():
            try:
                self.request('/api/change', {'action': name, 'value': value})
                self.status = self.request('/api/status')
                self.error = ''
            except RuntimeError as error:
                self.error = str(error)
            except Exception:
                self.error = '操作未完成，请查看网页状态'
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()


class View(Canvas):
    def __init__(self, fb, font):
        super().__init__(fb, font)
        self.page = 'home'

    def draw(self, state, error, busy, pin):
        key = self.page, str(state), error, busy, pin if self.page == 'web' else ''
        if self.render_key == key:
            return
        self.render_key = key
        self.fb.clear(BG);self.buttons = []
        w, h = self.width, self.height
        if self.page == 'web':
            self.heading('过滤设置')
            self.rounded(12, 63, w - 24, h - 78, 22, PANEL)
            self.center(76, '浏览器打开 HTTPS 地址', 12, MUTED, PANEL)
            ip = state.get('interfaces', {}).get('usb0') or 'kvm-337b.local'
            self.center(97, ip + ':8444', 15, WHITE, PANEL)
            wifi = state.get('interfaces', {}).get('wlan0')
            self.center(121, (wifi + ':8444') if wifi else '6 位数字配置码', 12, MUTED, PANEL)
            self.center(146, pin, 27, ACCENT, PANEL)
            self.center(h - 42, '白名单 · 规则更新 · DNS', 12, MUTED, PANEL)
            return
        self.text(14, 12, 'Pi-hole Go', 21)
        self.button(w - 68, 9, 56, 30, 'Apps', 'exit', PANEL, 12)
        enabled, effective = state.get('enabled', False), state.get('effective', False)
        paused = state.get('paused_seconds', 0)
        label = '正在应用…' if busy else '已暂停 ' + str(paused) + '秒' if paused else '正在拦截' if effective else '等待 USB 网络' if enabled else '拦截已关闭'
        self.rounded(12, 48, w - 24, 93, 20, PANEL)
        self.text(25, 59, label, 16, SUCCESS if effective and not paused else WHITE, PANEL)
        stats = state.get('stats', {})
        self.text(25, 83, str(stats.get('blocked', 0)), 26, WHITE, PANEL)
        self.text(w // 2 + 10, 91, '已拦截 · 近 1 小时', 10, MUTED, PANEL)
        note = error or state.get('error') or ('USB → 过滤 → Clash' if state.get('route') == 'clash' else 'USB → 过滤 → Wi-Fi')
        if not error and not state.get('error') and not state.get('sharing'):
            note = '请在 Clash Go 开启 USB 共享'
        self.text(25, 119, self.font.fit(note, 10, w - 50), 10, ERROR if error or state.get('error') else MUTED, PANEL)
        self.button(12, 151, w - 24, 36, '关闭拦截' if enabled else '开启拦截', 'toggle', ACCENT, 15)
        bw = (w - 32) // 2
        self.button(12, h - 43, bw, 33, '恢复拦截' if paused else '暂停 5 分钟', 'pause', CONTROL, 12)
        self.button(20 + bw, h - 43, bw, 33, '过滤设置', 'web', PANEL, 12)


@app()
def main(ctx):
    previous = signal.getsignal(signal.SIGTERM)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    font = Font();client = Client();view = View(ctx.fb, font)
    try:
        def tap(x, y):
            action = view.hit(x, y)
            if action == 'web':
                view.page = 'web'
            elif action == 'back':
                view.page = 'home'
            elif action == 'toggle' and client.status:
                client.action('enabled', not client.status.get('enabled'))
            elif action == 'pause' and client.status:
                client.action('pause', 0 if client.status.get('paused_seconds') else 300)
            elif action == 'exit':
                raise KeyboardInterrupt
        ctx.run(lambda dt: view.draw(client.status, client.error, client.busy, client.keys['pin']), fps=8, on_tap=tap)
    finally:
        client.end.set();client.thread.join(timeout=6)
        font.close();signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    main()
