# ExtScreen 副屏助手 —— 项目长期笔记

## 项目是什么

把 Android 手机变成 Windows 桌面的一块**扩展屏**。Windows 侧用 VDD（MttVDD）
虚拟显示器 + Sunshine 串流，手机侧用打补丁的 Moonlight（竖屏补丁 `USER_PORTRAIT`）。

```
Windows 真实显示器  +  VDD 虚拟屏(1080x2220, 位置 7280,0)
                          │  Sunshine 捕获 (DXGI Desktop Duplication)
                          ▼
                    RTSP 47989  ──►  Moonlight(手机, 竖屏补丁, stretchVideo)
```

托盘 `release/ExtScreen 副屏助手.exe`（PyInstaller + uac_admin 清单）负责：
服务启停、虚拟屏开关、布局、串流目标绑定、手机端自动拉流、环境修复。

## 目录

| 路径 | 内容 |
|---|---|
| `src/extscreen_core.py` | 服务状态、SCM API、日志探针、设备/显示器枚举、配置 |
| `src/extscreen_dock.py` | 托盘主程序（菜单、动作、状态轮询） |
| `src/extscreen_display.py` | 显示器布局、Sunshine 配置、EDID 匹配 |
| `tools/test_state_truthfulness.py` | 状态真实性回归测试（40 checks） |
| `tools/test_action_contract.py` | 动作返回值契约回归测试 |
| `tools/svc_ctl.py` `tools/vdd_ctl.py` | 短命提权脚本（起停服务 / 操作虚拟屏） |
| `tools/patch_moonlight.py` | Moonlight 竖屏补丁 |
| `build/dist/ExtScreenDock.exe` | 打包产物，部署到 `release/` |

## 关键设计决策（别退回去）

### 1. 「服务在跑」≠「能串流」

SCM 报 `SERVICE_RUNNING` 之后 Sunshine 还有 **20~30 秒**空窗期（枚举显示器 →
编译 shader → 建 NVENC → 起 HTTP → 注册 mDNS）。判据用**日志里的真实标志**：

- 就绪 `Registered Sunshine mDNS service`（初始化最后一步）
- 退出 `Unregistered Sunshine mDNS service`
- 客户端 `CLIENT CONNECTED` / `CLIENT DISCONNECTED`

`sunshine_ready()` / `sunshine_client_connected()` 在 `extscreen_core.py`。

**不要退回用时间差或服务状态码判断就绪** —— 用户明确要求过"真正去检查是否已经启动"。

### 2. 日志判读的三条铁律

1. `sunshine.log` 会滚动成 `.1/.2/.3/.4/.5`，**只读最新的 `sunshine.log`**
   —— 旧文件里的标志属于上一次会话
2. Sunshine 被服务停止**强杀**时不打印 CLIENT DISCONNECTED，只打印 Unregistered
   —— client 判定必须锚定「当前会话」（最后一个 READY 之后的事件）
3. 重启后新会话未连接时，上一轮 CONNECTED 仍留在日志里 —— 同上，靠 READY 位置切分

### 3. 开关判定只认 `ready`

`extscreen_on()` 只返回 `STATE['ready']`。用 `vdd or svc==RUNNING` 判会导致
用户在启动空窗期点「开启」反而被当成「关闭」。

### 4. 启停顺序与关闭顺序是相反的

- 开：挂虚拟屏 → 摆布局 → 写串流目标 → **最后**启 Sunshine 并等就绪
  （反过来 Sunshine 启动时枚举不到虚拟屏，得再重启一次，白等一轮 25~30 秒）
- 关：**先停 Sunshine → 再关虚拟屏**
  （Sunshine 的 DXGI 抓屏句柄还开着时，`Disable-PnpDevice` 会卡在等设备排空，
  实测卡满 150 秒超时，托盘一直显示"处理中…"）

### 5. 关虚拟屏走 `pnputil /disable-device`

`Disable-PnpDevice` 在 PS 5.1 里**没有 `-Timeout` 参数**，无法限制等待。
`pnputil /disable-device` 直接标记禁用，实测 2.2 秒。`Common.ps1` 的
`Set-VddEnabled` 已改成 pnputil 为主、cmdlet 兜底。

### 6. 虚拟屏按 EDID 匹配，不按 display_name

`display_name` 每次重新枚举都变（DISPLAY117→118→…），`device_id` 由 EDID 推导
跨重启不变。`sunshine_vdd_device_id()` 按 `manufacturer_id=MTT` 匹配，
省掉"为拿 id 而重启 Sunshine"。

### 7. 动作函数一律返回 `(ok, msg)`

`do_async` 按二元组解包。返回单值会抛 `cannot unpack non-iterable bool object`。
`do_async` 里另有单值兜底，`tools/test_action_contract.py` 锁死这个契约。

### 8. 服务启停走 advapi32 原生 SCM API

`start_service_scm()` / `stop_service_scm()`，不依赖 `sc.exe`（输出随系统语言变化、
沙箱里被 Program Blacklist 拦）。`sc.exe` 只作为 ACCESS_DENIED 时的兜底。

### 9. 自动拉流要处理陈旧会话

Moonlight 的 `ShortcutTrampoline` 有时复用上一轮会话状态，表现为**进 Game Activity
但画面全黑**。`launch_stream_on_phone()` 会等 `client` 真连上，没连上就
force-stop Moonlight 重拉一次。

## 环境事实（本机沙箱）

- adb：`C:\Users\volcano\AppData\Local\Android\Sdk\platform-tools\adb.exe`（不在 PATH）
- 测试机：三星 Note8 `SM-N9500`，Android 9，WebView 内核 Chrome 74
  （逻辑分辨率 1080x2220 / 物理 1440x2960）
- **沙箱会杀掉自己启动的子进程** → 托盘必须由用户手动双击才能常驻
- **提权进程运行超过约 1 分钟会被回收** → 长流程测试要拆成短命提权步骤
- 提权脚本无法重定向 stdout → 结果写 `tools/*_out.txt`
- `sc.exe` / `cmd.exe` 被 Program Blacklist 拦；从 Bash 调 `powershell` 也被拦
- Bash heredoc 写文件会被拦（误判为 "Invoking PowerShell from Bash"）→ 用 Edit/Write 工具
- `Disable-PnpDevice` 没有 `-Timeout`（PS 5.1）
- 端口：47989 = RTSP/串流，47990 = HTTPS 配置界面

## 关键常量

| 名称 | 值 |
|---|---|
| 虚拟屏分辨率 | 1080x2220 @60，位置 (7280, 0) |
| VDD device_id | `{f3c4cd9e-b807-5109-a0f6-bb57ac9e7018}`（跨重启不变） |
| VDD EDID | manufacturer_id=MTT, product_code=1337, friendly_name="VDD by MTT" |
| Moonlight PC UUID | `31D8D607-8EA8-1805-3498-573BF705B4E4` |
| Moonlight AppId | 881448767（Desktop） |
