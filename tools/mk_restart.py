# -*- coding: utf-8 -*-
"""生成 CRLF 重启批处理：结束旧托盘 → 用新构建覆盖 release → 重新拉起。"""
import os

BASE = r'C:\VOLCANO\myws\extScreen'
name = 'ExtScreen \u526f\u5c4f\u52a9\u624b.exe'
target = os.path.join(BASE, 'release', '_restart.cmd')
lines = [
    '@echo off',
    'ping -n 3 127.0.0.1 >nul',
    'taskkill /f /im "%s" >nul 2>&1' % name,
    'ping -n 4 127.0.0.1 >nul',
    'copy /y "%s" "%s" >nul' % (
        os.path.join(BASE, 'build', 'dist', 'ExtScreenDock.exe'),
        os.path.join(BASE, 'release', name)),
    'start "" "%s"' % os.path.join(BASE, 'release', name),
]
with open(target, 'w', encoding='gbk', newline='') as f:
    f.write('\r\n'.join(lines) + '\r\n')
print(target)
