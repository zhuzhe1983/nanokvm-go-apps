"""Bounded HTTPS downloads; never expose credential-bearing exceptions."""
import socket
import ssl
import urllib.error
import urllib.request
from .common import UserError, subscription_url

LIMIT = 4 * 1024 * 1024


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise permits a downgrade to unencrypted HTTP.
        subscription_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    url = subscription_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPSRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "clash", "Accept": "application/yaml, text/yaml, */*"})
    try:
        with opener.open(request, timeout=20) as response:
            payload = response.read(LIMIT + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise UserError("订阅服务器拒绝访问（HTTP %d），请更新订阅地址或检查权限" % error.code) from None
        raise UserError("订阅下载失败（HTTP %d），原节点已保留" % error.code) from None
    except (TimeoutError, socket.timeout):
        raise UserError("订阅下载超时，请检查设备 Wi-Fi 后重试") from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            raise UserError("订阅 HTTPS 证书校验失败，请检查设备时间和订阅地址") from None
        raise UserError("设备无法连接订阅服务器，请检查 Wi-Fi、DNS 或稍后重试") from None
    except (OSError, ValueError):
        raise UserError("订阅连接中断，请稍后重试；原节点已保留") from None
    if len(payload) > LIMIT:
        raise UserError("订阅超过 4 MiB，请减少节点或精简订阅")
    if not payload.strip():
        raise UserError("订阅服务器返回空内容")
    if payload.lstrip()[:80].lower().startswith((b"<!doctype html", b"<html")):
        raise UserError("订阅返回了网页，请填写 Clash / Mihomo 订阅直链")
    return payload
