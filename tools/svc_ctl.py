# -*- coding: utf-8 -*-
"""提权启动/停止 SunshineService，并等待它真正就绪。

只做一件事：用 SCM API 起服务，然后轮询本机 sunshine.log，
等 Sunshine 打出「初始化完成」标志才算数。

用法：
    python svc_ctl.py start
    python svc_ctl.py stop
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

SVC = 'SunshineService'
LOG_DIR = r'C:\Program Files\Sunshine\config'
LOG = os.path.join(LOG_DIR, 'sunshine.log')

adv = ctypes.WinDLL('advapi32', use_last_error=True)
adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
adv.OpenSCManagerW.restype = wintypes.HANDLE
adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
adv.OpenServiceW.restype = wintypes.HANDLE
adv.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
adv.StartServiceW.restype = wintypes.BOOL
adv.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
adv.ControlService.restype = wintypes.BOOL
adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ('dwServiceType', wintypes.DWORD),
        ('dwCurrentState', wintypes.DWORD),
        ('dwControlsAccepted', wintypes.DWORD),
        ('dwWin32ExitCode', wintypes.DWORD),
        ('dwServiceSpecificExitCode', wintypes.DWORD),
        ('dwCheckPoint', wintypes.DWORD),
        ('dwWaitHint', wintypes.DWORD),
    ]


SERVICE_RUNNING = 4
SERVICE_STOPPED = 1
SERVICE_STOP_PENDING = 3


def state():
    h = adv.OpenSCManagerW(None, None, 0x0001)
    if not h:
        return None
    try:
        hs = adv.OpenServiceW(h, SVC, 0x0004)
        if not hs:
            return None
        try:
            st = SERVICE_STATUS()
            if not adv.QueryServiceStatus(hs, ctypes.byref(st), ctypes.sizeof(st)):
                return None
            return int(st.dwCurrentState)
        finally:
            adv.CloseServiceHandle(hs)
    finally:
        adv.CloseServiceHandle(h)


def start():
    h = adv.OpenSCManagerW(None, None, 0x0001)
    hs = adv.OpenServiceW(h, SVC, 0x0010)
    if not hs:
        print('OpenService failed', ctypes.get_last_error())
        return
    r = adv.StartServiceW(hs, 0, None)
    print('StartServiceW ->', bool(r), 'err=', ctypes.get_last_error())
    adv.CloseServiceHandle(hs)
    adv.CloseServiceHandle(h)


def stop():
    h = adv.OpenSCManagerW(None, None, 0x0001)
    hs = adv.OpenServiceW(h, SVC, 0x0020)
    if not hs:
        print('OpenService failed')
        return
    st = SERVICE_STATUS()
    adv.ControlService(hs, 1, ctypes.byref(st))  # SERVICE_CONTROL_STOP
    adv.CloseServiceHandle(hs)
    adv.CloseServiceHandle(h)
    print('stop requested')


def log_tail():
    if not os.path.exists(LOG):
        return ''
    with open(LOG, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 512 * 1024))
        return f.read().decode('utf-8', 'ignore')


def wait_ready(timeout=150):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st = state()
        tail = log_tail()
        r = tail.rfind('Registered Sunshine mDNS service')
        u = tail.rfind('Unregistered Sunshine mDNS service')
        ready = (r >= 0 and r > u)
        cur = (st, ready)
        if cur != last:
            print('%6.1fs  svc=%-5s ready=%-5s' % (time.time() - t0, st, ready))
            last = cur
        if ready:
            print('%6.1fs  >>> Sunshine 真正就绪' % (time.time() - t0))
            return True
        time.sleep(1.0)
    print('timeout')
    return False


if __name__ == '__main__':
    # 提权启动时没法重定向 stdout，输出写文件
    OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'svc_out.txt')
    _real_print = print

    def print(*a, **kw):  # noqa: A001
        _real_print(*a, **kw)
        try:
            with open(OUT, 'a', encoding='utf-8') as f:
                f.write(' '.join(str(x) for x in a) + '\n')
        except Exception:
            pass

    cmd = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if cmd == 'start':
        start()
        wait_ready()
    elif cmd == 'stop':
        stop()
    elif cmd == 'state':
        print('svc =', state())
        wait_ready(5)
