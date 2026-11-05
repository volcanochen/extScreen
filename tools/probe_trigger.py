# -*- coding: utf-8 -*-
"""触发器：开一个记事本，依次触发
1) 右键标题栏系统菜单  2) Alt+Space 系统菜单  3) 右键客户区(普通上下文菜单)
每种之间停 1.5 秒，全部 ESC 关掉。配合 probe_menuevent.py 观察事件。
"""
import ctypes
import ctypes.wintypes as wt
import subprocess
import time

u = ctypes.windll.user32
k = ctypes.windll.kernel32

log = open(r'C:\VOLCANO\myws\extScreen\tools\probe_trigger_out.txt',
           'w', encoding='utf-8')


def say(s):
    log.write('%s %s\n' % (time.strftime('%H:%M:%S'), s))
    log.flush()


# 还原鼠标位置
pt = wt.POINT()
u.GetCursorPos(ctypes.byref(pt))

subprocess.Popen(['notepad.exe'])
time.sleep(1.5)
hwnd = u.FindWindowW('Notepad', None)
say('notepad hwnd=%s' % hwnd)
if not hwnd:
    log.close()
    raise SystemExit(1)

u.SetForegroundWindow(hwnd)
time.sleep(0.5)

r = wt.RECT()
u.GetWindowRect(ctypes.wintypes.HWND(hwnd), ctypes.byref(r)) \
    if False else u.GetWindowRect(hwnd, ctypes.byref(r))
say('rect %s,%s - %s,%s' % (r.left, r.top, r.right, r.bottom))

# --- 1) 右键标题栏 ---
u.SetCursorPos((r.left + r.right) // 2, r.top + 12)
time.sleep(0.4)
say('right-click title bar')
u.mouse_event(2, 0, 0, 0, 0)     # RIGHTDOWN
u.mouse_event(16, 0, 0, 0, 0)    # RIGHTUP
time.sleep(1.5)
u.keybd_event(0x1B, 0, 0, 0)     # ESC
u.keybd_event(0x1B, 0, 2, 0)
time.sleep(0.8)

# --- 2) Alt+Space ---
say('alt+space')
u.keybd_event(0x12, 0, 0, 0)     # ALT down
u.keybd_event(0x20, 0, 0, 0)     # SPACE down
u.keybd_event(0x20, 0, 2, 0)
u.keybd_event(0x12, 0, 2, 0)
time.sleep(1.5)
u.keybd_event(0x1B, 0, 0, 0)
u.keybd_event(0x1B, 0, 2, 0)
time.sleep(0.8)

# --- 3) 右键客户区 ---
u.SetCursorPos((r.left + r.right) // 2, (r.top + r.bottom) // 2)
time.sleep(0.4)
say('right-click client area')
u.mouse_event(2, 0, 0, 0, 0)
u.mouse_event(16, 0, 0, 0, 0)
time.sleep(1.5)
u.keybd_event(0x1B, 0, 0, 0)
u.keybd_event(0x1B, 0, 2, 0)
time.sleep(0.5)

u.SetCursorPos(pt.x, pt.y)
# 关掉记事本，不留垃圾
u.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE
time.sleep(0.5)
say('done')
log.close()
