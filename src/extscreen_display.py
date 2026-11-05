# -*- coding: utf-8 -*-
r"""
虚拟显示器的原生控制
--------------------
负责三件事，全部走 Win32 API，不依赖外部脚本：

1. 把虚拟显示器设成需要的分辨率 / 刷新率，并摆到主屏右侧；
2. 读出虚拟显示器的位置与尺寸，供托盘状态显示；
3. 把虚拟显示器的 device_id 写进 Sunshine 的 output_name，固定串流目标。

踩过的坑（务必保留这些注释）：
  * 进程必须先声明 DPI 感知。否则 SetWindowPos / 坐标计算会被系统按主屏缩放
    虚拟化，算出来的坐标送到屏幕外，窗口被 Windows 钳回主屏 —— 表现为
    "函数返回成功、画面里却没有窗口"。
  * Sunshine 的 output_name 要填 device_id（形如 {f3c4cd9e-...}），
    填 \\.\DISPLAYxx 会报 Failed to find device。
  * device_id 由显示器 EDID 序列号推导，是稳定的；而 \\.\DISPLAYxx 的编号
    会随插拔变化（曾经 DISPLAY79 → DISPLAY117）。所以只能存 device_id。
"""
import ctypes
import json
import os
import re
import time
from ctypes import wintypes

_user32 = ctypes.WinDLL('user32', use_last_error=True)

# DEVMODE 字段位
DM_POSITION = 0x00000020
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFREQUENCY = 0x00400000

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
CDS_UPDATEREGISTRY = 0x00000001
CDS_NORESET = 0x10000000
CDS_RESET = 0x40000000

DISP_CHANGE_SUCCESS = 0
DISP_CHANGE_BADMODE = -2

VDD_HINTS = ('vdd by mtt', 'virtual display driver', 'mttvdd')


class DEVMODEW(ctypes.Structure):
    _fields_ = [
        ('dmDeviceName', wintypes.WCHAR * 32),
        ('dmSpecVersion', wintypes.WORD), ('dmDriverVersion', wintypes.WORD),
        ('dmSize', wintypes.WORD), ('dmDriverExtra', wintypes.WORD),
        ('dmFields', wintypes.DWORD),
        ('dmPositionX', ctypes.c_long), ('dmPositionY', ctypes.c_long),
        ('dmDisplayOrientation', wintypes.DWORD), ('dmDisplayFixedOutput', wintypes.DWORD),
        ('dmColor', ctypes.c_short), ('dmDuplex', ctypes.c_short),
        ('dmYResolution', ctypes.c_short), ('dmTTOption', ctypes.c_short),
        ('dmCollate', ctypes.c_short),
        ('dmFormName', wintypes.WCHAR * 32), ('dmLogPixels', wintypes.WORD),
        ('dmBitsPerPel', wintypes.DWORD),
        ('dmPelsWidth', wintypes.DWORD), ('dmPelsHeight', wintypes.DWORD),
        ('dmDisplayFlags', wintypes.DWORD), ('dmDisplayFrequency', wintypes.DWORD),
        ('dmICMMethod', wintypes.DWORD), ('dmICMIntent', wintypes.DWORD),
        ('dmMediaType', wintypes.DWORD), ('dmDitherType', wintypes.DWORD),
        ('dmReserved1', wintypes.DWORD), ('dmReserved2', wintypes.DWORD),
        ('dmPanningWidth', wintypes.DWORD), ('dmPanningHeight', wintypes.DWORD),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128),
    ]


def set_dpi_aware():
    """进程级 DPI 感知。必须在做任何坐标运算之前调用。"""
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return 'per-monitor-v2'
    except Exception:
        pass
    try:
        _user32.SetProcessDPIAware()
        return 'system'
    except Exception:
        return 'none'


def dpi_awareness():
    """返回当前感知级别：1=系统 2=每显示器 0=未感知。"""
    try:
        _user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        ctx = _user32.GetThreadDpiAwarenessContext()
        return _user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(ctx))
    except Exception:
        return -1


# --------------------------------------------------------------------------
# 设备枚举
# --------------------------------------------------------------------------
def enum_devices():
    """列出所有显示适配器。

    返回 [{'name','desc','attached','primary','id'}]，name 形如 \\\\.\\DISPLAY1。
    """
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not _user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        out.append({
            'name': dd.DeviceName,
            'desc': dd.DeviceString,
            'attached': bool(dd.StateFlags & 0x1),
            'primary': bool(dd.StateFlags & 0x4),
            'id': dd.DeviceID,
        })
        i += 1
    return out


def current_mode(dev):
    """读取设备当前模式，失败返回 None。"""
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    if not _user32.EnumDisplaySettingsW(dev, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return None
    return dm


def list_modes(dev):
    """列出设备支持的所有模式 [(w,h,fps)]，已去重。"""
    seen = []
    i = 0
    while True:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if not _user32.EnumDisplaySettingsW(dev, i, ctypes.byref(dm)):
            break
        key = (dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayFrequency)
        if key not in seen:
            seen.append(key)
        i += 1
    return seen


def find_vdd():
    """找到已挂到桌面上的虚拟显示器，返回 (name, desc)；没有则 (None, None)。"""
    for d in enum_devices():
        if not d['attached']:
            continue
        low = (d['desc'] or '').lower()
        if any(h in low for h in VDD_HINTS):
            return d['name'], d['desc']
    return None, None


def find_vdd_any():
    """找到虚拟显示器的 GDI 适配器名（含未挂出的）。

    每次驱动 reload 后 GDI 名都会变（DISPLAY467→505…），必须现查现用。
    """
    for d in enum_devices():
        low = (d['desc'] or '').lower()
        if any(h in low for h in VDD_HINTS):
            return d['name'], d['desc']
    return None, None


def attach_monitor(dev, w, h, fps=60, x=None, y=None):
    """把「设备在但显示器没挂到桌面上」的虚拟屏显式挂出。

    驱动 reload（pnputil enable / 设备节点重建）后 Windows 只是创建
    monitor 对象，不一定把它放进桌面拓扑——表现为 EnumDisplayDevices
    一直 ATTACHED_TO_DESKTOP=0，重多少遍都没用。经典两步法（先对
    该适配器写 DM_POSITION+分辨率（CDS_NORESET|CDS_UPDATEREGISTRY），
    再空调用一次提交拓扑）可以强制挂出。

    注意：detached 显示器 EnumDisplaySettings 拿不到当前模式，
    所以这里从零构造 DEVMODE，不走 current_mode()。
    """
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    dm.dmFields = (DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
                   | DM_POSITION)
    dm.dmPelsWidth = w
    dm.dmPelsHeight = h
    dm.dmDisplayFrequency = fps
    if x is not None:
        dm.dmPositionX = x
    if y is not None:
        dm.dmPositionY = y
    rc = _user32.ChangeDisplaySettingsExW(
        ctypes.c_wchar_p(dev), ctypes.byref(dm), None,
        CDS_UPDATEREGISTRY | CDS_NORESET, None)
    if rc != DISP_CHANGE_SUCCESS:
        return False, f'挂出第一步失败 rc={rc}'
    rc2 = _user32.ChangeDisplaySettingsExW(None, None, None, 0, None)
    if rc2 != DISP_CHANGE_SUCCESS:
        return False, f'拓扑提交失败 rc={rc2}'
    return True, '已挂出'


def vdd_rect():
    """返回虚拟显示器的 (x, y, w, h)；未挂出返回 None。"""
    dev, _ = find_vdd()
    if not dev:
        return None
    dm = current_mode(dev)
    if not dm:
        return None
    return (dm.dmPositionX, dm.dmPositionY, dm.dmPelsWidth, dm.dmPelsHeight)


def desktop_bounds():
    """所有已挂显示器合并后的最小外接矩形 (x, y, right, bottom)。"""
    x0 = y0 = 10 ** 9
    x1 = y1 = -(10 ** 9)
    for d in enum_devices():
        if not d['attached']:
            continue
        dm = current_mode(d['name'])
        if not dm:
            continue
        x0 = min(x0, dm.dmPositionX)
        y0 = min(y0, dm.dmPositionY)
        x1 = max(x1, dm.dmPositionX + dm.dmPelsWidth)
        y1 = max(y1, dm.dmPositionY + dm.dmPelsHeight)
    if x0 > x1:
        return None
    return (x0, y0, x1, y1)


# --------------------------------------------------------------------------
# 应用模式 / 摆位
# --------------------------------------------------------------------------
def _supported(dev, w, h, fps):
    for mw, mh, mf in list_modes(dev):
        if mw == w and mh == h and (fps is None or mf == fps):
            return (mw, mh, mf)
    # 没有精确 fps 就退而求其次找同分辨率
    for mw, mh, mf in list_modes(dev):
        if mw == w and mh == h:
            return (mw, mh, mf)
    return None


def apply_mode(dev, w, h, fps=60, x=None, y=None, persist=True):
    """把 dev 切到 w x h @fps，并摆到 (x, y)。

    分辨率必须是驱动 vdd_settings.xml 里声明过的，否则返回 DISP_CHANGE_BADMODE。
    返回 (ok, 消息)。
    """
    hit = _supported(dev, w, h, fps)
    if not hit:
        modes = list_modes(dev)
        sample = ', '.join('%dx%d@%d' % m for m in modes[:6])
        return False, f'驱动不支持 {w}x{h}@{fps}，可用：{sample}…'
    mw, mh, mf = hit

    dm = current_mode(dev)
    if dm is None:
        return False, f'读不到 {dev} 的当前模式'
    dm.dmPelsWidth = mw
    dm.dmPelsHeight = mh
    dm.dmDisplayFrequency = mf
    dm.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    if x is not None and y is not None:
        dm.dmPositionX = x
        dm.dmPositionY = y
        dm.dmFields |= DM_POSITION

    flags = CDS_RESET
    if persist:
        flags |= CDS_UPDATEREGISTRY
    rc = _user32.ChangeDisplaySettingsExW(dev, ctypes.byref(dm), None, flags, None)
    if rc == DISP_CHANGE_SUCCESS:
        return True, f'已设为 {mw}x{mh}@{mf}'
    return False, f'ChangeDisplaySettingsEx 失败 rc={rc}'


def calc_position(dev, w, h, position='right', align='start', gap=0):
    """只算该摆在哪，不应用。返回 (x, y)；没有其它显示器时返回 None。

    align: start / center / end
    用主屏作对齐基准而不是所有屏的外接框，否则主屏上方还有副屏时
    （例如 4K 屏在 y=-740）虚拟屏会被顶到负坐标去。
    """
    others = []
    primary = None
    for d in enum_devices():
        if not d['attached'] or d['name'] == dev:
            continue
        dm = current_mode(d['name'])
        if not dm:
            continue
        rect = (dm.dmPositionX, dm.dmPositionY, dm.dmPelsWidth, dm.dmPelsHeight)
        others.append(rect)
        if d['primary'] and primary is None:
            primary = rect
    if not others:
        return None

    left = min(o[0] for o in others)
    top = min(o[1] for o in others)
    right = max(o[0] + o[2] for o in others)
    bottom = max(o[1] + o[3] for o in others)
    ref = primary or (left, top, right - left, bottom - top)
    rx, ry, rw, rh = ref

    def along(base, span, size):
        if align == 'center':
            return base + (span - size) // 2
        if align == 'end':
            return base + span - size
        return base

    position = (position or 'right').lower()
    if position == 'left':
        return (left - gap - w, along(ry, rh, h))
    if position == 'above':
        return (along(rx, rw, w), top - gap - h)
    if position == 'below':
        return (along(rx, rw, w), bottom + gap)
    return (right + gap, along(ry, rh, h))


def place(dev, w, h, fps=60, position='right', align='start', gap=0):
    """把设备摆到其它显示器的右/左/上/下方。"""
    xy = calc_position(dev, w, h, position, align, gap)
    if xy is None:
        return apply_mode(dev, w, h, fps, 0, 0)
    return apply_mode(dev, w, h, fps, xy[0], xy[1])


def place_right(dev, w, h, fps=60, gap=0, align='start'):
    """兼容旧调用：等价于 place(..., position='right')。"""
    return place(dev, w, h, fps, 'right', align, gap)


def mode_matches(dev, w, h):
    dm = current_mode(dev)
    if not dm:
        return False
    return dm.dmPelsWidth == w and dm.dmPelsHeight == h


# --------------------------------------------------------------------------
# Sunshine 串流目标绑定
# --------------------------------------------------------------------------
def parse_sunshine_displays(log_path, tail_bytes=4 * 1024 * 1024):
    """从 Sunshine 日志里解析显示器枚举块，返回 [{device_id, display_name, ...}]。

    日志里以 "Currently available display devices:" 开头，紧跟一段 JSON 数组。
    每次 Sunshine 启动都会打印，所以取最后一次即可。
    注意日志在 debug 级别下涨得很快，尾部窗口要给足，否则枚举块会被挤出去。
    """
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            raw = f.read()
        text = raw.decode('utf-8', 'ignore')
    except Exception:
        return []

    marker = 'Currently available display devices:'
    idx = text.rfind(marker)
    if idx < 0:
        return []
    body = text[idx + len(marker):]
    start = body.find('[')
    if start < 0:
        return []
    # 用括号配对截出完整 JSON 数组
    depth = 0
    end = -1
    for i in range(start, len(body)):
        c = body[i]
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return []
    try:
        data = json.loads(body[start:end])
    except Exception:
        return []
    return data if isinstance(data, list) else []


def sunshine_device_id_for(display_name, log_path):
    """按 \\\\.\\DISPLAYxx 找到对应的 device_id。"""
    norm = display_name.replace('\\', '\\\\')
    for item in parse_sunshine_displays(log_path):
        dn = item.get('display_name', '')
        if dn == display_name or dn == norm or dn.replace('\\\\', '\\') == display_name:
            return item.get('device_id')
    return None


# MttVDD 的 EDID 特征：manufacturer_id=MTT，product_code=1337
VDD_EDID_MANUFACTURER = 'MTT'


def sunshine_vdd_device_id(log_path):
    """在 Sunshine 的显示器枚举里找出虚拟屏的 device_id。

    不按 display_name 找：虚拟屏每次重新枚举都会拿到新的 DISPLAY 编号
    （DISPLAY117 → DISPLAY118 → …），而 device_id 是由 EDID 推导的，跨重启
    保持不变。按 EDID 特征匹配就不用为了拿 id 而重启 Sunshine。
    """
    fallback = None
    for item in parse_sunshine_displays(log_path):
        edid = item.get('edid') or {}
        if (edid.get('manufacturer_id') or '').upper() == VDD_EDID_MANUFACTURER:
            return item.get('device_id')
        name = (item.get('friendly_name') or '').lower()
        if 'vdd' in name or 'mtt' in name:
            fallback = fallback or item.get('device_id')
    return fallback


def write_sunshine_output(conf_path, device_id, extra=None):
    """把 output_name 写进 sunshine.conf，保留其它已有键。返回 (ok, 消息, changed)。"""
    want = 'output_name = %s' % device_id
    lines = []
    try:
        if os.path.exists(conf_path):
            with open(conf_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.read().splitlines()
    except Exception:
        lines = []

    changed = False
    found = False
    out = []
    for ln in lines:
        if re.match(r'\s*output_name\s*=', ln):
            found = True
            if ln.strip() != want:
                out.append(want)
                changed = True
            else:
                out.append(ln)
        else:
            out.append(ln)
    if not found:
        out.insert(0, want)
        changed = True

    if extra:
        for k, v in extra.items():
            pat = re.compile(r'\s*%s\s*=' % re.escape(k))
            hit = False
            newline = '%s = %s' % (k, v)
            out2 = []
            for ln in out:
                if pat.match(ln):
                    hit = True
                    if ln.strip() != newline:
                        out2.append(newline)
                        changed = True
                    else:
                        out2.append(ln)
                else:
                    out2.append(ln)
            if not hit:
                out2.append(newline)
                changed = True
            out = out2

    if not changed:
        return True, 'Sunshine 串流目标已是该虚拟屏', False
    try:
        with open(conf_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(out) + '\n')
        return True, '已写入串流目标 %s' % device_id, True
    except Exception as e:
        return False, '写入 sunshine.conf 失败：%s' % e, False
