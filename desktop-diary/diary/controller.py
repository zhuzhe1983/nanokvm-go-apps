"""One foreground app owns scheduling; pause/exit terminates its process group."""
from datetime import datetime
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .config import APP_DIR, app_environment, feature_enabled, load_settings, read_json, save_settings
from .diagnostics import save_failure
from .store import Store
from .devices import LinkMonitor, add_device, load_devices


class Controller:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lockpath = Path(os.environ.get("DIARY_APP_LOCK", "/tmp/nanokvm-visual-agent.lock"))
        self.lock = lockpath.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("已有视觉智能体应用运行") from None
        self.process = None
        self.scratch = None
        self.next_sample = 0.0
        self.last_finished = 0.0
        self.state = "已暂停"
        self.started = 0.0
        self.store = None
        self.job_id = ""
        self.last_error = read_json(self.root / "last-error.json", {})
        self.link = LinkMonitor()
        self.link_checked = 0.0
        self.device_reason = "请确认当前设备"
        try:
            self.settings = load_settings(self.root)
            self.devices = load_devices(self.root)
            self.store = Store(root, self.settings["timezone"])
            today = datetime.now(self.store.tz).date().isoformat()
            for device_id, in self.store.db.execute("SELECT DISTINCT device_id FROM observations WHERE day=?", (today,)).fetchall():
                self.store.export_day(today, device_id)
            # Starting a fresh foreground app requires an explicit start tap.
            # The cable could have changed while this app was closed.
            self.settings.update(paused=True, device_id="", device_name="", device_session="")
            save_settings(self.settings, self.root)
        except Exception:
            if self.store:
                self.store.close()
            self.lock.close()
            raise

    def cancel(self):
        process = self.process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
            self.process = None
        if self.scratch:
            self.scratch.cleanup()
            self.scratch = None

    def update(self, **changes):
        disruptive = "paused" in changes or "backend" in changes
        if disruptive:
            self.cancel()
        self.settings.update(changes)
        save_settings(self.settings, self.root)
        if disruptive or "interval_seconds" in changes:
            self.next_sample = 0
        if self.settings["paused"]:
            self.state = "已暂停"
        elif disruptive:
            self.state = "准备分析"

    def select_device(self, device_id):
        self.devices = load_devices(self.root)
        profile = next(item for item in self.devices if item["id"] == device_id)
        self.cancel()
        self.link = LinkMonitor()
        self.last_error = {}
        self.device_reason = ""
        self.settings.update(device_id=profile["id"], device_name=profile["name"], device_session=uuid.uuid4().hex)
        today = datetime.now(self.store.tz).date().isoformat()
        self.store.export_day(today, device_id)
        self.update(paused=False)

    def new_device(self):
        profile = add_device(self.root)
        self.select_device(profile["id"])

    def require_device(self, reason):
        self.cancel()
        self.settings.update(device_id="", device_name="", device_session="")
        self.device_reason = reason
        self.update(paused=True)

    def tick(self):
        now = time.monotonic()
        if now - self.link_checked >= 0.5:
            self.link_checked = now
            if self.link.changed() and self.settings["device_id"]:
                self.require_device("连接有变化，请确认设备")
        if not self.settings["device_id"]:
            return
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.process = None
            if self.scratch:
                self.scratch.cleanup()
                self.scratch = None
            outcome = read_json(self.root / "last-result.json", {})
            if outcome.get("job_id") != self.job_id:
                outcome = save_failure(self.root, RuntimeError("worker exited"), "analysis", self.job_id)
            if code == 0 and outcome.get("ok"):
                self.state = f"已记录 · {outcome.get('elapsed_seconds', 0):.1f}秒"
                self.last_finished = time.time()
            else:
                self.state = outcome.get("error", "分析中断，请检查配置")
                self.last_error = outcome
                if outcome.get("code") == "DEVICE_CHANGED":
                    self.require_device("连接有变化，请确认设备")
                    return
            # No queue or catch-up: wait a little even when an analysis ran long.
            self.next_sample = max(self.next_sample, now + 1)
        if self.process and now - self.started > 110:
            self.cancel()
            self.state = "分析超时，稍后重试"
            self.next_sample = now + self.settings["interval_seconds"]
        if self.settings["paused"] or not feature_enabled(self.root):
            if self.process:
                self.cancel()
            return
        if self.process is None and now >= self.next_sample:
            try:
                connection_env = app_environment()
            except Exception as error:
                self.last_error = save_failure(self.root, error, "config")
                self.state = self.last_error["error"]
                self.next_sample = now + self.settings["interval_seconds"]
                return
            self.scratch = tempfile.TemporaryDirectory(prefix="desktop-diary-job-")
            self.job_id = str(time.monotonic_ns())
            env = dict(os.environ, **connection_env)
            env.update(DIARY_DATA_DIR=str(self.root), DIARY_SCRATCH_DIR=self.scratch.name,
                       DIARY_PARENT_PID=str(os.getpid()), DIARY_JOB_ID=self.job_id)
            self.process = subprocess.Popen([sys.executable, str(APP_DIR / "runner.py"), "sample"],
                                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
            self.started = now
            self.next_sample = now + self.settings["interval_seconds"]
            self.state = "正在分析"

    def close(self):
        self.cancel()
        self.store.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()
