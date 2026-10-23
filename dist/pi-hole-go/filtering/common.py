"""Private state lives outside the application/release tree."""
import json
import os
from pathlib import Path
import secrets
import tempfile

APP = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("PIHOLE_GO_DATA", "/var/lib/nanokvm-agent/pi-hole-go"))
DEFAULTS = {"enabled": False, "pause_until": 0, "auto_update": True,
            "upstreams": ["223.5.5.5", "1.1.1.1"], "allowlist": [], "denylist": [],
            "updated_at": 0, "domain_count": 0}



class UserError(Exception):
    pass


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, ensure_ascii=False, allow_nan=False, indent=2)
            file.flush(); os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload); file.flush(); os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def private_keys(root=ROOT, *, shorten_pin=False):
    path = root / "keys.json"
    value = read(path)
    if value is None:
        value = {"pin": f"{secrets.randbelow(1_000_000):06d}",
                 "api": secrets.token_urlsafe(32)}
        write(path, value)
    elif shorten_pin and not (isinstance(value.get("pin"), str) and len(value["pin"]) == 6
                              and value["pin"].isascii() and value["pin"].isdigit()):
        # Only the service/installer migrates existing codes. UI readers cannot
        # race the service into choosing a different code; keep the API key.
        value["pin"] = f"{secrets.randbelow(1_000_000):06d}"
        write(path, value)
    return value
