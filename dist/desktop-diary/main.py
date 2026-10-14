#!/usr/bin/env python3
"""NanoKVM touch-screen entry point; SDK owns framebuffer and touch lifecycle."""
import os
import signal
import time

from appbase import AppContext, app
from diary.config import INTERVALS, app_environment, data_dir
from diary.controller import Controller
from diary.font import Font
from diary.ui import View


@app()
def main(ctx: AppContext):
    os.umask(0o077)
    previous_term = signal.getsignal(signal.SIGTERM)
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    for key, value in ctx.env.items():
        if key.startswith("DIARY_"):
            os.environ[key] = value
    control = Controller(data_dir())
    font = None
    try:
        font = Font()
        view = View(ctx.fb, font)
        cached = {"at": -100.0, "tags": [], "count": 0}

        def tap(x, y):
            action = view.hit(x, y)
            s = control.settings
            if action in ("settings", "legend", "status", "devices"):
                view.page = action
            elif action == "back":
                view.page = "cloud"
            elif action == "pause":
                if not s["device_id"]:
                    view.page = "devices"
                else:
                    control.update(paused=not s["paused"])
            elif action and action.startswith("device:"):
                control.select_device(action.split(":", 1)[1])
                view.page = "cloud"
            elif action == "new_device":
                control.new_device()
                view.page = "cloud"
            elif action == "device_next":
                view.device_offset = (view.device_offset + 3) % len(control.devices)
            elif action in ("show_cloud", "hide_cloud"):
                # Apply the action shown on screen, even if multiple touch
                # releases arrive before the next frame is drawn.
                control.update(show_cloud=action == "show_cloud")
            elif action == "interval":
                current = s["interval_seconds"]
                value = INTERVALS[(INTERVALS.index(current) + 1) % len(INTERVALS)] if current in INTERVALS else 30
                control.update(interval_seconds=value)
            elif action == "backend":
                control.update(backend="remote_vision" if s["backend"] == "local_ocr" else "local_ocr")
            elif action == "retry":
                if s["device_id"]:
                    control.update(paused=False)
                    view.page = "cloud"
                else:
                    view.page = "devices"
            cached["at"] = -100.0

        def tick(_dt):
            control.tick()
            now = time.monotonic()
            if now - cached["at"] > 3:
                cached.update(at=now, tags=control.store.cloud(control.settings),
                              count=control.store.count_today(control.settings["device_id"]))
            connection = app_environment()
            ready = bool(connection.get("DIARY_VISION_BASE_URL") and connection.get("DIARY_VISION_MODEL"))
            # Never show a previous device's cached cloud while confirmation is pending.
            tags, count = (cached["tags"], cached["count"]) if control.settings["device_id"] else ([], 0)
            view.draw(control.settings, control.state, tags, count, ready, control.last_error,
                      devices=control.devices, device_reason=control.device_reason)

        ctx.run(tick, fps=8, on_tap=tap)
    finally:
        control.close()
        if font:
            font.close()
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    main()
