# ExtScreen 开发史（HISTORY）

> 按日期记录的关键节点、翻车与修复。技术结论的沉淀版在
> [TECHNICAL.md](TECHNICAL.md)，这里保留过程。

## 2026-11-01 —— 项目立项：把手机变成真扩展屏

- 目标：不是投屏，是能在系统里拖窗口过去的**第二块显示器**。
- 排查 VDD 虚拟屏「驱动加载成功但一块屏都不挂」：`vdd_settings.xml` 三处
  错法（friendlyname 写了 Auto 占位名、CustomEdid 开了却没有 edid 文件、
  SDR10bit 与 HDRPlus 互斥同开）。修正后 1080x2220 挂出成功。
- 托盘 Dock v1.0（PyInstaller + uac_admin）：服务启停（advapi32 SCM API）、
  环境自检修复、一键开/关扩展屏。
- 定下「状态真实性」原则：托盘显示的必须是真实状态，不装、不猜。
- 沙箱环境踩坑备忘：sc/wmic 被禁、提权进程 ~1 分钟被回收、PowerShell
  stdout 被吞。

## 2026-11-02 —— 竖屏铺满 + 首个 Release

- 用户反馈「屏幕竖着放却显示横桌面」。根因不在渲染：Moonlight 请求的
  分辨率本身是横的——之前打的补丁里宽高交换分支的比较方向写反了
  （`if-gt` 应为 `if-ge`）。修完再补两刀：去掉 16 对齐（避免 1080→1088
  不等比）、`stretchVideo` 强制 true。填充率 98.3%。
- 手机端 UI 自动化大坑：Note8 上 `input tap` 会被当长按（input dispatcher
  卡 ready）。改走 `ShortcutTrampoline` 直拉串流，完全不碰界面。
- 仓库推上 GitHub（SSH ed25519），v1.0.0 发 Release（自写 REST API 脚本
  `publish_release.py`，资产名必须纯 ASCII——中文会被 GitHub 转义成点号）。

## 2026-11-03 —— 窗口投屏：从 Deskreen 到自研

- **虚拟屏分辨率可选** + v1.1.0 发布。
- 新需求「PC 窗口投到手机」。先对接 Deskreen CE：四轮联调（信令端口→
  调试端口→CDP 通道）全部卡死——打包版 Electron 用 fuses 关了
  --inspect/--remote-debugging-port，Dock 拿不到房间号。**决定自研**。
- 自研 cast 后端一次打通：PrintWindow(PW_RENDERFULLCONTENT) 抓帧 →
  MJPEG :8123 → mDNS（纯标准库 DNS-SD）→ 手机接收器（NsdManager+WebView）
  自动打开，全程零操作。真机端到端全通（03:58）。
- 当晚连修：mDNS SRV rdata 把 port 打成 4 字节（应 2 字节）→ Android 永远
  解析不出目标；probe 里 str in bytes 静默 TypeError；Chrome 74 WebView 的
  fetch() 同源 POST 报 Failed to fetch → 改 XHR。
- 下午三需求：USB 手机自动拉接收器；手机上报屏幕尺寸/朝向 → PC 按比例
  适配窗口；「无感操作」（点击不顶前台）。
- 深夜排障：手机黑屏（重启后的空窗期误报）、虚拟屏周期性掉线
  （ChangeDisplaySettingsExW 两步法自动重挂）、「闪一下又黑」
  （mDNS 陈旧实例覆盖好页面 → 接收器加健康检查+死链黑名单）。

## 2026-11-04 —— 交互打磨：触摸、高亮、菜单、多手机

- 凌晨：锁屏无感显示（setShowWhenLocked）；窗口选择器实时红框高亮
  （DPI 感知修正错位）；发现并弃用 WS_EX_NOACTIVATE（任务栏消失+点击无反应，
  改 fg-lock + AttachThreadInput 恢复前台）；触摸控制打通子窗口命中
  （资源管理器要发 DirectUIHWND）；窗口悬浮快捷按钮。
- 系统菜单接管第一版用 WinEvent——**探针实测证明标题栏右键根本不发菜单
  WinEvent**，推倒重来：WH_MOUSE_LL/WH_KEYBOARD_LL 低级钩子 + 吞事件 +
  NCHITTEST 判定。夜间自动化测试两次被用户物理鼠标带偏坐标（抱歉）。
- 悬浮按钮返工：TOPMOST 压别人置顶窗口 → Z 序紧跟目标窗口；点击无反馈 →
  按下变色 +「处理中…」+ 4s 超时还原。
- **「能投的窗口漏了几个」**：最小化过滤把 showCmd=3（最大化）误当最小化，
  最大化窗口全被误杀；顺带收编最小化窗口（选中自动还原再抓）。
- **手机横屏右侧黑条**：观众页用了 `inset:0`（Chrome 87+），Note8 的
  Chrome 74 整条忽略。改 Chrome 74 稳定写法 + object-fit:contain +
  触摸按内容区归一化。
- **多手机支持**：多台时弹选择框；未装接收器自动 `adb install`；
  发现「安装后没投上」的真凶是手机没联网（ERR_INTERNET_DISCONNECTED）——
  顺势上 **USB 回环**（`adb reverse`，127.0.0.1 直达 PC，断网可投）；
  选窗口+选手机合一界面。
- 停投后手机自动恢复原状（force-stop 接收器 + 撤回环，只清自己拉起的）。
- **多路投屏**：CastManager 多会话（每路独立端口/mDNS/手机记忆），
  双记事本真投 e2e 11/11。
- 「启动就死」排查：部署残留的自杀命令躺在 cmd.txt 里被新进程执行 →
  启动时丢弃残留命令。
- 五连反馈：红色按钮被自己窗口盖住（前台门卫）、「有会话时没地方投第二路」
  （菜单重构，选择入口永远可用）、「停止 Sunshine 超时」虚惊（耐心 45s+
  强杀兜底）、手机界面附规格、窗口列表带所属应用。
- 接着补：三元组解包残留炸「too many values to unpack」（回查处漏改）。
- 同窗口投 A 后再投 B 会踢掉 A → 复用会话多观众语义；手机商品名：先做
  联网查询（搜索引擎反爬不稳），按用户要求改纯本地对照表，再扩成
  **Google Play 官方设备表 11,456 条**打包进 exe。
- 比例适配改「保面积只调一边」；标题栏投屏按钮第一版（跨进程 Owner，
  当晚未出事，但埋雷）。

## 2026-11-05 —— 多会话抢台 + 全线冻结事故

- **抢台 bug**：投 X 给 B，B 却显示已在投 A 的 Y，窗口尺寸还被搞乱。
  日志实锤：接收器「见活服务就切」的策略在多会话下互相吸；/device 交叉
  上报触发互相 fit。修复：接收器**粘滞会话 pin**（粘的死了才放手）；
  服务端适配只认首台设备；launch 端口兜底优先本会话 mon。
- **全线输入冻结事故**（凌晨）：除投屏窗口外所有窗口失去响应，点任务栏
  可解锁。根因是标题栏按钮的**跨进程 Owner**——Windows 隐式耦合输入队列，
  浮层进程一卡全桌面停摆。应急杀 Dock+浮层；永久修复：浮层改单实例
  前台跟随（不设跨进程 Owner），钩子线程全面防阻塞
  （SendMessageTimeout / 剪贴板出钩子线程 / AttachThreadInput finally 释放）。
- **画面模糊**：推流宽度 720 → 1080、质量 70 → 80。
- 文档整理：本文件 + [TECHNICAL.md](TECHNICAL.md) + README 更新。

## 里程碑

| 版本 | 日期 | 内容 |
|---|---|---|
| v1.0.0 | 11-02 | 扩展屏核心（VDD+Sunshine+Moonlight 竖屏补丁） |
| v1.1.0 | 11-03 | 虚拟屏分辨率可选 |
| v1.2+ | 11-03~05 | 窗口投屏全链路：多会话/多手机/触摸回传/USB 回环/标题栏按钮/手机名对照表 |

## 反模式备忘（每条都有血泪）

1. 跨进程 GWL_HWNDPARENT（Owner）——输入队列耦合，禁用。
2. 低级钩子线程里同步 SendMessage / 剪贴板 / 对话框——会冻结全系统输入。
3. Chrome 74 WebView：禁 `inset:`、fetch 同源 POST 不可用、必须 XHR。
4. `WindowFromPoint` 不跳过 DWM cloaked 窗口（Edge 预启动窗口会挡判定）。
5. `WindowPlacement.showCmd`：3 是最大化不是最小化。
6. Windows `SO_REUSEADDR` 双绑假成功——bind 前必须 connect 探测。
7. mDNS SRV port 是 2 字节；实例名要带随机后缀；多播要锚定真实网卡。
8. `adb input tap` 在部分机型被当长按——用 ShortcutTrampoline/am start。
9. Google Play CSV 是手机型号→商品名的权威数据源（5.4 万行 UTF-16）。
10. 部署脚本写 cmd.txt 前先确认 Dock 在跑；Dock 启动时丢弃残留命令。
