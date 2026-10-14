"""NanoKVM-style rounded controls, warm dark surfaces and readable status details."""
from datetime import datetime
from functools import lru_cache
import math
import time

from .font import blend565, rgb565

BG = rgb565("#191918")
PANEL = rgb565("#2D2D29")
CONTROL = rgb565("#414039")
WHITE = rgb565("#F7F7F5")
MUTED = rgb565("#B5B5AD")
ACCENT = rgb565("#307FFA")
SUCCESS = rgb565("#81CEA5")
ERROR = rgb565("#FF9787")


def visible_rect(fb):
    # Firmware exposes either 284x240 or 240x284 physical scanout. The hidden
    # 14-pixel strip is on the leading edge of the long physical dimension.
    r = fb.rotate % 360
    pw = getattr(fb, "phys_w", fb.height if r % 180 else fb.width)
    ph = getattr(fb, "phys_h", fb.width if r % 180 else fb.height)
    left, top = (14 if pw == 284 else 0), (14 if ph == 284 else 0)
    def logical(x, y):
        if r == 90:
            return y, pw - 1 - x
        if r == 180:
            return pw - 1 - x, ph - 1 - y
        if r == 270:
            return ph - 1 - y, x
        return x, y
    corners = [logical(x, y) for x in (left, pw - 1) for y in (top, ph - 1)]
    x, y = min(p[0] for p in corners), min(p[1] for p in corners)
    return x, y, max(p[0] for p in corners) - x + 1, max(p[1] for p in corners) - y + 1


def cloud_layout(tags, bounds, font):
    x, y, w, h = bounds
    boxes = []
    for tag in tags:
        preferred = 12 + round(12 * tag.get("display_weight", 0.5))
        placed = False
        for size in range(preferred, 11, -2):
            label = font.fit(tag["label"], size, w - 8)
            width, height = font.width(label, size) + 8, size + 4
            if not label:
                continue
            positions = [(px, py) for py in range(y, y + h - height + 1, 4)
                         for px in range(x, x + w - width + 1, 4)]
            positions.sort(key=lambda p: ((p[0] + width / 2 - x - w / 2) ** 2 +
                                          1.4 * (p[1] + height / 2 - y - h / 2) ** 2, p[1], p[0]))
            for px, py in positions:
                if all(px + width <= b["x"] or b["x"] + b["w"] <= px or
                       py + height <= b["y"] or b["y"] + b["h"] <= py for b in boxes):
                    boxes.append({"x": px, "y": py, "w": width, "h": height, "size": size,
                                  "label": label, "category": tag["category"]})
                    placed = True
                    break
            if placed:
                break
    return boxes


@lru_cache(maxsize=96)
def rounded_spans(w, h, radius, color, background):
    radius = min(radius, w / 2, h / 2)
    spans = []
    for row in range(h):
        dy = max(radius - row - .5, row + .5 - (h - radius), 0)
        edge = radius - math.sqrt(max(0, radius * radius - dy * dy)) if dy else 0
        inset = math.ceil(edge)
        spans.append((inset, row, w - 2 * inset, color))
        if inset:
            alpha = round((inset - edge) * 7)
            if alpha:
                shade = blend565(color, background, alpha)
                spans.extend(((inset - 1, row, 1, shade), (w - inset, row, 1, shade)))
    return spans


class View:
    def __init__(self, fb, font):
        self.fb, self.font = fb, font
        self.origin_x, self.origin_y, self.width, self.height = visible_rect(fb)
        self.page = "cloud"
        self.buttons = []
        self.layout_key = None
        self.layout = []
        self.pressed_action = None
        self.pressed_until = 0
        self.render_key = None
        self.device_offset = 0

    def text(self, x, y, text, size=14, color=WHITE, background=BG):
        self.font.draw(self.fb, self.origin_x + int(x), self.origin_y + int(y), text, size, color, background)

    def center(self, y, text, size=14, color=WHITE, background=BG):
        self.text((self.width - self.font.width(text, size)) // 2, y, text, size, color, background)

    def box(self, x, y, w, h, color=PANEL):
        self.fb.fill_rect(self.origin_x + int(x), self.origin_y + int(y), int(w), int(h), color)

    def rounded(self, x, y, w, h, radius=20, color=PANEL, background=BG):
        for dx, dy, width, shade in rounded_spans(int(w), int(h), radius, color, background):
            if width > 0:
                self.box(x + dx, y + dy, width, 1, shade)

    def line(self, x0, y0, x1, y1, color=WHITE, thickness=2):
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        for step in range(steps + 1):
            self.box(round(x0 + (x1 - x0) * step / steps), round(y0 + (y1 - y0) * step / steps), thickness, thickness, color)

    def icon(self, name, x, y, color=WHITE, background=CONTROL):
        if name == "back":
            self.line(x + 13, y + 4, x + 5, y + 12, color, 3)
            self.line(x + 5, y + 12, x + 13, y + 20, color, 3)
        elif name == "settings":
            for row, knob in ((5, 7), (12, 16), (19, 10)):
                self.line(x + 3, y + row, x + 22, y + row, color)
                self.rounded(x + knob - 3, y + row - 3, 8, 8, 4, color, background)
        elif name == "chevron":
            self.line(x + 6, y + 7, x + 11, y + 12, color)
            self.line(x + 11, y + 12, x + 6, y + 17, color)

    def button(self, x, y, w, h, label, action, color=CONTROL, size=15, icon=None):
        if self.pressed_action == action and time.monotonic() < self.pressed_until:
            color = blend565(WHITE, color, 1)
        self.rounded(x, y, w, h, h // 2, color)
        if icon:
            self.icon(icon, x + (w - 26) // 2, y + (h - 26) // 2, background=color)
        else:
            self.text(x + (w - self.font.width(label, size)) // 2, y + (h - size) // 2 - 2,
                      label, size, background=color)
        self.buttons.append((x, y, w, h, action))

    def hit(self, x, y):
        x -= self.origin_x
        y -= self.origin_y
        for bx, by, w, h, action in self.buttons:
            if bx <= x < bx + w and by <= y < by + h:
                self.pressed_action, self.pressed_until = action, time.monotonic() + .22
                return action
        return None

    def heading(self, title, back="back"):
        self.button(12, 8, 44, 44, "返回", back, PANEL, icon="back")
        self.text(70, 18, title, 20)

    def wrapped(self, text, x, y, width, size=14, lines=3, background=BG):
        rest = text
        for _ in range(lines):
            take = 0
            while take < len(rest) and self.font.width(rest[:take + 1], size) <= width:
                take += 1
            if not take:
                break
            line, rest = rest[:take], rest[take:]
            self.text(x, y, line, size, background=background)
            y += size + 6
            if not rest:
                break

    def draw(self, settings, state, tags, count=0, remote_ready=False, diagnostic=None, clock_text=None,
             devices=None, device_reason=""):
        diagnostic = diagnostic or {}
        devices = devices or []
        clock_text = clock_text or datetime.now().strftime("%H:%M")
        tag_key = tuple((t["label"], t["category"], round(t["display_weight"], 1)) for t in tags)
        palette_key = tuple((key, item["name"], item["color"]) for key, item in settings["palette"].items())
        key = (self.page, settings["paused"], settings["show_cloud"], settings["backend"],
               settings["interval_seconds"], state, tag_key, palette_key, count, remote_ready,
               str(diagnostic), clock_text, settings["device_id"], settings["device_name"],
               tuple((d["id"], d["name"]) for d in devices), self.device_offset, device_reason,
               self.pressed_action if time.monotonic() < self.pressed_until else None)
        if key == self.render_key:
            return
        self.render_key = key
        self.fb.clear(BG)
        self.buttons = []
        w, h = self.width, self.height
        if self.page == "devices":
            self.heading("当前设备")
            self.center(54, "选择后开始记录 · 每台单独保存", 11, MUTED)
            visible = devices[self.device_offset:self.device_offset + 3]
            for i, device in enumerate(visible):
                selected = device["id"] == settings["device_id"]
                label = self.font.fit(device["name"], 15, w - 70)
                self.button(18, 76 + i * 39, w - 36, 34, label,
                            "device:" + device["id"], ACCENT if selected else CONTROL, 15)
            bw = (w - 44) // 2
            self.button(18, h - 38, bw, 30, "新设备", "new_device", PANEL, 12)
            self.button(26 + bw, h - 38, bw, 30, "下一页" if len(devices) > 3 else "返回",
                        "device_next" if len(devices) > 3 else "back", PANEL, 12)
            return
        if self.page == "settings":
            self.heading("日记设置")
            self.rounded(12, 62, w - 24, 46, 23, CONTROL)
            self.text(27, 75, "采集频率", 14, background=CONTROL)
            value = f"{settings['interval_seconds']} 秒"
            self.text(w - 48 - self.font.width(value, 17), 72, value, 17, background=CONTROL)
            self.icon("chevron", w - 38, 73, MUTED, CONTROL)
            self.buttons.append((12, 62, w - 24, 46, "interval"))
            self.rounded(12, 116, w - 24, 52, 22, CONTROL)
            self.text(27, 122, "分析方式", 11, MUTED, CONTROL)
            backend = "Go+ 本地识字" if settings["backend"] == "local_ocr" else "远程视觉模型"
            self.text(27, 140, backend, 16, background=CONTROL)
            self.icon("chevron", w - 38, 130, MUTED, CONTROL)
            self.buttons.append((12, 116, w - 24, 52, "backend"))
            hint = "离线识字 · 基础标签" if settings["backend"] == "local_ocr" else ("模型已配置 · 完整画面理解" if remote_ready else "请在网页应用配置中填写模型")
            self.center(173, hint, 11, MUTED)
            self.button(30, h - 50, w - 60, 40, "标签颜色", "legend", PANEL, 14)
            return
        if self.page == "legend":
            self.heading("标签颜色", "settings")
            row_h = min(35, (h - 128) // 4)
            cw = (w - 32) // 2
            for i, item in enumerate(list(settings["palette"].values())[:7]):
                x, y = 12 + (i % 2) * (cw + 8), 63 + (i // 2) * (row_h + 7)
                color = rgb565(item["color"])
                self.rounded(x, y, cw, row_h, 14, PANEL)
                self.rounded(x + 10, y + row_h // 2 - 4, 8, 8, 4, color, PANEL)
                label = self.font.fit(item["name"], 12, cw - 31)
                self.text(x + 25, y + (row_h - 12) // 2 - 1, label, 12, background=PANEL)
            self.center(h - 39, "字号表示近期权重", 12, MUTED)
            self.center(h - 21, "只描述画面内容，不判断你的心情", 11, MUTED)
            return
        if self.page == "status":
            self.heading("运行状态")
            self.rounded(12, 64, w - 24, h - 126, 22, PANEL)
            if diagnostic:
                recovered = state.startswith("已记录")
                self.text(26, 75, "上次异常 · 已恢复" if recovered else "最近一次异常", 13,
                          SUCCESS if recovered else ERROR, PANEL)
                self.wrapped(diagnostic.get("error", state), 26, 100, w - 52, 14, 3, PANEL)
                stamp = diagnostic.get("at", "")
                self.text(26, h - 83, self.font.fit(diagnostic.get("code", ""), 10, w - 143), 10, MUTED, PANEL)
                if stamp:
                    try:
                        stamp = datetime.fromisoformat(stamp).astimezone().strftime("%m-%d %H:%M:%S")
                    except ValueError:
                        stamp = ""
                    self.text(w - 26 - self.font.width(stamp, 10), h - 83, stamp, 10, MUTED, PANEL)
            else:
                self.text(26, 80, "运行正常", 17, SUCCESS, PANEL)
                self.wrapped(state, 26, 111, w - 52, 14, 2, PANEL)
            self.button(36, h - 54, w - 72, 44, "立即重试" if diagnostic and not state.startswith("已记录") else "立即记录", "retry", ACCENT)
            return

        self.text(16, 12, "桌面日记", 20)
        self.text(w - 17 - self.font.width(clock_text, 15), 15, clock_text, 15)
        failed = not settings["paused"] and not any(state.startswith(prefix) for prefix in ("已记录", "正在", "准备", "已暂停"))
        label = "待选设备" if not settings["device_id"] else "已暂停" if settings["paused"] else (self.font.fit(state.split("，", 1)[0], 12, 88) if failed else ("分析中" if state == "正在分析" else "记录正常"))
        color = MUTED if settings["paused"] else ERROR if failed else ACCENT
        self.rounded(12, 43, w - 24, 28, 14, PANEL)
        self.rounded(23, 54, 6, 6, 3, color, PANEL)
        self.text(35, 49, label, 12, color, PANEL)
        mode = ("本地" if settings["backend"] == "local_ocr" else "远程") + f" · {settings['interval_seconds']}秒"
        self.text(w - 25 - self.font.width(mode, 12), 49, mode, 12, MUTED, PANEL)
        self.buttons.append((12, 39, w - 24, 36, "status" if settings["device_id"] else "devices"))
        cy, ch = 79, h - 145
        self.rounded(12, cy, w - 24, ch, 22, PANEL)
        if not settings["device_id"]:
            self.center(cy + max(4, (ch - 53) // 2), "确认当前连接的设备", 16, WHITE, PANEL)
            hint = "连接有变化，请重新选择" if device_reason.startswith("连接") else "手机和电脑，分别记下"
            self.center(cy + max(4, (ch - 53) // 2) + 24, hint, 11, MUTED, PANEL)
        elif not settings["show_cloud"]:
            self.center(cy + max(6, (ch - 55) // 2), "标签已隐藏", 18, WHITE, PANEL)
            self.center(cy + max(6, (ch - 55) // 2) + 27, "点击这里恢复标签", 12, ACCENT, PANEL)
            # Keep the device footer separate from the recovery tap target.
            self.buttons.append((12, cy, w - 24, ch - 27, "show_cloud"))
        elif tags:
            bounds = (22, cy + 6, w - 44, ch - 25)
            layout_key = tag_key, bounds
            if layout_key != self.layout_key:
                self.layout = cloud_layout(tags, bounds, self.font)
                self.layout_key = layout_key
            for tag in self.layout:
                color = settings["palette"].get(tag["category"], settings["palette"]["neutral"])["color"]
                self.text(tag["x"] + 4, tag["y"], tag["label"], tag["size"], rgb565(color), PANEL)
        else:
            self.center(cy + max(4, (ch - 50) // 2), "记录屏幕的每一刻", 17, WHITE, PANEL)
            self.center(cy + max(4, (ch - 50) // 2) + 25, "开始后，这里会出现标签", 11, MUTED, PANEL)
        # The cloud footer is a direct device switcher, present even when hidden.
        name = self.font.fit(settings["device_name"], 11, w - 137)
        footer = f"{name} · 今天 {count} 条" if settings["device_id"] else "点击选择设备"
        self.center(cy + ch - 20, footer, 11, MUTED, PANEL)
        self.icon("chevron", w - 35, cy + ch - 26, MUTED, PANEL)
        self.buttons.append((12, cy + ch - 27, w - 24, 27, "devices"))
        primary = w - 142
        self.button(12, h - 56, primary, 44, "选择设备" if not settings["device_id"] else "开始记录" if settings["paused"] else "停止分析", "pause", ACCENT, 15)
        self.button(20 + primary, h - 56, 58, 44,
                    "显示标签" if not settings["show_cloud"] else "仅记录",
                    "show_cloud" if not settings["show_cloud"] else "hide_cloud", CONTROL, 12)
        self.button(w - 56, h - 56, 44, 44, "设置", "settings", CONTROL, icon="settings")
