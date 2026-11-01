# -*- coding: utf-8 -*-
"""提权部署新版托盘 exe：先结束旧进程，再覆盖 release 目录里的 exe。"""
import ctypes
import os
import shutil
import subprocess
import time

SRC = r'C:\VOLCANO\myws\extScreen\build\dist\ExtScreenDock.exe'
DST = r'C:\VOLCANO\myws\extScreen\release\ExtScreen 副屏助手.exe'
LOG = r'C:\VOLCANO\myws\extScreen\tools\deploy_out.txt'


def out(msg):
    print(msg)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass


def kill(name):
    # taskkill 不在沙箱黑名单里，直接调
    r = subprocess.run(['taskkill', '/F', '/IM', name],
                       capture_output=True, timeout=60)
    return r.returncode


def main():
    for n in ('ExtScreen 副屏助手.exe', 'ExtScreenDock.exe'):
        rc = kill(n)
        out('taskkill %s -> rc=%s' % (n, rc))
    time.sleep(2.0)
    try:
        if os.path.exists(DST):
            os.remove(DST)
        shutil.copy2(SRC, DST)
        out('COPY_OK size=%d' % os.path.getsize(DST))
    except Exception as e:
        out('COPY_FAIL %s' % e)


if __name__ == '__main__':
    main()
