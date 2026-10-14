# Clash Go 0.2.1

NanoKVM Go+ ARM32 的 Mihomo 代理与 USB 网络共享应用。应用列表使用英文名称，应用内由 FreeType 渲染中文。参考 OpenClash 的订阅、策略组、服务开关设计；不安装依赖 OpenWrt 的 LuCI / OpenClash 软件包。

## 使用

1. 从设备 Apps 页面打开 **Clash Go**。
2. 小屏有代理开关、节点选择、网络配置及 USB 共享开关。节点右侧显示真实 HTTPS 延迟（ms），低于 300 ms 为绿、300～799 ms 为黄、更高为红；未测/测速中/超时/失败分别标注。进入节点页和翻页自动测当前 3 个节点，结果缓存 60 秒，点“刷新延迟”可重测。最多同时测 2 个节点，单次 8 秒超时，不切换当前节点；退出节点页后不再发起新探测。延迟目标为 Google gstatic 的 generate_204，不是带宽测速。
3. 网络配置 → 网页配置：浏览器打开小屏所示 **HTTPS 地址，端口 8443**，输入 6 位数字配置码。使用设备自身的 HTTPS 证书。
4. **oixCloud**：在网页粘贴 **Access Token**，点击“登录并加载节点”。应用通过官方专用核心取得套餐、到期时间、已用/剩余/今日流量，再获取托管节点。专用核心支持其高级协议配置，实际线路仍由套餐及官网节点筛选器决定。也可在“通用订阅”直接导入 Clash / Mihomo 地址；两种来源切换使用。
5. 导入订阅后选择节点、开启代理。开启代理同时开启 USB 网络共享；关闭代理保留直连共享，关闭共享则一并关闭代理。

只有成功取得节点后才切换来源；失败恢复原节点。账号认证成功但没有可用托管节点时，仍可显示套餐及加载错误；更换正在使用的 oixCloud 账号失败时保留原账号。网页可分别刷新套餐、更新托管节点、更新通用订阅。oixCloud 核心每天自动更新托管节点；通用订阅使用“更新并使用已存订阅”手动刷新。退出 oixCloud 仅清理本机账号和托管缓存，不撤销其他设备的 Token；若当前使用该账号，同时关闭 USB 共享。

未导入有效节点时不能启用代理。初次安装所有转发开关均关闭。Wi-Fi / USB 地址动态显示；不硬编码家庭 IP。不修改 USB 的原厂 DHCP 地址与租约配置。

## 网络配置

- 规则模式：局域网地址以及可编辑的域名后缀直连，其余流量走选择的节点。默认域名后缀为 `cn/local/lan`；不是完整的国内 IP 数据库规则。
- 全局模式：除局域网地址外均走所选节点。
- DNS：1～3 个 IPv4 服务器；代理开启时重定向 USB 设备普通 TCP/UDP 53 请求至 Mihomo DNS。应用自身的 DoH/DoT 不属于普通 DNS 重定向。
- Wi-Fi：扫描或手动输入 SSID，支持 WPA2 密码或开放网络。切换后有 60 秒确认期，不确认则恢复原网络；建议通过 USB 网关地址访问配置页。未实现 WPA-Enterprise / Portal 认证。
- 当前只转发 IPv4；开启共享时拒绝 USB 的 IPv6 转发，避免绕过所选代理。

网络只匹配 `usb0` 输入。Mihomo TUN 不修改设备默认路由；独立路由表 `2023`、规则优先级 `12023`，iptables 只管理 `CG_*` 自有链。KVM 管理、本机截图、日记模型请求不被本应用接管。代理内核异常时 USB 代理路由不可达，不自动降为直连；关闭代理才显式恢复直连共享。

## 生命周期与数据

触摸 App 是控制面板；点击右上角 Apps 返回列表，后台 `clash-go.service` 继续服务。因此可与 Desktop Diary 等前台应用配合，但设备仍一次只显示一个 App。内核设置 64 MiB Go 内存软目标；服务申请 112 MiB cgroup 上限，但当前固件不提供 MemoryCurrent，不能确认该硬限制生效。内存与吞吐量需按节点协议、订阅大小和 KVM 负载实测。

配置、Access Token、订阅、运行内核和节点缓存在 `/var/lib/nanokvm-agent/clash-go/`（目录 0700 / 配置 0600）。网页不回传 Token 或订阅密钥；套餐显示最近一次成功刷新的时间。Token 通过 stdin 交给官方账号客户端；专用核心运行时从私有进程环境读取，命令行不带 Token。网站使用带限速的配置码登录、HttpOnly/Secure/SameSite cookie、同源写入校验。Mihomo 管理 API 和调试混合代理端口只监听回环地址；局域网通过 HTTPS 配置页管理。

安装：`python3 build.py` 生成 `../dist/clash-go-0.2.1.zip`，上传 NanoKVM App 安装页，后置脚本安装 systemd 服务及包内经 SHA-256 校验的官方 Mihomo **v1.19.32 ARMv7** 以及专用 **alpha-oix-9a6b3c1 ARMv7**；两者按节点来源启动其中一个。专用核心固定版本和 SHA-256，来自滚动 Pre-Alpha 发布，不自动替换内核。不需要安装 Python 第三方依赖，不覆盖共享 `appbase` SDK。

停用：先从页面关闭 USB 共享，再 `systemctl disable --now clash-go.service`。删除应用前也应先停止服务，保留数据目录可恢复订阅；需要彻底清理时再单独处理其中的私有配置。

## 参考与许可

- [OpenClash](https://github.com/vernesong/OpenClash)：面向 OpenWrt 的管理界面参考，没有复制其代码。
- [Mihomo 官方 v1.19.32](https://github.com/MetaCubeX/mihomo/releases/tag/v1.19.32)：包内 ARMv7 二进制来源及对应源码；许可随 `vendor/MIHOMO-LICENSE` 分发。
- [Mihomo TUN](https://wiki.metacubex.one/config/inbound/tun/) 与 [代理集合](https://wiki.metacubex.one/config/proxy-providers/)。
- [oixCloud 官方 OpenClash 接入说明](https://docs.dler.io/en/other/openclash.html) 与 [维护中的集成](https://github.com/pickrui/OpenClash)。
- [Mihomo oix 专用核心源码](https://github.com/vernesong/mihomo-oix/tree/9a6b3c1ced307ac78f3725c8a847420df4a486f3)；GPL-3.0 许可见 `vendor/MIHOMO-OIX-LICENSE`。使用其 `oix account` 命令、`OIX_TOKEN` 和 `oixCloud` 管理 provider，不解密或改写高级协议字段，不调用购买/签到/修改筛选器接口。
- [oixCloud 官网](https://oixcloud.com/)。

验证记录见工作区 `VALIDATION.md`。安装成功不等于账号节点可用，必须用实际订阅另行验证代理出口。
