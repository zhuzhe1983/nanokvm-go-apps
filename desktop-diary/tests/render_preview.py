"""Render the real UI into PPM files without touching the hardware display.

Uses illustrative tags, not actual model output. No runtime dependencies.
"""
from array import array
import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diary.config import load_settings
from diary.font import Font
from diary.ui import View, visible_rect


class Buffer:
    def __init__(self, rotate=90, physical=(240, 284)):
        self.rotate = rotate
        self.phys_w, self.phys_h = physical
        self.width, self.height = physical[::-1] if rotate % 180 else physical
        self.pixels = array("H", [0]) * (self.width * self.height)

    def clear(self, color):
        self.pixels = array("H", [color]) * len(self.pixels)

    def fill_rect(self, x, y, w, h, color):
        left, right = max(0, x), min(self.width, x + w)
        if right <= left:
            return
        line = array("H", [color]) * (right - left)
        for row in range(max(0, y), min(self.height, y + h)):
            self.pixels[row * self.width + left:row * self.width + right] = line

    def save(self, path):
        x, y, w, h = visible_rect(self)
        data = bytearray()
        for row in range(y, y + h):
            for col in range(x, x + w):
                c = self.pixels[row * self.width + col]
                data.extend((((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31))
        path.write_bytes(f"P6\n{w} {h}\n255\n".encode() + data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--rotate", type=int, default=90)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        settings = load_settings(Path(folder))
    settings["paused"] = False
    settings.update(device_id="laptop", device_name="笔记本电脑")
    devices = [{"id": "laptop", "name": "笔记本电脑"}, {"id": "redmagic", "name": "红魔手机"},
               {"id": "other", "name": "设备 3"}]
    tags = [{"label": label, "category": category, "display_weight": weight} for label, category, weight in [
        ("编程", "focus", 1), ("阅读", "focus", .7), ("轻松", "relaxed", .6),
        ("低落主题", "melancholy", .5), ("成人内容", "adult", .4), ("音乐", "positive", .3)]]
    font = Font()
    try:
        fb = Buffer(args.rotate)
        view = View(fb, font)
        for name in ("cloud", "settings", "legend", "hidden", "empty", "error", "paused", "devices", "unconfirmed"):
            view.page = name if name in ("settings", "legend", "devices") else "status" if name == "error" else "cloud"
            settings["device_id"] = "" if name == "unconfirmed" else "laptop"
            settings["show_cloud"] = name != "hidden"
            settings["paused"] = name == "paused"
            diagnostic = {"error": "截图超时，视频通道未出帧", "code": "CAPTURE_TIMEOUT", "at": "2026-10-13T10:42:00+00:00"} if name == "error" else None
            state = diagnostic["error"] if diagnostic else "已记录 · 7.1秒"
            view.draw(settings, state, [] if name == "empty" else tags, 128, True, diagnostic, "18:42", devices, "连接有变化")
            fb.save(args.output / (name + ".ppm"))
    finally:
        font.close()


if __name__ == "__main__":
    main()
