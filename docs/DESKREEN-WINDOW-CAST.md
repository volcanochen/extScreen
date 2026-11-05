# 窗口投屏设计与使用（自研 cast 后端）

> **本文已归档**：窗口投屏的最新完整设计（多会话、多手机、USB 回环、
> 粘滞会话、标题栏按钮等）统一收录在 [TECHNICAL.md](TECHNICAL.md)，
> 开发过程见 [HISTORY.md](HISTORY.md)。本文保留 2026-11-03 初版设计
> 与当时的验证记录，作为历史参考。

> 2026-11-03：原方案对接 Deskreen CE，但打包版把调试口全关了
> （--inspect / --remote-debugging-port 都不监听，无法自动读房间号），
> 按用户决定切换为**自研后端**。Deskreen 相关代码保留作兼容
> （`extscreen_deskreen.py`），默认菜单走自研 cast。

## 架构

```
Windows Dock（提权）                         Android 副屏接收器 APK
────────────────────                         ──────────────────────
托盘「窗口投屏」→ 选窗口（WinForms 列表）      前台服务 + NsdManager
PrintWindow(PW_RENDERFULLCONTENT) 抓帧         发现 _extscreen-cast._tcp
→ 缩放 720px → JPEG(70) ~20fps                解析 TXT path → http://ip:8123/
ThreadingHTTPServer :8123                     WebView 自动打开
  GET /      观众页（断流 1s 自动重连）          │  <img src=/stream>
  GET /stream MJPEG（multipart/x-mixed-replace)│  浏览器原生解码
  GET /info  JSON 状态                        ▼
mDNS 广播 _extscreen-cast._tcp ◄──解算 SRV/TXT─┘
```

## 关键事实（别退回去）

1. **mDNS 层在 `extscreen_mdns.py`**（公共模块），服务类型参数化：
   自研=`extscreen-cast`，Deskreen 兼容=`extscreen-deskreen`。
   Windows 多播必须 IP_ADD_MEMBERSHIP + IP_MULTICAST_IF 锚定真实网卡。
2. **抓帧必须用 PrintWindow + PW_RENDERFULLCONTENT**：能抓被遮挡窗口；
   尺寸取 DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)（真边界）。
   GetDIBits 的 biHeight 用负值（自上而下），BGRX 喂 PIL.frombuffer。
3. **本地 HTTP 探测/连接一律裸 socket**，urllib 会走系统代理把 127.0.0.1 劫走。
4. **bytes/str 混用是历史事故来源**（probe 用 str in bytes 静默 TypeError），
   裸 except 至少打日志。
5. MJPEG 在 WebView(Chrome 74)+现代浏览器都原生支持，无 JS 信令；
   延迟约 0.1s（LAN），适合文档/终端/监控，不适合高速游戏。
6. 手机端零操作：接收器发现服务即自动打开；断流 1s 自动重连；
   服务停止/窗口关闭由托盘状态行如实显示。
7. 窗口选择器走 PowerShell + WinForms（项目约定，避开 tkinter 依赖），
   窗口列表以 base64 JSON 传入，选中 hwnd 从 stdout 回收。

## 配置项（config.json）

```json
{
  "castPort": 8123,          // 占用时自动换
  "castWidth": 720,          // 推流宽度
  "castFps": 20,
  "castQuality": 70,
  "castAdbLaunch": true,     // 投屏开始时用 adb 自动拉起 USB 连接的手机接收器
  "castFitWindow": true,     // 按手机屏幕比例自动调整被投窗口（画面铺满无黑边）
  "castFitWidth": 700,       // 适配后的窗口可见宽度（0=不调整窗口）
  "castFitFollowRotate": true // 手机横竖屏切换时跟随调整窗口比例
}
```

## 设备信息上报与窗口适配（2026-11-03 下午新增）

- 接收器 App 在 apply(url)/onResume/onConfigurationChanged/断流重连时，
  向 PC `POST /device` 上报 `{w, h, rot}`（Display.getRealSize，已含旋转）。
- PC `CastMonitor.set_device()` 收到后：记录进状态（托盘显示「手机 1080x2220竖」），
  并按 `castFitWindow/castFitWidth/castFitFollowRotate` 把被投窗口
  `SetWindowPos` 成同比例（保留左上角位置、不超过所在显示器工作区）。
- 效果：窗口比例=手机比例 → MJPEG 画面在手机上铺满无黑边；手机旋转，
  窗口跟随变横/竖。
- 探针注意：验证 mDNS 应答时，探针 socket 必须 `IP_MULTICAST_IF` 锚定
  真实网卡，否则查询从虚拟网卡出去、永远收不到应答（假阴性，实测踩过）。

## 部署 / 使用

1. 手机装 `build/ExtScreenViewer/ExtScreenViewer.apk`，打开一次（之后开机自启）。
2. 托盘 → 窗口投屏 → 选择窗口并开始分享… → 挑窗口。
3. 手机自动出画面。停止：托盘 → 停止窗口投屏（或直接退出 Dock）。

## 触摸回传控制（2026-11-03 晚新增）

- 观众页把 touchstart/move/end 归一化后 `POST /touch`（**必须用
  XMLHttpRequest**——Chrome 74 WebView 的 fetch() 同源 POST 也报
  "TypeError: Failed to fetch"，实测踩过；Content-Type 用 text/plain
  免预检）。监听挂在 body 上（img 未出画也能点）。
- PC `handle_touch`：画面归一化坐标 → 窗口客户区坐标 → **PostMessage**
  `WM_LBUTTONDOWN/UP`、`WM_MOUSEMOVE`（wParam 带 MK_ 按键标志）。
  安全边界：不用 SendInput/SetCursorPos（不动真实光标、不干扰桌面）；
  PostMessage 不激活窗口（后台窗口不会被点到前台，Z 序不变）。
- 手势：单指=左键，双指=右键，按住拖动=拖拽。配置项 `castTouchControl`。
- **无感操作**（`castNoActivate`，默认开）：投屏开始时给目标窗口挂
  `WS_EX_NOACTIVATE` + `LockSetForegroundWindow(LSFW_LOCK)`，手机点击
  不会把后台窗口顶到前台（Chromium 收到鼠标按下会自己抢前台，仅靠
  PostMessage 挡不住，实测复现）；停止投屏精确还原窗口样式并解锁。
  用户按 ALT 或自己点击其他窗口时系统自动解锁——用户主动操作优先。
- **mDNS SRV 目标名每会话唯一**（`x-<实例名>.local`）：手机端会独立解析
  SRV 目标名，共享 PC 主机名会命中陈旧/外部解析结果（实测解析到
  192.168.50.16 → ERR_CONNECTION_REFUSED，页面被周期性覆盖成死页）。
- 已知边界：拖拽依赖应用读取 WM_MOUSEMOVE 的 wParam 按键标志——
  原生/Chromium 应用正常；.NET WinForms 不读（Button 恒 None），
  这类窗口只支持点击不支持拖拽。

## 验证记录（2026-11-03）

- 沙箱端到端：真窗口（WorkBuddy）抓帧 → /stream 返回 multipart + JPEG ✓，
  /info JSON ✓，观众页 ✓，mDNS 应答（PC 回环）✓
- 契约测试 `tools/test_cast_contract.py` 27 checks + 状态真实性 40 checks 全过
- **真机端到端全通（2026-11-03 03:58）**：PC 起 cast 服务（非沙箱、真实网络栈），
  Note8 (192.168.50.132) 经 mDNS 自动发现 `_extscreen-cast._tcp` →
  NsdManager 解析 → WebView 自动打开 `http://192.168.50.111:8123/` →
  手机屏幕实时显示 WorkBuddy 窗口画面（adb 截图确认），全程零操作。
  排障过程见 `.workbuddy/memory/2026-11-03.md`，关键坑：SRV rdata 曾把
  port 打成 4 字节（应 2 字节）导致 Android 端解析目标名错位 → 服务永不生效；
  实例名带随机后缀避免 mdnssd 缓存污染。
