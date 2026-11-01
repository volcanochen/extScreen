# -*- mode: python ; coding: utf-8 -*-
"""
ExtScreen 副屏助手 —— PyInstaller 打包配置

用法（必须在仓库根目录执行，spec 用相对路径定位源文件）：
    python -m PyInstaller --noconfirm ExtScreenDock.spec

产物：dist/ExtScreenDock.exe
      再用 tools/deploy.py 部署到 release/ExtScreen 副屏助手.exe
"""

import os

# spec 文件所在目录即仓库根（PyInstaller 会把 cwd 切到 spec 目录）
ROOT = os.path.abspath(os.getcwd())

a = Analysis(
    [os.path.join('src', 'extscreen_dock.py')],
    pathex=[os.path.join(ROOT, 'src')],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ExtScreenDock',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 托盘需要管理员权限：启停 Sunshine 服务、开关虚拟显示器都要
    uac_admin=True,
    icon=[os.path.join(ROOT, 'app.ico')],
)
