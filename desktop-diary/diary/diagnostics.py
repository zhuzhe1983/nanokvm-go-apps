"""Bounded failure history: codes and source locations, never payloads or secrets."""
from datetime import datetime, timezone
import errno
from pathlib import Path
import sqlite3
import traceback

from .analysis import AnalysisError
from .config import atomic_json


def failure(error, stage):
    stage = getattr(error, "stage", stage)
    code = getattr(error, "code", "UNEXPECTED")
    if isinstance(error, AnalysisError):
        message = str(error)
    elif isinstance(error, OSError) and error.errno == errno.ENOSPC:
        message, code = "存储空间不足，请清理日记", "STORAGE_FULL"
    elif isinstance(error, PermissionError):
        message, code = "无法写入日记，请检查目录权限", "STORAGE_PERMISSION"
    elif isinstance(error, sqlite3.Error):
        message, code = "日记数据库暂不可用，稍后重试", getattr(error, "sqlite_errorname", "SQLITE_ERROR")
    else:
        names = {"config": "配置", "capture": "截图", "ocr": "本地识字", "vision": "远程视觉", "storage": "日记存储"}
        message = names.get(stage, "分析") + "异常，点状态查看详情"
    cause = error.__cause__ or error
    frames = traceback.extract_tb(cause.__traceback__)[-5:]
    outcome = {"ok": False, "at": datetime.now(timezone.utc).isoformat(),
            "stage": stage, "code": code, "error": message, "exception_type": type(cause).__name__,
            "frames": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name} for f in frames]}
    if type(getattr(error, "provider_code", None)) is int:
        outcome["provider_code"] = error.provider_code
    return outcome


def save_failure(root, error, stage, job_id=""):
    outcome = failure(error, stage)
    outcome["job_id"] = job_id
    atomic_json(root / "last-error.json", outcome)
    atomic_json(root / "last-result.json", outcome)
    return outcome
