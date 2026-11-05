# -*- coding: utf-8 -*-
"""窗口枚举诊断：列出所有可见顶层窗口及每条的过滤判定。
只读，不注入任何输入。输出 tools/winlist_diag_out.txt
"""
import ctypes
import ctypes.wintypes as wt

OUT = r'C:\VOLCANO\myws\extScreen\tools\winlist_diag_out.txt'
log = open(OUT, 'w', encoding='utf-8')
u = ctypes.windll.user32
k32 = ctypes.windll.kernel32
d = ctypes.windll.dwmapi

DWMWA_CLOAKED = 14


def is_cloaked(h):
    v = ctypes.c_uint32(0)
    d.DwmGetWindowAttribute(ctypes.c_void_p(h), DWMWA_CLOAKED,
                            ctypes.byref(v), 4)
    return v.value


def exe_of(h):
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    hp = k32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED
    name = '?'
    if hp:
        buf = ctypes.create_unicode_buffer(512)
        sz = wt.DWORD(512)
        if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz)):
            name = buf.value.replace('\\', '/').split('/')[-1]
        k32.CloseHandle(hp)
    return name, pid.value


rows = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
def on_window(h, _lp):
    if not u.IsWindowVisible(h):
        return True
    cls = ctypes.create_unicode_buffer(128)
    u.GetClassNameW(h, cls, 128)
    n = u.GetWindowTextLengthW(h)
    title = ''
    if n > 0:
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        title = b.value.strip()
    ex = u.GetWindowLongW(h, -20) & 0xFFFFFFFF
    cloaked = is_cloaked(h)
    buf2 = (ctypes.c_uint32 * 16)()
    buf2[0] = 60
    u.GetWindowPlacement(h, buf2)
    show = buf2[2]
    reasons = []
    if cloaked:
        reasons.append('cloaked=%d' % cloaked)
    if ex & 0x80:
        reasons.append('TOOLWINDOW')
    if n <= 0 or not title:
        reasons.append('no-title')
    if show in (2, 3, 7):
        reasons.append('minimized(show=%d)' % show)
    exe, pid = exe_of(h)
    rows.append((h, cls.value, title[:46], exe, pid, ex, show,
                 ';'.join(reasons) or 'LISTED'))
    return True


u.EnumWindows(on_window, None)

log.write('%-10s %-22s %-46s %-18s %s\n'
          % ('hwnd', 'class', 'title', 'exe', 'filter'))
for h, cls, title, exe, pid, ex, show, flt in rows:
    log.write('%-10d %-22s %-46s %-18s %s\n' % (h, cls, title, exe, flt))
listed = [r for r in rows if r[7] == 'LISTED']
log.write('\nTOTAL visible=%d  LISTED=%d  FILTERED=%d\n'
          % (len(rows), len(listed), len(rows) - len(listed)))
log.write('\n== FILTERED 明细（class/exstyle/cloak/min 只列关键位）==\n')
for h, cls, title, exe, pid, ex, show, flt in rows:
    if flt != 'LISTED':
        log.write('  %-22s %-18s ex=0x%08X show=%d  %s  | %s\n'
                  % (cls, exe, ex, show, flt, title))
log.close()
print('done, rows=%d listed=%d' % (len(rows), len(listed)))
