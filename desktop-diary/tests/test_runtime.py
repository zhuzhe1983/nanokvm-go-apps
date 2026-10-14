import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diary.analysis import AnalysisError, capture, remote_vision, validate_result
from diary.config import app_environment, atomic_json, load_settings
from diary.controller import Controller
from diary.diagnostics import save_failure
from diary.mcp import BridgeError
from diary.store import Store
from diary.ui import View, cloud_layout, visible_rect
from diary.devices import LinkMonitor, read_link
from diary.tags import normalize_tags
import runner


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = load_settings(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, stamp, label="编程", confidence=0.9):
        return {"captured_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                "source": "local_ocr", "description": "画面显示代码编辑器。",
                "tags": [{"label": label, "category": "focus", "weight": 1, "confidence": confidence}]}

    def test_txt_dates_repair_and_cloud_decay(self):
        store = Store(self.root)
        try:
            # UTC previous day belongs to the next local day in Shanghai.
            t = datetime(2026, 10, 13, 16, 1, tzinfo=timezone.utc).timestamp()
            store.append(self.record(t))
            store.append(self.record(t - 600, "阅读"))
            path = self.root / "logs/legacy/2026-10-14.txt"
            self.assertIn("2026-10-14 00:01:00 +0800", path.read_text())
            path.write_text("interrupted export")
            store.export_day("2026-10-14")
            self.assertIn("代码编辑器", path.read_text())
            self.settings["device_id"] = "legacy"
            tags = store.cloud(self.settings, now=t)
            self.assertEqual(tags[0]["label"], "编程")
            self.assertAlmostEqual(tags[0]["score"] / tags[1]["score"], 4)
            self.assertEqual(store.cloud(self.settings, now=t + 1801), [])
        finally:
            store.close()

    def test_invalid_model_content_and_nonfinite_weights(self):
        for value in [[], {"description": ""}, {"description": "正常", "tags": "bad"}]:
            with self.assertRaises(AnalysisError):
                validate_result(value, self.settings["palette"])
        result = validate_result({"description": "只记录\n画面。", "tags": [
            {"label": "低落主题", "category": "melancholy", "weight": float("nan"), "confidence": 10},
            {"label": "低落主题", "category": "adult"},
            {"label": "编辑器", "category": ["invalid"]}]}, self.settings["palette"])
        self.assertEqual(len(result["tags"]), 2)
        self.assertEqual(result["tags"][0]["weight"], 0.5)
        self.assertEqual(result["tags"][1]["category"], "neutral")
        self.assertNotIn("\n", result["description"])

    def test_remote_image_protocol_and_no_reasoning_fallback(self):
        received = []
        response_body = {"choices": [{"message": {"content": json.dumps({
            "description": "屏幕显示文章。", "tags": [{"label": "阅读", "category": "focus", "weight": .8, "confidence": .9}]
        }, ensure_ascii=False)}}]}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                data = json.dumps(response_body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.dict(os.environ, {"DIARY_VISION_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1", "DIARY_VISION_MODEL": "test-vlm"}):
                result = remote_vision(b"image-fixture", "image/jpeg", self.settings)
                self.assertEqual(result["tags"][0]["label"], "阅读")
                path, body = received[0]
                self.assertEqual(path, "/v1/chat/completions")
                self.assertEqual(body["model"], "test-vlm")
                image_url = body["messages"][1]["content"][1]["image_url"]["url"]
                self.assertEqual(base64.b64decode(image_url.split(",")[1]), b"image-fixture")
                response_body["choices"][0]["message"] = {"content": "", "reasoning": "private reasoning"}
                with self.assertRaises(AnalysisError):
                    remote_vision(b"test", "image/jpeg", self.settings)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_mutual_exclusion_pause_kills_worker_and_hidden_keeps_it(self):
        fake_app = self.root / "fake-app"
        fake_app.mkdir()
        (fake_app / "runner.py").write_text("import time; time.sleep(120)\n")
        with patch.dict(os.environ, {"DIARY_APP_LOCK": str(self.root / "lease")}):
            control = Controller(self.root / "data")
            try:
                with self.assertRaises(RuntimeError):
                    Controller(self.root / "other")
                with patch("diary.controller.APP_DIR", fake_app):
                    control.select_device("laptop")
                    control.tick()
                process = control.process
                scratch = Path(control.scratch.name)
                (scratch / "frame.jpg").write_bytes(b"private screen")
                next_sample = control.next_sample
                control.update(show_cloud=False)
                self.assertIs(control.process, process)
                self.assertEqual(control.next_sample, next_sample)
                control.update(paused=True)
                self.assertIsNotNone(process.poll())
                self.assertFalse(scratch.exists())
                control.tick()
                self.assertIsNone(control.process)
            finally:
                control.close()

    def test_layout_inside_display_and_without_overlap(self):
        class Font:
            def width(self, text, size):
                return len(text) * size

            def fit(self, text, size, width):
                return text[:width // size]

        tags = [{"label": str(i) + "标签", "category": "neutral", "display_weight": (18 - i) / 18} for i in range(18)]
        bounds = (9, 83, 222, 126)
        layout = cloud_layout(tags, bounds, Font())
        self.assertGreater(len(layout), 3)
        for i, item in enumerate(layout):
            self.assertGreaterEqual(item["x"], bounds[0])
            self.assertLessEqual(item["x"] + item["w"], bounds[0] + bounds[2])
            self.assertLessEqual(item["y"] + item["h"], bounds[1] + bounds[3])
            for other in layout[i + 1:]:
                self.assertTrue(item["x"] + item["w"] <= other["x"] or other["x"] + other["w"] <= item["x"] or
                                item["y"] + item["h"] <= other["y"] or other["y"] + other["h"] <= item["y"])
        for rotate in (0, 90, 180, 270):
            for pw, ph in ((240, 284), (284, 240)):
                fb = type("FB", (), {"rotate": rotate, "phys_w": pw, "phys_h": ph,
                      "width": ph if rotate % 180 else pw, "height": pw if rotate % 180 else ph})()
                x, y, w, h = visible_rect(fb)
                self.assertEqual(w * h, 240 * 270)
                self.assertLessEqual(x + w, fb.width)
                self.assertLessEqual(y + h, fb.height)
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)

    def test_hidden_cloud_has_explicit_recovery_and_separate_device_footer(self):
        class Font:
            def __init__(self):
                self.texts = []

            def width(self, text, size):
                return len(text) * size

            def fit(self, text, size, width):
                return text[:width // size]

            def draw(self, fb, x, y, text, *args):
                self.texts.append(text)

        fb = type("FB", (), {"rotate": 90, "phys_w": 240, "phys_h": 284,
                              "width": 284, "height": 240,
                              "clear": lambda *args: None,
                              "fill_rect": lambda *args: None})()
        font = Font()
        view = View(fb, font)
        self.settings.update(device_id="laptop", device_name="笔记本电脑",
                             show_cloud=False, paused=False)
        view.draw(self.settings, "正在分析", [], count=10)
        self.assertIn("点击这里恢复标签", font.texts)
        self.assertIn("显示标签", font.texts)
        # Two visible recovery targets, while the footer still switches device.
        actions = [button[-1] for button in view.buttons]
        self.assertEqual(actions.count("show_cloud"), 2)
        self.assertNotIn("hide_cloud", actions)
        for x, y, w, h, action in view.buttons:
            self.assertEqual(view.hit(view.origin_x + x + w // 2,
                                      view.origin_y + y + h // 2), action)
        self.settings["show_cloud"] = True
        view.draw(self.settings, "正在分析", [])
        actions = [button[-1] for button in view.buttons]
        self.assertNotIn("show_cloud", actions)
        self.assertIn("hide_cloud", actions)

    def test_cloud_preference_survives_restart_and_device_changes(self):
        with patch.dict(os.environ, {"DIARY_APP_LOCK": str(self.root / "lease")}):
            control = Controller(self.root / "data")
            try:
                control.select_device("laptop")
                control.update(show_cloud=False)
                control.select_device("redmagic")
                self.assertFalse(control.settings["show_cloud"])
                control.require_device("连接有变化，请确认设备")
                self.assertFalse(control.settings["show_cloud"])
            finally:
                control.close()
            control = Controller(self.root / "data")
            try:
                self.assertFalse(control.settings["show_cloud"])
                control.select_device("laptop")
                control.update(show_cloud=True)
                control.update(show_cloud=True)
                self.assertTrue(control.settings["show_cloud"])
                self.assertFalse(control.settings["paused"])
                self.assertTrue(load_settings(self.root / "data")["show_cloud"])
            finally:
                control.close()

    def test_capture_retries_only_transient_transport(self):
        with patch("diary.analysis._capture_once", side_effect=[BridgeError("NanoKVM transport failed; request was not retried."), (b"image", "image/jpeg", {})]) as fetch:
            self.assertEqual(capture()[0], b"image")
            self.assertEqual(fetch.call_count, 2)
        with patch("diary.analysis._capture_once", side_effect=BridgeError("NanoKVM HTTP request failed (status 401).")) as fetch:
            with self.assertRaises(AnalysisError) as caught:
                capture()
            self.assertEqual(caught.exception.code, "MCP_AUTH")
            self.assertEqual(fetch.call_count, 1)

    def test_snapshot_tool_timeout_is_distinct_from_transport_failure(self):
        timed_out = {"result": {"isError": True, "structuredContent": {
            "ok": False, "retCode": -13, "message": "snapshot timeout", "width": 0, "height": 0}}}
        recovered = {"result": {"structuredContent": {"ok": True}, "content": [{
            "type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(b"fixture").decode()}]}}
        with patch("diary.mcp.Remote") as remote, patch("diary.analysis.time.sleep"):
            remote.return_value.request.side_effect = [timed_out, timed_out]
            with self.assertRaises(AnalysisError) as caught:
                capture()
            self.assertEqual(caught.exception.code, "CAPTURE_TIMEOUT")
            self.assertEqual(caught.exception.provider_code, -13)
            self.assertEqual(remote.return_value.request.call_count, 2)
            fault = save_failure(self.root, caught.exception, "capture")
            self.assertEqual(fault["provider_code"], -13)
        with patch("diary.mcp.Remote") as remote, patch("diary.analysis.time.sleep"):
            remote.return_value.request.side_effect = [timed_out, recovered]
            self.assertEqual(capture()[0], b"fixture")

    def test_diagnostics_persist_without_exception_secrets(self):
        try:
            raise RuntimeError("Authorization: Bearer private-test-secret; screenshot payload")
        except RuntimeError as error:
            value = save_failure(self.root, error, "storage", "test-job")
        self.assertEqual(value["stage"], "storage")
        self.assertEqual(value["exception_type"], "RuntimeError")
        self.assertTrue(value["frames"])
        for path in self.root.glob("*.json"):
            self.assertNotIn("private-test-secret", path.read_text())
            self.assertNotIn("screenshot payload", path.read_text())
        atomic_json(self.root / "last-result.json", {"ok": True})
        self.assertEqual(json.loads((self.root / "last-error.json").read_text())["job_id"], "test-job")

    def test_saved_connections_override_stale_launcher_environment(self):
        atomic_json(self.root / "app.json", {"env": {
            "DIARY_VISION_BASE_URL": {"value": "https://new.example/v1"},
            "DIARY_VISION_API_KEY": {"value": ""}}})
        with patch("diary.config.APP_DIR", self.root), patch.dict(os.environ, {
                "DIARY_VISION_BASE_URL": "https://old.example/v1", "DIARY_VISION_API_KEY": "old-secret"}):
            env = app_environment()
            self.assertEqual(env["DIARY_VISION_BASE_URL"], "https://new.example/v1")
            self.assertEqual(env["DIARY_VISION_API_KEY"], "")
            atomic_json(self.root / "app.json", {"env": {"DIARY_VISION_BASE_URL": {"value": "https://updated.example/v1"}}})
            self.assertEqual(app_environment()["DIARY_VISION_BASE_URL"], "https://updated.example/v1")

    def test_cloud_merges_names_across_colors_and_aliases_without_inflating_frame(self):
        store = Store(self.root)
        try:
            # Keep the cutoff exact after ISO timestamps round to microseconds.
            t = int(time.time())
            record = self.record(t)
            record["tags"] = [
                {"label": "星空", "category": "neutral", "weight": .5, "confidence": 1},
                {"label": "星空", "category": "relaxed", "weight": 1, "confidence": 1},
                {"label": "夜晚", "category": "relaxed", "weight": 1, "confidence": 1},
                {"label": "夜景", "category": "neutral", "weight": 1, "confidence": 1},
                {"label": "壁纸", "category": "neutral", "weight": 1, "confidence": 1},
                {"label": "桌面壁纸", "category": "neutral", "weight": 1, "confidence": 1},
                {"label": "桌面", "category": "neutral", "weight": 1, "confidence": 1}]
            store.append(record)
            older = self.record(t - 300)
            older["tags"] = [{"label": "星空", "category": "neutral", "weight": 1, "confidence": 1}]
            store.append(older)
            tags = store.cloud({**self.settings, "device_id": "legacy"}, now=t)
            self.assertEqual({tag["label"] for tag in tags}, {"星空", "桌面壁纸"})
            star = next(tag for tag in tags if tag["label"] == "星空")
            self.assertEqual(star["category"], "relaxed")
            self.assertAlmostEqual(star["score"], 1.5)
            self.assertEqual(star["occurrences"], 2)
            self.assertEqual(next(tag for tag in tags if tag["label"] == "桌面壁纸")["score"], 1)
            # Distinct activities must survive; only explicit synonyms merge.
            words = ["游戏", "游戏开发", "编程", "聊天", "直播", " 星\u200b空 ", "星空"]
            merged = normalize_tags([{"label": w} for w in words], self.settings["palette"])
            self.assertEqual(len(merged), 6)
        finally:
            store.close()

    def test_device_migration_is_non_destructive_and_every_view_is_filtered(self):
        t = int(time.time())
        old = self.record(t, "历史")
        day = datetime.now().astimezone().date().isoformat()
        db = sqlite3.connect(self.root / "diary.sqlite3")
        db.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY,ts REAL,day TEXT,body TEXT)")
        original = json.dumps(old, ensure_ascii=False)
        db.execute("INSERT INTO observations VALUES(1,?,?,?)", (t, day, original))
        db.commit(); db.close()
        store = Store(self.root)
        try:
            self.assertEqual(store.db.execute("SELECT body,device_id FROM observations WHERE id=1").fetchone(), (original, "legacy"))
            for device, label in (("laptop", "编程"), ("redmagic", "游戏")):
                record = self.record(t, label)
                record["device"] = {"id": device, "name": device}
                store.append(record)
                cloud = store.cloud({**self.settings, "device_id": device}, now=t)
                self.assertEqual([tag["label"] for tag in cloud], [label])
                self.assertEqual(store.count_today(device), 1)
                text = store.export_day(day, device).read_text()
                self.assertIn(label, text)
                self.assertNotIn("历史", text)
                self.assertNotIn("游戏" if device == "laptop" else "编程", text)
            self.assertEqual(store.cloud({**self.settings, "device_id": ""}, now=t), [])
            with self.assertRaises(ValueError):
                store.log_path(day, "../escape")
        finally:
            store.close()

    def test_link_change_cancels_worker_and_clears_active_device(self):
        fake_app = self.root / "fake-app"
        fake_app.mkdir()
        (fake_app / "runner.py").write_text("import time; time.sleep(120)\n")
        with patch.dict(os.environ, {"DIARY_APP_LOCK": str(self.root / "lease")}):
            control = Controller(self.root / "data")
            try:
                self.assertEqual(control.settings["device_id"], "")
                with patch("diary.controller.APP_DIR", fake_app):
                    control.select_device("laptop")
                    control.tick()
                process = control.process
                control.link_checked = 0
                with patch.object(control.link, "changed", return_value=True):
                    control.tick()
                self.assertIsNotNone(process.poll())
                self.assertTrue(control.settings["paused"])
                self.assertEqual(control.settings["device_id"], "")
                control.select_device("redmagic")
                self.assertEqual(control.settings["device_name"], "红魔手机")
            finally:
                control.close()

    def test_inflight_device_switch_cannot_commit_under_either_device(self):
        settings = {**self.settings, "paused": False, "device_id": "laptop", "device_name": "笔记本电脑", "device_session": "old"}
        changed = {**settings, "device_id": "redmagic", "device_session": "new"}
        with patch.dict(os.environ, {"DIARY_DATA_DIR": str(self.root), "DIARY_PARENT_PID": str(os.getppid())}), \
                patch("runner.sys.platform", "darwin"), patch("runner.signal.signal"), \
                patch("runner.load_settings", side_effect=[settings, changed]), \
                patch("runner.observe", return_value=self.record(time.time())):
            self.assertEqual(runner.sample(), 2)
        self.assertFalse((self.root / "diary.sqlite3").exists())

    def test_known_link_nodes_are_observations_not_device_fingerprints(self):
        usb = self.root / "usb/controller"; usb.mkdir(parents=True)
        video = self.root / "video"; video.mkdir()
        (usb / "state").write_text("configured\n")
        for name, value in (("status", "stable"), ("width", "3840"), ("height", "2160")):
            (video / name).write_text(value)
        value = read_link(usb.parent, video)
        self.assertEqual(value["video"], ["stable", "3840", "2160"])
        self.assertNotIn("device_id", value)
        with patch("diary.devices.read_link", side_effect=[value, value, {**value, "video": ["unstable", "0", "0"]}]):
            monitor = LinkMonitor()
            self.assertFalse(monitor.changed())
            self.assertTrue(monitor.changed())


if __name__ == "__main__":
    unittest.main()
