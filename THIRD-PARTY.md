# 第三方组件与归属

本项目自身代码采用 MIT License（见根目录 `LICENSE`），但集成了以下第三方组件。

## 运行时依赖（不随本仓库分发）

| 组件 | 作者 | 许可 | 用途 |
|---|---|---|---|
| [Sunshine](https://github.com/LizardByte/Sunshine) | LizardByte | GPL-3.0 | 游戏串流主机端 |
| [Moonlight](https://github.com/moonlight-stream/moonlight-android) | moonlight-stream | GPL-3.0 | 游戏串流客户端 |
| [MttVDD](https://github.com/VirtualDisplayDriver/VDD) | VirtualDisplayDriver | MIT | 虚拟显示器驱动 |
| [scrcpy](https://github.com/Genymobile/scrcpy) | Genymobile | Apache-2.0 | 控制手机本体 |
| [pystray](https://github.com/mcanthony/pystray) | mcanthony | Apache-2.0 | 托盘图标 |
| [Pillow](https://github.com/python-pillow/Pillow) | python-pillow | MIT-CMU | 图标绘制 |

## 已 vendor 的源码片段

`vendor/moonlight-desktop-extender/` 来自
[moonlight-desktop-extender](https://github.com/Tubifix77/moonlight-desktop-extender)
（MIT License，Copyright (c) 2026 Tubifix77），上游许可证原文见该目录下的 `LICENSE`。

本项目对其中 `scripts/Common.ps1` 做了**一处修改**：

> `Set-VddEnabled` 的 disable 分支改用 `pnputil /disable-device` 作为主路径，
> `Disable-PnpDevice` 降级为兜底。
>
> 原因：`Disable-PnpDevice` 会等待设备排空，而 Sunshine 的 DXGI 抓屏句柄会一直
> 持有虚拟显示器设备，导致该调用永久阻塞（实测卡满 150 秒超时）。
> Windows PowerShell 5.1 的 `Disable-PnpDevice` 没有 `-Timeout` 参数，无法限流。
> `pnputil` 立即标记设备为 disabled，实测耗时约 2.2 秒。

除上述一处外，`vendor/` 下的文件与上游保持一致。

## 构建期工具（不随本仓库分发）

| 工具 | 用途 |
|---|---|
| [apktool](https://apktool.org/) | 反编译 / 回编 Moonlight APK |
| Android SDK Build-Tools | `apksigner` 签名、`zipalign` 对齐 |
| JDK 17 | `keytool` 生成签名密钥、`javac` |
| PyInstaller | 打包托盘 exe |
