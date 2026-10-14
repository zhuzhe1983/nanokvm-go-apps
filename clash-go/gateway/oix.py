"""Use the official oix core account client and managed provider, no converter."""
import json
import os
import subprocess
import time
from .common import UserError

VERSION = "alpha-oix-9a6b3c1"
FIELDS = ("plan", "plan_time", "plan_rank", "used", "traffic", "unused", "today_used", "token_client")


def access_token(value):
    if not isinstance(value, str):
        raise UserError("请输入 oixCloud Access Token")
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or len(value) > 4096 or "://" in value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise UserError("请填写 Access Token 本身，不能使用订阅网址")
    return value


def account(binary, token):
    token = access_token(token)
    try:
        result = subprocess.run([str(binary), "oix", "account"], input=json.dumps({"token": token}),
                                text=True, capture_output=True, timeout=40,
                                env={**os.environ, "OIX_CLIENT": "openclash", "GOMEMLIMIT": "48MiB", "GOGC": "50"})
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise UserError("oixCloud 登录超时，请检查设备网络后重试") from None
    except (OSError, ValueError):
        raise UserError("oixCloud 专用核心不可用，请重新安装应用") from None
    if result.returncode or not isinstance(data, dict) or data.get("code"):
        code = data.get("code") if isinstance(data, dict) else ""
        messages = {"rejected": "oixCloud 拒绝此 Access Token，请检查是否失效或已被撤销",
                    "rate_limited": "oixCloud 请求过于频繁，请稍后重试",
                    "timeout": "oixCloud 账号请求超时，请稍后重试",
                    "unavailable": "专用核心缺少 oixCloud 接口配置，请重新安装应用"}
        raise UserError(messages.get(code, "设备无法取得 oixCloud 账号信息，请检查网络后重试"))
    info = data.get("account")
    if not isinstance(info, dict):
        raise UserError("oixCloud 未返回套餐信息，原账号已保留")
    clean = {key: str(info[key])[:160] for key in FIELDS if isinstance(info.get(key), (str, int, float))}
    return {"token": access_token(data.get("token", token)), "account": clean,
            "updated_at": int(time.time()), "rebound": bool(data.get("rebound"))}
