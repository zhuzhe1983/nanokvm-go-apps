"""Runtime settings live outside the installed application and its releases."""
from pathlib import Path
import json
import os
import tempfile
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parents[1]
INTERVALS = (10, 30, 60, 120)


def app_environment():
    """Reload saved app values so a running launcher cannot retain old endpoints."""
    specs = read_json(APP_DIR / "app.json", {}).get("env", {})
    result = {}
    for key, spec in specs.items():
        if not key.startswith("DIARY_") or not isinstance(spec, dict):
            continue
        # Explicit saved values, including an intentionally cleared key, win.
        value = spec.get("value", os.environ.get(key, spec.get("default", "")))
        if not isinstance(value, str):
            raise ValueError("应用连接配置须为文本")
        result[key] = value
    return result


def data_dir():
    return Path(os.environ.get("DIARY_DATA_DIR", "/var/lib/nanokvm-agent/desktop-diary"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_settings(root=None):
    root = root or data_dir()
    defaults = read_json(APP_DIR / "config.default.json")
    user = read_json(root / "settings.json", {})
    settings = {**defaults, **user}
    from .devices import valid_id
    if settings["device_id"] and not valid_id(settings["device_id"]):
        raise ValueError("设备档案编号错误")
    for key in ("device_id", "device_name", "device_session"):
        if not isinstance(settings[key], str):
            raise ValueError("设备档案配置须为文本")
    if settings["backend"] not in ("local_ocr", "remote_vision"):
        raise ValueError("未知分析方式")
    if type(settings["interval_seconds"]) is not int or not 5 <= settings["interval_seconds"] <= 3600:
        raise ValueError("采集间隔须为 5～3600 秒")
    for key in ("paused", "show_cloud"):
        if type(settings[key]) is not bool:
            raise ValueError("开关配置必须为布尔值")
    ZoneInfo(settings["timezone"])
    for key in ("cloud_window_seconds", "cloud_half_life_seconds"):
        if type(settings[key]) is not int or settings[key] <= 0:
            raise ValueError("标签云时间配置须为正整数")
    for key, item in settings["palette"].items():
        color = item["color"]
        if len(color) != 7 or not color.startswith("#"):
            raise ValueError("标签颜色须为 #RRGGBB")
        int(color[1:], 16)
    if "neutral" not in settings["palette"]:
        raise ValueError("需要 neutral 默认分类")
    return settings


def save_settings(settings, root=None):
    atomic_json((root or data_dir()) / "settings.json", settings)


def feature_enabled(root=None):
    definition = read_json(APP_DIR / "features/observe.json")
    overrides = read_json((root or data_dir()) / "features.json", {})
    return bool(overrides.get(definition["id"], definition["enabled"]))
