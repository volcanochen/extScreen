## ExtScreen 副屏助手 v1.1.0

把 Android 手机变成 Windows 桌面的**一块扩展屏** —— 不是投屏，不是远程控制，
而是真正可以拖窗口过去的第二块显示器。

```
Windows 真实显示器  +  VDD 虚拟屏 (竖屏, 默认 800x1646 @60Hz)
                          │  Sunshine 捕获 (DXGI Desktop Duplication)
                          ▼
                    RTSP 47989  ──►  Moonlight (手机, 竖屏补丁)
```

### 下载

| 文件 | 说明 |
|---|---|
| `ExtScreen-Dock-v1.1.0.exe` | 托盘主程序（**需要管理员权限运行**） |
| `MttVDD-25.7.23-signed-drivers-x86.zip` | 虚拟显示器驱动（官方签名版） |
| `sunshine-installer.msi` | Sunshine 串流主机端安装包 |

### 安装顺序

1. 安装 **Sunshine**（`sunshine-installer.msi`），装完重启
2. 安装 **MttVDD 虚拟显示器驱动**（解压 zip 后运行 `VDD Control.exe`，
   或用托盘菜单「环境与依赖 → 安装 / 修复虚拟显示器驱动…」）
3. 手机端装 **Moonlight 竖屏补丁版**（见仓库 `docs/MOONLIGHT-PATCH.md`）
4. 双击 `ExtScreen-Dock-v1.1.0.exe`，从托盘菜单点「开启扩展屏」

### 托盘状态对照

| 图标 | 文案 | 含义 |
|---|---|---|
| 灰 | `Sunshine 已停止` | 服务没跑 |
| 绿 | `Sunshine 启动中…` | 服务进程活着，但 Sunshine 还在初始化（实测 20~30 秒） |
| 绿 | `已就绪，等待手机连接` | 真正初始化完成，可以串流了 |
| 蓝 | `扩展屏已挂出，等待手机连接` | 虚拟屏已挂出，等手机连 |
| 蓝 | `手机正在接收画面` | 客户端已连上，正在推流 |
| 红 | `Sunshine 未安装` / `环境异常` | 需要安装或修复 |

> **「服务在跑」不等于「能串流」。** SCM 报 `SERVICE_RUNNING` 之后 Sunshine 还要
> 枚举显示器、编译 shader、建 NVENC 编码器、起 HTTP 线程、注册 mDNS。
> 这段空窗期托盘如实显示「Sunshine 启动中…」，判据取自 `sunshine.log` 里的
> 真实标志 `Registered Sunshine mDNS service`，不是时间差也不是状态码。

### 本版新增（v1.1.0）

- **虚拟屏分辨率可选**：托盘「虚拟屏分辨率」子菜单、以及设置窗口都可以随时切换。
  提供从最细腻 `1440x2960` 到最大字 `540x1110` 的竖屏逐档（900/800/720/640/540），
  外加横屏档（1920x1080 / 2220x1080 / 1280x720）。当前选中的档位会打勾，点一下即时生效，
  无需重启 Sunshine（虚拟屏 device_id 由 EDID 推导，跨重启稳定）。
- **出厂默认分辨率改为 `800x1646` 竖屏**：手机 PPI 比电脑显示器高得多，
  降低虚拟屏「渲染分辨率」后，手机端把更少像素的画面拉伸放大，文字/图标更大更清楚
  （代价是略糊，但「看得清」优先）。
  - 不想用默认？托盘菜单或设置窗口改一下即可；你本机的 `config.json` 会覆盖这个出厂默认值。

### 延续自 v1.0.0 的修复

- 托盘状态不再说谎（区分「服务已起」与「已能串流」）
- 开关不再反向关闭（空窗期点「开启」会继续往前推）
- 关闭扩展屏不再卡死（先停 Sunshine 再关虚拟屏，关屏走 `pnputil /disable-device`，150 秒 → 2.2 秒）
- 托盘崩溃修复 `cannot unpack non-iterable bool object`
- 虚拟屏按 EDID 匹配（跨重启稳定）
- 服务启停用 advapi32 原生 API

### 已知限制

- 左右各约 9px 黑边未消除（流 2220 宽 vs 窗口 2180）
- 托盘需要管理员权限

完整说明与设计细节见 [仓库 README](https://github.com/volcanochen/extScreen#readme)。
