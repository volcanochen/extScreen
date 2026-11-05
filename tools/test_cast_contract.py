# -*- coding: utf-8 -*-
"""
窗口投屏（自研 cast 后端）契约与状态真实性测试。

锁死的关键约定：
  1. 动作函数一律返回 (ok, msg) 二元组（do_async 的解包契约）
  2. mDNS 服务名格式正确：_extscreen-cast._tcp.local（自研）/ _extscreen-deskreen（兼容）
  3. 窗口枚举可见、标题非空；真实抓帧 + JPEG 编码可用
  4. 观众页 HTML 包含 /stream 与断流重连脚本
  5. 托盘状态文本与 CastMonitor 快照一致（不装作已就绪）
  6. 退出时 Dock 会停掉 cast（菜单引用不悬空）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import extscreen_cast as cast  # noqa: E402
import extscreen_mdns as mdns  # noqa: E402

PASS = 0


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print('  PASS %s' % name)
    else:
        raise AssertionError('%s  %s' % (name, detail))


# ---- 1. 动作契约 ----
print('[1] 动作返回值契约')
ok, msg = cast.action_stop()
check('stop 返回二元组且成功', ok is True and isinstance(msg, str))

# ---- 2. mDNS 服务类型 ----
print('[2] mDNS 服务名与记录格式')
a = mdns.MdnsAnnouncer('ExtScreen-cast', 8123, {'path': '/'},
                       svc_type='extscreen-cast')
check('cast 服务类型', a.type_ptr == '_extscreen-cast._tcp.local', a.type_ptr)
check('cast 实例全名',
      a.full == 'ExtScreen-cast._extscreen-cast._tcp.local', a.full)
b = mdns.MdnsAnnouncer('X', 3131, {}, svc_type='_extscreen-deskreen')
check('兼容 deskreen 服务类型',
      b.type_ptr == '_extscreen-deskreen._tcp.local', b.type_ptr)
pkt = a._build_response(0)
check('应答头 ANCOUNT=5',
      __import__('struct').unpack('>6H', pkt[:12])[3] == 5)

# 逐条解析记录，验证 SRV rdata 结构：priority(2) weight(2) port(2) + 目标名
# （历史事故：曾把 port 打成 4 字节，Android 端解析目标名错位 → 永远发现不了）
def _parse_records(pkt):
    recs = []
    off = 12
    an = __import__('struct').unpack('>6H', pkt[:12])[3]
    for _ in range(an):
        name, off = mdns._read_name(pkt, off)
        rtype, qclass, ttl, rdlen = __import__('struct').unpack(
            '>HHIH', pkt[off:off + 10])
        off += 10
        recs.append((name, rtype, qclass, pkt[off:off + rdlen]))
        off += rdlen
    return recs

recs = _parse_records(pkt)
srv = [r for r in recs if r[1] == 33]
check('应答含 1 条 SRV', len(srv) == 1, str(len(srv)))
prio, weight, port = __import__('struct').unpack('>HHH', srv[0][3][:6])
check('SRV priority=0 weight=0', (prio, weight) == (0, 0), srv[0][3][:6].hex())
check('SRV port=8123', port == 8123, str(port))
srv_name, _ = mdns._read_name(srv[0][3], 6)
check('SRV 目标名正确', srv_name == a.srv_target, srv_name)
a_rec = [r for r in recs if r[1] == 1]
check('应答含 A 记录', len(a_rec) == 1)
check('cache-flush 位在 unique 记录上',
      all(r[2] & 0x8000 for r in recs if r[1] in (33, 16, 1)))

# ---- 3. 窗口枚举 + 抓帧 ----
print('[3] 窗口枚举与抓帧')
wins = cast.list_windows()
check('能看到可见窗口', len(wins) > 0)
check('返回 (hwnd, title, exe) 三元组',
      all(isinstance(t, str) and t and isinstance(e, str)
          for _, t, e in wins))
check('窗口带所属应用名（exe）', any(e for _, _, e in wins))
img = cast._capture_window(wins[0][0])
check('PrintWindow 抓帧成功', img is not None and img.size[0] > 0)
jpeg, w, h = cast._encode_frame(img, 720, 70)
check('JPEG 编码（magic FFD8）', jpeg[:2] == b'\xff\xd8')
check('宽度约束生效', w <= 720)

# ---- 4. 观众页 ----
print('[4] 观众页 HTML 与触摸回传')
html = cast.VIEWER_HTML.format(title='测试窗口')
check('引用 /stream', '/stream' in html)
check('断流自动重连', 'onerror' in html and 'Date.now' in html)
check('无屏幕标题覆盖层（#info 已删）',
      '#info' not in html and "id=\"info\"" not in html
      and "id='info'" not in html)
check('触摸事件回传 /touch（XHR，fetch 在 Chrome74 WebView 不可靠）',
      "XMLHttpRequest" in html and "'/touch'" in html
      and 'touchstart' in html and 'touchend' in html)
check('单指左键/双指右键', "b: twoFinger ? 'r' : 'l'" in html)
# Chrome 74 WebView 兼容：inset 是 Chrome 87+ 属性，74 整条忽略导致
# 横屏定位错乱右侧留黑（2026-11-05 实测）
check('观众页不用 inset 简写属性（Chrome74 不支持）', 'inset:' not in html)
check('object-fit:contain 任意朝向铺满视口', 'object-fit:contain' in html)
check('触摸归一化按内容区算黑边',
      'naturalWidth' in html and '(r.width - cw)' in html)

# ---- 4b. 触摸 → 鼠标消息（只投给被投窗口） ----
print('[4b] 触摸回传处理')
from extscreen_core import load_ext_config  # noqa: E402
check('handle_touch 存在', callable(getattr(cast.CastMonitor, 'handle_touch')))
check('castTouchControl 默认开', bool(load_ext_config().get('castTouchControl', True)))
check('castNoActivate 默认开', bool(load_ext_config().get('castNoActivate', True)))
check('lock_foreground 可调用',
      callable(getattr(cast, 'lock_foreground', None)))
# NOACTIVATE 样式已弃用：它会摘掉任务栏按钮、并挡住用户自己的点击（实测）
check('start 不再修改窗口样式', 'set_window_noactivate(hwnd, True)'
      not in open(cast.__file__, encoding='utf-8').read())
mon_t = cast.CastMonitor()
mon_t.hwnd = 0
for bad in ({}, {'a': 'down'}, {'a': 'bogus', 'x': 1, 'y': 2},
            {'a': 'down', 'x': 'x', 'y': None}):
    try:
        mon_t.handle_touch(bad)     # hwnd=0 / 非法数据 → 安全返回
    except Exception as e:
        raise AssertionError('handle_touch 对 %r 抛异常: %s' % (bad, e))
check('非法触摸数据安全返回', True)
import inspect
_src = inspect.getsource(cast.CastMonitor.handle_touch)
_code = _src.split('"""')[2] if _src.count('"""') >= 2 else _src
check('实现只用 PostMessage（无全局注入）',
      'PostMessageW' in _code and 'SendInput' not in _code
      and 'SetCursorPos' not in _code)

# ---- 5. 托盘状态文本 ----
print('[5] 托盘状态与快照一致')
import extscreen_dock as dock  # noqa: E402

cast.MONITOR.state.update(run=False)
check('未启动状态', '未启动' in dock.menu_cast(None))
cast.MONITOR.state.update(run=True, title='记事本', err='')
cast.MONITOR.clients = 0
check('等待手机状态带窗口标题', '记事本' in dock.menu_cast(None))
cast.MONITOR.clients = 2
check('分享中状态带设备数', '2 台在看' in dock.menu_cast(None))
cast.MONITOR.state.update(err='窗口已关闭')
check('错误如实上菜单', '窗口已关闭' in dock.menu_cast(None))
# 多路：两路同时在投 → 标题都列出 + 「N 路」
cast.MONITOR.state.update(err='')
m2 = cast.CastMonitor()
m2.state.update(run=True, title='浏览器')
cast.MANAGER.mons.append(m2)
check('多路状态列出每路标题',
      '记事本' in dock.menu_cast(None) and '浏览器' in dock.menu_cast(None)
      and '2 路分享中' in dock.menu_cast(None))
check('多路 toggle 文案=全部停止',
      '全部停止（2 路' in dock.menu_cast_stop(None))
cast.MANAGER.mons.remove(m2)
m2.stop()
check('停止文案=单路', dock.menu_cast_stop(None) == '停止窗口投屏')
cast.MONITOR.state.update(run=False)
check('停止项在无会话时由 visible 隐藏（文案仍安全）',
      dock.menu_cast_stop(None) == '停止窗口投屏')
cast.MONITOR.clients = 0
check('手机规格查询可用', callable(getattr(dock, 'phone_specs', None)))
_java = open(os.path.join(os.path.dirname(cast.__file__), '..',
                          'tools', 'deskreen-viewer', 'java',
                          'cn.volcanochen.extscreenview'.replace('.', os.sep),
                          'ViewerService.java'),
             encoding='utf-8').read()
check('接收器粘滞会话：粘的活着不切台', 'ViewerBridge.pin' in _java
      and 'if (urlAlive(p)) return;' in _java)
check('接收器粘滞死亡才释放并拉黑', 'ViewerBridge.pin = null' in _java
      and 'deadUntil.put(p, now + DEAD_MS)' in _java)
check('多观众不挪窗口（2nd device 不 refit）',
      'window not refitted' in open(cast.__file__, encoding='utf-8').read())
check('launch 端口兜底优先本会话 mon',
      'if not port and mon is not None' in
      open(dock.__file__, encoding='utf-8').read())
names = dock._phone_names_load()
check('对照表规模 ≥ 100 条（实际 %d）' % len(names), len(names) >= 100)
check('对照表覆盖用户机型（Google Play 注册表核实）',
      names.get('SM-S9080') == 'Galaxy S22 Ultra'
      and names.get('SM-N9500') == 'Galaxy Note8')
check('对照表覆盖国产品牌',
      all(names.get(k) for k in ('VOG-AL00', '2201123G', 'PJD110')))
check('表外型号显示未知手机（不联网）',
      '未知手机' in open(dock.__file__, encoding='utf-8').read()
      and '_phone_name_online' not in
      open(dock.__file__, encoding='utf-8').read())
check('标题栏按钮：单实例前台跟随（禁跨进程 Owner——输入队列耦合致全线冻结）',
      'GetForegroundWindow' in dock._CAPTION_PS
      and 'SetWindowLongPtr' not in dock._CAPTION_PS
      and '-8' not in dock._CAPTION_PS)
check('标题栏按钮：过滤工具窗/隐藏窗',
      '0x00C00000' in dock._CAPTION_PS
      and '0x80' in dock._CAPTION_PS and 'DwmGetCloak' in dock._CAPTION_PS)
check('标题栏按钮：点击回传 CLICK hwnd',
      "CLICK {0}' -f $target.ToInt64()" in dock._CAPTION_PS)
check('钩子线程防阻塞：NCHITTEST 走 SendMessageTimeout',
      'SendMessageTimeoutW' in open(dock.__file__, encoding='utf-8').read()
      and 'SMTO_ABORTIFHUNG' in open(dock.__file__, encoding='utf-8').read())
check('钩子线程防阻塞：剪贴板不在线程内直接调',
      'do_async(_copy_url, mon)' in open(dock.__file__,
                                         encoding='utf-8').read())
check('AttachThreadInput 必在 finally 释放',
      'if attached:' in open(cast.__file__, encoding='utf-8').read())
check('全局标题栏按钮：状态文件同步颜色',
      'caption_state.json' in open(dock.__file__, encoding='utf-8').read()
      and 'CAPTION_STATE_FILE' in open(dock.__file__, encoding='utf-8').read()
      and 'ConvertFrom-Json' in dock._CAPTION_PS)
check('选择器窗口行带应用名', '[{0}] {1}' in dock._PICKER_PS)
check('选择器手机按钮带规格', '$p.info' in dock._PICKER_PS
      and '$p.info' in dock._PHONE_PS)
check('Sunshine 停止有 45s 耐心 + 强杀兜底',
      "'taskkill', '/F', '/IM', 'sunshine.exe'" in
      open(dock.__file__, encoding='utf-8').read()
      and 'range(90)' in open(dock.__file__, encoding='utf-8').read())

# ---- 6. Deskreen 兼容模块仍可用（不回归） ----
print('[6] Deskreen 兼容模块')
import extscreen_deskreen as dk  # noqa: E402
check('deskreen 模块仍可导入', hasattr(dk, 'MONITOR'))
check('MdnsAnnouncer 从 mdns 模块导出', hasattr(dk, 'MdnsAnnouncer'))

# ---- 7. adb 自动拉起接收器（USB 零操作） ----
print('[7] adb 拉起手机接收器')
check('dock 有 probe_usb_phone', callable(getattr(dock, 'probe_usb_phone', None)))
check('dock 有 launch_viewer_on_phone',
      callable(getattr(dock, 'launch_viewer_on_phone', None)))
check('接收器包名固定', dock.VIEWER_PKG == 'cn.volcanochen.extscreenview',
      dock.VIEWER_PKG)
check('usb 状态缓存字段存在', 'usbPhone' in dock.STATE)

# ---- 8. 手机屏幕信息上报 → 窗口适配 ----
print('[8] 设备信息上报与窗口适配')
cfg = load_ext_config()
check('castAdbLaunch 默认开', bool(cfg.get('castAdbLaunch', True)))
check('castFitWindow 默认开', bool(cfg.get('castFitWindow', True)))
check('castFitWidth 默认 700', int(cfg.get('castFitWidth') or 0) == 700)
check('castFitFollowRotate 默认开',
      bool(cfg.get('castFitFollowRotate', True)))

mon = cast.MONITOR
mon.device = None
mon.set_device({'w': 0, 'h': 0})            # 非法数据忽略
check('非法设备信息被忽略', mon.device is None)
mon.set_device({'w': 1080, 'h': 2220, 'rot': 0})
check('设备信息已记录', mon.device and mon.device['w'] == 1080)
check('状态行带分辨率', '1080x2220竖' in mon.snapshot()['device'],
      mon.snapshot()['device'])
mon.set_device({'w': 2220, 'h': 1080, 'rot': 1})
check('旋转后比例更新', mon.device['w'] == 2220 and '横' in mon.snapshot()['device'])
check('fit_window_to_aspect 可调用',
      callable(getattr(cast, 'fit_window_to_aspect', None)))
# 非法 hwnd / 宽度为 0 时安全返回 False，不抛
check('fit 对无效 hwnd 安全',
      cast.fit_window_to_aspect(0, 1080, 2220, 700) is False)
check('fit 对 fit_w=0 不动作',
      cast.fit_window_to_aspect(1, 1080, 2220, 0) is False)
mon.device = None
mon.state['device'] = ''

# ---- 9. 设置窗口快速投屏出口 ----
print('[9] 设置窗口快速投屏入口')
import settings_gui  # noqa: E402
ps = settings_gui.SETTINGS_PS
check('设置窗口有快速投屏按钮', '快速投屏' in ps and 'btnCast' in ps)
check('按钮回传 cast 动作', '{"action":"cast"}' in ps)

# ---- 10. 多手机选择 + 自动安装接收器 ----
print('[10] 多手机选择 / 自动安装 APK')
src_dock = open(dock.__file__, encoding='utf-8').read()
check('launch_viewer_on_phone 按设备定向(-s serial)', "'-s', s, 'shell'" in src_dock)
check('多台手机弹选择框', callable(getattr(dock, '_choose_phones', None)))
check('选择框手机列表走 base64 JSON', 'PHONES_B64' in src_dock
      and 'FromBase64String' in src_dock)
check('全部手机选项存在', '__ALL__' in src_dock)
check('未安装时自动装 APK', 'viewer_installed' in src_dock
      and 'install_viewer' in src_dock and "'install', '-r', '-t'" in src_dock)
check('APK 路径可解析', dock.viewer_apk_path().endswith('ExtScreenViewer.apk'),
      dock.viewer_apk_path())
check('pm list packages 校验安装', "'pm', 'list', 'packages'" in src_dock)
check('USB 回环 adb reverse（断网手机也能投）',
      "'reverse', 'tcp:%d' % port" in src_dock
      and '127.0.0.1:%d/' in src_dock)
check('选窗对话框内含手机选择（合一界面）',
      'PHONES:' in dock._PICKER_PS and '全部手机' in dock._PICKER_PS
      and '$multiPhone' in dock._PICKER_PS)
check('_pick_window 返回 serials',
      "'wins': [{'hwnd': int(h), 'title': t, 'exe': e}" in src_dock
      and "elif line.startswith('PHONES:')" in src_dock)
check('list_windows 三元组解包无残留（炸过 too many values to unpack）',
      'for h, t in wins' not in src_dock
      and 'for h, t in extscreen_cast.list_windows()' not in src_dock)
check('停投后 force-stop 接收器恢复手机原状',
      callable(getattr(dock, 'stop_viewer_for_serials', None))
      and "'am', 'force-stop', VIEWER_PKG" in src_dock
      and "'reverse', '--remove-all'" in src_dock)
check('拉起成功记录到会话 launched_serials（多观众取并集）',
      'mon.launched_serials = list(dict.fromkeys(' in src_dock
      and 'prev + launched_now' in src_dock)
check('同窗口再投不重启会话（多观众加入）',
      '窗口已在投屏中，新手机直接加入当前画面' in src_dock)
check('已投手机不被打断（prev 跳过）', 'if s in prev:' in src_dock)
check('poll_loop 逐路检测投屏停止跳变并清理', '_wasRun' in src_dock
      and 'stop_viewer_for_serials(' in src_dock)
check('多路投屏：CastManager 槽位/端口/查找',
      callable(getattr(cast, 'CastManager', None))
      and cast.MANAGER.mons[0] is cast.MONITOR
      and cast.MANAGER.next_port() >= 8123
      and cast.MANAGER.acquire(0, 'x') is not None)
check('多路状态：menu_cast 列出每路', 'MANAGER.running()' in src_dock
      and '路分享中' in src_dock)
check('多路停止：全部停止 + 单路停止', 'cast_stop_all' in src_dock
      and '_stop_one_mon' in src_dock)
check('会话级手机清理', 'launched_serials' in src_dock
      and 'stop_viewer_for_serials' in src_dock)

print('=' * 58)
print('ALL PASSED (%d checks)' % PASS)