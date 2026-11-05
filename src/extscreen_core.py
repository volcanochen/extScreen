# -*- coding: utf-8 -*-
"""
ExtScreen 副屏助手
--------------------
Windows 托盘工具：把 Android 手机变成电脑的第二块屏幕（Sunshine + Moonlight + VDD），
或者随时切换成「控制手机本体」模式（scrcpy）。

托盘图标状态：
  灰  扩展屏关闭
  绿  扩展屏已就绪，等待手机连接
  蓝  虚拟屏已挂出，手机可看到画面
  红  环境异常，需要修复
"""
import os
import re
import sys
import json
import time
import socket
import threading
import subprocess
import ctypes
from ctypes import wintypes

import pystray
from PIL import Image, ImageDraw

# --------------------------------------------------------------------------
# 常量与路径
# --------------------------------------------------------------------------
APP_NAME = 'ExtScreen 副屏助手'
APP_ID = 'ExtScreenDock'

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
    MEIPASS = getattr(sys, '_MEIPASS', APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    MEIPASS = APP_DIR

TOOLS_DIR = r'C:\Tools\moonlight-desktop-extender'
VDISPLAY_PS1 = os.path.join(TOOLS_DIR, 'scripts', 'vdisplay.ps1')
EXT_CONFIG = os.path.join(TOOLS_DIR, 'config.json')
SCRCPY_DIR = r'C:\Tools\scrcpy'
SCRCPY_EXE = os.path.join(SCRCPY_DIR, 'scrcpy.exe')
ADB_EXE = os.path.join(SCRCPY_DIR, 'adb.exe')
# 没装 scrcpy 时退回官方 SDK 的 adb（本机就是这种情况）
ADB_FALLBACK = os.path.join(
    os.environ.get('LOCALAPPDATA', r'C:\Users\volcano\AppData\Local'),
    'Android', 'Sdk', 'platform-tools', 'adb.exe')
SUNSHINE_SVC = 'SunshineService'
SUNSHINE_DIR = r'C:\Program Files\Sunshine'
VDD_DEVICE = r'ROOT\DISPLAY\0001'
VDD_SETTINGS = r'C:\VirtualDisplayDriver\vdd_settings.xml'

LOG_PATH = os.path.join(APP_DIR, 'extscreen.log')
CREATE_NO_WINDOW = 0x08000000

COLOR_OFF = (130, 130, 130)
COLOR_READY = (46, 160, 67)
COLOR_LIVE = (30, 130, 255)
COLOR_ERR = (200, 60, 60)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def log(msg):
    try:
        line = time.strftime('[%Y-%m-%d %H:%M:%S] ') + str(msg)
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def run(cmd, timeout=180):
    """执行命令，返回 (returncode, 输出文本)。不弹黑窗。"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        out = b''
        out += r.stdout or b''
        out += r.stderr or b''
        for enc in ('utf-8', 'gbk'):
            try:
                return r.returncode, out.decode(enc)
            except UnicodeDecodeError:
                continue
        return r.returncode, out.decode('utf-8', 'ignore')
    except subprocess.TimeoutExpired:
        return -1, 'timeout'
    except Exception as e:
        return -1, str(e)


def powershell(script_path, *args, timeout=180):
    cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
           '-WindowStyle', 'Hidden', '-File', script_path] + list(args)
    return run(cmd, timeout)


# --------------------------------------------------------------------------
# Win32：服务状态、显示器枚举
# --------------------------------------------------------------------------
_advapi = ctypes.WinDLL('advapi32', use_last_error=True)
_user32 = ctypes.WinDLL('user32', use_last_error=True)

SERVICE_STOPPED = 1
SERVICE_RUNNING = 4
# 服务确实存在，但当前通道查不到状态（权限/句柄异常）——不能当成"未安装"
SERVICE_UNKNOWN = -1
DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ('dwServiceType', wintypes.DWORD),
        ('dwCurrentState', wintypes.DWORD),
        ('dwControlsAccepted', wintypes.DWORD),
        ('dwWin32ExitCode', wintypes.DWORD),
        ('dwServiceSpecificExitCode', wintypes.DWORD),
        ('dwCheckPoint', wintypes.DWORD),
        ('dwWaitHint', wintypes.DWORD),
        ('dwProcessId', wintypes.DWORD),
        ('dwServiceFlags', wintypes.DWORD),
    ]


# 必须声明 argtypes/restype：否则 64 位下返回的 SC_HANDLE 会被截成 32 位，
# 后续 QueryServiceStatusEx 一律失败，表现为“明明服务在跑却报未安装”。
_advapi.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_advapi.OpenSCManagerW.restype = wintypes.HANDLE
_advapi.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
_advapi.OpenServiceW.restype = wintypes.HANDLE
_advapi.QueryServiceStatusEx.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_advapi.QueryServiceStatusEx.restype = wintypes.BOOL
_advapi.CloseServiceHandle.argtypes = [wintypes.HANDLE]
_advapi.CloseServiceHandle.restype = wintypes.BOOL
_advapi.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
_advapi.StartServiceW.restype = wintypes.BOOL
_advapi.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                   ctypes.c_void_p]
_advapi.ControlService.restype = wintypes.BOOL

# SCM 权限位
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 1
# 常见 Win32 错误码
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_ACCESS_DENIED = 5


def start_service_scm(name):
    """用 SCM 原生 API 启动服务，返回 (ok: bool, err: int)。

    比 `sc start` 可靠：sc.exe 的输出随系统语言变化，解析容易碎，
    而且它是外部进程，沙箱/受限环境里常被拦。这里直接调 advapi32。
    """
    hscm = _advapi.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not hscm:
        return False, ctypes.get_last_error()
    try:
        hs = _advapi.OpenServiceW(hscm, name,
                                  SERVICE_START | SERVICE_QUERY_STATUS | SERVICE_STOP)
        if not hs:
            return False, ctypes.get_last_error()
        try:
            if _advapi.StartServiceW(hs, 0, None):
                return True, 0
            err = ctypes.get_last_error()
            if err == ERROR_SERVICE_ALREADY_RUNNING:
                return True, err
            return False, err
        finally:
            _advapi.CloseServiceHandle(hs)
    finally:
        _advapi.CloseServiceHandle(hscm)


def stop_service_scm(name):
    """用 SCM 原生 API 停止服务，返回 (ok: bool, err: int)。"""
    hscm = _advapi.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not hscm:
        return False, ctypes.get_last_error()
    try:
        hs = _advapi.OpenServiceW(hscm, name,
                                  SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not hs:
            return False, ctypes.get_last_error()
        try:
            st = SERVICE_STATUS_PROCESS()
            need = wintypes.DWORD(0)
            _advapi.QueryServiceStatusEx(hs, 0, ctypes.byref(st),
                                         ctypes.sizeof(st), ctypes.byref(need))
            if int(st.dwCurrentState) == SERVICE_STOPPED:
                return True, 0
            if not _advapi.ControlService(hs, SERVICE_CONTROL_STOP, None):
                err = ctypes.get_last_error()
                if err == ERROR_SERVICE_NOT_ACTIVE:
                    return True, err
                return False, err
            return True, 0
        finally:
            _advapi.CloseServiceHandle(hs)
    finally:
        _advapi.CloseServiceHandle(hscm)


def _service_exists(name):
    """服务是否已注册（只读注册表，不依赖 SCM 句柄）。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Services\%s' % name):
            return True
    except Exception:
        return False


def service_state(name):
    """返回服务状态码（4=运行中，1=已停止）。

    服务不存在返回 None；服务存在但查不到状态返回 SERVICE_UNKNOWN。
    SCM 句柄拿不到时退回注册表 + sc query，避免把"查不到"误报成"未安装"。
    """
    SC_MANAGER_CONNECT = 0x0001
    SERVICE_QUERY_STATUS = 0x0004
    hscm = _advapi.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if hscm:
        try:
            hs = _advapi.OpenServiceW(hscm, name, SERVICE_QUERY_STATUS)
            if hs:
                try:
                    st = SERVICE_STATUS_PROCESS()
                    need = wintypes.DWORD(0)
                    ok = _advapi.QueryServiceStatusEx(
                        hs, 0, ctypes.byref(st), ctypes.sizeof(st), ctypes.byref(need))
                    if ok:
                        return int(st.dwCurrentState)
                finally:
                    _advapi.CloseServiceHandle(hs)
        finally:
            _advapi.CloseServiceHandle(hscm)

    # 句柄通道失败：注册表确认服务是否存在，再尽力取状态
    if not _service_exists(name):
        return None
    rc, out = run(['sc', 'query', name], timeout=20)
    m = re.search(r'STATE\s*:\s*(\d+)', out or '')
    if m:
        return int(m.group(1))
    return SERVICE_UNKNOWN


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128),
    ]


def list_monitors():
    """返回 [(device_name, device_string, attached)]，覆盖所有显示适配器。"""
    out = []
    i = 0
    while True:
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not _user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
            break
        attached = bool(dd.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP)
        out.append((dd.DeviceName, dd.DeviceString, attached))
        i += 1
    return out


VDD_NAME_HINTS = ('Virtual Display Driver', 'MttVDD', 'VirtualDisplay', 'Virtual Display')


def vdd_attached():
    """虚拟显示器是否已经挂在桌面上。"""
    for name, desc, attached in list_monitors():
        if not attached:
            continue
        for hint in VDD_NAME_HINTS:
            if hint.lower() in (desc or '').lower():
                return True
    return False


def monitor_count():
    return sum(1 for _, _, a in list_monitors() if a)


# --------------------------------------------------------------------------
# 设备 / 依赖检测
# --------------------------------------------------------------------------
def adb_devices():
    """返回已授权的 adb 设备列表 [(serial, model)]。"""
    if not os.path.exists(ADB_EXE):
        return []
    rc, out = run([ADB_EXE, 'devices', '-l'], timeout=20)
    res = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('*') or line.startswith('List of'):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            model = ''
            for p in parts[2:]:
                if p.startswith('model:'):
                    model = p.split(':', 1)[1]
            res.append((parts[0], model or 'Android'))
    return res


def sunshine_exe():
    """返回 sunshine.exe 完整路径，找不到返回空串。"""
    for p in (os.path.join(SUNSHINE_DIR, 'sunshine.exe'),
              r'C:\Program Files (x86)\Sunshine\sunshine.exe'):
        if os.path.exists(p):
            return p
    return ''


def sunshine_config_dir():
    """Sunshine 的 config 目录（按实际安装位置推断）。"""
    exe = sunshine_exe()
    base = os.path.dirname(exe) if exe else SUNSHINE_DIR
    return os.path.join(base, 'config')


def sunshine_installed():
    """三重判定：主程序 / config 目录 / 服务注册。

    旧版只看 SCM 句柄，句柄被截断时会误报"未安装"，这里补齐。
    """
    if sunshine_exe():
        return True
    if os.path.isdir(sunshine_config_dir()):
        return True
    return _service_exists(SUNSHINE_SVC)


# --------------------------------------------------------------------------
# Sunshine 是否「真正可用」
#
# 关键：SCM 说 SERVICE_RUNNING 不等于能接受串流。
# Sunshine 进程起来后还要枚举显示器、编译 shader、建 NVENC 编码器、
# 起 HTTP 线程、最后才注册 mDNS，实测这段空窗有 25~30 秒。
# 这段空窗里拿服务状态当「已启动」会骗用户：点一下连接必然失败。
# --------------------------------------------------------------------------
SUNSHINE_READY_PORT = 47990      # HTTPS 配置界面
SUNSHINE_STREAM_PORT = 47989     # RTSP / 串流端口
SUNSHINE_LOG = 'sunshine.log'

# Sunshine 内部初始化完成的标志。mDNS 注册是最后一步，最可靠；
# 万一配置里关掉了 mDNS，退而求其次看配置界面是否已就绪。
_READY_MARKS = ('Registered Sunshine mDNS service',
                'Configuration UI available at')
_STOPPED_MARKS = ('Unregistered Sunshine mDNS service',)


def _sunshine_log_paths():
    """当前日志 + 轮转的旧日志，按新到旧排列。"""
    d = sunshine_config_dir()
    paths = []
    cur = os.path.join(d, SUNSHINE_LOG)
    if os.path.exists(cur):
        paths.append(cur)
        i = 1
        while True:
            p = os.path.join(d, '%s.%d' % (SUNSHINE_LOG, i))
            if not os.path.exists(p):
                break
            paths.append(p)
            i += 1
    return paths


def _sunshine_log_tail(max_bytes=1024 * 1024):
    """读取最新那份 sunshine.log 的尾部文本。返回 (text, ok)。"""
    paths = _sunshine_log_paths()
    if not paths:
        return '', False
    try:
        with open(paths[0], 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode('utf-8', 'ignore'), True
    except Exception:
        return '', False


def sunshine_ready():
    """Sunshine 是否已经真正可以接受串流。返回 (ready: bool, reason: str)。

    判据：
      1. 服务状态是 RUNNING
      2. 日志里出现过初始化完成标志，且之后没有出现注销标志
         —— 这一步才是「真的能连」的关键，服务状态只说明进程活着
    """
    if service_state(SUNSHINE_SVC) != SERVICE_RUNNING:
        return False, '服务未运行'

    # 只看最新的那个日志文件：sunshine.log 会滚动成 .1/.2/…，
    # 旧文件里的就绪标志属于上一次会话，不能拿来判「当前」。
    tail, ok = _sunshine_log_tail(512 * 1024)
    if not ok:
        return False, '找不到 Sunshine 日志，无法确认就绪状态'
    ready_at = max((tail.rfind(m) for m in _READY_MARKS), default=-1)
    stopped_at = max((tail.rfind(m) for m in _STOPPED_MARKS), default=-1)

    if ready_at < 0:
        return False, '服务已起，但 Sunshine 还在初始化（尚未完成）'
    if stopped_at > ready_at:
        return False, 'Sunshine 上次已退出，等待重新就绪'
    return True, '已就绪'


def sunshine_client_connected():
    """Sunshine 当前是否有客户端连着（手机正在看画面）。

    三个坑都要避开：
      1. sunshine.log 会滚动成 .1/.2/…，旧文件里的 CLIENT CONNECTED
         属于上一次会话，不能拿来判「当前」——只读最新那一份。
      2. Sunshine 被服务停止强杀时**不会**打印 CLIENT DISCONNECTED，
         只打印 Unregistered Sunshine mDNS service。所以只看 CONNECTED /
         DISCONNECTED 的先后会把「早就断开的连接」一直算成连着。
      3. 重启后新会话还没连上时，上一轮的 CONNECTED 仍留在日志里，
         同样会误报。

    因此这里把判定锚定在「当前这一轮会话」上：先找到最后一次
    初始化完成(READY)的位置，只在它之后比较 CONNECTED / DISCONNECTED；
    若之后出现过注销(STOPPED)，说明这一轮已经结束，直接算没连。
    """
    tail, ok = _sunshine_log_tail()
    if not ok:
        return False
    ready_at = max((tail.rfind(m) for m in _READY_MARKS), default=-1)
    stopped_at = max((tail.rfind(m) for m in _STOPPED_MARKS), default=-1)

    # 这一轮 Sunshine 已经退出过，之后的一切都不算数
    if ready_at < 0 or stopped_at > ready_at:
        return False
    # 只看本次就绪之后发生的连接事件
    last_con = tail.rfind('CLIENT CONNECTED', ready_at)
    last_dis = tail.rfind('CLIENT DISCONNECTED', ready_at)
    return last_con > last_dis


def vdd_installed():
    rc, out = run(['pnputil', '/enum-devices', '/class', 'Display', '/ids'], timeout=30)
    return 'MttVDD' in out


def scrcpy_running():
    rc, out = run(['tasklist', '/FI', 'IMAGENAME eq scrcpy.exe', '/NH'], timeout=15)
    return 'scrcpy.exe' in out


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
DEFAULT_EXT_CONFIG = {
    'resolution': '800x1646',
    # 出厂默认 800x1646 竖屏：手机 PPI 高，降低渲染分辨率→手机端拉伸后字更大更清楚
    # （可在托盘「虚拟屏分辨率」子菜单或设置窗口随时改；用户本机 config.json 会覆盖此值）
    'orientation': 'portrait',
    'position': 'right',
    'align': 'start',
    'forceSdr': True,
    'streamAudio': False,
    'fallbackWidth': 1080,
    'fallbackHeight': 2220,
    'fallbackFps': 60,
    'vddHardwareId': 'Root\\MttVDD',
    'vddSettingsPath': VDD_SETTINGS,
    'sunshineService': SUNSHINE_SVC,
    'sunshineDir': '',
    'displayWaitSeconds': 15,
    # 手机端 Moonlight 自动拉起串流用：PC 的 UUID 由 adb logcat 自动抓取后缓存
    'moonlightUuid': '',
    'moonlightAppId': '881448767',
    'moonlightAppName': 'Desktop',
    # 窗口投屏（Deskreen CE 后端）
    'deskreenPath': '',          # 自动探测；装在非常规位置时手动指定 exe
    'deskreenAutoStart': False,  # Dock 启动时自动拉起 Deskreen 并广播
    'deskreenInspectPort': 9229, # Node inspector 端口（兜底通道）
    'deskreenDebugPort': 9222,   # CDP 调试端口（主通道，读房间号用）
    # 窗口投屏（自研 cast 后端）
    'castPort': 8123,            # HTTP 服务端口（占用时自动换）
    'castWidth': 1080,           # 推流宽度（手机端显示宽度；720 在 1080p 手机上发虚）
    'castFps': 20,               # 帧率上限
    'castQuality': 80,           # JPEG 质量
    'castAdbLaunch': True,       # 投屏开始时用 adb 自动拉起 USB 连接的手机接收器
    'castFitWindow': True,       # 按手机屏幕比例自动调整被投窗口大小（画面铺满无黑边）
    'castFitWidth': 700,         # 适配后的窗口可见宽度（像素，0=保持原窗口大小只记比例）
    'castFitFollowRotate': True, # 手机横竖屏切换时跟随调整窗口比例
    'castTouchControl': True,    # 手机触摸回传为鼠标消息（只投给被投窗口）
    'castNoActivate': True,      # 无感操作：投屏期间点击不把窗口顶到前台
}

# 第一个是「跟随手机屏幕」，其余是手动指定。
#
# 手机 PPI 比电脑显示器高得多：虚拟屏渲染的像素越密，串流到手机被拉伸后
# 内容就越小、越看不清。所以这里提供一组「竖屏、逐档降低渲染分辨率」的选项——
# 数值越小，Windows 在越少的像素上画同样的桌面，手机端拉伸后文字/图标越大越清楚
# （代价是略糊，但「看得清」优先）。档位保持 ≈19.5:9（与 Note8 同比例），手机上不变形。
#
# 注意：这里的每一项都必须同时写进 extscreen_dock.py 的 VDD_XML 模板
# （<resolutions>），否则驱动不声明该模式，ChangeDisplaySettingsExW 会 BADMODE。
# 修改后请两处一起改，保持一致。
RESOLUTION_PRESETS = [
    ('自动：跟随手机屏幕铺满（推荐）', 0, 0),
    # —— 竖屏：从最细腻到最大字，逐档放大 ——
    ('1440 x 2960   竖屏 · 最细腻（字最小）', 1440, 2960),
    ('1080 x 2220   竖屏 · 标准', 1080, 2220),
    ('900 x 1850    竖屏 · 字稍大', 900, 1850),
    ('800 x 1646    竖屏 · 字更大（默认）', 800, 1646),
    ('720 x 1480    竖屏 · 字明显更大', 720, 1480),
    ('640 x 1320    竖屏 · 字很大', 640, 1320),
    ('540 x 1110    竖屏 · 字最大最清楚', 540, 1110),
    # —— 横屏：偶尔横放手机或接平板时用 ——
    ('1920 x 1080   横屏 · 标准', 1920, 1080),
    ('2220 x 1080   横屏 · 宽屏', 2220, 1080),
    ('1280 x 720    横屏 · 轻量', 1280, 720),
]


def load_ext_config():
    try:
        with open(EXT_CONFIG, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        for k, v in DEFAULT_EXT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return dict(DEFAULT_EXT_CONFIG)


def save_ext_config(cfg):
    try:
        os.makedirs(os.path.dirname(EXT_CONFIG), exist_ok=True)
        with open(EXT_CONFIG, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True, '配置已保存'
    except Exception as e:
        return False, str(e)
