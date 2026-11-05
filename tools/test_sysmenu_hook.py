# -*- coding: utf-8 -*-
"""e2e：低级钩子接管系统菜单（与 extscreen_dock._menu_hook_thread 同逻辑）。
对记事本依次触发：右键标题栏 / 右键客户区 / Alt+Space。
断言：标题栏和 Alt+Space 命中接管（记录 MENU 行），客户区不命中。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import threading
import time

OUT = r'C:\VOLCANO\myws\extScreen\tools\test_sysmenu_out.txt'
result = open(OUT, 'w', encoding='utf-8')
PASS = []
FAILED = []

WM_NCHITTEST = 0x0084
HTCAPTION, HTSYSMENU = 2, 3
GA_ROOT = 2
WM_RBUTTONUP = 0x0205
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
LLKHF_ALTDOWN = 0x20
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_APP_SYSMENU = 0x8000 + 0x5C7

u = ctypes.windll.user32
k32 = ctypes.windll.kernel32
u.SetProcessDPIAware()   # 测试进程必须感知 DPI，否则 SetCursorPos 坐标错位

TARGETS = set()
seen = []


class _MSLLPOINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', _MSLLPOINT), ('mouseData', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32),
                ('extra', ctypes.c_size_t)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vk', ctypes.c_uint32), ('scan', ctypes.c_uint32),
                ('flags', ctypes.c_uint32), ('time', ctypes.c_uint32),
                ('extra', ctypes.c_size_t)]


def _input_window_at(x, y):
    """真实输入路由语义的 WindowFromPoint：跳过 cloaked/穿透/最小化。"""
    import ctypes.wintypes as wt
    d = ctypes.windll.dwmapi
    u.GetTopWindow.argtypes = [ctypes.c_void_p]
    u.GetTopWindow.restype = ctypes.c_void_p
    u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetWindow.restype = ctypes.c_void_p
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h = u.GetTopWindow(None)
    while h:
        if u.IsWindowVisible(h) and not u.IsIconic(h):
            if not (u.GetWindowLongW(h, -20) & 0x20):
                cloaked = ctypes.c_uint32(0)
                d.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)
                if not cloaked.value:
                    r = wt.RECT()
                    if u.GetWindowRect(h, ctypes.byref(r)) \
                            and r.left <= x < r.right \
                            and r.top <= y < r.bottom:
                        return h
        h = u.GetWindow(h, 2)
    u.WindowFromPoint.argtypes = [wt.POINT]
    u.WindowFromPoint.restype = ctypes.c_void_p
    return u.WindowFromPoint(wt.POINT(x, y)) or 0


def _menu_target_at(x, y):
    w = _input_window_at(x, y)
    if not w:
        return 0
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetAncestor.restype = ctypes.c_void_p
    root = u.GetAncestor(w, GA_ROOT) or w
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_ssize_t]
    u.SendMessageW.restype = ctypes.c_ssize_t
    ht = u.SendMessageW(root, WM_NCHITTEST, 0, lp) & 0xFFFF
    result.write('hit: pt=(%d,%d) root=%s ht=%s in_targets=%s\n'
                 % (x, y, int(root), ht, int(root) in TARGETS))
    result.flush()
    if ht in (HTCAPTION, HTSYSMENU) and int(root) in TARGETS:
        return int(root)
    return 0


def _ll_mouse_cb(ncode, wparam, lparam):
    try:
        if ncode == 0 and wparam == WM_RBUTTONUP:
            ms = ctypes.cast(ctypes.c_void_p(lparam),
                             ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            t = _menu_target_at(ms.pt.x, ms.pt.y)
            if t:
                seen.append(('mouse', t))
                u.PostMessageW(ctypes.c_void_p(HOST), WM_APP_SYSMENU, t, 0)
                return 1
    except Exception as e:
        result.write('mouse cb err %r\n' % e)
        result.flush()
    return u.CallNextHookEx(None, ncode, wparam, lparam)


def _ll_key_cb(ncode, wparam, lparam):
    try:
        if ncode == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(ctypes.c_void_p(lparam),
                             ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if kb.vk == 0x20 and (kb.flags & LLKHF_ALTDOWN):
                fg = u.GetForegroundWindow() or 0
                if fg and int(fg) in TARGETS:
                    seen.append(('key', int(fg)))
                    u.PostMessageW(ctypes.c_void_p(HOST),
                                   WM_APP_SYSMENU, int(fg), 0)
                    return 1
    except Exception as e:
        result.write('key cb err %r\n' % e)
        result.flush()
    return u.CallNextHookEx(None, ncode, wparam, lparam)


def _host_wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_APP_SYSMENU:
        result.write('MENU target=%d %s\n' % (wparam, time.strftime('%H:%M:%S')))
        result.flush()
        return 0
    return u.DefWindowProcW(ctypes.c_void_p(hwnd), ctypes.c_uint(msg),
                            ctypes.c_size_t(wparam), ctypes.c_ssize_t(lparam))


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)
MOUSEPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               ctypes.c_size_t, ctypes.c_ssize_t)

u.RegisterClassW.argtypes = [ctypes.c_void_p]
u.RegisterClassW.restype = ctypes.c_uint16
u.CreateWindowExW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p,
                              ctypes.c_wchar_p, ctypes.c_uint32,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p]
u.CreateWindowExW.restype = ctypes.c_void_p
u.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t]
u.DefWindowProcW.restype = ctypes.c_ssize_t
u.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_uint32]
u.SetWindowsHookExW.restype = ctypes.c_void_p
u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                             ctypes.c_size_t, ctypes.c_ssize_t]
u.CallNextHookEx.restype = ctypes.c_ssize_t
u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                           ctypes.c_size_t, ctypes.c_ssize_t]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                ('hInstance', ctypes.c_void_p), ('hIcon', ctypes.c_void_p),
                ('hCursor', ctypes.c_void_p),
                ('hbrBackground', ctypes.c_void_p),
                ('lpszMenuName', ctypes.c_wchar_p),
                ('lpszClassName', ctypes.c_wchar_p)]


HOST = None
wndproc_ref = WNDPROC(_host_wndproc)
mref = MOUSEPROC(_ll_mouse_cb)
kref = MOUSEPROC(_ll_key_cb)


def setup():
    global HOST
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc_ref
    wc.lpszClassName = 'ExtSysMenuTest'
    wc.hInstance = k32.GetModuleHandleW(None)
    assert u.RegisterClassW(ctypes.byref(wc)), 'RegisterClass'
    HOST = u.CreateWindowExW(0, wc.lpszClassName, 'ExtSysMenuTest',
                             0, 0, 0, 0, 0, None, None, wc.hInstance, None)
    assert HOST, 'CreateWindow'
    mh = u.SetWindowsHookExW(WH_MOUSE_LL, mref, None, 0)
    kh = u.SetWindowsHookExW(WH_KEYBOARD_LL, kref, None, 0)
    result.write('hooks mouse=%s keyboard=%s\n' % (mh, kh))
    result.flush()


def trigger_thread():
    time.sleep(1.2)
    subprocess.Popen(['notepad.exe'])
    hwnd = 0
    for _ in range(20):
        time.sleep(0.3)
        hwnd = u.FindWindowW('Notepad', None)
        if hwnd:
            break
    result.write('notepad=%s\n' % hwnd)
    result.flush()
    TARGETS.add(int(hwnd))
    u.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    import ctypes.wintypes as wt2
    r = wt2.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    result.write('rect %d,%d - %d,%d\n' % (r.left, r.top, r.right, r.bottom))
    result.flush()
    # 挪到固定空位，避免被其他悬浮窗盖住标题栏
    u.SetWindowPos(hwnd, 0, 100, 100, 800, 600, 0x0004)   # SWP_NOZORDER
    time.sleep(0.4)
    u.GetWindowRect(hwnd, ctypes.byref(r))
    result.write('moved rect %d,%d - %d,%d\n' % (r.left, r.top, r.right, r.bottom))
    result.flush()
    # 移动后、点击前：多点 hittest 探测（隔离「移动/点击」变量）
    u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_size_t, ctypes.c_ssize_t]
    u.SendMessageW.restype = ctypes.c_ssize_t
    for (px, py) in [(r.left + 150, r.top + 12),
                     ((r.left + r.right) // 2, (r.top + r.bottom) // 2),
                     (r.left + 400, r.top + 300)]:
        lp = ((py & 0xFFFF) << 16) | (px & 0xFFFF)
        ht = u.SendMessageW(hwnd, WM_NCHITTEST, 0, lp) & 0xFFFFFFFF
        result.write('pre-hit (%d,%d) -> %d\n' % (px, py, ht))
    result.flush()

    save = wt2.POINT()
    u.GetCursorPos(ctypes.byref(save))

    # 1) 右键标题栏（避开按钮区，取偏左位置）→ 应命中
    u.SetCursorPos(r.left + 150, r.top + 12)
    time.sleep(0.3)
    u.mouse_event(2, 0, 0, 0, 0)
    u.mouse_event(16, 0, 0, 0, 0)
    time.sleep(1.0)
    n1 = len(seen)

    # 清状态：补一次普通左键点击（被吞掉的右键 DOWN/UP 不配对会让窗口
    # 挂着鼠标捕获，影响下一次 hittest——真实场景由模态菜单天然隔开）
    u.SetCursorPos(r.left + 400, r.top + 300)
    time.sleep(0.3)
    u.mouse_event(4, 0, 0, 0, 0)    # LEFTDOWN
    u.mouse_event(8, 0, 0, 0, 0)    # LEFTUP
    time.sleep(0.6)

    # 2) 右键客户区 → 不应命中
    u.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    time.sleep(0.3)
    u.mouse_event(2, 0, 0, 0, 0)
    u.mouse_event(16, 0, 0, 0, 0)
    time.sleep(1.0)
    n2 = len(seen)

    # 3) Alt+Space → 应命中
    u.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    u.keybd_event(0x12, 0, 0, 0)
    u.keybd_event(0x20, 0, 0, 0)
    u.keybd_event(0x20, 0, 2, 0)
    u.keybd_event(0x12, 0, 2, 0)
    time.sleep(1.0)
    n3 = len(seen)

    u.SetCursorPos(save.x, save.y)
    u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE 清场
    u.PostMessageW(ctypes.c_void_p(HOST), 0x0012, 0, 0)  # WM_QUIT

    result.write('SUMMARY n_titlebar=%d n_client=%d n_altsp=%d\n'
                 % (n1, n2 - n1, n3 - n2))
    result.write('seen=%r\n' % (seen,))
    result.flush()


setup()
threading.Thread(target=trigger_thread, daemon=True).start()


class MSG(ctypes.Structure):
    _fields_ = [('hwnd', ctypes.c_void_p), ('message', ctypes.c_uint),
                ('wParam', ctypes.c_size_t), ('lParam', ctypes.c_ssize_t),
                ('time', ctypes.c_uint32),
                ('ptX', ctypes.c_long), ('ptY', ctypes.c_long)]


m = MSG()
while u.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
    u.TranslateMessage(ctypes.byref(m))
    u.DispatchMessageW(ctypes.byref(m))
result.write('pump exit\n')
result.close()
