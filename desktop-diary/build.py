#!/usr/bin/env python3
"""Explicit allowlist: credentials, observations, tests, and caches never ship."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent
DIST = ROOT.parent / "dist"
FILES = ["app.json", "main.py", "runner.py", "config.default.json", "README.md"]
FILES += [str(p.relative_to(ROOT)) for folder, pattern in (("diary", "*.py"), ("features", "*.json"), ("skills", "*.md")) for p in sorted((ROOT / folder).glob(pattern))]


def main():
    manifest = json.loads((ROOT / "app.json").read_text())
    DIST.mkdir(exist_ok=True)
    version = manifest["version"]
    stage = DIST / ROOT.name
    stage.mkdir(exist_ok=True)
    # The allowlist is also used for ZIP generation; stale local files cannot
    # leak into ZIP. Only replace paths owned by this release.
    for name in FILES:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    archive = DIST / f"{ROOT.name}-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            z.write(ROOT / name, f"{ROOT.name}/{name}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "  " + archive.name + "\n")
    print(f"{archive} ({archive.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
