"""SQLite source of truth, readable TXT export, and decaying tag weights."""
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

from .devices import LEGACY_ID, LEGACY_NAME, valid_id
from .tags import diverse_tags, normalize_tags


class Store:
    def __init__(self, root, timezone="Asia/Shanghai"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tz = ZoneInfo(timezone)
        self.db = sqlite3.connect(self.root / "diary.sqlite3", timeout=5)
        self.db.execute("CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, ts REAL NOT NULL, day TEXT NOT NULL, body TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS obs_time ON observations(ts)")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(observations)")}
        if "device_id" not in columns:
            # Keep old bodies untouched: their source device cannot be proven.
            self.db.execute("ALTER TABLE observations ADD COLUMN device_id TEXT NOT NULL DEFAULT 'legacy'")
        self.db.execute("CREATE INDEX IF NOT EXISTS obs_device_time ON observations(device_id,ts)")
        self.db.commit()
        self.logs = self.root / "logs"
        self.logs.mkdir(exist_ok=True, mode=0o700)

    def close(self):
        self.db.close()

    def line(self, value):
        local = datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00")).astimezone(self.tz)
        source = "本地 OCR" if value["source"] == "local_ocr" else "远程视觉"
        tags = "、".join(t["label"] for t in value["tags"])
        device = value.get("device", {"name": LEGACY_NAME})
        return f"[{local:%Y-%m-%d %H:%M:%S %z}] [{device['name']}] [{source}] {value['description']}\n标签：{tags or '无'}\n\n"

    def log_path(self, day, device_id):
        if not valid_id(device_id):
            raise ValueError("设备编号无效")
        directory = self.logs / device_id
        directory.mkdir(exist_ok=True, mode=0o700)
        return directory / (day + ".txt")

    def append(self, value):
        stamp = datetime.fromisoformat(value["captured_at"].replace("Z", "+00:00"))
        day = stamp.astimezone(self.tz).date().isoformat()
        device_id = value.get("device", {}).get("id", LEGACY_ID)
        target = self.log_path(day, device_id)
        with self.db:
            self.db.execute("INSERT INTO observations(ts,day,body,device_id) VALUES(?,?,?,?)",
                            (stamp.timestamp(), day, json.dumps(value, ensure_ascii=False, allow_nan=False), device_id))
        # Rebuild today's export at app start to repair an interrupted append.
        with target.open("a", encoding="utf-8") as f:
            f.write(self.line(value))

    def export_day(self, day, device_id=LEGACY_ID):
        rows = self.db.execute("SELECT body FROM observations WHERE day=? AND device_id=? ORDER BY ts,id", (day, device_id))
        target = self.log_path(day, device_id)
        temp = target.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(self.line(json.loads(row[0])))
        temp.replace(target)
        return target

    def count_today(self, device_id):
        day = datetime.now(self.tz).date().isoformat()
        return self.db.execute("SELECT COUNT(*) FROM observations WHERE day=? AND device_id=?", (day, device_id)).fetchone()[0]

    def cloud(self, settings, now=None):
        now = now if now is not None else time.time()
        rows = self.db.execute("SELECT ts,body FROM observations WHERE device_id=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 500",
                               (settings["device_id"], now - settings["cloud_window_seconds"], now))
        tags = {}
        for stamp, body in rows:
            decay = 2 ** (-(now - stamp) / settings["cloud_half_life_seconds"])
            # Canonicalize legacy records on read, without rewriting original logs.
            for tag in normalize_tags(json.loads(body)["tags"], settings["palette"]):
                key = tag["label"].casefold()
                score = tag["weight"] * tag["confidence"] * decay
                if key not in tags:
                    tags[key] = {**tag, "score": 0.0, "occurrences": 0, "categories": {}}
                tags[key]["score"] += score
                tags[key]["occurrences"] += 1
                votes = tags[key]["categories"]
                votes[tag["category"]] = votes.get(tag["category"], 0.0) + score
        ranked = diverse_tags(sorted(tags.values(), key=lambda x: (-x["score"], x["label"])), 10)
        maximum = max((tag["score"] for tag in ranked), default=1) or 1
        for tag in ranked:
            # Stable, single color selected by recent weighted evidence.
            votes = tag.pop("categories")
            tag["category"] = max(votes, key=lambda category: (votes[category], category))
            tag["display_weight"] = math.sqrt(tag["score"] / maximum)
        return ranked
