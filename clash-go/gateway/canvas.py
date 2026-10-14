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


class Canvas:
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
