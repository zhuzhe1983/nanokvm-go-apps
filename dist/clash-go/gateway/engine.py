"""Mihomo lifecycle and USB-only routing. No third-party subscription converter."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import shutil
import tempfile
import subprocess
import threading
import time
import urllib.request
import urllib.parse

from .common import APP, ROOT, DEFAULTS, UserError, private_keys, read, write, write_bytes, subscription_url
from . import oix
from .latency import Latency, probe
from .subscription import download
from .network import Network, addresses, command, wifi_status, wifi_scan, wpa_set


def core_config(settings, secret, root, usb_ip):
    managed = settings.get("source") == "oix"
    provider_name = "oixCloud" if managed else "account"
    provider = {"type": "file", "path": "providers/oixCloud", "health-check": {"enable": False}} if managed else {
        "type": "file", "path": "providers/account.yaml" if settings["subscription_url"] else "providers/empty.yaml"}
    rules = [f"IP-CIDR,{cidr},DIRECT,no-resolve" for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")]
    if settings["mode"] == "rule":
        rules += ["DOMAIN-SUFFIX," + domain + ",DIRECT" for domain in settings.get("direct_domains", ["cn", "local", "lan"])]
    rules.append("MATCH,PROXY")
    return {"mixed-port": 7890, "allow-lan": False, "bind-address": "127.0.0.1",
            "mode": "rule", "log-level": "silent", "ipv6": False,
            "interface-name": "wlan0", "external-controller": "127.0.0.1:9090", "secret": secret,
            "profile": {"store-selected": True}, "unified-delay": True,
            "tun": {"enable": True, "device": "cg-tun", "stack": "system", "auto-route": False,
                    "auto-redirect": False, "auto-detect-interface": False, "mtu": 1500,
                    "dns-hijack": ["any:53", "tcp://any:53"]},
            "dns": {"enable": True, "listen": usb_ip + ":1053", "ipv6": False,
                    "enhanced-mode": "redir-host", "fake-ip-range": "198.19.0.1/16",
                    "default-nameserver": settings["dns"], "nameserver": settings["dns"]},
            "proxy-providers": {provider_name: provider},
            "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["REJECT"], "use": [provider_name]}],
            "rules": rules}


class Engine:
    def __init__(self, root=ROOT, binary=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.binary = str(binary or root / "bin/mihomo")
        self.oix_binary = str(root / "bin/mihomo-oix")
        self.settings = {**DEFAULTS, **read(root / "settings.json", {})}
        self.keys = private_keys(root, shorten_pin=True)
        self.net = Network(root)
        self.process = None
        self.lock = threading.RLock()
        self.error = ""
        self.latency = Latency(lambda provider, name: probe(self.keys["api"], provider, name))
        self.wifi_pending = None
        self.wifi_deadline = 0
        (root / "providers").mkdir(exist_ok=True, mode=0o700)
        write(root / "providers/empty.yaml", {"proxies": []})

    def api(self, path, method="GET", body=None, timeout=3):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request("http://127.0.0.1:9090" + path, data,
                  {"Authorization": "Bearer " + self.keys["api"], "Content-Type": "application/json"}, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(1024 * 1024)
            return json.loads(payload) if payload else {}
        except Exception:
            raise UserError("Clash 内核暂不可用，请查看运行状态") from None

    def nodes(self):
        try:
            group = self.api("/proxies/PROXY")
            return [name for name in group.get("all", []) if name not in ("REJECT", "DIRECT")], group.get("now", "")
        except UserError:
            return [], ""

    def stop_core(self):
        self.latency.reset()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
        self.process = None

    def configured(self):
        return bool(self.settings.get("oix", {}).get("token")) if self.settings.get("source") == "oix" else bool(self.settings["subscription_url"])

    def runtime(self):
        if self.settings.get("source") != "oix":
            return self.root
        name = self.settings.get("oix_home", "")
        if not re.fullmatch(r"oix-[a-z0-9_]+", name):
            raise UserError("oixCloud 缓存目录无效，请重新登录")
        return self.root / "runtimes" / name

    def start_core(self):
        self.stop_core()
        managed = self.settings.get("source") == "oix"
        home = self.runtime()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (home / "providers").mkdir(exist_ok=True, mode=0o700)
        # Migrate old HTTP providers; subsequent boots use the validated cache.
        if not managed and self.settings["subscription_url"] and not (home / "providers/account.yaml").exists():
            write_bytes(home / "providers/account.yaml", download(self.settings["subscription_url"]))
        addr = addresses().get("usb0", "127.0.0.1/8").split("/")[0]
        config = core_config(self.settings, self.keys["api"], home, addr)
        write(home / "config.json", config)
        binary = self.oix_binary if managed else self.binary
        env = {k: v for k, v in os.environ.items() if not k.startswith("OIX_")}
        env.update(GOMEMLIMIT="64MiB", GOGC="50")
        if managed:
            env.update(OIX_TOKEN=self.settings["oix"]["token"], OIX_CLIENT="openclash")
        args = [binary, "-d", str(home), "-f", str(home / "config.json")]
        # Raw parse messages may contain credentials. Never expose them.
        result = subprocess.run([binary, "-t", *args[1:]], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=45, env=env)
        if result.returncode:
            raise UserError("内核无法解析订阅配置，请使用兼容的 Clash / Mihomo 格式")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        deadline = time.monotonic() + (45 if managed else 10)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise UserError("Clash 内核启动失败，请检查内存或 TUN 支持")
            try:
                group = self.api("/proxies/PROXY", timeout=.25)
                if group.get("type") == "Selector" and Path("/sys/class/net/cg-tun").exists():
                    if not self.configured() or any(n not in ("DIRECT", "REJECT") for n in group.get("all", [])):
                        return
            except UserError:
                pass
            time.sleep(.1)
        self.stop_core()
        if managed:
            raise UserError("oixCloud 托管节点未加载；请检查网络、节点筛选器或 Token 权限")
        raise UserError("订阅已下载，但内核未加载节点；请检查 proxies 格式及协议是否兼容")

    def boot(self):
        self.wifi_pending = read(self.root / "wifi-pending.json")
        if self.wifi_pending:
            self.finish_wifi(False)
        self.net.cleanup()
        if self.configured():
            try:
                self.start_core()
                self.choose(self.settings["selected"])
            except Exception as error:
                self.error = str(error) if isinstance(error, UserError) else "订阅内核未就绪，USB 代理流量已阻断"
                if self.settings["enabled"]:
                    self.net.chain("filter", "CG_OUT", [["-j", "DROP"]])
                    self.net.hook("filter", "FORWARD", ["-i", "usb0", "-j", "CG_OUT"])
                    self.net.chain("filter", "CG_V6", [["-j", "DROP"]], six=True)
                    self.net.hook("filter", "FORWARD", ["-i", "usb0", "-j", "CG_V6"], six=True)
                    return
        self.net.apply(self.settings["sharing"], self.settings["enabled"])
        write(self.root / "settings.json", self.settings)

    def choose(self, name):
        names, _ = self.nodes()
        if not names:
            raise UserError("订阅已下载，但内核未加载可用节点；请检查是否包含兼容的 proxies 配置")
        name = name if name in names else names[0]
        self.api("/proxies/PROXY", "PUT", {"name": name})
        self.settings["selected"] = name

    def change(self, action, value):
        with self.lock:
            before = copy.deepcopy(self.settings)
            provider_path = self.root / "providers/account.yaml"
            previous_provider = provider_path.read_bytes() if provider_path.exists() else None
            candidate_home = None
            account = None
            core_changed = False
            provider_changed = False
            try:
                if action in ("subscription", "subscription_refresh"):
                    url = subscription_url(value if action == "subscription" else self.settings["subscription_url"])
                    payload = download(url)
                    write_bytes(provider_path, payload)
                    provider_changed = True
                    self.settings.update(subscription_url=url, source="subscription", subscription_updated_at=int(time.time()))
                    if before["enabled"]:
                        self.net.chain("filter", "CG_OUT", [["-j", "DROP"]])
                    core_changed = True
                    self.start_core()
                    self.choose(self.settings["selected"])
                elif action in ("oix_login", "oix_refresh"):
                    token = value if action == "oix_login" else self.settings.get("oix", {}).get("token", "")
                    account = oix.account(self.oix_binary, token)
                    parent = self.root / "runtimes"
                    parent.mkdir(exist_ok=True, mode=0o700)
                    candidate_home = Path(tempfile.mkdtemp(prefix="oix-", dir=parent))
                    self.settings.update(source="oix", oix=account, oix_home=candidate_home.name)
                    if before["enabled"]:
                        self.net.chain("filter", "CG_OUT", [["-j", "DROP"]])
                    core_changed = True
                    self.start_core()
                    self.choose(self.settings["selected"])
                elif action == "oix_info":
                    token = self.settings.get("oix", {}).get("token", "")
                    current = self.settings.get("oix", {})
                    self.settings["oix"] = {**current, **oix.account(self.oix_binary, token)}
                elif action == "oix_logout":
                    # Local sign-out: never revoke a token used by other devices.
                    if before.get("source") == "oix":
                        core_changed = True
                        self.stop_core()
                        self.settings.update(enabled=False, sharing=False, source="subscription", selected="")
                    self.settings.update(oix={}, oix_home="")
                elif action == "enabled":
                    if type(value) is not bool:
                        raise UserError("开关格式错误")
                    if value:
                        if not self.configured():
                            raise UserError("请先在网页导入你的节点订阅")
                        if not self.process or self.process.poll() is not None:
                            core_changed = True
                            self.start_core()
                        self.choose(self.settings["selected"])
                        self.settings["sharing"] = True
                    self.settings["enabled"] = value
                elif action == "sharing":
                    if type(value) is not bool:
                        raise UserError("开关格式错误")
                    self.settings["sharing"] = value
                    if not value:
                        self.settings["enabled"] = False
                elif action == "node":
                    if value not in self.nodes()[0]:
                        raise UserError("节点不存在，请刷新订阅")
                    self.choose(value)
                elif action == "network":
                    if not isinstance(value, dict) or value.get("mode") not in ("rule", "global"):
                        raise UserError("网络模式格式错误")
                    dns = value.get("dns", [])
                    if not isinstance(dns, list) or not 1 <= len(dns) <= 3:
                        raise UserError("请填写 1～3 个 IPv4 DNS 地址")
                    try:
                        dns = [str(ipaddress.IPv4Address(v)) for v in dns]
                    except (ValueError, TypeError):
                        raise UserError("DNS 地址格式错误") from None
                    domains = value.get("direct_domains", [])
                    if not isinstance(domains, list) or len(domains) > 100 or any(
                            not isinstance(d, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", d) for d in domains):
                        raise UserError("直连域名格式错误，最多 100 项")
                    self.settings.update(mode=value["mode"], dns=dns, direct_domains=domains)
                    if self.process:
                        if before["enabled"]:
                            self.net.chain("filter", "CG_OUT", [["-j", "DROP"]])
                        core_changed = True
                        self.start_core(); self.choose(self.settings["selected"])
                else:
                    raise UserError("未知操作")
                self.net.apply(self.settings["sharing"], self.settings["enabled"])
                write(self.root / "settings.json", self.settings)
                self.error = ""
                old_home = before.get("oix_home", "")
                if old_home and old_home != self.settings.get("oix_home") and re.fullmatch(r"oix-[a-z0-9_]+", old_home):
                    shutil.rmtree(self.root / "runtimes" / old_home, ignore_errors=True)
            except Exception as error:
                self.settings = before
                # Restore old core/provider after a failed import/config change.
                if core_changed or provider_changed:
                    try:
                        self.stop_core()
                        if previous_provider is not None:
                            write_bytes(provider_path, previous_provider)
                        else:
                            provider_path.unlink(missing_ok=True)
                        if self.configured():
                            self.start_core(); self.choose(before["selected"])
                        else:
                            self.stop_core()
                    except Exception:
                        # Keep the requested proxy state closed until it can recover.
                        self.error = "恢复内核失败，USB 代理流量已阻断"
                try:
                    self.net.apply(before["sharing"], before["enabled"])
                except Exception:
                    self.error = "网络未能恢复，请关闭共享后重试"
                if candidate_home:
                    shutil.rmtree(candidate_home, ignore_errors=True)
                if account and before.get("source") != "oix":
                    # Account access and node availability are separate. A user
                    # without eligible nodes still needs to see the plan status.
                    account["node_error"] = str(error) if isinstance(error, UserError) else "托管节点加载失败"
                    self.settings["oix"] = account
                    try:
                        write(self.root / "settings.json", self.settings)
                        error = UserError("oixCloud 账号已登录，套餐已取得；" + account["node_error"])
                    except OSError:
                        self.settings = before
                raise error if isinstance(error, UserError) else UserError("配置未完成，原配置已保留") from None

    def measure_latency(self, names, force=False):
        if not isinstance(names, list) or not 1 <= len(names) <= 3 or any(not isinstance(n, str) for n in names) or type(force) is not bool:
            raise UserError("每次只能测当前页的 1～3 个节点")
        if not self.lock.acquire(blocking=False):
            raise UserError("正在更新配置，请稍后测速")
        try:
            current, _ = self.nodes()
            if any(n not in current for n in names):
                raise UserError("节点已更新，请重新打开节点列表")
            provider = "oixCloud" if self.settings.get("source") == "oix" else "account"
            return self.latency.request(names, provider, force)
        finally:
            self.lock.release()

    def status(self):
        names, selected = self.nodes() if self.process else ([], "")
        running = bool(self.process and self.process.poll() is None)
        if self.settings["enabled"] and not running:
            self.error = "代理内核已停止，USB 代理流量已阻断"
        latencies, latency_busy = self.latency.snapshot(names)
        account = self.settings.get("oix", {})
        managed = self.settings.get("source") == "oix"
        return {"version": "0.2.3", "core_version": oix.VERSION if managed else "v1.19.32", "running": running,
                "source": self.settings.get("source", "subscription"),
                "oix": {"connected": bool(account.get("token")), "active": managed,
                        "account": {k: account.get("account", {}).get(k, "") for k in oix.FIELDS},
                        "updated_at": account.get("updated_at", 0), "node_error": account.get("node_error", "")},
                "subscription_updated_at": self.settings.get("subscription_updated_at", 0),
                "enabled": self.settings["enabled"], "sharing": self.settings["sharing"],
                "mode": self.settings["mode"], "dns": self.settings["dns"],
                "direct_domains": self.settings.get("direct_domains", ["cn", "local", "lan"]),
                "subscription_saved": self.configured(), "manual_subscription_saved": bool(self.settings["subscription_url"]), "nodes": names, "selected": selected,
                "interfaces": addresses(), "wifi": wifi_status(), "error": self.error,
                "latencies": latencies, "latency_busy": latency_busy,
                "wifi_pending": self.wifi_pending is not None,
                "wifi_seconds": max(0, int(self.wifi_deadline - time.monotonic()))}

    def connect_wifi(self, ssid, password):
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
            raise UserError("Wi-Fi 名称须为 1～32 字节")
        if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
            raise UserError("WPA2 密码须为 8～63 位；开放网络留空")
        with self.lock:
            if self.wifi_pending:
                raise UserError("请先确认或取消上次 Wi-Fi 更改")
            old = command(["wpa_cli", "-i", "wlan0", "list_networks"]).stdout
            previous = next((line.split("\t")[0] for line in old.splitlines() if "[CURRENT]" in line), None)
            result = command(["wpa_cli", "-i", "wlan0", "add_network"]).stdout.strip()
            if not result.isdigit():
                raise UserError("无法新建 Wi-Fi 配置")
            try:
                wpa_set(result, "ssid", ssid.encode().hex())
                if password:
                    wpa_set(result, "psk", json.dumps(password, ensure_ascii=False))
                else:
                    wpa_set(result, "key_mgmt", "NONE")
                self.wifi_pending = {"old": previous, "new": result}
                write(self.root / "wifi-pending.json", self.wifi_pending)
                command(["wpa_cli", "-i", "wlan0", "select_network", result])
                self.wifi_deadline = time.monotonic() + 60
                threading.Thread(target=self._wifi_timer, daemon=True).start()
            except Exception:
                command(["wpa_cli", "-i", "wlan0", "remove_network", result], check=False)
                self.wifi_pending = None
                (self.root / "wifi-pending.json").unlink(missing_ok=True)
                raise

    def _wifi_timer(self):
        while self.wifi_pending and time.monotonic() < self.wifi_deadline:
            time.sleep(1)
        if self.wifi_pending:
            self.finish_wifi(False)

    def finish_wifi(self, keep):
        with self.lock:
            pending = self.wifi_pending
            if not pending:
                raise UserError("没有待确认的 Wi-Fi 更改")
            if keep:
                current = wifi_status()
                if current["wpa_state"] != "COMPLETED" or current["id"] != pending["new"]:
                    raise UserError("新 Wi-Fi 尚未连接成功")
                if "FAIL" in command(["wpa_cli", "-i", "wlan0", "save_config"]).stdout:
                    raise UserError("Wi-Fi 保存失败，请检查固件配置")
            else:
                command(["wpa_cli", "-i", "wlan0", "remove_network", pending["new"]], check=False)
                if pending["old"]:
                    command(["wpa_cli", "-i", "wlan0", "select_network", pending["old"]], check=False)
            self.wifi_pending = None
            (self.root / "wifi-pending.json").unlink(missing_ok=True)

    def close(self):
        if self.wifi_pending:
            self.finish_wifi(False)
        self.net.cleanup()
        self.stop_core()
