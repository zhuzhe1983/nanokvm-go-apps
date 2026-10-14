#!/usr/bin/env python3
"""Touch UI is a client; closing it does not stop USB networking."""
import json
import signal
import ssl
import threading
import time
import urllib.request

from appbase import app
from gateway.common import private_keys
from gateway.font import Font, rgb565
from gateway.canvas import Canvas, BG, PANEL, CONTROL, ACCENT, WHITE, MUTED, SUCCESS, ERROR


class Client:
    def __init__(self):
        self.keys = private_keys()
        self.context = ssl.create_default_context(cafile="/etc/kvm/server.crt")
        self.status = {}
        self.error = ""
        self.disconnected = False
        self.busy = False
        self.latency_names = ()
        self.force_latency = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.poll, daemon=True)
        self.thread.start()

    def request(self, path, body=None):
        req = urllib.request.Request("https://localhost:8443" + path,
              json.dumps(body).encode() if body is not None else None,
              {"Authorization": "Bearer " + self.keys["api"], "Content-Type": "application/json", "X-Clash-Go": "1"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.context))
        try:
            with opener.open(req, timeout=60 if body is not None else 5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            return {"error": json.load(error).get("error", "操作失败")}

    def poll(self):
        while not self.stop.is_set():
            try:
                self.status = self.request("/api/status")
                self.keys = private_keys()
                if self.disconnected:
                    self.error = ""
                self.disconnected = False
                names = [n for n in self.latency_names if n in self.status.get("nodes", [])]
                if names and self.status.get("running") and not self.busy:
                    force, self.force_latency = self.force_latency, False
                    try:
                        self.request("/api/latency", {"names": names, "force": force})
                    except Exception:
                        pass  # Optional probes never turn a working connection into an error.
            except Exception:
                self.error = "网络服务未连接"
                self.disconnected = True
            self.stop.wait(2)

    def action(self, name, value):
        if self.busy:
            return
        self.busy = True
        def work():
            try:
                result = self.request("/api/change", {"action": name, "value": value})
                self.error = result.get("error", "")
                self.status = self.request("/api/status")
            except Exception:
                self.error = "操作超时，请查看网页状态"
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()


class View(Canvas):
    def __init__(self, fb, font):
        super().__init__(fb, font)
        self.page = "home"
        self.offset = 0

    def draw(self, state, error, busy, pin):
        key = self.page, str(state), error, busy, self.offset, pin if self.page == "web" else ""
        if self.render_key == key:
            return
        self.render_key = key
        self.fb.clear(BG); self.buttons = []
        w, h = self.width, self.height
        if self.page == "web":
            self.heading("网页配置")
            addr = state.get("interfaces", {}).get("usb0", "").split("/")[0] or "kvm-337b.local"
            self.rounded(12, 65, w - 24, h - 85, 22, PANEL)
            self.center(77, "浏览器打开 HTTPS 地址", 12, MUTED, PANEL)
            self.center(98, addr + ":8443", 15, WHITE, PANEL)
            wifi = state.get("interfaces", {}).get("wlan0", "").split("/")[0]
            if wifi:
                self.center(119, wifi + ":8443", 12, MUTED, PANEL)
            self.center(140, "6 位数字配置码", 11, MUTED, PANEL)
            self.center(156, pin, 25, ACCENT, PANEL)
            self.center(h - 41, "订阅 · 节点 · Wi-Fi", 12, MUTED, PANEL)
            return
        if self.page == "network":
            self.heading("网络配置")
            sharing = state.get("sharing", False)
            self.button(16, 66, w - 32, 44, "USB 共享：" + ("已开启" if sharing else "已关闭"), "sharing", ACCENT if sharing else CONTROL, 15)
            ssid = state.get("wifi", {}).get("ssid") or "Wi-Fi 未连接"
            self.center(120, self.font.fit(ssid, 14, w - 36), 14)
            ip = state.get("interfaces", {}).get("wlan0", "").split("/")[0]
            self.center(143, ip or "等待网络地址", 12, MUTED)
            if error:
                self.center(161, self.font.fit(error, 11, w - 32), 11, ERROR)
            self.button(26, h - 57, w - 52, 42, "Wi-Fi / DNS / 订阅设置", "web", PANEL, 13)
            return
        if self.page == "nodes":
            self.heading("选择节点")
            nodes = state.get("nodes", [])
            if not nodes:
                self.center(90, "先导入你的节点订阅", 16)
                self.center(118, "支持 oixCloud 的 Clash 订阅", 11, MUTED)
                self.button(30, h - 59, w - 60, 42, "打开配置入口", "web", ACCENT, 14)
                return
            if self.offset >= len(nodes):
                self.offset = 0
            for i, name in enumerate(nodes[self.offset:self.offset + 3]):
                y = 66 + i * 40
                action = "node:" + str(self.offset + i)
                color = ACCENT if state.get("selected") == name else CONTROL
                self.button(18, y, w - 36, 35, "", action, color, 13)
                self.text(30, y + 9, self.font.fit(name, 13, w - 132), 13, WHITE, color)
                result = state.get("latencies", {}).get(name, {})
                status, ms = result.get("state"), result.get("ms")
                label, shade = {"pending": ("测速中", MUTED), "timeout": ("超时", ERROR),
                                "failed": ("失败", ERROR)}.get(status, ("未测", MUTED))
                if status == "ok" and isinstance(ms, int):
                    label = str(ms) + "ms"
                    shade = SUCCESS if ms < 300 else rgb565("#F7CC76") if ms < 800 else ERROR
                self.text(w - 30 - self.font.width(label, 11), y + 10, label, 11, shade, color)
            testing = state.get("latency_busy", False)
            bw = (w - 46) // 2
            self.button(18, h - 46, bw, 36, "测速中" if testing else "刷新延迟", "measure", PANEL if testing else CONTROL, 12)
            self.button(28 + bw, h - 46, bw, 36, f"下一页 {self.offset // 3 + 1}/{(len(nodes) + 2) // 3}", "next", PANEL, 12)
            return
        self.text(16, 12, "Clash Go", 22)
        self.button(w - 71, 9, 59, 30, "Apps", "exit", PANEL, 12)
        enabled = state.get("enabled", False)
        self.rounded(12, 47, w - 24, 75, 22, PANEL)
        label = "正在应用…" if busy else "代理已开启" if enabled else "代理已关闭"
        self.text(26, 58, label, 18, SUCCESS if enabled else WHITE, PANEL)
        desc = error or state.get("error") or (state.get("selected") if enabled else "USB 共享 " + ("开启" if state.get("sharing") else "关闭"))
        self.text(26, 88, self.font.fit(desc or "等待配置", 12, w - 52), 12, ERROR if error else MUTED, PANEL)
        self.button(12, 133, w - 24, 43, "关闭代理" if enabled else "开启代理", "toggle", ACCENT, 16)
        bw = (w - 32) // 2
        self.button(12, h - 53, bw, 40, "节点", "nodes", CONTROL, 14)
        self.button(20 + bw, h - 53, bw, 40, "网络配置", "network", CONTROL, 14)


@app()
def main(ctx):
    previous = signal.getsignal(signal.SIGTERM)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    font = Font(); client = Client(); view = View(ctx.fb, font)
    try:
        def tap(x, y):
            action = view.hit(x, y)
            state = client.status
            if action in ("nodes", "network", "web"):
                view.page = action
            elif action == "back":
                view.page = "home"
            elif action == "toggle":
                if not state.get("subscription_saved"):
                    view.page = "web"
                else:
                    client.action("enabled", not state.get("enabled"))
            elif action == "sharing":
                client.action("sharing", not state.get("sharing"))
            elif action == "next":
                view.offset = view.offset + 3 if view.offset + 3 < len(state.get("nodes", [])) else 0
            elif action == "measure":
                if not state.get("latency_busy"):
                    client.force_latency = True
            elif action == "exit":
                raise KeyboardInterrupt
            elif action and action.startswith("node:"):
                index = int(action.split(":")[1])
                if index < len(state.get("nodes", [])):
                    client.action("node", state["nodes"][index])
                    view.page = "home"
        def tick(dt):
            nodes = client.status.get("nodes", [])
            if view.offset >= len(nodes):
                view.offset = 0
            client.latency_names = tuple(nodes[view.offset:view.offset + 3]) if view.page == "nodes" else ()
            view.draw(client.status, client.error, client.busy, client.keys["pin"])
        ctx.run(tick, fps=8, on_tap=tap)
    finally:
        client.stop.set(); client.thread.join(timeout=6)
        font.close(); signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
