# ExtScreen 副屏助手

把 Android 手机变成 Windows 桌面的**一块扩展屏** —— 不是投屏，不是远程控制，
而是真正可以拖窗口过去的第二块显示器。

Windows 侧用 [MttVDD](https://github.com/VirtualDisplayDriver/VDD) 虚拟显示器 +
[Sunshine](https://github.com/LizardByte/Sunshine) 串流，手机侧用打了竖屏补丁的
[Moonlight](https://github.com/moonlight-stream/moonlight-android)。

![竖屏扩展屏](docs/images/验证结果-竖屏扩展屏_预览.png)

```
Windows 真实显示器  +  VDD 虚拟屏 (1080x2220 @60Hz, 位置 7280,0)
                          │  Sunshine 捕获 (DXGI Desktop Duplication)
                          ▼
                    RTSP 47989  ──►  Moonlight (手机, 竖屏补丁, stretchVideo)
```

---

## 托盘状态含义

托盘图标颜色与菜单文案**严格对应真实状态**，不猜、不猜时间差：

| 图标 | 菜单文案 | 含义 |
|---|---|---|
| 灰 | `Sunshine 已停止` | 服务没跑 |
| 绿 | `已就绪，等待手机连接` | Sunshine 真正初始化完成，可以串流了 |
| 蓝 | `扩展屏已挂出，等待手机连接` | 虚拟屏已挂出，等手机连 |
| 蓝 | `手机正在接收画面` | 客户端已连上，正在推流 |
| 绿 | `Sunshine 启动中…` | **服务进程活着，但 Sunshine 还在初始化**（实测 20~30 秒） |
| 红 | `Sunshine 未安装` / `环境异常` | 需要安装或修复 |

> **「服务在跑」不等于「能串流」。**
> SCM 报 `SERVICE_RUNNING` 之后，Sunshine 还要枚举显示器、编译 shader、建 NVENC
> 编码器、起 HTTP 线程、注册 mDNS。这段实测 **20~30 秒**的空窗期里，
> 托盘显示的是 `Sunshine 启动中…`，而不是假装一切正常。
> 判据来自 `sunshine.log` 里的真实标志 `Registered Sunshine mDNS service`。

---

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Windows | 10 / 11 | 需要 DXGI Desktop Duplication |
| Sunshine | ≥ 0.21 | 提供 RTSP 串流服务 |
| MttVDD | 25.7.23 | 虚拟显示器驱动 |
| Moonlight | 12.2 (竖屏补丁版) | 手机端客户端 |
| scrcpy | 5.0.1 | 可选，用于「控制手机本体」模式 |
| Python | 3.9+ | 仅构建时需要 |

内置依赖路径（可在 `src/extscreen_core.py` 顶部修改）：

```python
TOOLS_DIR   = r'C:\Tools\moonlight-desktop-extender'   # 虚拟屏控制脚本
SCRCPY_DIR  = r'C:\Tools\scrcpy'                        # scrcpy + adb
SUNSHINE_DIR = r'C:\Program Files\Sunshine'
VDD_SETTINGS = r'C:\VirtualDisplayDriver\vdd_settings.xml'
```

---

## 快速开始

### 1. 装依赖

托盘菜单「环境与依赖」里有一键安装入口，也可以手动：

- **Sunshine**：从 [Releases](https://github.com/LizardByte/Sunshine/releases) 下载安装
- **MttVDD**：安装驱动并注册 `ROOT\DISPLAY\0001`
- **Moonlight 竖屏版**：见 [`docs/MOONLIGHT-PATCH.md`](docs/MOONLIGHT-PATCH.md)

### 2. 跑托盘

从 [Releases](https://github.com/volcanochen/extScreen/releases) 下载
`ExtScreen 副屏助手.exe`，**双击运行**（需要管理员权限，用于启停服务和操作虚拟显示器）。

### 3. 开扩展屏

托盘菜单点「开启扩展屏」，托盘会自动：

1. 挂出虚拟屏（1080×2220）
2. 摆好显示器布局（虚拟屏放在主屏右侧）
3. 把 Sunshine 的串流目标绑定到虚拟屏
4. 启动 Sunshine 并**等到真正就绪**
5. 在手机上拉起 Moonlight 串流

手机端会竖屏铺满显示电脑桌面，可以直接把窗口拖过去。

---

## 从源码构建

```bash
# 依赖
pip install pystray pillow

# 打包（必须在仓库根目录执行）
python -m PyInstaller --noconfirm ExtScreenDock.spec

# 部署到 release/
python tools/deploy.py
```

产物：`dist/ExtScreenDock.exe` → `release/ExtScreen 副屏助手.exe`

---

## 目录结构

```
src/
  extscreen_core.py       服务状态、SCM API、日志探针、设备/显示器枚举、配置
  extscreen_dock.py       托盘主程序（菜单、动作、状态轮询）
  extscreen_display.py    显示器布局、Sunshine 配置、EDID 匹配
  settings_gui.py         设置界面
tools/
  test_state_truthfulness.py  状态真实性回归测试（40 checks）
  test_action_contract.py     动作返回值契约回归测试
  svc_ctl.py / vdd_ctl.py     短命提权脚本（起停服务 / 操作虚拟屏）
  patch_moonlight.py          Moonlight 竖屏补丁
  e2e_cycle.py                开→串流→关 端到端循环测试
vendor/moonlight-desktop-extender/   运行时依赖的虚拟屏控制脚本（已含本项目补丁）
docs/                            补丁说明、构建脚本、验证截图
```

---

## 设计要点

### 1. 就绪判据用日志真实标志，不用时间差

```python
# src/extscreen_core.py
_READY_MARKS   = ('Registered Sunshine mDNS service', 'Configuration UI available at')
_STOPPED_MARKS = ('Unregistered Sunshine mDNS service',)
```

### 2. 日志判读的三条铁律

1. `sunshine.log` 会滚动成 `.1/.2/.3/.4/.5`，**只读最新的那个** ——
   旧文件里的标志属于上一次会话
2. Sunshine 被服务停止**强杀**时不打印 `CLIENT DISCONNECTED`，只打印 Unregistered
   → 客户端判定必须锚定「最后一个 READY 之后」的事件
3. 重启后新会话尚未连接时，上一轮的 `CLIENT CONNECTED` 还留在日志里 → 同样会误报

### 3. 开关判定只认真就绪

`extscreen_on()` 只认 `STATE['ready']`。在初始化空窗期点「开启扩展屏」会**继续往前推**，
而不是反向把已经开好的扩展屏关掉。

### 4. 虚拟屏按 EDID 匹配，不按 display_name

虚拟屏每次重新枚举都会拿到新的 `DISPLAY117 → DISPLAY118 → …`，
而 `device_id` 由 EDID 推导、跨重启不变。所以按 EDID 特征
（`manufacturer_id = MTT`）匹配，**不需要为了拿 device_id 而重启 Sunshine**。

### 5. 关屏顺序：先停 Sunshine，再关虚拟屏

Sunshine 的 DXGI 抓屏句柄会让 `Disable-PnpDevice` 永久阻塞在排空等待上
（实测卡满 150 秒超时）。改为先停服务再关屏，且关屏走 `pnputil /disable-device`
（PS 5.1 的 `Disable-PnpDevice` **没有** `-Timeout` 参数，无法限流）。
实测：**150 秒超时 → 2.2 秒**。

### 6. 服务启停用 advapi32 原生 API

`sc.exe` 的输出随系统语言变化，解析脆弱。改用 `StartServiceW` / `ControlService` /
`QueryServiceStatus`，`sc.exe` 降级为 `ACCESS_DENIED` 时的兜底。

---

## 回归测试

```bash
python tools/test_state_truthfulness.py    # 40 checks —— 就绪探针、日志滚动、状态文案、开关方向
python tools/test_action_contract.py       # do_async 返回值契约
```

`do_async` 约定动作函数返回 `(ok, msg)` 二元组。历史上 `restart_vdd()` 漏返回
msg 导致托盘崩溃（`cannot unpack non-iterable bool object`），契约测试锁死了这一点。

---

## 已知限制

- 左右各 9px 黑边未消除（流 2220 vs 窗口 2180）
- `sunshine.log` 系列旧日志里残留大量未启用虚拟适配器，IDD 资源可能被占满，
  建议定期清理
- 托盘需要**管理员权限**（启停服务、操作虚拟显示器设备节点）

---

## License

MIT
