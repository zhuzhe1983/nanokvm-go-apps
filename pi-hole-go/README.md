# Pi-hole Go

为经 NanoKVM Go+ 的 USB 网卡、借用 Wi-Fi 上网的电脑或手机提供 DNS 广告过滤。使用官方 Pi-hole FTL 6.8 ARMv7 内核，配有原生触屏面板和 HTTPS 设置页。

0.1.0 是首个适配版本，已完成容器中的 ARMv7 内核、真实 DNS 流量和浏览器验证；尚待 NanoKVM 实机验证。Go+ 可供 Linux 使用的内存少于 Pi-hole 完整安装的官方建议，本应用缩短统计窗口、关闭长期查询记录并限制规则数量；容器测试不能替代实机与 Clash 并行运行的资源测试。

## 使用

1. 先在 NanoKVM 设置中开启 NCM 虚拟网卡，连接 Wi-Fi，在 **Clash Go 0.2.3 或更新版本**开启 USB 共享。代理可按需开关。
2. 从原厂应用设置上传 `pi-hole-go-0.1.0.zip`。首次安装默认关闭拦截；升级保留配置。
3. 打开小屏 **Pi-hole Go**，点击“开启拦截”。绿色“正在拦截”表示内核和 DNS 转发已就绪；网络共享和 Wi-Fi 仍需正常工作。
4. “过滤设置”显示当前设备地址和随机生成的 6 位配置码。浏览器通过 **HTTPS 的 8444 端口**访问。证书复用 NanoKVM 自带证书，浏览器可能提示为本地证书。

小屏可开关拦截、暂停 5 分钟或提前恢复，并查看近 1 小时的拦截数量。网页提供拦截比例、白名单、自定义拦截、上游 DNS、每日自动更新和手动更新。暂停到期会自动恢复；退出小屏应用不会停止后台过滤。

白名单和自定义拦截均以域名为单位，覆盖其子域名，各最多 200 条。同一个域名不能同时填写在两份名单中。白名单优先于过滤规则。规则更新失败时保留原规则；修改名单会重建数据库并短暂重启 FTL，期间使用原 DNS 通路。

## 生效范围

```text
USB 电脑 / 手机 → Pi-hole 域名过滤 → Clash DNS（代理开启时）→ Wi-Fi
                                → 配置的上游 DNS（直连时）→ Wi-Fi
```

- 仅处理从 `usb0` 进入的常规 IPv4 DNS 请求，包括 TCP 和 UDP 53 端口；原厂 DHCP 继续工作。
- 家里其他 Wi-Fi 设备不会自动受到过滤。不更改路由器 DNS，不修改 NanoKVM 主路由或系统 resolver。
- IPv6 转发中的明文 DNS 被阻断，以免绕过 IPv4 过滤；此版本不提供完整 IPv6 DNS 服务。
- 浏览器 DoH、系统私有 DNS/DoT、直接连接 IP，以及视频平台与正文共用域名的广告可能绕过过滤。应用不解密 HTTPS，不承诺拦截所有广告。
- Clash Go 0.2.3 会保留 Pi-hole 的 DNS 优先级。旧版 Clash 重新开启代理或共享时，可能越过 Pi-hole。

## 内核、规则与资源

- 官方 FTL **v6.8 ARMv7**，未经修改；安装时校验压缩包和解压后二进制的 SHA-256。
- 内置 **HaGeZi Light**，发布时为 **68,940** 条规则。每日从其官方 HTTPS 地址更新，上限 8 MiB / 250,000 条。只接受域名及简单 `||domain^` 规则，拒绝网页、脚本及不兼容格式。
- 不启动 Pi-hole DHCP、NTP 服务，不加载系统共享的 `dnsmasq.d`；FTL 自身 DNS 用 5354 端口，管理 API 仅监听 `127.0.0.1:5471`。
- 仅向面板提供近 1 小时汇总。FTL 隐藏域名和客户端，不记录 DNS 查询日志、不保留长期查询历史。重启 FTL 会重置近期计数。
- 保留 1,000 条 DNS 缓存；临时数据库使用磁盘，服务设置 96 MiB cgroup 上限（是否生效取决于固件）。诊断日志限制为每份约 1 MiB。
- 无第三方 Python 包依赖。触屏中文使用设备已有 FreeType、Pango/Cairo 和系统字体。

## 安装与卸载边界

运行数据位于 `/var/lib/nanokvm-agent/pi-hole-go/`，配置码和 API 凭据仅在设备生成。`/etc/pihole` 是指向本应用私有数据目录的链接；若该路径已属于其他安装，安装器会退出，不覆盖已有 Pi-hole。

安装器注册 `pi-hole-go.service`，不覆盖共享 `appbase` SDK。防火墙只管理 `PG_DNS`、`PG_IN`、`PG_V6` 及其精确入口。只有内核通过 DNS 和 API 就绪检查后，才接入重定向。关闭应用服务或服务异常退出时，清除自身规则；Clash 和原厂 DNS 通路保留。

停止并卸载后台服务（保留配置及应用文件，以便恢复）：

```sh
cd /kvmcomm/apps/pi-hole-go
python3 uninstall.py
```

先执行卸载脚本，再从原厂应用界面移除应用。直接删除应用目录不等于卸载后台服务。

## 开发与许可

```sh
python3 -m unittest discover -s pi-hole-go/tests -v
python3 pi-hole-go/build.py
```

生成 `dist/pi-hole-go/`、单个顶级目录的 ZIP 和 SHA-256 文件。测试、截图、设备数据和凭据不进入发布包。

第三方内核、数据库结构和规则的版权归其作者，完整许可及固定来源见 `vendor/` 和 `vendor/manifest.json`：

- [Pi-hole FTL v6.8](https://github.com/pi-hole/FTL/releases/tag/v6.8)，EUPL-1.2，见 `vendor/FTL-LICENSE`；[对应完整源码](https://github.com/pi-hole/FTL/tree/29c8b13f4bba0c4f4f47598e45a65cbf1cedac88)。
- [Pi-hole Core v6.5 数据库结构](https://github.com/pi-hole/pi-hole/blob/f503a65d3a07cb71fd931389f1d3babf718249aa/advanced/Templates/gravity.db.sql)，EUPL-1.2，见 `vendor/CORE-LICENSE`。
- [HaGeZi DNS Blocklists](https://github.com/hagezi/dns-blocklists)，GPL-3.0，见 `vendor/HAGEZI-LICENSE`。
- [Pi-hole 硬件要求](https://docs.pi-hole.net/main/prerequisites/)与[配置参考](https://docs.pi-hole.net/ftldns/configfile/)。

Pi-hole Go 是社区适配应用，非 Pi-hole 官方产品。
