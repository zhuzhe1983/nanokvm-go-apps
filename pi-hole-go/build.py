#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parent
DIST = ROOT.parent / "dist"
FILES = ["app.json", "main.py", "server.py", "install.py", "cleanup.py", "uninstall.py", "README.md"]
FILES += [str(p.relative_to(ROOT)) for folder, pattern in (("filtering", "*.py"), ("web", "*"), ("scripts", "*.sh"), ("vendor", "*"))
          for p in sorted((ROOT / folder).glob(pattern)) if p.is_file()]


def main():
    manifest = json.loads((ROOT / "app.json").read_text())
    DIST.mkdir(exist_ok=True)
    stage = DIST / ROOT.name
    for name in FILES:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    archive = DIST / f"{ROOT.name}-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for name in FILES:
            output.write(ROOT / name, f"{ROOT.name}/{name}")
    archive.with_suffix(".zip.sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    print(archive, archive.stat().st_size)


if __name__ == "__main__":
    main()
