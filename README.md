# NanoKVM Go+ Apps

为 NanoKVM Go+ 开发的原生触摸屏应用。每个应用使用独立目录，`dist/` 是统一的 App Server 发布目录。

| 应用 | 版本 | 功能 | 发布包 |
| --- | --- | --- | --- |
| [Desktop Diary](desktop-diary/README.md) | 0.2.2 | 每 30 秒观察桌面，按设备记录 TXT，中文标签云；独立的本地 OCR / 远程视觉模式 | [ZIP](dist/desktop-diary-0.2.2.zip) · [SHA-256](dist/desktop-diary-0.2.2.zip.sha256) |
| [Clash Go](clash-go/README.md) | 0.2.1 | Mihomo 代理、USB 网络共享、oixCloud、HTTPS 网络配置、触摸节点选择与延迟 | [ZIP](dist/clash-go-0.2.1.zip) · [SHA-256](dist/clash-go-0.2.1.zip.sha256) |

## 安装

下载 ZIP，在 NanoKVM Web 的“设置 → 应用 → 上传 ZIP”安装，再从设备 Apps 打开。原厂启动器使用英文应用名，应用内部自行渲染中文。

- **Desktop Diary**：首次启动暂停，确认被控设备档案后开始记录。完整画面描述需要自行配置支持图像输入的 OpenAI 兼容服务；Go+ 本地 OCR 仅做文字摘录和规则标签。模型地址、模型 ID、模型密钥和 MCP 密钥在官方应用配置表单填写。
- **Clash Go**：首次安装代理和 USB 共享关闭；升级保留已有设置。小屏“网络配置 → 网页配置”显示地址及 6 位配置码，浏览器通过 HTTPS 访问 8443 端口。自行提供订阅或 oixCloud Access Token。节点延迟为 HTTPS 请求耗时，不是带宽测速。

Clash Go 的后台网络服务在退出触摸面板后继续运行；Desktop Diary 退出后停止采集。可在 Clash Go 提供网络时使用桌面日记。游戏伴侣和智能运维目前是设计方向，尚未实现。

## 开发与构建

目标环境为 NanoKVM Go+ ARM32 固件、Python 3 和原厂 `appbase` SDK。先核对设备 `/kvmcomm/apps/` 下的 SDK 文档与接口，不覆盖共享 SDK。中文字体与 FreeType 复用设备系统资源。

```sh
python3 -m unittest discover -s desktop-diary/tests -v
python3 -m unittest discover -s clash-go/tests -v
python3 desktop-diary/build.py
python3 clash-go/build.py
```

构建脚本按文件白名单生成 `dist/<app>/` 和单顶级目录的 ZIP。仓库仅保留当前版本的发布包；新版本需要同步 README 和 `.gitignore` 中的 ZIP 白名单。历史发布包和调试资料可留在本地，不参与提交。

Clash Go 包含经 SHA-256 固定的官方 Mihomo ARMv7 内核和 oix 专用内核，版本、来源及 GPL-3.0 许可见 [应用说明](clash-go/README.md) 与 `vendor/`。这些内核按订阅来源二选一启动。

运行数据写入设备 `/var/lib/nanokvm-agent/<app>/`，模型与 MCP 凭据由安装表单或受保护配置提供。仓库不包含可直接使用的账号、Token、订阅地址或设备连接配置。提交前检查源码和解压后的发布包，方法见 [发布与敏感信息检查](SECURITY.md)。

## 设计与硬件资料

- [应用方向与功能脚本契约](PRODUCT_DIRECTIONS.md)
- [Go+ NPU 与本地视觉模型研究](NPU_RESEARCH.md)
- [Sipeed 自定义应用文档](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_Go/custom_app.html)
- [NanoKVM Go Apps SDK](https://github.com/sipeed/NanoKVM-Go-Apps/tree/base)

NPU 研究包含指定日期、固件和画面下的实测值；不代表所有输入的性能。Clash Go 只管理 USB 转发和自有路由、防火墙规则，具体兼容性与功能边界见各应用 README。
