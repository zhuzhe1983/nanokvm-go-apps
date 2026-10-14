#!/usr/bin/env python3
"""Headless diagnostics and the isolated analysis worker."""
import argparse
import json
import os
import signal
import sys
import time

from diary.analysis import AnalysisError, observe
from diary.config import atomic_json, data_dir, load_settings
from diary.store import Store
from diary.diagnostics import save_failure
from diary.devices import read_link


def sample():
    os.umask(0o077)
    if os.environ.get("DIARY_PARENT_PID") != str(os.getppid()):
        return 2
    def cancelled(*_):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        raise SystemExit(2)
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    if sys.platform == "linux":
        import ctypes
        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != parent:
            cancelled()
    root = data_dir()
    store = None
    stage = "config"
    job_id = os.environ.get("DIARY_JOB_ID", "")
    try:
        settings = load_settings(root)
        if not settings["device_id"] or not settings["device_session"]:
            raise AnalysisError("请先确认当前设备", "DEVICE_REQUIRED", "device")
        link = read_link()
        stage = "analysis"
        result = observe(settings)
        # A changed mode or a pause invalidates in-flight output.
        current = load_settings(root)
        if (current["paused"] or current["backend"] != settings["backend"] or
                current["device_id"] != settings["device_id"] or
                current["device_session"] != settings["device_session"]):
            return 2
        if read_link() != link:
            raise AnalysisError("连接有变化，请确认设备", "DEVICE_CHANGED", "device")
        result.update(schema_version=2, device={"id": settings["device_id"], "name": settings["device_name"],
                      "session_id": settings["device_session"], "identity_source": "user_confirmed"})
        stage = "storage"
        store = Store(root, settings["timezone"])
        store.append(result)
        atomic_json(root / "last-result.json", {"ok": True, "elapsed_seconds": result["elapsed_seconds"], "job_id": job_id})
        return 0
    except Exception as error:
        save_failure(root, error, stage, job_id)
        return 1
    finally:
        if store:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sample", "run", "once", "status"])
    parser.add_argument("--device", help="用户确认的设备档案编号（once/run 必填）")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "sample":
        return sample()
    if args.command == "status":
        from diary.config import read_json
        print(json.dumps({"settings": load_settings(), "last": read_json(data_dir() / "last-result.json", {})}, ensure_ascii=False))
        return 0
    from diary.controller import Controller
    if not args.device:
        parser.error("once/run 需要 --device 档案编号，以免混写设备日记")
    control = Controller(data_dir())
    stop = False

    def finish(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    try:
        control.select_device(args.device)
        while not stop:
            control.tick()
            if args.command == "once" and control.started and control.process is None:
                print(control.state)
                return 0 if control.last_finished else 1
            time.sleep(0.2)
    finally:
        control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
