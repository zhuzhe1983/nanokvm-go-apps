"""User-confirmed device profiles. Link state is a change detector, not identity."""
from pathlib import Path
import re
import uuid

from .config import atomic_json, read_json

LEGACY_ID = "legacy"
LEGACY_NAME = "历史未分类"


def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value))


def load_devices(root):
    path = Path(root) / "devices.json"
    profiles = read_json(path)
    if profiles is None:
        profiles = [{"id": "laptop", "name": "笔记本电脑"}, {"id": "redmagic", "name": "红魔手机"}]
        atomic_json(path, profiles)
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("设备档案格式错误")
    seen = set()
    for profile in profiles:
        if (not isinstance(profile, dict) or not valid_id(profile.get("id")) or
                profile["id"] == LEGACY_ID or profile["id"] in seen or
                not isinstance(profile.get("name"), str) or not profile["name"].strip() or
                len(profile["name"]) > 24 or any(ord(c) < 32 for c in profile["name"])):
            raise ValueError("设备档案名称或编号错误")
        seen.add(profile["id"])
    return profiles


def add_device(root):
    profiles = load_devices(root)
    profile = {"id": "device-" + uuid.uuid4().hex[:12], "name": f"设备 {len(profiles) + 1}"}
    profiles.append(profile)
    atomic_json(Path(root) / "devices.json", profiles)
    return profile


def read_link(usb_root=Path("/sys/class/udc"), video_root=Path("/proc/lt7911_info")):
    """Known Go+ read-only nodes; never treat EDID/resolution as a host ID."""
    result = {}
    try:
        for path in sorted(usb_root.glob("*/state")):
            result["usb:" + path.parent.name] = path.read_text().strip()
    except OSError:
        result["usb:error"] = "unavailable"
    try:
        if video_root.exists():
            result["video"] = [ (video_root / name).read_text().strip()
                                for name in ("status", "width", "height") ]
    except OSError:
        result["video"] = ["unavailable"]
    return result


class LinkMonitor:
    def __init__(self):
        self.previous = read_link()

    def changed(self):
        current = read_link()
        changed = current != self.previous
        self.previous = current
        return changed
