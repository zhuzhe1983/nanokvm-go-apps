"""Independent local OCR and remote vision backends; no implicit fallback."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .config import APP_DIR
from .tags import diverse_tags, normalize_tags


class AnalysisError(Exception):
    def __init__(self, message, code="ANALYSIS", stage="analysis"):
        super().__init__(message)
        self.code, self.stage = code, stage


def _capture_once():
    from .mcp import Remote
    remote = Remote()
    try:
        remote.initialize()
        reply = remote.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "screenshot", "arguments": {"quality": 75, "timeoutMs": 5000}}})
        result = reply.get("result") or {}
        meta = result.get("structuredContent") or {}
        if "error" in reply or result.get("isError") or meta.get("ok") is False:
            # The transport can succeed while the camera returns a tool error.
            # Keep numeric diagnostics, never arbitrary provider text/payloads.
            message = str(meta.get("message", "")).lower()
            if "timeout" in message or meta.get("retCode") == -13:
                error = AnalysisError("截图超时，视频通道未出帧", "CAPTURE_TIMEOUT", "capture")
            elif "no signal" in message:
                error = AnalysisError("没有视频信号，请检查连接", "CAPTURE_NO_SIGNAL", "capture")
            else:
                error = AnalysisError("无法获取画面，请检查视频输入", "CAPTURE_UNAVAILABLE", "capture")
            if type(meta.get("retCode")) is int:
                error.provider_code = meta["retCode"]
            raise error
        stamp = meta.get("capturedAt")
        if stamp:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
            except (TypeError, ValueError, AttributeError):
                raise AnalysisError("截图时间格式异常", "CAPTURE_TIME", "capture") from None
            if age > 20 or age < -60:
                raise AnalysisError("截图时间异常，请检查设备时间与信号")
        for block in result.get("content", []):
            if block.get("type") == "image" and block.get("mimeType") in ("image/jpeg", "image/png"):
                image = base64.b64decode(block["data"], validate=True)
                if not image or len(image) > 12 * 1024 * 1024:
                    raise AnalysisError("截图大小异常")
                return image, block["mimeType"], meta
        raise AnalysisError("没有可分析的目标画面")
    finally:
        remote.close()


def capture():
    from .mcp import BridgeError
    for attempt in range(2):
        try:
            return _capture_once()
        except AnalysisError as error:
            if error.code == "CAPTURE_TIMEOUT" and attempt == 0:
                time.sleep(0.3)
                continue
            raise
        except BridgeError as error:
            detail = str(error).lower()
            transient = any(s in detail for s in ("timed out", "transport failed", "status 502", "status 503", "status 504"))
            if transient and attempt == 0:
                time.sleep(0.3)
                continue
            if "certificate" in detail or "fingerprint" in detail:
                message, code = "截图连接证书异常，请检查设备证书", "MCP_CERT"
            elif "token" in detail or "status 401" in detail or "status 403" in detail:
                message, code = "截图授权失败，请检查 MCP 密钥", "MCP_AUTH"
            elif transient:
                message, code = "截图连接中断，将按间隔重试", "MCP_CONNECTION"
            else:
                message, code = "截图服务响应异常，请检查 MCP", "MCP_PROTOCOL"
            raise AnalysisError(message, code, "capture") from error


def bounded_number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return min(1.0, max(0.0, float(value)))


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def validate_result(value, palette):
    if not isinstance(value, dict):
        raise AnalysisError("模型结果不是 JSON 对象")
    description = clean_text(value.get("description"), 600)
    if not description:
        raise AnalysisError("模型未返回可用描述")
    raw_tags = value.get("tags", [])
    if not isinstance(raw_tags, list):
        raise AnalysisError("模型标签格式错误")
    tags = normalize_tags(raw_tags, palette)
    return {"description": description, "tags": diverse_tags(tags, 8)}


def local_ocr(image, mime, settings):
    binary = Path("/kvmcomm/ocr/nano_ocr_app")
    if not binary.is_file():
        raise AnalysisError("本机没有 Go+ OCR 组件")
    # Isolate files and discard diagnostic output: it may contain OCR text.
    with tempfile.TemporaryDirectory(prefix="ocr-", dir=os.environ.get("DIARY_SCRATCH_DIR", "/tmp")) as folder:
        path = Path(folder) / ("frame.jpg" if mime == "image/jpeg" else "frame.png")
        path.write_bytes(image)
        path.chmod(0o600)
        try:
            result = subprocess.run([str(binary), "--image", str(path), "--log", "none"],
                                    cwd=folder, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:
            raise AnalysisError("本地 OCR 超时") from None
        output = path.with_name("frame_output_result.json")
        if result.returncode or not output.exists():
            raise AnalysisError("本地 OCR 未完成，请检查模型与可用内存")
        try:
            obj = json.loads(output.read_text())
            ocr = obj.get("ocr")
            if not isinstance(ocr, dict):
                raise ValueError("missing OCR object")
        except (ValueError, AttributeError):
            raise AnalysisError("本地识字结果异常，将按间隔重试", "OCR_OUTPUT", "ocr") from None
        text = clean_text(ocr.get("text"), 300)
        confidence = bounded_number(ocr.get("confidence"), 0.5)
    # These are explicit text rules, not a scene-understanding model.
    rules = [
        ("编程", "focus", r"python|javascript|typescript|github|终端|代码|编译|def |class "),
        ("系统设置", "neutral", r"设置|关于手机|关于本机|蓝牙|wi-fi|wlan|版本"),
        ("阅读", "focus", r"文章|阅读|文档|章节|教程|论文"),
        ("游戏", "relaxed", r"游戏|任务奖励|副本|签到|关卡|战斗"),
        ("聊天", "neutral", r"聊天|消息|微信|联系人|会话"),
        ("低落主题", "melancholy", r"抑郁|低落|悲伤|忧郁"),
        ("成人主题", "adult", r"色情|成人内容|pornography|nsfw"),
        ("愉快主题", "positive", r"开心|欢乐|喜悦|愉快"),
        ("紧张主题", "tense", r"紧张|焦虑|惊悚")
    ]
    tags = [{"label": label, "category": category, "weight": 0.7, "confidence": confidence}
            for label, category, pattern in rules if re.search(pattern, text, re.I)]
    if text:
        # Avoid copying long identifiers in the visible excerpt.
        excerpt = re.sub(r"\b[\w-]{24,}\b|\d{12,}", "[长标识已略]", text)[:140]
        description = "本地文字观察：画面可见“" + excerpt + "”。"
        if not tags:
            tags = [{"label": "文字内容", "category": "neutral", "weight": 0.5, "confidence": confidence}]
    else:
        description = "本地文字观察：未识别到清晰文字，无法据此描述图像内容。"
        tags = [{"label": "待识别", "category": "neutral", "weight": 0.2, "confidence": 0.3}]
    return validate_result({"description": description, "tags": tags}, settings["palette"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AnalysisError("模型服务发生重定向，请填写最终 Base URL")


def remote_vision(image, mime, settings):
    base = os.environ.get("DIARY_VISION_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("DIARY_VISION_MODEL", "").strip()
    url = urlsplit(base)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.query or url.fragment:
        raise AnalysisError("请在网页应用配置中填写视觉模型 Base URL")
    if not model:
        raise AnalysisError("请配置视觉模型 ID")
    prompt = (APP_DIR / "skills/desktop-observation.md").read_text()
    prompt += "\n可用分类：" + json.dumps(settings["palette"], ensure_ascii=False)
    payload = {"model": model, "stream": False, "temperature": 0.2, "max_tokens": 700,
               "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": [
                   {"type": "text", "text": "描述这张当前桌面截图，并生成标签 JSON。"},
                   {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(image).decode()}}
               ]}]}
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("DIARY_VISION_API_KEY", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(base + "/chat/completions", json.dumps(payload).encode(), headers)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=60) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise AnalysisError("模型响应过大")
        reply = json.loads(raw)
        content = reply["choices"][0]["message"].get("content")
        # Do not turn private chain-of-thought fields into user-visible logs.
        if not isinstance(content, str) or not content.strip():
            raise AnalysisError("模型未返回正文，请检查多模态与输出设置")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        return validate_result(json.loads(content), settings["palette"])
    except AnalysisError:
        raise
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise AnalysisError("模型授权失败，请检查 API 密钥", "VISION_AUTH", "vision") from None
        if error.code == 429:
            raise AnalysisError("模型服务繁忙，将按间隔重试", "VISION_LIMIT", "vision") from None
        raise AnalysisError(f"模型服务返回 HTTP {error.code}", "VISION_HTTP", "vision") from None
    except urllib.error.URLError:
        raise AnalysisError("无法连接模型服务，请检查网络与地址", "VISION_NETWORK", "vision") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise AnalysisError("模型结果格式异常，将按间隔重试", "VISION_OUTPUT", "vision") from None
    except Exception:
        raise AnalysisError("远程视觉请求未完成，将按间隔重试", "VISION_REQUEST", "vision") from None


def observe(settings):
    started = time.monotonic()
    try:
        image, mime, metadata = capture()
    except AnalysisError as error:
        error.stage = "capture"
        raise
    except Exception as error:
        raise AnalysisError("截图异常，点状态查看详情", "CAPTURE_UNEXPECTED", "capture") from error
    captured_at = metadata.get("capturedAt") or datetime.now(timezone.utc).isoformat()
    backend = settings["backend"]
    try:
        result = (local_ocr if backend == "local_ocr" else remote_vision)(image, mime, settings)
    except AnalysisError as error:
        error.stage = "ocr" if backend == "local_ocr" else "vision"
        raise
    except Exception as error:
        stage = "ocr" if backend == "local_ocr" else "vision"
        raise AnalysisError("本地识字异常" if stage == "ocr" else "远程视觉异常", stage.upper() + "_UNEXPECTED", stage) from error
    return {"schema_version": 1, "captured_at": captured_at, "source": backend,
            "capture_id": metadata.get("captureId", ""),
            "frame_sha256": hashlib.sha256(image).hexdigest(),
            "elapsed_seconds": round(time.monotonic() - started, 2), **result}
