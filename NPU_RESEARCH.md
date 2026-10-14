# Go+ NPU 与本地视觉模型研究

核对日期：2026-10-13。区分厂商资料、设备实测与尚未验证的推断。

## 硬件和资源

- [Sipeed 产品规格](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_Go/introduction.html)：Go+ 为双核 Cortex-A53、3.2 TOPS NPU、512 MB RAM、64 GB 存储。
- [系统构建文档](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_Go/system/introduction.html)：AX620Q，ARM32 / armhf。设备 `/proc/ax_proc/chip_type` 实读 `AX620QE_CHIP`。
- 注意精度口径：[AX620Q 通用芯片资料](https://axera-tech.com/zh-hans/product/2932.html)另写 2.4 TOPS INT8 / 9.6 TOPS INT4。不能据此把 Go+ 的 3.2 TOPS 自动标为 INT8；具体 SKU、频率和稀疏计算口径未获一致说明。保留 Go+ 整机标称值。
- 实测 `/proc/cmdline` 为 `mem=256M`，Linux 总内存约 246 MiB；另有 256 MiB CMM，供 NPU 与视频硬件共用。
- 本次 KVM 运行时 CMM 已用约 105.7 MiB、余约 **150.3 MiB**。这不是 512 MB 可全部装模型；64 GB eMMC 也不等于模型运行内存。
- NPU 驱动 V3.0.0（2024-11-20），系统 SDK V3.0.0（2025-03-19）；读取的 NPU 时钟 594 MHz。没有变更内存分区、时钟或系统服务。

## 能否放自己的模型

可以。它是可编程推理加速器，不是只能识字的固定电路。流程为：训练／选择模型 → ONNX → 量化校准 → Pulsar2 编译为 AX620E 目标的 `.axmodel` → ARM32 程序通过 AX_ENGINE 加载并运行 → CPU 后处理。模型转换通常在开发机完成，不在 Go+ 上训练。

依据：[Pulsar2 AX620E/AX620Q 快速入门](https://pulsar2-docs.readthedocs.io/zh-cn/latest/user_guides_quick/quick_start_ax620e.html)、[算子兼容表](https://pulsar2-docs.readthedocs.io/en/latest/appendix/op_support_list_ax620e.html)、[AX620Q YOLOv8 示例](https://github.com/AXERA-TECH/axera-pi-zero-docs/blob/main/source/doc_guide_algorithm.md)。不是把任意 `.onnx`、`.gguf` 或 `.safetensors` 改扩展名就能使用。

需要同时匹配目标芯片、NPU 模式、算子、Pulsar2／运行库版本、预处理、后处理和主机 ABI。AX650 的 axmodel 不能直接当 AX620Q 模型，AX630C 的模型也需核对运行条件；随模型分发的 ARM64 可执行文件不能直接运行于本机 ARM32 系统。

## 已完成的实测

设备提供 `/kvmcomm/ocr/nano_ocr_app`，支持指定 `--det_model`、`--rec_model`、`--dict` 及离线 `--image`。也提供帧 socket 输入。`/opt/lib/libax_engine.so` 已存在。官方 [NanoKVM_OCR 源码](https://github.com/sipeed/NanoKVM_OCR)可参考，但线上源码说明和设备二进制参数存在版本差异，以设备 `--help` 为准。

自带文字检测模型约 0.96 MB，识别模型约 3.15 MB。对一张真实 2560×1440 MCP 截图执行一次本地离线 OCR：

| 项目 | 本次结果 |
| --- | --- |
| 进程总耗时 | 7.105 秒，包含启动与退出 |
| OCR 内部总计 | 6.295 秒 |
| 六块检测的 NPU 推理 | 每块约 79–94 ms |
| 文字裁剪与识别阶段 | 4.844 秒 |
| OCR 块数 | 32 |
| 进程峰值 RSS（内核 rusage） | 43,312 KiB；不包含全部 CMM 分配 |

这不是所有模型都需要 7 秒，也不是纯 NPU 推理耗时。画面分辨率、文字块数、CPU 后处理都会影响总时间。未开启原厂 Memory Fabric 后台记录。用户已选择桌面日记默认 **30 秒**采集。

## Hugging Face 模型检索

本次在 [AXERA-TECH 模型库](https://huggingface.co/AXERA-TECH)与公开搜索中**未找到 Qwen3.8 的 axmodel 移植包**；这仅表示本次未查到，不代表所有私人或未来仓库都没有。官方 [Qwen3.8 集合](https://huggingface.co/collections/Qwen/qwen38)有 27B 多模态权重，但不等于 AX620Q 部署产物。

| 已找到的模型 | 能力与目标平台 | 对 Go+ 的判断 |
| --- | --- | --- |
| [Qwen3.5-0.8B INT4](https://huggingface.co/AXERA-TECH/Qwen3.5-0.8B-AX650-GPTQ-Int4-C128-P1152-CTX2047) | 视觉＋语言，AX650；卡片写 CMM 0.94 GiB | 芯片目标不同，内存远超本机余量，不能直接使用 |
| [Qwen3-VL-2B INT4 AX630C](https://huggingface.co/AXERA-TECH/Qwen3-VL-2B-Instruct-GPTQ-Int4-AX630C/blob/main/README.md) | 视觉＋语言，AX630C；卡片写 CMM 2.0 GB | 超过整机内存，不能直接使用 |
| [SmolVLM-256M INT8](https://huggingface.co/AXERA-TECH/SmolVLM-256M-Instruct) | 视觉＋语言，有 AX630C、AX650 模型；标注英语 | 更值得研究的小模型，但未找到 Go+ ARM32 实测；卡片未给 Go+ 内存占用，不能承诺可运行 |
| [Qwen3-0.6B AX637](https://huggingface.co/AXERA-TECH/Qwen3-0.6B-AX637) | 纯文本；官方启动 CMM 增量 774 MB | 非视觉，芯片不符且内存超额 |

上述容量区分下载体积与运行内存。SmolVLM 页中的 AX630C 速度也不能当 Go+ 性能。AX-LLM 的支持芯片列表列出 AX630C，未明确列 AX620Q；需移植运行时、核实 ABI 与资源后才能称为支持。

27B 即使理想 4-bit 权重也约 13.5 GB（未计视觉编码器、KV cache、量化元数据与临时缓冲），明显不适合本机。把模型按层从 eMMC 交换不等于获得可用的 30 秒视觉描述体验。

## 对桌面日记的结论

本地 OCR 已验证；小型场景分类、检测、固定词表标签是合理后续方向，但需要专门训练／转换与验证。完整的中文自然语言画面描述，尚未找到可直接部署到这台 Go+ 的成熟本地 VLM。

产品保留独立的本地、远程后端。首版本地 OCR＋规则明确为简易模式，不伪装成 VLM，也不偷偷调用远端。远程视觉直接处理截图。将来如果小 VLM 移植达标，再替换本地后端，不改变记录和标签云契约。
