# -*- coding: utf-8 -*-
"""探针：监听全局菜单相关 WinEvent，写入 tools/probe_menuevent_out.txt。
只观察，不干扰。用于搞清「标题栏右键 / Alt+Space 打开系统菜单」到底上报什么事件。
"""
import ctypes
import ctypes.wintypes as wt
import time

OUT = r'C:\VOLCANO\myws\extScreen\tools\probe_menuevent_out.txt'
log = open(OUT, 'w', encoding='utf-8')

EVENTS = {2: 'FOREGROUND', 3: 'DESTROYED', 4: 'MENUSTART', 5: 'MENUEND',
          6: 'POPUPSTART', 7: 'POPUPEND'}

u = ctypes.windll.user32


def cb(hook, event, hwnd, idObject, idChild, tid, tm):
    try:
        if event not in EVENTS:
            return
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        log.write('%s %s hwnd=%s idObject=%s idChild=%s cls=%s pid=%s\n' % (
            time.strftime('%H:%M:%S'), EVENTS[event], hwnd,
            idObject, idChild, cls.value, pid.value))
        log.flush()
    except Exception as e:
        log.write('cb err %r\n' % e)
        log.flush()


WINPROC = ctypes.WINFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_long, ctypes.c_long, ctypes.c_uint32, ctypes.c_uint32)
f = WINPROC(cb)

u.SetWinEventHook.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
u.SetWinEventHook.restype = ctypes.c_void_p
h = u.SetWinEventHook(2, 7, None, f, 0, 0, 0)
log.write('hook=%s %s\n' % (h, time.strftime('%H:%M:%S')))
log.flush()

class MSG(ctypes.Structure):
    _fields_ = [('hwnd', ctypes.c_void_p), ('message', ctypes.c_uint),
                ('wParam', ctypes.c_size_t), ('lParam', ctypes.c_ssize_t),
                ('time', ctypes.c_uint32),
                ('ptX', ctypes.c_long), ('ptY', ctypes.c_long)]

m = MSG()
while u.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
    pass
log.write('pump exit\n')
log.close()
