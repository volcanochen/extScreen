# -*- coding: utf-8 -*-
"""多路投屏 e2e：两个窗口同时投两路，验证端口顺延 + 两路同时可访问。
不碰手机、不发触摸。结束全部停掉。"""
import ctypes
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, 'src')
from extscreen_cast import MANAGER, CastMonitor  # noqa: E402

ok = []
fail = []


def check(name, cond):
    (ok if cond else fail).append(name)
    print(('PASS ' if cond else 'FAIL ') + name)


u = ctypes.windll.user32


def find_notepads(n):
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _lp):
        if u.IsWindowVisible(h):
            cls = ctypes.create_unicode_buffer(64)
            u.GetClassNameW(h, cls, 64)
            if cls.value == 'Notepad':
                found.append(int(h))
        return len(found) < n
    u.EnumWindows(cb, None)
    return found[:n]


subprocess.Popen(['notepad.exe'])
time.sleep(1.2)
subprocess.Popen(['notepad.exe'])
time.sleep(1.2)
wins = find_notepads(2)
check('找到两个记事本窗口', len(wins) == 2)
if len(wins) != 2:
    raise SystemExit(1)
h1, h2 = wins

m1 = MANAGER.acquire(h1, 'n1')
p1 = MANAGER.next_port()
r1 = m1.start(h1, 'n1', port=p1)
check('第 1 路启动 %s' % (r1,), r1[0])
time.sleep(0.6)
m2 = MANAGER.acquire(h2, 'n2')
p2 = MANAGER.next_port()
r2 = m2.start(h2, 'n2', port=p2)
check('第 2 路启动 %s' % (r2,), r2[0])
check('两路端口不同 (第1路=%d, 第2路=%d)' % (p1, p2),
      p1 != p2 and p2 > p1)   # 被占端口会跳过，不要求严格 +1
check('两路同时在投', len(MANAGER.running()) == 2)
check('by_hwnd 各自命中', MANAGER.by_hwnd(h1) is m1
      and MANAGER.by_hwnd(h2) is m2)

for m, p in ((m1, p1), (m2, p2)):
    try:
        resp = urllib.request.urlopen(
            'http://127.0.0.1:%d/info' % p, timeout=5)
        body = resp.read().decode('utf-8', 'replace')
        check('端口 %d /info 可访问' % p, resp.status == 200)
    except Exception as e:
        check('端口 %d /info 可访问 (%r)' % (p, e), False)

# 会话互不干扰：停第 1 路，第 2 路还活着
m1.stop()
time.sleep(0.6)
check('停第 1 路后第 2 路还在投', len(MANAGER.running()) == 1
      and MANAGER.by_hwnd(h2) is m2)
try:
    resp = urllib.request.urlopen('http://127.0.0.1:%d/info' % p2, timeout=5)
    check('第 2 路停止后仍可访问', resp.status == 200)
except Exception as e:
    check('第 2 路停止后仍可访问 (%r)' % e, False)
m2.stop()
time.sleep(0.5)
check('全部停止', len(MANAGER.running()) == 0)

for h in (h1, h2):
    u.PostMessageW(h, 0x0010, 0, 0)   # WM_CLOSE 清场

print('RESULT: %d pass, %d fail' % (len(ok), len(fail)))
