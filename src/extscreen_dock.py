# -*- coding: utf-8 -*-
"""ExtScreen 副屏助手 —— 托盘主程序。"""
import os
import re
import sys
import json
import time
import base64
import threading
import subprocess
import ctypes

import pystray
from PIL import Image, ImageDraw

import extscreen_display as disp
import extscreen_cast
from extscreen_core import (
    APP_NAME, APP_DIR, MEIPASS,
    TOOLS_DIR, VDISPLAY_PS1, EXT_CONFIG,
    SCRCPY_DIR, SCRCPY_EXE, ADB_EXE, ADB_FALLBACK,
    SUNSHINE_SVC, SUNSHINE_DIR, VDD_DEVICE, VDD_SETTINGS,
    LOG_PATH, COLOR_OFF, COLOR_READY, COLOR_LIVE, COLOR_ERR,
    SERVICE_RUNNING, SERVICE_STOPPED, SERVICE_UNKNOWN, ERROR_ACCESS_DENIED,
    log, run, powershell,
    service_state, list_monitors, vdd_attached, monitor_count,
    adb_devices, sunshine_installed, vdd_installed, scrcpy_running,
    sunshine_exe, sunshine_config_dir, sunshine_ready,
    sunshine_client_connected, start_service_scm, stop_service_scm,
    RESOLUTION_PRESETS, load_ext_config, save_ext_config,
)
from settings_gui import show_settings

PAYLOAD = os.path.join(APP_DIR, 'payload')


# --------------------------------------------------------------------------
# 动作
# --------------------------------------------------------------------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def start_service(wait_ready=True, timeout=60):
    """启动 Sunshine 服务。

    wait_ready=True 时，不只看 SCM 报 RUNNING，而是等到 Sunshine 真正
    完成初始化（mDNS 注册）才算成功。服务状态变成 RUNNING 之后 Sunshine
    还有 25~30 秒的空窗期，这期间手机连过去必然失败，早点返回会让用户
    以为「启动好了」其实没好。
    """
    if service_state(SUNSHINE_SVC) == SERVICE_RUNNING:
        if not wait_ready:
            return True, 'Sunshine 已在运行'
        ok, why = sunshine_ready()
        if ok:
            return True, 'Sunshine 已在运行且已就绪'
        # 服务在跑但没就绪，继续等它初始化完
        if _wait_ready(timeout):
            return True, 'Sunshine 已就绪'
        return False, 'Sunshine 运行中但未就绪：%s' % why

    ok, err = start_service_scm(SUNSHINE_SVC)
    if not ok and err == ERROR_ACCESS_DENIED:
        # 没有提权权限时退回 sc.exe（沙箱/受限环境里 sc.exe 还可能被拦）
        run(['sc', 'start', SUNSHINE_SVC], timeout=60)
    if service_state(SUNSHINE_SVC) != SERVICE_RUNNING:
        # sc 不可用等情况下退回用日志判据
        if not wait_ready:
            return False, '启动 Sunshine 失败（错误码 %s）' % err
    if not wait_ready:
        for _ in range(24):
            if service_state(SUNSHINE_SVC) == SERVICE_RUNNING:
                return True, 'Sunshine 已启动'
            time.sleep(0.5)
        return False, '启动 Sunshine 失败（错误码 %s）' % err

    ok = _wait_ready(timeout)
    if ok:
        return True, 'Sunshine 已启动并就绪'
    return False, 'Sunshine 启动超时（未在 %d 秒内完成初始化）' % timeout


def _wait_ready(timeout=60):
    """轮询等待 Sunshine 真正就绪，返回是否成功。"""
    deadline = time.time() + timeout
    last = ''
    while time.time() < deadline:
        ok, why = sunshine_ready()
        if ok:
            return True
        last = why
        if service_state(SUNSHINE_SVC) is None:
            return False
        time.sleep(1.0)
    log('wait_ready timeout: %s' % last)
    return False


def stop_service():
    if service_state(SUNSHINE_SVC) is None:
        return True, '未安装服务'
    if service_state(SUNSHINE_SVC) == SERVICE_STOPPED:
        return True, 'Sunshine 已停止'
    ok, err = stop_service_scm(SUNSHINE_SVC)
    if not ok and err == ERROR_ACCESS_DENIED:
        # 没有提权权限时退回 sc.exe
        run(['sc', 'stop', SUNSHINE_SVC], timeout=60)
    # 有客户端连着时 Sunshine 收尾（存配置/关 NVENC/通知客户端）能拖 20s+，
    # 12 秒就报超时纯属虚惊（2026-11-05 用户截图）。给足 45 秒，
    # 实在不停再强杀进程兜底——服务 SCM 句柄还挂着，强杀后状态自然会到 STOPPED。
    for i in range(90):
        if service_state(SUNSHINE_SVC) == SERVICE_STOPPED:
            return True, 'Sunshine 已停止'
        if i == 60:   # 30 秒还没停 → 强杀进程兜底
            log('sunshine stop: still running after 30s, taskkill fallback')
            run(['taskkill', '/F', '/IM', 'sunshine.exe', '/T'], timeout=30)
        time.sleep(0.5)
    if service_state(SUNSHINE_SVC) == SERVICE_STOPPED:
        return True, 'Sunshine 已停止'
    return False, '停止 Sunshine 超时（已尝试强制结束进程）'


# --------------------------------------------------------------------------
# Sunshine 安装 / 启动（托盘里的安装入口）
# --------------------------------------------------------------------------
SUNSHINE_RELEASE_URL = 'https://github.com/LizardByte/Sunshine/releases/latest'
SUNSHINE_API_URL = 'https://api.github.com/repos/LizardByte/Sunshine/releases/latest'


def _notify(msg):
    try:
        if ICON is not None:
            ICON.notify(str(msg), APP_NAME)
    except Exception:
        pass


def find_sunshine_installer():
    """在几个常见位置找离线安装包（打包时放在 payload 里）。"""
    cands = [
        os.path.join(PAYLOAD, 'sunshine-installer.msi'),
        os.path.join(APP_DIR, 'payload', 'sunshine-installer.msi'),
        os.path.join(os.path.dirname(APP_DIR), 'release', 'payload',
                     'sunshine-installer.msi'),
        os.path.join(os.path.dirname(APP_DIR), 'payload', 'sunshine-installer.msi'),
        os.path.join(TOOLS_DIR, 'sunshine-installer.msi'),
    ]
    for p in cands:
        p = os.path.abspath(p)
        if os.path.isfile(p) and os.path.getsize(p) > 5 * 1024 * 1024:
            return p
    return ''


def download_sunshine_installer(dest_dir=None):
    """从 GitHub 拉最新版 MSI。返回 (路径, 说明)；失败时路径为空串。"""
    import urllib.request
    dest_dir = dest_dir or PAYLOAD
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        return '', '无法创建下载目录：%s' % e

    try:
        req = urllib.request.Request(SUNSHINE_API_URL, headers={
            'User-Agent': 'ExtScreenDock',
            'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=45) as r:
            info = json.loads(r.read().decode('utf-8', 'ignore'))
    except Exception as e:
        return '', '获取版本信息失败：%s' % e

    url, name = '', ''
    for a in info.get('assets') or []:
        n = a.get('name') or ''
        low = n.lower()
        if low.endswith('.msi') and 'installer' in low and 'arm64' not in low:
            url, name = a.get('browser_download_url') or '', n
            break
    if not url:
        return '', '该版本没有 Windows 安装包（MSI）'

    out = os.path.join(dest_dir, 'sunshine-installer.msi')
    tmp = out + '.part'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ExtScreenDock'})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, 'wb') as f:
            total = int(r.headers.get('Content-Length') or 0)
            got, mark = 0, 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and got - mark >= 15 * 1024 * 1024:
                    mark = got
                    _notify('正在下载 Sunshine %d%%' % (got * 100 // total))
        os.replace(tmp, out)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return '', '下载失败：%s' % e
    return out, '已下载 %s（%.1f MB）' % (name, os.path.getsize(out) / 1048576.0)


def open_sunshine_page():
    import webbrowser
    try:
        webbrowser.open(SUNSHINE_RELEASE_URL)
        return True, '已在浏览器打开 Sunshine 下载页'
    except Exception as e:
        return False, '打开下载页失败：%s' % e


def open_sunshine_config():
    d = sunshine_config_dir()
    if not os.path.isdir(d):
        return False, '配置目录不存在：%s' % d
    try:
        os.startfile(d)
        return True, '已打开 %s' % d
    except Exception as e:
        return False, str(e)


def wait_service_ready(seconds=30):
    """装完等 SCM 把服务登记好，避免立刻 start 时找不到服务。"""
    for _ in range(int(seconds)):
        st = service_state(SUNSHINE_SVC)
        if st in (SERVICE_RUNNING, SERVICE_STOPPED):
            return st
        time.sleep(1.0)
    return service_state(SUNSHINE_SVC)


def install_sunshine(force=False):
    """一键安装：优先本地 MSI，没有就下 GitHub 最新版，装完自动起服务。"""
    msgs = []
    if sunshine_installed() and not force:
        st = service_state(SUNSHINE_SVC)
        if st == SERVICE_RUNNING:
            return True, 'Sunshine 已安装且正在运行，无需重复安装'
        if st == SERVICE_STOPPED:
            ok, m = start_service(wait_ready=True, timeout=90)
            return ok, 'Sunshine 已安装，' + m
        if st == SERVICE_UNKNOWN:
            return False, ('检测到 Sunshine 已安装，但当前权限下查不到服务状态。\n'
                           '请以管理员身份重新运行本程序，或在服务管理器里手动启动 '
                           'SunshineService。')

    msi = find_sunshine_installer()
    if msi:
        msgs.append('使用本地安装包：%s' % msi)
    else:
        msgs.append('本机没有离线安装包，正在从 GitHub 下载最新版…')
        _notify('正在获取 Sunshine 最新版本…')
        msi, m = download_sunshine_installer()
        msgs.append('  ' + m)
        if not msi:
            open_sunshine_page()
            msgs.append('已自动打开下载页：%s' % SUNSHINE_RELEASE_URL)
            msgs.append('下载 *-installer.msi 后放到下面的目录，再点一次「安装」：')
            msgs.append('  ' + PAYLOAD)
            return False, '\n'.join(msgs)

    msgs.append('开始静默安装，约需 1~3 分钟，请不要关闭本程序…')
    rc, out = run(['msiexec', '/i', msi, '/qn', '/norestart'], timeout=900)
    msgs.append('msiexec 返回 %d%s' % (rc, '（需要重启系统）' if rc == 3010 else ''))
    if rc not in (0, 3010):
        tail = (out or '').strip()[-300:]
        if tail:
            msgs.append(tail)
        return False, '\n'.join(msgs)

    wait_service_ready(30)
    if not sunshine_installed():
        msgs.append('安装程序返回成功，但没有找到 Sunshine 安装目录')
        return False, '\n'.join(msgs)
    msgs.append('安装完成：%s' % (sunshine_exe() or sunshine_config_dir()))

    ok, m = start_service(wait_ready=True, timeout=90)
    msgs.append(m)
    return ok, '\n'.join(msgs)


def start_or_install_sunshine():
    """菜单主入口：没装就装，装了没启动就启动，启动中就等它就绪。"""
    if sunshine_installed():
        if service_state(SUNSHINE_SVC) == SERVICE_RUNNING:
            # 服务在跑：区分「已就绪」和「还在初始化」
            ok, why = sunshine_ready()
            if ok:
                return True, 'Sunshine 已在运行且已就绪'
            # 还在初始化，等它完成，而不是重复启动
            if _wait_ready(60):
                return True, 'Sunshine 已完成初始化，可以连接手机了'
            return False, 'Sunshine 仍在初始化：%s' % why
        return start_service()
    return install_sunshine()


def install_vdd_driver():
    """安装/修复虚拟显示器驱动。"""
    inf = _vdd_inf()
    if not os.path.exists(inf):
        return False, '找不到驱动文件：%s' % inf
    if vdd_installed():
        ok, _ = restart_vdd()
        return ok, ('驱动已注册，已重启虚拟显示器' if ok
                    else '驱动已注册，但虚拟屏没能挂出')
    rc, out = run(['pnputil', '/add-driver', inf, '/install'], timeout=300)
    if rc == 0 and not vdd_attached():
        recreate_vdd_device()
    ok = vdd_attached()
    return ok, ('虚拟显示器驱动安装完成' if ok
                else '驱动已提交安装但虚拟屏未挂出：%s' % (out or '').strip()[-200:])


def _force_attach_vdd():
    """设备在但显示器没进桌面拓扑时，用两步法显式挂出。

    驱动 reload 后 Windows 只创建 monitor 对象、不放进拓扑，
    pnputil disable/enable 重多少遍都白搭——这是「重启后仍未挂出」
    弹窗的真实原因。用 ChangeDisplaySettingsExW 两步法补上最后一击。
    """
    gdi, _ = disp.find_vdd_any()
    if not gdi:
        return False, '找不到虚拟显示适配器'
    cfg = load_ext_config()
    w, h, _src = target_resolution(cfg)
    pos = disp.calc_position(gdi, w, h,
                             cfg.get('position', 'right'),
                             cfg.get('align', 'start'))
    x, y = (pos[0], pos[1]) if pos else (None, None)
    ok, msg = disp.attach_monitor(gdi, w, h, 60, x=x, y=y)
    log(f'vdd force-attach {gdi}: {msg}')
    return ok, msg


def restart_vdd():
    """强制重启虚拟显示器设备节点，返回 (ok, msg)。
    这是关键修复：设备处于「已启用但没挂出显示器」的卡死状态时，
    原配置脚本会跳过重启直接干等 20 秒然后报错。

    注意返回的是 (ok, msg) 二元组：do_async 会按这个形状解包，
    早前这里只返回 bool，导致托盘弹「cannot unpack non-iterable bool object」。
    """
    log('force restart VDD device')
    run(['pnputil', '/disable-device', VDD_DEVICE], timeout=60)
    time.sleep(1.0)
    rc, out = run(['pnputil', '/enable-device', VDD_DEVICE], timeout=60)
    log(f'enable-device rc={rc} out={out.strip()[:200]}')
    for _ in range(24):
        if vdd_attached():
            return True, '虚拟屏已重新挂出'
        time.sleep(0.5)
    if vdd_attached():
        return True, '虚拟屏已重新挂出'
    # 设备重启了但 Windows 没把它放进桌面拓扑：显式挂出（实测有效）
    ok, msg = _force_attach_vdd()
    for _ in range(10):
        if vdd_attached():
            return True, '虚拟屏已重新挂出（拓扑挂出）'
        time.sleep(0.5)
    if vdd_attached():
        return True, '虚拟屏已重新挂出（拓扑挂出）'
    return False, '虚拟屏重启后仍未挂出：%s' % msg


NEFCON = r'C:\VirtualDisplayDriver\nefconw.exe'
VDD_CLASS_GUID = '4d36e968-e325-11ce-bfc1-08002be10318'


def recreate_vdd_device():
    """按驱动自带 install.bat 的做法彻底重建设备节点。

    disable/enable 只是让驱动对象重载，不会重新读取 vdd_settings.xml；
    删掉设备节点再重建，驱动才会按新配置重新枚举显示器。
    """
    log('recreate VDD device node via nefconw')
    if not os.path.exists(NEFCON):
        log(f'nefconw missing: {NEFCON}')
        return False
    steps = [
        ('remove', [NEFCON, '--remove-device-node',
                    '--hardware-id', r'Root\MttVDD',
                    '--class-guid', VDD_CLASS_GUID]),
        ('create', [NEFCON, '--create-device-node',
                    '--hardware-id', r'Root\MttVDD',
                    '--class-name', 'Display',
                    '--class-guid', VDD_CLASS_GUID]),
        ('install', [NEFCON, '--install-driver',
                     '--inf-path', r'C:\VirtualDisplayDriver\MttVDD.inf']),
    ]
    for title, cmd in steps:
        rc, out = run(cmd, timeout=180)
        log(f'nefcon {title} rc={rc} out={(out or "").strip()[:400]}')
        time.sleep(1.5)
    for _ in range(30):
        if vdd_attached():
            return True
        time.sleep(1.0)
    return vdd_attached()


def adb_path():
    """优先用 scrcpy 自带的 adb，没有就用官方 SDK 的。"""
    for p in (ADB_EXE, ADB_FALLBACK):
        if os.path.exists(p):
            return p
    return None


def phone_screen():
    """读手机屏幕逻辑尺寸，返回 (短边, 长边)；没接手机返回 None。"""
    adb = adb_path()
    if not adb:
        return None
    rc, out = run([adb, 'shell', 'wm', 'size'], timeout=15)
    if rc != 0:
        return None
    m = re.search(r'Override size:\s*(\d+)x(\d+)', out) \
        or re.search(r'Physical size:\s*(\d+)x(\d+)', out)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (min(a, b), max(a, b))


def moonlight_pc_uuid(refresh=False):
    """拿到 Moonlight 里这台 PC 的 UUID。

    优先用配置里缓存的值；否则从 Moonlight 的 logcat 里抓
    “New PC (xxx) is UUID ....”，抓到就写回配置，省得每次再抓。
    """
    cfg = load_ext_config()
    if not refresh and cfg.get('moonlightUuid'):
        return cfg['moonlightUuid']
    adb = adb_path()
    if not adb:
        return ''
    rc, out = run([adb, 'logcat', '-d'], timeout=25)
    if rc != 0:
        return ''
    found = re.findall(r'is UUID ([0-9A-Fa-f-]{36})', out)
    if not found:
        return ''
    uuid = found[-1].upper()
    cfg['moonlightUuid'] = uuid
    save_ext_config(cfg)
    log('moonlight pc uuid = %s' % uuid)
    return uuid


def _moonlight_start(adb, uuid, app_id, app_name):
    """真正调起 Moonlight 的 ShortcutTrampoline。返回 (ok, msg)。"""
    rc, out = run([adb, 'shell', 'am', 'start', '-n',
                   'com.limelight/.ShortcutTrampoline',
                   '-e', 'UUID', uuid,
                   '-e', 'AppId', app_id,
                   '-e', 'AppName', app_name], timeout=30)
    if rc != 0 or 'Error' in (out or ''):
        return False, '拉起串流失败: %s' % (out or '').strip()[:200]
    return True, ''


def launch_stream_on_phone():
    """直接拉起手机上的 Moonlight 串流，不用手动点。

    走 ShortcutTrampoline（Moonlight 给桌面快捷方式用的入口），
    绕开 AppView 里那种"点一下就被判成长按"的麻烦。

    还要处理一个坑：Sunshine 重启后 Moonlight 可能还留着上一轮的会话状态，
    这时 ShortcutTrampoline 会把 App 拉进 Game Activity 但画面全黑，
    看起来像"连上了"其实没连上。所以这里等一下看客户端是否真的连上，
    没连上就重启一次 Moonlight 再拉——实测这样能恢复。
    """
    adb = adb_path()
    if not adb:
        return False, '未找到 adb，无法自动拉起串流'
    cfg = load_ext_config()
    uuid = moonlight_pc_uuid()
    if not uuid:
        return False, '未拿到 Moonlight 的 PC UUID（先在手机上手点一次串流即可）'
    app_id = str(cfg.get('moonlightAppId') or '881448767')
    app_name = str(cfg.get('moonlightAppName') or 'Desktop')

    ok, msg = _moonlight_start(adb, uuid, app_id, app_name)
    if not ok:
        return False, msg

    for _ in range(10):
        time.sleep(1.0)
        if sunshine_client_connected():
            return True, '已在手机端拉起串流（%s）' % app_name

    # 没检测到连接：大概率是 Moonlight 那边会话状态陈旧，重启应用再拉一次
    log('launch_stream: no client yet, restarting Moonlight')
    run([adb, 'shell', 'am', 'force-stop', 'com.limelight'], timeout=30)
    time.sleep(2.0)
    ok, msg = _moonlight_start(adb, uuid, app_id, app_name)
    if not ok:
        return False, msg
    for _ in range(15):
        time.sleep(1.0)
        if sunshine_client_connected():
            return True, '已在手机端拉起串流（%s，重连后成功）' % app_name
    return True, '已拉起串流，但暂未检测到客户端连接，请查看手机画面'


# --------------------------------------------------------------------------
# 手机接收器（窗口投屏的 Android 端）
# --------------------------------------------------------------------------
VIEWER_PKG = 'cn.volcanochen.extscreenview'
VIEWER_ACT = VIEWER_PKG + '/.MainActivity'


def viewer_apk_path():
    """找接收器 APK：exe/src 旁边，或仓库的 build/ExtScreenViewer/。"""
    here = APP_DIR
    for c in (os.path.join(here, 'ExtScreenViewer.apk'),
              os.path.join(here, '..', 'build', 'ExtScreenViewer',
                           'ExtScreenViewer.apk')):
        p = os.path.normpath(c)
        if os.path.exists(p):
            return p
    return ''


def viewer_installed(adb, serial):
    """该手机上接收器是否已安装。"""
    rc, out = run([adb, '-s', serial, 'shell', 'pm', 'list', 'packages',
                   VIEWER_PKG], timeout=15)
    return ('package:%s' % VIEWER_PKG) in (out or '')


def install_viewer(adb, serial, name):
    """adb 安装/覆盖安装接收器 APK。返回 (ok, msg)。"""
    apk = viewer_apk_path()
    if not apk:
        return False, '找不到接收器 APK（ExtScreenViewer.apk）'
    log('viewer install on %s: %s' % (serial, apk))
    rc, out = run([adb, '-s', serial, 'install', '-r', '-t', apk],
                  timeout=180)
    if rc == 0 and 'Success' in (out or ''):
        return True, '%s 已自动安装接收器' % name
    log('viewer install %s failed: %s' % (serial, (out or '')[:300]))
    return False, '%s 安装接收器失败：%s' % (
        name, (out or '').strip()[:120])


PHONE_NAMES_FILE = os.path.join(APP_DIR, 'phone_names.json')
# 型号 → 商品名对照表（2026-11-05 联网核实过的条目；表外型号显示「未知手机」。
# 要补条目就编辑 exe 旁边的 phone_names.json，格式 {"SM-XXXXX": "商品名"}）
_PHONE_NAMES_SEED = {
    'SM-S9080': 'Galaxy S22 Ultra 5G',
    'SM-S9110': 'Galaxy S23',
    'SM-S9160': 'Galaxy S23+',
    'SM-S9180': 'Galaxy S23 Ultra',
    'SM-S9210': 'Galaxy S24',
    'SM-S9260': 'Galaxy S24 Ultra',
    'SM-S9310': 'Galaxy S25',
    'SM-S9380': 'Galaxy S25 Ultra',
    'SM-N9500': 'Galaxy Note8',
    'SM-N9600': 'Galaxy Note9',
    'SM-G9910': 'Galaxy S21 5G',
    'SM-G9960': 'Galaxy S21+ 5G',
}
_phone_names = None                    # 惰性加载的缓存 dict


def _norm_model(model):
    return (model or '').upper().replace('_', '-')


def _phone_names_load():
    """对照表加载顺序：exe 旁 phone_names.json（用户手工编辑优先）
    → 打包内置的 phone_names.json（Google Play 设备注册表，1.1 万+ 条）
    → 内置种子（离线兜底）。"""
    global _phone_names
    if _phone_names is not None:
        return _phone_names
    d = dict(_PHONE_NAMES_SEED)
    for base in (APP_DIR, MEIPASS):
        p = os.path.join(base, 'phone_names.json')
        try:
            with open(p, encoding='utf-8') as f:
                d.update(json.load(f))
        except Exception:
            continue
    _phone_names = d
    return d


def phone_specs(adb, serial):
    """一次 adb 往返拿手机规格与名称。

    返回 (display_name, info)：display_name 优先商品名
    （marketname，如 Galaxy S23 Ultra），没有则用型号；info 为
    'Android 16 · 720x1544 · 5.7″' 样式规格串。"""
    rc, out = run([adb, '-s', serial, 'shell',
                   'wm size; wm density; '
                   'echo rel=$(getprop ro.build.version.release); '
                   'echo mkt=$(getprop ro.product.marketname); '
                   'echo mdl=$(getprop ro.product.model)'], timeout=20)
    if rc != 0:
        return '', ''
    w = h = 0
    dpi = 0
    ver = market = model = ''
    for line in (out or '').splitlines():
        line = line.strip()
        if line.startswith('Override size:'):
            try:
                w, h = [int(x) for x in
                        line.split(':', 1)[1].strip().split('x')]
            except Exception:
                pass
        elif line.startswith('Physical size:'):
            try:
                w2, h2 = [int(x) for x in
                          line.split(':', 1)[1].strip().split('x')]
            except Exception:
                w2 = h2 = 0
            if not (w and h):
                w, h = w2, h2
        elif line.startswith('Physical density:'):
            try:
                dpi = int(line.split(':', 1)[1].strip())
            except Exception:
                pass
        elif line.startswith('rel='):
            ver = line[4:].strip()
        elif line.startswith('mkt='):
            market = line[4:].strip()
        elif line.startswith('mdl='):
            model = line[4:].strip()
    # 商品名：系统 marketname → 本地对照表（内置种子 + phone_names.json）
    # → 都没有就「未知手机」（不联网，见 2026-11-05 用户要求）
    names = _phone_names_load()
    key = _norm_model(market or model or serial)
    name = market or names.get(key) or '未知手机'
    if not (w and h):
        info = ('Android %s' % ver) if ver else ''
        return name, info
    inch = ''
    if dpi > 50:
        inch = ' · %.1f″' % ((w * w + h * h) ** 0.5 / dpi)
    info = 'Android %s · %dx%d%s' % (ver or '?', w, h, inch)
    return name, info


# 多台手机同时插 USB 时的选择框（项目约定：WinForms 弹窗走 PowerShell）。
# 手机列表用 base64 JSON 传入，避免引号地狱；点选后往 stdout 写 serial，
# 全部=__ALL__，取消=空行。
_PHONE_PS = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class PhonePickApi {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
'@
[PhonePickApi]::SetProcessDPIAware() | Out-Null
$js = [System.Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String('$PHONES_B64'))
$phones = $js | ConvertFrom-Json
$f = New-Object System.Windows.Forms.Form
$f.Text = 'ExtScreen 副屏助手'
$f.FormBorderStyle = 'FixedDialog'
$f.MaximizeBox = $false
$f.MinimizeBox = $false
$f.TopMost = $true
$f.StartPosition = 'CenterScreen'
$f.Size = New-Object System.Drawing.Size(380, (150 + 46 * $phones.Count))
$lbl = New-Object System.Windows.Forms.Label
$lbl.Text = '检测到多台手机，投屏到哪一台？'
$lbl.Location = New-Object System.Drawing.Point(18, 14)
$lbl.AutoSize = $true
$f.Controls.Add($lbl)
$y = 44
    foreach ($p in $phones) {
        $b = New-Object System.Windows.Forms.Button
        if ($p.info) {
            $b.Text = ('{0}  ·  {1}' -f $p.model, $p.info)
        } else {
            $b.Text = $p.model
        }
        $b.TextAlign = 'MiddleLeft'
        $b.Padding = New-Object System.Windows.Forms.Padding(6, 0, 0, 0)
        $b.Tag = $p.serial
        $b.Size = New-Object System.Drawing.Size(320, 38)
        $b.Location = New-Object System.Drawing.Point(22, $y)
        $b.Add_Click({
            [Console]::Out.WriteLine($this.Tag)
            $f.Close()
        })
        $f.Controls.Add($b)
        $y += 46
    }
$ball = New-Object System.Windows.Forms.Button
$ball.Text = '全部手机'
$ball.Tag = '__ALL__'
$ball.Size = New-Object System.Drawing.Size(320, 38)
$ball.Location = New-Object System.Drawing.Point(22, $y)
$ball.Add_Click({
    [Console]::Out.WriteLine('__ALL__')
    $f.Close()
})
$f.Controls.Add($ball)
[PhonePickApi]::SetForegroundWindow($f.Handle) | Out-Null
[System.Windows.Forms.Application]::Run($f)
'''


def _choose_phones(devs):
    """多台手机时弹选择框，返回要投的 serial 列表（空 = 取消）。"""
    adb = adb_path()
    phones = []
    for s, m in devs:
        name, info = phone_specs(adb, s) if adb else ('', '')
        phones.append({'serial': s, 'model': name or m, 'info': info})
    payload = base64.b64encode(json.dumps(
        phones, ensure_ascii=False).encode('utf-8')).decode('ascii')
    ps = _PHONE_PS.replace('$PHONES_B64', payload)
    try:
        r = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-Command', ps],
            capture_output=True, timeout=600, creationflags=0x08000000)
        lines = r.stdout.decode('utf-8', 'replace').strip().splitlines()
        sel = lines[0].strip() if lines else ''
    except Exception as e:
        log(f'phone pick: {e}')
        return []
    if sel == '__ALL__':
        return [s for s, _ in devs]
    if sel:
        return [sel]
    return []


def probe_usb_phone():
    """Dock 启动时查一下有没有 Android 设备插着 USB，结果记进 STATE。"""
    try:
        devs = adb_devices()
    except Exception as e:
        log(f'probe usb phone: {e}')
        devs = []
    STATE['usbPhone'] = bool(devs)
    log('usb phone: %s' % (', '.join('%s(%s)' % d for d in devs)
                           or '未连接'))
    return devs


def stop_viewer_for_serials(serials):
    """让指定手机退出接收器：force-stop 回到原界面，撤掉 USB 回环。

    跳过仍被其他在投会话使用的手机；拔掉的手机自然略过。
    """
    serials = [s for s in (serials or []) if s]
    if not serials:
        return
    adb = adb_path()
    if not adb:
        return
    try:
        devs = dict(probe_usb_phone())
    except Exception:
        devs = {}
    still = set(extscreen_cast.MANAGER.launched_serials())
    for s in serials:
        if s in still or s not in devs:
            continue
        run([adb, '-s', s, 'shell', 'am', 'force-stop', VIEWER_PKG],
            timeout=15)
        run([adb, '-s', s, 'reverse', '--remove-all'], timeout=15)
        log('viewer stopped on %s (force-stop + reverse removed)' % s)


def launch_viewer_on_phone(url=None, serials=None, mon=None):
    """用 adb 拉起手机端「副屏接收器」。返回 (ok, msg)。

    serials 非空时直接用（选窗口对话框里已经选过手机），为 None 时：
    单台直拉、多台弹选择框。'__ALL__' 已由调用方展开成列表。

    拉起前对每台手机建 `adb reverse tcp:PORT` 回环：debug_url 用
    http://127.0.0.1:PORT/，流量走 USB 线回 PC——手机没连 WiFi/没插卡
    也能投（实测 S9080 断网 ERR_INTERNET_DISCONNECTED 后回环拉通）。
    拔掉 USB 回环失效，接收器健康检查失败会自动退回 mDNS 发现。

    url 非空时通过 debug_url extra 直接把投屏地址推给手机——
    手机插着 USB 时这是秒级通道，不用等 mDNS 发现（20s/拍，切换
    投屏窗口后黑屏 1~2 分钟就是等它）。无线场景由 mDNS 兜底。

    手机锁屏/熄屏时先唤醒再拉起：接收器 Activity 挂了
    setShowWhenLocked+setTurnScreenOn，锁屏上也能直接显示画面
    （非安全锁直接可见；PIN 锁点亮屏幕显示在锁屏上层，解锁后可触摸控制）。
    """
    adb = adb_path()
    if not adb:
        return False, '未找到 adb'
    # 现查设备列表（不走 3s 缓存，插拔后立刻准确）
    devs = probe_usb_phone()
    if not devs:
        return False, '没有已连接的 USB 手机'
    if serials is None:
        if len(devs) == 1:
            serials = [devs[0][0]]
        else:
            serials = _choose_phones(devs)
            if not serials:
                return False, '已取消选择手机'
    # USB 回环端口：优先从 url 里取；取不到用本会话 mon 的端口，
    # 绝不能随手抓「第一路在投会话」——多会话时会把手机指到别人那路
    # （2026-11-05 实测：B 手机被指到 Y 会话的端口上）
    port = 0
    if url:
        m = re.search(r':(\d+)/', url)
        port = int(m.group(1)) if m else 0
    if not port and mon is not None:
        port = int(mon.snapshot().get('port') or 0)
    if not port:
        for m in extscreen_cast.MANAGER.running():
            port = int(m.snapshot().get('port') or 0)
            if port:
                break
    launched, notes, launched_now = 0, [], []
    prev = list(getattr(mon, 'launched_serials', []) or []) if mon else []
    for s, m in devs:
        if s not in serials:
            continue
        name = m or s
        if s in prev:
            # 已在这路会话上的手机：URL 没变就别打断它
            launched += 1
            continue
        # 没装接收器就自动装（覆盖安装不丢数据）
        if not viewer_installed(adb, s):
            ok, msg = install_viewer(adb, s, name)
            notes.append(msg)
            if not ok:
                continue
        # USB 回环：手机访问自己的 127.0.0.1:PORT → 经数据线回到 PC
        dial_url = url
        if port:
            run([adb, '-s', s, 'reverse', 'tcp:%d' % port,
                 'tcp:%d' % port], timeout=15)
            dial_url = 'http://127.0.0.1:%d/' % port
        run([adb, '-s', s, 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'],
            timeout=15)
        run([adb, '-s', s, 'shell', 'wm', 'dismiss-keyguard'], timeout=15)
        cmd = [adb, '-s', s, 'shell', 'am', 'start', '-n', VIEWER_ACT]
        if dial_url:
            cmd += ['--es', 'debug_url', dial_url]
        rc, out = run(cmd, timeout=25)
        if rc == 0 and 'Error' not in (out or ''):
            launched += 1
            launched_now.append(s)
        else:
            notes.append('%s 拉起接收器失败' % name)
            log('viewer launch %s failed: %s' % (s, (out or '')[:200]))
    if launched:
        log('viewer launched on %d phone(s) (url=%s, usb-reverse=%s)'
            % (launched, 'yes' if url else 'no', port or 0))
        # 记到会话上：该会话停投时逐台 force-stop 恢复手机原状。
        # 同一窗口多台手机 = 一路流多观众，launched_serials 取并集
        if mon is not None:
            mon.launched_serials = list(dict.fromkeys(
                prev + launched_now))
        msg = '已在 %d 台手机上拉起接收器' % launched
        if notes:
            msg += '\n' + '\n'.join(notes)
        return True, msg
    return False, '\n'.join(notes) or 'adb 拉起接收器失败'


def usb_phone_connected():
    """USB 上有没有已授权的 Android 设备（优先用缓存，3 秒内不重查）。"""
    now = time.time()
    if now - STATE.get('_usb_checked', 0) < 3:
        return bool(STATE.get('usbPhone'))
    devs = probe_usb_phone()
    STATE['_usb_checked'] = now
    return bool(devs)


def target_resolution(cfg=None):
    """算出虚拟屏该用的分辨率。返回 (w, h, 来源说明)。

    配置 resolution 可以是 'auto'（跟随手机屏幕）或 '宽x高'。
    auto 时按 orientation 决定长边/短边怎么摆：手机竖放就建竖屏虚拟屏，
    这样虚拟屏、Sunshine 捕获、Moonlight 请求三者同比例，画面 1:1 铺满。
    （前提：手机端 Moonlight 已打补丁，见 tools/patch_moonlight.py）
    """
    cfg = cfg or load_ext_config()
    res = str(cfg.get('resolution') or 'auto').lower()
    if 'x' in res:
        try:
            w, h = [int(v) for v in res.split('x', 1)]
            return w, h, '手动指定 %dx%d' % (w, h)
        except Exception:
            pass

    orient = (cfg.get('orientation') or 'portrait').lower()
    scr = phone_screen()
    if scr:
        short, long_ = scr
        if orient == 'portrait':
            return short, long_, '手机屏幕 %dx%d 竖屏' % (short, long_)
        return long_, short, '手机屏幕 %dx%d 横屏' % (long_, short)
    w = int(cfg.get('fallbackWidth') or 1080)
    h = int(cfg.get('fallbackHeight') or 2220)
    if orient == 'landscape' and h > w:
        w, h = h, w
    elif orient == 'portrait' and w > h:
        w, h = h, w
    return w, h, '默认值（未检测到手机）'


def bind_sunshine_output(verbose=True):
    r"""把虚拟屏的 device_id 写进 sunshine.conf，让 Sunshine 只串流这块屏。

    Sunshine 的 output_name 必须填 device_id，填 \\.\DISPLAYxx 会报
    Failed to find device；而 device_id 由 EDID 推导，显示器改名也不变。
    """
    dev, desc = disp.find_vdd()
    if not dev:
        return False, '虚拟屏未挂出，无法绑定串流目标', False
    log_path = os.path.join(sunshine_config_dir(), 'sunshine.log')
    conf_path = os.path.join(sunshine_config_dir(), 'sunshine.conf')

    # 优先按 EDID 特征找虚拟屏的 device_id：跨重启不变，不用重启 Sunshine
    did = disp.sunshine_vdd_device_id(log_path)
    if did:
        log('bind sunshine: 按 EDID 匹配到虚拟屏 device_id %s' % did)
    else:
        # 日志里没有虚拟屏（Sunshine 启动时它还没挂出），退回按 display_name 找
        did = disp.sunshine_device_id_for(dev, log_path)
        if not did and service_state(SUNSHINE_SVC) == SERVICE_RUNNING:
            # 只能重启一次 Sunshine 让它重新枚举
            log('bind sunshine: 日志无设备列表，重启 Sunshine 重新枚举')
            stop_service()
            start_service(wait_ready=True)
            for _ in range(12):
                did = disp.sunshine_vdd_device_id(log_path) \
                    or disp.sunshine_device_id_for(dev, log_path)
                if did:
                    break
                time.sleep(1.0)
    if not did:
        return False, 'Sunshine 日志里还没记录这块屏（重启一次 Sunshine 即可）', False
    cfg = load_ext_config()
    extra = {
        # 串流调试期曾开到 debug，日志涨得很快，正式跑用 info 就够
        'min_log_level': 'info',
        'stream_audio': 'enabled' if cfg.get('streamAudio') else 'disabled',
    }
    ok, msg, changed = disp.write_sunshine_output(conf_path, did, extra)
    log('bind sunshine: %s (changed=%s)' % (msg, changed))
    return ok, msg, changed


def set_resolution(res_str):
    """切换虚拟屏分辨率，并写进配置。返回 (ok, msg)。

    为什么降低分辨率能让手机看清楚：手机 PPI 远高于电脑显示器，虚拟屏渲染的
    像素越密，串流到手机被拉伸后内容就越小越糊。把渲染分辨率调低，Windows 在
    更少的像素上画同样的桌面，手机端拉伸后文字/图标变大，反而看得清。

    切换逻辑：
      1. 写配置（resolution / fallbackWidth / fallbackHeight）；
      2. 若当前没开屏，直接返回「下次开启生效」；
      3. 若已开屏，确保 vdd_settings.xml 含该模式（模板已含全部档位），再用
         ChangeDisplaySettingsExW 实时切换——device_id 由 EDID 推导、跨重启不变，
         所以 Sunshine 的串流目标不用改、也不用重启 Sunshine；
      4. 万一驱动没声明该模式（例如旧配置未重建节点），重建设备节点强制驱动
         重读 vdd_settings.xml，再切一次。
    """
    cfg = load_ext_config()
    res = str(res_str).lower().strip()
    if res != 'auto':
        try:
            w, h = [int(v) for v in res.split('x', 1)]
        except Exception:
            return False, '分辨率格式应为 宽x高，例如 720x1480'
        if w <= 0 or h <= 0:
            return False, '分辨率数值无效'
        cfg['fallbackWidth'] = w
        cfg['fallbackHeight'] = h
    cfg['resolution'] = res
    ok_s, m_s = save_ext_config(cfg)
    if not ok_s:
        return False, '保存配置失败：%s' % m_s

    if not vdd_attached():
        return True, '已保存为「%s」，下次开启扩展屏时生效' % res

    # 已开启：确保驱动支持该模式（模板已含全部新档位），再实时切换
    ensure_vdd_config()
    dev, _ = disp.find_vdd()
    if not dev:
        return False, '虚拟屏已挂出但找不到设备节点'
    w, h, src = target_resolution(cfg)
    ok, m = disp.place(dev, w, h, 60,
                       position=cfg.get('position', 'right'),
                       align=cfg.get('align', 'start'))
    if not ok and ('驱动不支持' in m or 'BADMODE' in m):
        # 驱动没这个模式：重建设备节点，强制驱动重读 vdd_settings.xml
        log('set_resolution: 驱动不支持 %dx%d，重建设备节点' % (w, h))
        recreate_vdd_device()
        dev, _ = disp.find_vdd()
        if dev:
            ok, m = disp.place(dev, w, h, 60,
                              position=cfg.get('position', 'right'),
                              align=cfg.get('align', 'start'))
    if ok:
        return True, '已切换到 %dx%d（%s）。若手机画面异常，重连一次即可' % (w, h, src)
    return False, '切换失败：%s' % m


def ensure_layout():
    """把虚拟屏调成目标分辨率并摆到主屏右侧。返回 (ok, 消息列表)。"""
    msgs = []
    dev, desc = disp.find_vdd()
    if not dev:
        return False, ['虚拟屏未挂出']

    cfg = load_ext_config()
    w, h, src = target_resolution(cfg)
    pos = cfg.get('position', 'right')
    align = cfg.get('align', 'start')
    want = disp.calc_position(dev, w, h, pos, align)
    cur = disp.vdd_rect()
    want_xy = (want[0], want[1]) if want else None

    if disp.mode_matches(dev, w, h) and want_xy and cur \
            and (cur[0], cur[1]) == want_xy:
        msgs.append('分辨率与位置都已是 %dx%d @(%d,%d)（%s）'
                    % (w, h, want_xy[0], want_xy[1], src))
    else:
        if disp.mode_matches(dev, w, h):
            # 分辨率对但位置不对：常见于副屏插拔过，虚拟屏被留在老坐标上，
            # 和主屏之间空出一段死区，鼠标根本跨不过去
            log('ensure_layout: 分辨率已对，重摆位置 %s -> %s' % (cur, want_xy))
        ok, m = disp.place(dev, w, h, 60, position=pos, align=align)
        log('ensure_layout: %s' % m)
        msgs.append(m)
        if not ok:
            return False, msgs

    rect = disp.vdd_rect()
    if rect:
        msgs.append('虚拟屏位置 (%d,%d) 尺寸 %dx%d' % (rect[0], rect[1], rect[2], rect[3]))
    return True, msgs


def enable_extscreen():
    msgs = []
    # 顺序很重要：先把虚拟屏挂出来、布局摆正、串流目标写好，最后才启动 Sunshine。
    # 反过来的话 Sunshine 启动时枚举不到虚拟屏，还得再重启一次才能生效，
    # 用户要白等两轮初始化（每轮 25~30 秒）。
    if not vdd_attached():
        msgs.append('虚拟屏未挂出，强制重启驱动…')
        restart_vdd()

    if not vdd_attached():
        msgs.append('警告：虚拟屏仍未出现，请用「环境检测与修复」')
        return False, '\n'.join(msgs)

    ok2, lay = ensure_layout()
    msgs.extend(lay)
    if not ok2:
        return False, '\n'.join(msgs)

    ok3, bmsg, changed = bind_sunshine_output()
    msgs.append(bmsg)

    # 启动并等到 Sunshine 真正能接受串流（不是只看服务状态码）
    ok, msg = start_service(wait_ready=True, timeout=90)
    msgs.append(msg)
    if not ok:
        return False, '\n'.join(msgs)

    if changed:
        # 配置改过，需要重启才生效；重启后同样要等就绪
        msgs.append('串流目标已变更，重启 Sunshine 使其生效…')
        stop_service()
        ok4, smsg = start_service(wait_ready=True, timeout=90)
        msgs.append(smsg)
        if not ok4:
            return False, '\n'.join(msgs)

    # 顺着把手机端的串流也拉起来；失败不影响扩展屏本身可用
    ok5, lmsg = launch_stream_on_phone()
    msgs.append(lmsg)

    return True, '\n'.join(msgs)


def disable_extscreen():
    msgs = []
    # 顺序很重要：必须「先停 Sunshine，再关虚拟屏」。
    # 反过来的话 Sunshine 还在用 DXGI 桌面复制抓这块虚拟屏，设备句柄没释放，
    # Disable-PnpDevice 会一直卡在「等待设备排空」上——实测能卡满 150 秒
    # 直接超时，表现就是托盘永远停在「处理中…」，用户以为程序死了。
    # 跟 enable_extscreen 的顺序正好相反：开是「先挂屏最后起 Sunshine」。
    ok, msg = stop_service()
    msgs.append(msg)
    # 给 60 秒上限：排空等太久就该报错，别把界面吊死
    rc, out = powershell(VDISPLAY_PS1, 'off', timeout=60)
    log(f'vdisplay off rc={rc}\n{out[-500:]}')
    if rc == 0:
        msgs.append('虚拟屏已关闭')
    elif 'timeout' in (out or '').lower():
        msgs.append('虚拟屏关闭超时（设备被占用），请用「环境检测与修复」强制重启驱动')
    else:
        msgs.append('关闭虚拟屏异常')
    return True, '\n'.join(msgs)


def launch_scrcpy():
    if not os.path.exists(SCRCPY_EXE):
        return False, f'未找到 scrcpy：{SCRCPY_EXE}'
    if scrcpy_running():
        return True, '手机控制窗口已在运行'
    cfg = load_ext_config()
    limit = min(max(cfg['fallbackWidth'], cfg['fallbackHeight']), 1280)
    args = [SCRCPY_EXE, '--window-title', 'Android 手机控制',
            '--max-size', str(limit), '--stay-awake', '--power-on',
            '--clipboard-autosync']
    try:
        subprocess.Popen(args, creationflags=0x08000000)  # CREATE_NO_WINDOW
        return True, '已在独立窗口打开手机控制'
    except Exception as e:
        return False, str(e)


def _vdd_inf():
    return os.path.join(PAYLOAD, 'vdd', 'SignedDrivers', 'x86', 'VDD', 'MttVDD.inf')


VIRTUAL_HINTS = ('virtual', 'meta ', 'sudomaker', 'parsec', 'mttvdd', 'basic render')


def gpu_name():
    """从已注册的显示适配器里挑出真实显卡名，写入 vdd_settings.xml。"""
    for _name, desc, _attached in list_monitors():
        d = (desc or '').strip()
        if not d:
            continue
        if any(h in d.lower() for h in VIRTUAL_HINTS):
            continue
        return d
    return 'default'


VDD_XML = """<?xml version="1.0" encoding="utf-8"?>
<vdd_settings>
  <monitors>
    <count>1</count>
  </monitors>
  <gpu>
    <friendlyname>{gpu}</friendlyname>
  </gpu>
  <global>
    <g_refresh_rate>60</g_refresh_rate>
    <g_refresh_rate>90</g_refresh_rate>
    <g_refresh_rate>120</g_refresh_rate>
  </global>
  <resolutions>
    <!-- 横屏标准 -->
    <resolution><width>1920</width><height>1080</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>2560</width><height>1440</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>1280</width><height>720</height><refresh_rate>60</refresh_rate></resolution>
    <!-- 横屏宽 -->
    <resolution><width>2220</width><height>1080</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>2960</width><height>1440</height><refresh_rate>60</refresh_rate></resolution>
    <!-- 竖屏：默认 / 物理 -->
    <resolution><width>1080</width><height>2220</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>1440</width><height>2960</height><refresh_rate>60</refresh_rate></resolution>
    <!-- 竖屏：逐档降低渲染分辨率，让手机上字更大更清楚（≈19.5:9，不变形） -->
    <resolution><width>900</width><height>1850</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>800</width><height>1646</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>720</width><height>1480</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>640</width><height>1320</height><refresh_rate>60</refresh_rate></resolution>
    <resolution><width>540</width><height>1110</height><refresh_rate>60</refresh_rate></resolution>
  </resolutions>
  <options>
    <CustomEdid>false</CustomEdid>
    <PreventSpoof>false</PreventSpoof>
    <EdidCeaOverride>false</EdidCeaOverride>
    <HardwareCursor>true</HardwareCursor>
    <SDR10bit>false</SDR10bit>
    <HDRPlus>false</HDRPlus>
    <logging>true</logging>
    <debuglogging>false</debuglogging>
  </options>
</vdd_settings>
"""


def ensure_vdd_config():
    """把 vdd_settings.xml 修正成驱动能接受的写法。

    三种常见错法都会让驱动加载成功、却一块屏都不挂：
      friendlyname 写成 Best GPU (Auto) 这类非真实显卡名；
      CustomEdid=true 但目录里没有 user_edid.bin；
      SDR10bit 与 HDRPlus 同时为 true（驱动自带样例明确注明二者互斥）。
    """
    import shutil
    import datetime

    want = VDD_XML.format(gpu=gpu_name())
    try:
        cur = ''
        if os.path.exists(VDD_SETTINGS):
            with open(VDD_SETTINGS, 'r', encoding='utf-8', errors='ignore') as f:
                cur = f.read()
        if cur.strip() == want.strip():
            return True, '配置已是正确写法'
        if cur.strip():
            bak = VDD_SETTINGS + '.bak.' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            try:
                shutil.copyfile(VDD_SETTINGS, bak)
            except Exception:
                pass
        with open(VDD_SETTINGS, 'w', encoding='utf-8', newline='\n') as f:
            f.write(want)
        log('vdd_settings.xml rewritten')
        return True, '配置已修正'
    except Exception as e:
        return False, f'配置写入失败：{e}'


def repair_environment():
    """递进式排查：重启设备 → 换回原始配置 → 升级驱动，每步都报告结果。"""
    import shutil

    report = []
    success = False

    # ---------- 前置检查 ----------
    if sunshine_installed():
        st = service_state(SUNSHINE_SVC)
        report.append('Sunshine：已安装（服务%s）' % {
            SERVICE_RUNNING: '运行中', SERVICE_STOPPED: '已停止',
            SERVICE_UNKNOWN: '状态未知'}.get(st, '未知'))
    else:
        report.append('Sunshine：未安装，开始一键安装…')
        ok_ins, m_ins = install_sunshine()
        for line in (m_ins or '').splitlines():
            report.append('      ' + line)
        report.append('      安装结果：' + ('成功' if ok_ins else '失败'))

    if vdd_installed():
        report.append('虚拟显示器驱动：已注册')
    else:
        inf = _vdd_inf()
        if os.path.exists(inf):
            report.append('虚拟显示器驱动：缺失，正在安装…')
            rc, _ = run(['pnputil', '/add-driver', inf, '/install'], timeout=300)
            report.append('  驱动安装' + ('完成' if rc == 0 else f'失败 rc={rc}'))
        else:
            report.append(f'虚拟显示器驱动：缺失，找不到 {inf}')

    adapters = list_monitors()
    report.append(f'显示适配器：共 {len(adapters)} 个，已挂载 {monitor_count()} 块')
    report.append(f'目标显卡：{gpu_name()}')
    report.append('')

    # ---------- 1 修正配置 + 重启设备 ----------
    report.append('[1/3] 修正 vdd_settings.xml 并重启虚拟显示器…')
    ok_cfg, msg_cfg = ensure_vdd_config()
    report.append('      ' + ('OK  ' if ok_cfg else '失败 ') + msg_cfg)
    ok_restart, _ = restart_vdd()
    if ok_restart:
        success = True
        report.append('      OK 虚拟屏已挂出')
    else:
        report.append('      重启后仍未挂出')

    # ---------- 2 重建虚拟显示器设备节点 ----------
    if not success:
        report.append('[2/3] 删除并重建虚拟显示器设备节点…')
        if recreate_vdd_device():
            success = True
            report.append('      OK 重建后虚拟屏已挂出')
        else:
            report.append('      重建后仍未挂出')

    # ---------- 3 升级驱动 ----------
    if not success:
        inf = _vdd_inf()
        if os.path.exists(inf):
            report.append('[3/3] 升级到新版驱动 25.7.23…')
            rc, out = run(['pnputil', '/add-driver', inf, '/install'], timeout=300)
            report.append(f'      驱动更新 rc={rc}')
            ok_restart2, _ = restart_vdd()
            if ok_restart2:
                success = True
                report.append('      OK 新驱动挂出了虚拟屏')
            else:
                report.append('      新驱动仍未挂出')

    report.append('')
    if success:
        report.append('结果：虚拟屏已就绪，扩展屏可以正常使用。')
    else:
        report.append('结果：虚拟屏仍然无法挂出。')
        report.append('')
        report.append(f'最可能的原因：系统里已有 {len(adapters)} 个显示适配器，其中 37 个是')
        report.append('Meta / SudoMaker / Parsec / EV 四个虚拟显示器驱动注册的，却全部未启用。')
        report.append('IDD 虚拟显示器的资源被占满后，VDD 就没法再注册新的适配器。')
        report.append('')
        report.append('处理办法（任选其一，做完再点一次「环境检测与修复」）：')
        report.append('  1. 设备管理器 → 显示适配器，卸载 Meta Virtual Monitor、')
        report.append('     SudoMaker Virtual Display Adapter、EV Virtual Display Device；')
        report.append('  2. 若你确实要用 VR / Parsec，可只卸载其余两个；')
        report.append('  3. 卸载后重启电脑，再运行本工具。')

    return bool(success), '\n'.join(report)


# --------------------------------------------------------------------------
# 开机自启
# --------------------------------------------------------------------------
def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Run') as k:
            winreg.QueryValueEx(k, 'ExtScreenDock')
            return True
    except Exception:
        return False


def set_autostart(enable):
    import winreg
    key = r'Software\Microsoft\Windows\CurrentVersion\Run'
    exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, 'ExtScreenDock', 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(k, 'ExtScreenDock')
                except FileNotFoundError:
                    pass
    except Exception as e:
        log(f'autostart failed: {e}')


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
STATE = {
    'svc': None,
    'installed': False,
    'vdd': False,
    'monitors': 0,
    'scrcpy': False,
    'devices': [],
    'busy': False,
    # Sunshine 是否真正完成初始化（能接受串流），与 svc 状态区分开
    'ready': False,
    'ready_reason': '',
    # 手机当前是否连着 Sunshine（正在看画面）
    'client': False,
    # USB 上是否连着 Android 设备（窗口投屏 adb 拉起接收器用）
    'usbPhone': False,
    '_usb_checked': 0,
}


def refresh_state(full=True):
    try:
        STATE['installed'] = sunshine_installed()
        STATE['svc'] = service_state(SUNSHINE_SVC)
        STATE['vdd'] = vdd_attached()
        STATE['monitors'] = monitor_count()
        # ready 判定要读日志，放到 full 轮次里做，避免每 4 秒都扫文件
        if full:
            STATE['scrcpy'] = scrcpy_running()
            STATE['devices'] = adb_devices()
            try:
                ok, why = sunshine_ready()
            except Exception:
                ok, why = False, '检测异常'
            STATE['ready'] = ok
            STATE['ready_reason'] = why
            try:
                STATE['client'] = sunshine_client_connected()
            except Exception:
                STATE['client'] = False
    except Exception as e:
        log(f'refresh_state: {e}')


def status_color():
    if STATE['svc'] is None:
        return COLOR_ERR if not STATE.get('installed') else COLOR_OFF
    if STATE['svc'] != SERVICE_RUNNING:
        return COLOR_OFF
    # 服务在跑但 Sunshine 还没完成初始化：用「就绪」色，别装作一切正常
    if not STATE.get('ready'):
        return COLOR_READY
    if STATE.get('client'):
        return COLOR_LIVE
    if STATE['vdd']:
        return COLOR_LIVE
    return COLOR_READY


def status_text():
    if STATE['busy']:
        return '处理中…'
    if STATE['svc'] is None:
        if STATE.get('installed'):
            return 'Sunshine 服务未注册'
        return 'Sunshine 未安装'
    if STATE['svc'] == SERVICE_UNKNOWN:
        return 'Sunshine 状态未知'
    if STATE['svc'] != SERVICE_RUNNING:
        return 'Sunshine 已停止'
    # 服务起来了不等于能串流，这里如实说明还在初始化
    if not STATE.get('ready'):
        return 'Sunshine 启动中…'
    if STATE.get('client'):
        return '手机正在接收画面'
    if STATE['vdd']:
        return '扩展屏已挂出，等待手机连接'
    return '已就绪，等待手机连接'


def make_image(color):
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 4, 48, 60), radius=8, outline=color, width=4)
    d.line((27, 11, 37, 11), fill=color, width=2)
    if color == COLOR_LIVE:
        d.rounded_rectangle((22, 30, 42, 50), radius=2, fill=color)
    elif color == COLOR_READY:
        d.ellipse((27, 39, 37, 49), outline=color, width=3)
    elif color == COLOR_ERR:
        d.line((25, 24, 39, 46), fill=color, width=4)
        d.line((39, 24, 25, 46), fill=color, width=4)
    else:
        d.rounded_rectangle((22, 30, 42, 50), radius=2, outline=color, width=2)
    return img


ICON = None


def update_icon():
    if ICON is None:
        return
    ICON.icon = make_image(status_color())
    ICON.title = f'{APP_NAME}\n{status_text()}'
    try:
        ICON.update_menu()
    except Exception:
        pass


def show_message(text, title=APP_NAME):
    """长文本用系统弹窗展示（托盘气泡装不下）。"""
    MB_OK = 0x0
    MB_ICONINFO = 0x40
    MB_TOPMOST = 0x40000
    try:
        ctypes.windll.user32.MessageBoxW(
            None, str(text), str(title), MB_OK | MB_ICONINFO | MB_TOPMOST)
    except Exception as e:
        log(f'messagebox fail: {e}')


def do_async(fn, *args):
    def worker():
        STATE['busy'] = True
        update_icon()
        try:
            res = fn(*args)
            # 约定：动作函数返回 (ok, msg)。万一路径上返回了单值，
            # 在这里兜住，别让整个托盘崩掉弹「cannot unpack ...」。
            if isinstance(res, tuple) and len(res) == 2:
                ok, msg = res
            else:
                ok, msg = (bool(res), '' if res is None else str(res))
            refresh_state()
            update_icon()
            if len(msg) > 150:
                show_message(('' if ok else '【失败】\n\n') + msg)
            elif ok:
                ICON.notify(msg.replace('\n', '  '), APP_NAME)
            else:
                ICON.notify('失败：' + msg.replace('\n', '  '), APP_NAME)
        except Exception as e:
            log(f'action error: {e}')
            show_message(f'执行出错：\n{e}')
        finally:
            STATE['busy'] = False
            refresh_state()
            update_icon()
    threading.Thread(target=worker, daemon=True).start()


def toggle_extscreen(icon=None, item=None):
    # 用 extscreen_on() 判定，别用「服务在跑就算开着」：
    # 否则 Sunshine 还在初始化的空窗期里，用户点「开启」反而会被当成「关闭」
    if extscreen_on():
        do_async(disable_extscreen)
    else:
        do_async(enable_extscreen)


def open_settings():
    def worker():
        saved, payload = show_settings()
        if payload == '__cast__':
            # 设置窗口右上角「快速投屏」按钮
            cast_pick_and_start()
            return
        if not saved:
            return
        set_autostart(bool(payload.get('autostart')))
        res = payload.get('resolution', 'auto')
        label = '跟随手机屏幕' if res == 'auto' else res
        try:
            ICON.notify(f'已保存（{label}），正在应用', APP_NAME)
        except Exception:
            pass
        do_async(enable_extscreen)

    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------
# 菜单
# --------------------------------------------------------------------------
def menu_title(item):
    return status_text()


def extscreen_on():
    """扩展屏当前是否算「开着」。

    只认「Sunshine 真就绪」这一件事。服务进程活着但 Sunshine 还在初始化时
    不算开着——那种状态下点「开启扩展屏」应该继续往前推，而不是反向关闭。
    """
    return bool(STATE.get('ready'))


def menu_toggle(item):
    return '关闭扩展屏' if extscreen_on() else '开启扩展屏'


def menu_sunshine(item):
    """一级入口：把"未安装/未启动/启动中/正常"直接写在菜单上，点一下就能处理。"""
    if STATE['svc'] is None:
        if STATE.get('installed'):
            return '⚠ Sunshine 服务异常 —— 点此修复'
        return '⚠ Sunshine 未安装 —— 点此安装'
    if STATE['svc'] == SERVICE_UNKNOWN:
        return '⚠ Sunshine 状态未知 —— 点此检查'
    if STATE['svc'] == SERVICE_STOPPED:
        return 'Sunshine 已停止 —— 点此启动'
    if not STATE.get('ready'):
        # 服务在跑但还没初始化完，这时点一下应该去「等它就绪」而不是重复启动
        return 'Sunshine 启动中…（%s）' % (STATE.get('ready_reason') or '初始化中')
    return 'Sunshine 运行中 ✓'


def menu_devices(item):
    devs = STATE['devices']
    if not devs:
        return '手机：未连接'
    return '手机：' + '、'.join(m[1] for m in devs)


def menu_monitors(item):
    return f'显示器：{STATE["monitors"]} 块'


def _noop(icon=None, item=None):
    return None


# --------------------------------------------------------------------------
# 窗口投屏（自研 cast 后端）：状态行 + 菜单 + 窗口选择器
# --------------------------------------------------------------------------
import extscreen_cast

# PowerShell 窗口选择器（窗口 + 手机合一）：列出可分享窗口（红框高亮），
# 多台手机时同屏列出目标手机；输出两行：hwnd、PHONES:serial[,serial|__ALL__]
_PICKER_PS = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public struct PRECT { public int L; public int T; public int R; public int B; }
public class WinApi {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out PRECT r);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out PRECT r, int c);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
    public static PRECT Frame(IntPtr h) {
        PRECT r;
        if (DwmGetWindowAttribute(h, 9, out r, 16) != 0) GetWindowRect(h, out r);
        return r;
    }
}
'@
# DPI 感知：不声明的话本进程的 GetWindowRect/窗体坐标都被系统按主屏
# 缩放虚拟化，高亮框在缩放/多屏场景下会错位（实测踩过）。声明后全程物理像素。
[WinApi]::SetProcessDPIAware() | Out-Null
$json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$B64'))
$cfg = $json | ConvertFrom-Json
$wins = $cfg.wins
$phones = @($cfg.phones)          # [{serial, model}]，可为空
$multiPhone = ($phones.Count -gt 1)
# 对话框按 DPI 缩放（进程已 DPI 感知，尺寸是物理像素，高分屏要放大才正常）
$g = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
$scale = $g.DpiX / 96.0
$g.Dispose()
$formH = 480
if ($multiPhone) { $formH = 665 }
$form = New-Object System.Windows.Forms.Form
$form.Text = '选择要投屏的窗口'
$form.Font = New-Object System.Drawing.Font(
    'Microsoft YaHei UI', [float](9 * $scale))
$form.Size = New-Object System.Drawing.Size([int](520 * $scale),
                                            [int]($formH * $scale))
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$label = New-Object System.Windows.Forms.Label
$label.Text = '选一个窗口，屏幕上红框标出实际目标：'
$label.Location = New-Object System.Drawing.Point([int](10 * $scale),
                                                  [int](8 * $scale))
$label.AutoSize = $true
$list = New-Object System.Windows.Forms.ListBox
$list.Location = New-Object System.Drawing.Point([int](10 * $scale),
                                                 [int](32 * $scale))
$list.Size = New-Object System.Drawing.Size([int](480 * $scale),
                                            [int](340 * $scale))
foreach ($w in $wins) {
    [void]$list.Items.Add(('[{0}] {1}   [#{2}]' -f $w.exe, $w.title,
                           $w.hwnd))
}
# ---- 手机区：0/1 台只显示状态行，多台出现选择列表（含全部手机） ----
$plabel = New-Object System.Windows.Forms.Label
$plabel.Location = New-Object System.Drawing.Point([int](10 * $scale),
                                                   [int](382 * $scale))
$plabel.AutoSize = $true
$plist = $null
if ($multiPhone) {
    $plabel.Text = '投到哪台手机？（走 USB 直连，手机不联网也能投）：'
    $plist = New-Object System.Windows.Forms.ListBox
    $plist.Location = New-Object System.Drawing.Point([int](10 * $scale),
                                                      [int](404 * $scale))
    $plist.Size = New-Object System.Drawing.Size([int](480 * $scale),
                                                 [int](140 * $scale))
    foreach ($p in $phones) {
        if ($p.info) {
            [void]$plist.Items.Add(('{0}  ·  {1}' -f $p.model, $p.info))
        } else {
            [void]$plist.Items.Add($p.model)
        }
    }
    [void]$plist.Items.Add('全部手机')
    if ($plist.Items.Count -gt 0) { $plist.SelectedIndex = 0 }
} elseif ($phones.Count -eq 1) {
    $plabel.Text = ('手机：{0}（USB 自动拉起）' -f $phones[0].model)
} else {
    $plabel.Text = '手机：USB 未连接（手机连上同一 WiFi 也可自动发现）'
}
$btnY = 385
if ($multiPhone) { $btnY = 560 }
$ok = New-Object System.Windows.Forms.Button
$ok.Text = '开始投屏'
$ok.Location = New-Object System.Drawing.Point([int](330 * $scale),
                                               [int]($btnY * $scale))
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '取消'
$cancel.Location = New-Object System.Drawing.Point([int](420 * $scale),
                                                   [int]($btnY * $scale))
$ctrls = @($label, $list, $plabel, $ok, $cancel)
if ($plist -ne $null) { $ctrls += $plist }
$form.Controls.AddRange($ctrls)

# ---- 高亮覆盖层：区域=边框环形，中间镂空点击穿透，不抢焦点 ----
$hl = New-Object System.Windows.Forms.Form
$hl.FormBorderStyle = 'None'
$hl.StartPosition = 'Manual'
$hl.ShowInTaskbar = $false
$hl.Enabled = $false
$hl.TopMost = $true
$hl.BackColor = [System.Drawing.Color]::Red
$hl.Size = New-Object System.Drawing.Size(1, 1)
$script:cur = [long]0
function Show-Highlight([long]$hwndVal) {
    $script:cur = $hwndVal
    $h = [IntPtr]$hwndVal
    if ($h -eq [IntPtr]::Zero) {
        if ($hl.Visible) { $hl.Visible = $false }
        return
    }
    $r = [WinApi]::Frame($h)
    $w = $r.R - $r.L
    $ht = $r.B - $r.T
    if ($w -lt 60 -or $ht -lt 60) {
        if ($hl.Visible) { $hl.Visible = $false }
        return
    }
    $t = 6
    # 注意：PS 里逗号优先级高于算术，New-Object Type(a, b-2*c) 会把表达式
    # 拆成数组运算报 op_Multiply——先算到变量再传
    $iw = $w - 2 * $t
    $ih = $ht - 2 * $t
    $outer = New-Object System.Drawing.Rectangle(0, 0, $w, $ht)
    $inner = New-Object System.Drawing.Rectangle($t, $t, $iw, $ih)
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddRectangle($outer)
    $path.AddRectangle($inner)
    $hl.Region = New-Object System.Drawing.Region($path)
    $hl.Size = New-Object System.Drawing.Size($w, $ht)
    $hl.Location = New-Object System.Drawing.Point($r.L, $r.T)
    if (-not $hl.Visible) {
        $ex = [WinApi]::GetWindowLong($hl.Handle, -20)
        [WinApi]::SetWindowLong($hl.Handle, -20, $ex -bor 0x08000000) | Out-Null
    }
    [WinApi]::SetWindowPos($hl.Handle, [IntPtr](-1), $r.L, $r.T, $w, $ht, 0x50) | Out-Null
    $hl.Visible = $true
}
$list.Add_SelectedIndexChanged({
    $v = [long]0
    if ($list.SelectedIndex -ge 0) {
        $item = [string]$list.SelectedItem
        $hash = $item.LastIndexOf('[#')
        $v = [long]$item.Substring($hash + 2).TrimEnd(']')
    }
    Show-Highlight $v
})
# 跟随刷新：窗口移动/缩放时红框跟上，并保持置顶
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({ Show-Highlight $script:cur })
$form.Add_FormClosed({ $timer.Stop(); $hl.Dispose() })

$ok.Add_Click({ $form.DialogResult = 'OK'; $form.Close() })
$cancel.Add_Click({ $form.DialogResult = 'Cancel'; $form.Close() })
$list.Add_DoubleClick({ $form.DialogResult = 'OK'; $form.Close() })
$form.AcceptButton = $ok
$timer.Start()
if ($list.Items.Count -gt 0) { $list.SelectedIndex = 0 }
$r = $form.ShowDialog()
if ($r -eq 'OK' -and $list.SelectedIndex -ge 0) {
    $item = [string]$list.SelectedItem
    $hash = $item.LastIndexOf('[#')
    $hwnd = $item.Substring($hash + 2).TrimEnd(']')
    Write-Output $hwnd
    if ($multiPhone) {
        # 默认第一台；选「全部手机」输出 __ALL__
        $sel = '__ALL__'
        if ($plist.SelectedIndex -ge 0 -and
                $plist.SelectedIndex -lt $phones.Count) {
            $sel = $phones[$plist.SelectedIndex].serial
        }
        Write-Output ('PHONES:' + $sel)
    }
}
'''


def _pick_window():
    """弹窗选窗口（+多手机时选目标），返回 (hwnd, title, serials|None)。

    serials=None 表示手机侧无需选择（0/1 台自动），[] 表示用户取消，
    非空列表是用户挑的 serial；'__ALL__' 展开为全部。
    """
    wins = extscreen_cast.list_windows()
    if not wins:
        show_message('没有找到可分享的窗口。', '窗口投屏')
        return None
    try:
        devs = probe_usb_phone()
    except Exception:
        devs = []
    adb = adb_path()
    phones = []
    for s, m in devs:
        # 每台一次 adb 查规格与商品名，选择界面直接展示
        name, info = phone_specs(adb, s) if adb else ('', '')
        phones.append({'serial': s, 'model': name or m, 'info': info})
    data = json.dumps({
        'wins': [{'hwnd': int(h), 'title': t, 'exe': e}
                 for h, t, e in wins[:60]],
        'phones': phones,
    }, ensure_ascii=False)
    ps = _PICKER_PS.replace('$B64', base64.b64encode(
        data.encode('utf-8')).decode('ascii'))
    rc, out = run(['powershell.exe', '-NoProfile', '-ExecutionPolicy',
                   'Bypass', '-Command', ps], timeout=300)
    hwnd, serials = 0, None
    for line in (out or '').splitlines():
        line = line.strip()
        if line.isdigit() and not hwnd:
            hwnd = int(line)
        elif line.startswith('PHONES:'):
            sel = line[len('PHONES:'):].strip()
            if sel == '__ALL__':
                serials = [s for s, _ in devs]
            elif sel:
                serials = [sel]
            else:
                serials = []
    if not hwnd:
        return None
    for h, t, _e in wins:
        if int(h) == hwnd:
            return hwnd, t, serials
    return None


def cast_pick_and_start(icon=None, item=None):
    """选窗口并开始投屏（菜单动作）。"""
    def worker():
        STATE['busy'] = True
        update_icon()
        try:
            pick = _pick_window()
            if not pick:
                return
            hwnd, title, serials = pick
            mon = extscreen_cast.MANAGER.acquire(hwnd, title)
            if mon is None:
                show_message('投屏路数已达上限（%d 路），先停掉一路再试。'
                             % extscreen_cast.MANAGER.MAX, '窗口投屏')
                return
            snap = mon.snapshot()
            if snap['run'] and int(mon.hwnd or 0) == int(hwnd):
                # 同一窗口再投另一台手机：一路流本来就是多观众，
                # 千万别重启会话——start() 的切换语义会先把已投的手机全踢掉
                # （2026-11-05 用户实测：投 B 手机 A 就停了）
                ok, msg = True, '窗口已在投屏中，新手机直接加入当前画面'
            else:
                mon.launched_serials = []   # 全新会话，历史清零
                ok, msg = mon.start(hwnd, title,
                                    port=extscreen_cast.MANAGER.next_port())
            refresh_state()
            update_icon()
            # 手机插着 USB 就直接 adb 拉起接收器并推送地址，秒级出画面，
            # 不用等 mDNS 慢慢发现；没插 USB 时靠 mDNS 自动发现兜底
            if ok and load_ext_config().get('castAdbLaunch', True) \
                    and usb_phone_connected():
                lok, lmsg = launch_viewer_on_phone(
                    mon.snapshot().get('url'), serials=serials, mon=mon)
                # 选机被取消/失败也要如实说，不能只报投屏已开始
                msg += '\n' + lmsg
            if len(msg) > 150:
                show_message(('' if ok else '【失败】\n\n') + msg)
            elif ok:
                ICON.notify(msg.replace('\n', '  '), APP_NAME)
            else:
                ICON.notify('失败：' + msg.replace('\n', '  '), APP_NAME)
        except Exception as e:
            log(f'cast pick error: {e}')
            show_message(f'执行出错：\n{e}')
        finally:
            STATE['busy'] = False
            refresh_state()
            update_icon()
    threading.Thread(target=worker, daemon=True).start()


def menu_cast(item):
    """窗口投屏真实状态（状态真实性原则：不装作已就绪）。多路逐个列。"""
    runs = extscreen_cast.MANAGER.running()
    usb = '，USB 手机已连接' if STATE.get('usbPhone') else ''
    if not runs:
        return '窗口投屏：未启动%s' % usb
    parts = []
    for m in runs:
        s = m.snapshot()
        if s['err']:
            parts.append('%s ⚠%s' % (s['title'] or '窗口', s['err']))
        elif s['clients'] > 0:
            parts.append('%s（%d 台在看）' % (s['title'], s['clients']))
        else:
            parts.append('%s（等待手机）' % s['title'])
    head = ('窗口投屏：%d 路分享中' % len(runs)) if len(runs) > 1 \
        else '窗口投屏：分享中'
    return head + ' — ' + '；'.join(parts)[:120]


def menu_cast_stop(item):
    n = len(extscreen_cast.MANAGER.running())
    if n > 1:
        return '全部停止（%d 路投屏）' % n
    return '停止窗口投屏'


def cast_stop_menu(icon=None, item=None):
    do_async(cast_stop_all)


def _stop_one_mon(mon):
    """停掉一路投屏（do_async 用，返回 (ok, msg) 契约）。"""
    ok = mon.stop()
    return (True, '窗口投屏已停止') if ok else (False, '投屏未在运行')


def cast_stop_all():
    """停掉全部投屏会话（poll_loop 的跳变检测会逐台恢复手机）。"""
    runs = extscreen_cast.MANAGER.running()
    for m in runs:
        m.stop()
    return True, ('已停止 %d 路投屏' % len(runs)) if runs else '投屏未在运行'


def _copy_to_clipboard(text):
    """ctypes 直写剪贴板（不经 PowerShell，免引号转义坑）。"""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x2
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    if not u.OpenClipboard(0):
        return False
    try:
        u.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        h = k.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(buf))
        p = k.GlobalLock(h)
        ctypes.memmove(p, buf, ctypes.sizeof(buf))
        k.GlobalUnlock(h)
        u.SetClipboardData(CF_UNICODETEXT, h)
        return True
    except Exception as e:
        log(f'clipboard: {e}')
        return False
    finally:
        u.CloseClipboard()


def cast_copy_url(icon=None, item=None):
    runs = extscreen_cast.MANAGER.running()
    if not runs:
        ICON.notify('投屏还没启动，先选一个窗口开始', APP_NAME)
        return
    s = runs[0].snapshot()
    if len(runs) > 1:
        urls = '\n'.join(m.snapshot()['url'] for m in runs)
        ok = _copy_to_clipboard(urls)
        ICON.notify('已复制 %d 个地址%s' % (len(runs),
                                           '' if ok else '（复制失败）'), APP_NAME)
        return
    if s['url']:
        ok = _copy_to_clipboard(s['url'])
        ICON.notify('已复制 %s' % (s['url'] if ok else '（复制失败）'), APP_NAME)
    else:
        ICON.notify('投屏还没启动，先选一个窗口开始', APP_NAME)


def cast_help(icon=None, item=None):
    show_message(
        '窗口投屏使用说明\n\n'
        '1. 点「选择窗口并开始分享…」，从列表里挑一个窗口。\n'
        '2. Dock 自动广播地址（USB 连接时秒推到手机）；\n'
        '   手机上的「副屏接收器」App 会自动打开画面，\n'
        '   全程无需碰手机。\n'
        '3. 画面走局域网（MJPEG），延迟约 0.1 秒。\n'
        '4. 手机上可以直接操作：单指=鼠标左键，双指=右键，\n'
        '   按住拖动=拖拽。触摸只作用于被投窗口，\n'
        '   不影响桌面（点击不会把后台窗口顶到前台）。\n'
        '5. 窗口关掉或最小化前先点「停止窗口投屏」。\n\n'
        'config.json 可调：castWidth（推流宽度，默认 1080，\n'
        '觉得模糊可再调高）/ castFps / castQuality（JPEG 质量）/\n'
        'castFitWindow / castFitWidth / castFitFollowRotate /\n'
        'castAdbLaunch / castTouchControl。',
        '窗口投屏说明')


# --------------------------------------------------------------------------
# 分辨率子菜单：随时切换虚拟屏渲染分辨率
# --------------------------------------------------------------------------
def _res_val(w, h):
    return 'auto' if (w == 0 and h == 0) else '%dx%d' % (w, h)


def _res_checked(val):
    """菜单勾选状态：读当前配置，命中即打勾（每次打开菜单都会重算）。"""
    def _f(item):
        try:
            cfg = load_ext_config()
        except Exception:
            return False
        return str(cfg.get('resolution') or 'auto').lower() == val
    return _f


def _res_action(val):
    """点击：异步切换分辨率。"""
    def _f(icon=None, item=None):
        do_async(set_resolution, val)
    return _f


def _build_resolution_menu():
    items = []
    for label, w, h in RESOLUTION_PRESETS:
        val = _res_val(w, h)
        items.append(pystray.MenuItem(
            label, _res_action(val), checked=_res_checked(val)))
    return pystray.Menu(*items)


RES_SUBMENU = _build_resolution_menu()


MENU = pystray.Menu(
    pystray.MenuItem(menu_title, _noop, enabled=False),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem(menu_sunshine,
                     lambda icon=None, item=None: do_async(start_or_install_sunshine)),
    pystray.MenuItem(menu_toggle, toggle_extscreen, default=True),
    pystray.MenuItem('虚拟屏分辨率', RES_SUBMENU),
    pystray.MenuItem('打开手机控制窗口 (scrcpy)',
                     lambda icon=None, item=None: do_async(launch_scrcpy)),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem(menu_devices, _noop, enabled=False),
    pystray.MenuItem(menu_cast, _noop, enabled=False),
    pystray.MenuItem('窗口投屏', pystray.Menu(
        pystray.MenuItem('选择窗口并开始分享…', cast_pick_and_start),
        # 有会话在投才显示停止项；没有时上面那条永远可用（多路投第二路）
        pystray.MenuItem(menu_cast_stop, cast_stop_menu,
                         visible=lambda item: bool(
                             extscreen_cast.MANAGER.running())),
        pystray.MenuItem('复制观看地址', cast_copy_url),
        pystray.MenuItem('使用说明…', cast_help),
    )),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem(menu_monitors, _noop, enabled=False),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem('环境与依赖', pystray.Menu(
        pystray.MenuItem('安装 / 重新安装 Sunshine…',
                         lambda icon=None, item=None: do_async(
                             lambda: install_sunshine(force=True))),
        pystray.MenuItem('安装 / 修复虚拟显示器驱动…',
                         lambda icon=None, item=None: do_async(install_vdd_driver)),
        pystray.MenuItem('打开 Sunshine 下载页',
                         lambda icon=None, item=None: do_async(open_sunshine_page)),
        pystray.MenuItem('打开 Sunshine 配置目录',
                         lambda icon=None, item=None: do_async(open_sunshine_config)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('环境检测与修复…',
                         lambda icon=None, item=None: do_async(repair_environment)),
    )),
    pystray.MenuItem('设置…', lambda icon=None, item=None: open_settings()),
    pystray.MenuItem('刷新状态',
                     lambda icon=None, item=None: (refresh_state(), update_icon())),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem(
        '退出',
        lambda icon=None, item=None: (
            _caption_kill(),
            [m.stop() for m in extscreen_cast.MANAGER.mons], icon.stop())),
)


# --------------------------------------------------------------------------
# 窗口悬浮快捷按钮：点一下开始分享该窗口，再点一下关闭
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 全局标题栏投屏按钮：所有带标题栏的窗口，在最小化按钮左边多一个
# 「投/停」按钮，视觉上与系统最小化/最大化/关闭并列（2026-11-05 用户要求）
#
# 实现方式：真 native caption button 需要 DLL 注入每个进程，不可行；
# 用「跨进程 Owner 浮层」等效——每个窗口一个属主化浮层：
#   - SetWindowLongPtr(GWLP_HWNDPARENT=目标窗口)：浮层永远贴着目标窗口
#     上层（不会浮到无关窗口上）、目标最小化时系统自动隐藏浮层
#   - 一个 PowerShell 进程管理所有浮层（每 250ms 枚举新增/销毁/定位）
#   - 颜色状态由 Dock 写 caption_state.json（hwnd→stop/share），浮层轮询
#   - 点击输出 'CLICK <hwnd>'，Dock 切换该窗口的投屏状态
_CAPTION_PS = r'''
# 全局标题栏投屏按钮（单实例、前台跟随）。
#
# 重要教训（2026-11-05 凌晨全线输入冻结事故）：不要把浮层的属主
# 设置成别的进程的窗口（GWL_HWNDPARENT 跨进程赋值）——Windows 会因此
# 耦合两个线程的输入队列，本进程一卡，所有挂了浮层的窗口全部失去响应，
# 且每窗一个浮层 = 耦合全桌面。现改为：单个浮层，只跟随当前前台窗口
# ——按钮「看起来」在每个窗口上（谁在前台就出现在谁家标题栏），
# TOPMOST 在此前提下天然正确（前台窗口本来就在最上层）。
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public struct QRECT { public int L; public int T; public int R; public int B; }
public class QuickApi {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int a, out QRECT r, int c);
    [DllImport("dwmapi.dll")] public static extern int DwmGetCloak(IntPtr h, out int v);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", EntryPoint = "GetClassNameW", CharSet = CharSet.Unicode)]
    public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", EntryPoint = "GetWindowTextW", CharSet = CharSet.Unicode)]
    public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    public static QRECT Frame(IntPtr h) {
        QRECT r;
        if (DwmGetWindowAttribute(h, 9, out r, 16) != 0) { r.L = 0; r.T = 0; r.R = 0; r.B = 0; }
        return r;
    }
}
'@
[QuickApi]::SetProcessDPIAware() | Out-Null
$stateFile = '$STATE_FILE'
$dockPid = [int]'$DOCK_PID'
$g = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero)
$scale = $g.DpiX / 96.0
$g.Dispose()
$skipTitle = 'ExtScreen|选择要投屏|副屏助手'

function Test-Target([IntPtr]$h) {
    if ($h -eq [IntPtr]::Zero) { return $false }
    if (-not [QuickApi]::IsWindow($h)) { return $false }
    if ([QuickApi]::IsIconic($h)) { return $false }
    if (-not [QuickApi]::IsWindowVisible($h)) { return $false }
    $ex = [QuickApi]::GetWindowLong($h, -20)
    if (($ex -band 0x80) -ne 0) { return $false }        # TOOLWINDOW
    $st = [QuickApi]::GetWindowLong($h, -16)
    if (($st -band 0x00C00000) -eq 0) { return $false }  # 无 WS_CAPTION
    $cloak = 0
    [QuickApi]::DwmGetCloak($h, [ref]$cloak) | Out-Null
    if ($cloak -ne 0) { return $false }                  # 隐藏/其他虚拟桌面
    $cls = New-Object System.Text.StringBuilder 256
    [QuickApi]::GetClassName($h, $cls, 256) | Out-Null
    if ($cls.ToString() -eq 'ExtScreenMenuHost') { return $false }
    $ti = New-Object System.Text.StringBuilder 256
    [QuickApi]::GetWindowText($h, $ti, 256) | Out-Null
    if ($ti.ToString() -match $skipTitle) { return $false }
    $wpid = [uint32]0
    [QuickApi]::GetWindowThreadProcessId($h, [ref]$wpid) | Out-Null
    if ([int]$wpid -eq $dockPid) { return $false }
    $r = [QuickApi]::Frame($h)
    if (($r.R - $r.L) -lt 220 -or ($r.B - $r.T) -lt 120) { return $false }
    return $true
}

$f = New-Object System.Windows.Forms.Form
$f.FormBorderStyle = 'None'
$f.ShowInTaskbar = $false
$f.TopMost = $true
$f.StartPosition = 'Manual'
$f.Size = New-Object System.Drawing.Size([int](34 * $scale),
                                          [int](29 * $scale))
$f.BackColor = [System.Drawing.Color]::ForestGreen
$lbl = New-Object System.Windows.Forms.Label
$lbl.Dock = 'Fill'
$lbl.TextAlign = 'MiddleCenter'
$lbl.ForeColor = [System.Drawing.Color]::White
$lbl.Text = '投'
$lbl.Font = New-Object System.Drawing.Font(
    'Microsoft YaHei UI', [float](10 * $scale),
    [System.Drawing.FontStyle]::Bold)
$f.Controls.Add($lbl)
$ex = [QuickApi]::GetWindowLong($f.Handle, -20)
[QuickApi]::SetWindowLong($f.Handle, -20, $ex -bor 0x08000000) | Out-Null
$target = [IntPtr]::Zero    # 当前按钮挂在哪个前台窗口
$pend = 0                   # 点击反馈剩余 tick

$lbl.Add_Click({
    if ($target -ne [IntPtr]::Zero) {
        [Console]::Out.WriteLine(('CLICK {0}' -f $target.ToInt64()))
        [Console]::Out.Flush()
        $pend = 8   # 8 tick x 250ms = 2s 反馈期
        $f.BackColor = [System.Drawing.Color]::DimGray
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    try {
        $fg = [QuickApi]::GetForegroundWindow()
        if (-not (Test-Target $fg)) {
            if ($f.Visible) { $f.Visible = $false }
            $target = [IntPtr]::Zero
            return
        }
        if ($fg -ne $target) {
            $target = $fg
            if ($pend -le 0) { $f.BackColor = [System.Drawing.Color]::ForestGreen }
        }
        $st = 'share'
        try {
            if ([System.IO.File]::Exists($stateFile)) {
                $map = [System.IO.File]::ReadAllText($stateFile) |
                       ConvertFrom-Json
                $k = [string]$target.ToInt64()
                if ($map.PSObject.Properties[$k]) { $st = 'stop' }
            }
        } catch {}
        if ($pend -gt 0) {
            $pend = $pend - 1
        } elseif ($st -eq 'stop') {
            if ($f.BackColor -ne [System.Drawing.Color]::Firebrick) {
                $f.BackColor = [System.Drawing.Color]::Firebrick
                $lbl.Text = '停'
            }
        } else {
            if ($f.BackColor -ne [System.Drawing.Color]::ForestGreen) {
                $f.BackColor = [System.Drawing.Color]::ForestGreen
                $lbl.Text = '投'
            }
        }
        $r = [QuickApi]::Frame($target)
        $x = $r.R - [int](147 * $scale)   # 关闭/最大化/最小化三个按钮之后
        $y = $r.T + [int](2 * $scale)     # 与标题栏按钮同排
        [QuickApi]::SetWindowPos($f.Handle, [IntPtr](-1), $x, $y, 0, 0,
                                 0x51) | Out-Null
        if (-not $f.Visible) { $f.Visible = $true }
    } catch {}
})
$timer.Start()
[System.Windows.Forms.Application]::Run()
'''

_caption = {'proc': None}
CAPTION_STATE_FILE = os.path.join(APP_DIR, 'caption_state.json')


def _caption_kill():
    p = _caption.get('proc')
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    _caption['proc'] = None


def _caption_toggle(hwnd):
    """标题栏按钮点击：该窗口在投则停（只停它那一路），没投则投它。"""
    mon = extscreen_cast.MANAGER.by_hwnd(hwnd)
    if mon:
        do_async(_stop_one_mon, mon)
        return
    _start_cast_worker(hwnd)


def _caption_reader(proc):
    """读浮层进程输出：CLICK <hwnd> → 切换该窗口投屏。"""
    try:
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').strip()
            if line.startswith('CLICK '):
                try:
                    hwnd = int(line.split()[1])
                except Exception:
                    continue
                do_async(_caption_toggle, hwnd)
    except Exception as e:
        log(f'caption reader: {e}')


def _caption_write_state():
    """把各会话在投窗口写给浮层进程（hwnd→stop），按钮变红色「停」。"""
    try:
        m = {}
        for mon in extscreen_cast.MANAGER.running():
            if mon.hwnd:
                m[str(int(mon.hwnd))] = 'stop'
        tmp = CAPTION_STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(m, f)
        os.replace(tmp, CAPTION_STATE_FILE)
    except Exception as e:
        log(f'caption state write: {e}')


def _caption_ensure():
    """浮层进程死了就重新拉起（正常情况下整租生命周期只跑一个）。"""
    p = _caption.get('proc')
    if p and p.poll() is None:
        return
    ps = _CAPTION_PS.replace('$STATE_FILE', CAPTION_STATE_FILE)
    ps = ps.replace('$DOCK_PID', str(os.getpid()))
    try:
        p = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-Command', ps],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        _caption['proc'] = p
        threading.Thread(target=_caption_reader, args=(p,), daemon=True,
                         name='caption-btn').start()
        log('caption overlay process started')
    except Exception as e:
        log(f'caption overlay spawn: {e}')


def _start_cast_worker(hwnd):
    """开始投屏指定窗口（后台线程，快捷按钮/系统菜单共用）。"""

    def worker():
        STATE['busy'] = True
        update_icon()
        try:
            title = ''
            for h, t, _e in extscreen_cast.list_windows():
                if int(h) == int(hwnd):
                    title = t
                    break
            mon = extscreen_cast.MANAGER.acquire(hwnd, title or '窗口')
            if mon is None:
                log('cast: session limit reached (%d)'
                    % extscreen_cast.MANAGER.MAX)
                return
            snap = mon.snapshot()
            if snap['run'] and int(mon.hwnd or 0) == int(hwnd):
                # 同窗口再投：不重启会话（同一路流多观众，见 cast_pick_and_start）
                ok, msg = True, '窗口已在投屏中，新手机直接加入当前画面'
            else:
                mon.launched_serials = []
                ok, msg = mon.start(hwnd, title or '窗口',
                                    port=extscreen_cast.MANAGER.next_port())
            refresh_state()
            update_icon()
            if ok and load_ext_config().get('castAdbLaunch', True) \
                    and usb_phone_connected():
                launch_viewer_on_phone(mon.snapshot().get('url') or None,
                                       mon=mon)
        except Exception as e:
            log(f'quick toggle: {e}')
        finally:
            STATE['busy'] = False
            refresh_state()
            update_icon()
    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------
# 系统菜单接管：目标窗口标题栏右键（或 Alt+Space）时，用我们的菜单替换
# 原生系统菜单（含投屏开关；原生 还原/移动/大小/最小化/最大化/关闭 转发回窗口）
#
# 实测（2026-11-04 探针 tools/probe_menuevent.py）：标题栏右键 / Alt+Space
# 打开系统菜单**不产生任何 WinEvent**（EVENT_SYSTEM_MENUSTART/POPUPSTART 全程
# 零上报），WinEvent 方案从根上不成立。改用 WH_MOUSE_LL / WH_KEYBOARD_LL
# 低级钩子：
#   - 鼠标：右键抬起(RBUTTONUP)时用 WM_NCHITTEST 判断光标是否在目标窗口的
#     HTCAPTION/HTSYSMENU 区域 → 吞掉事件（原生菜单根本不会弹出）→ 弹我们的菜单
#   - 键盘：Alt+Space 的 SYSKEYDOWN → 同样吞掉并弹菜单
# 回调里绝不做弹菜单等慢操作（低级钩子回调超时会被系统静默摘除），
# 只 PostMessage 给本线程的宿主窗口，在 WndProc 里弹。
# --------------------------------------------------------------------------
WM_NCHITTEST = 0x0084
HTCAPTION = 2
HTSYSMENU = 3
GA_ROOT = 2
WM_RBUTTONUP = 0x0205
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_ALTDOWN = 0x20
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_APP_SYSMENU = 0x8000 + 0x5C7   # 宿主窗口自定义消息：wParam=目标 hwnd
WM_CANCELMODE = 0x001F
WM_SYSCOMMAND = 0x0112
SC_SIZE = 0xF000
SC_MOVE = 0xF010
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF120
SC_CLOSE = 0xF060
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
SWP_NOACTIVATEX = 0x0010

_menu_state = {'owner': None, 'active': False, 'proc_ref': None}


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


def _menu_targets():
    """接管哪些窗口的系统菜单：正在投的（任意一路）+ 快捷按钮钉住的。"""
    t = set()
    for m in extscreen_cast.MANAGER.running():
        if m.hwnd:
            t.add(int(m.hwnd))
    return t


def _input_window_at(x, y):
    """真实输入路由语义的 WindowFromPoint。

    实测（2026-11-04）：WindowFromPoint 不跳过 DWM cloaked 窗口——Edge 预启动
    的隐藏窗口（msedge.exe, Chrome_WidgetWin_1, visible=1 但不在屏幕上）会
    全屏挡住判定，把标题栏点击判给它。真实输入路由会跳过 cloaked/穿透层，
    所以按 Z 序自己走：跳过 cloaked、WS_EX_TRANSPARENT、最小化窗口。
    """
    u = ctypes.windll.user32
    d = ctypes.windll.dwmapi
    u.GetTopWindow.argtypes = [ctypes.c_void_p]
    u.GetTopWindow.restype = ctypes.c_void_p
    u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetWindow.restype = ctypes.c_void_p
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.IsIconic.argtypes = [ctypes.c_void_p]
    u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    h = u.GetTopWindow(None)
    while h:
        if u.IsWindowVisible(h) and not u.IsIconic(h):
            if not (u.GetWindowLongW(h, -20) & 0x20):   # WS_EX_TRANSPARENT
                cloaked = ctypes.c_uint32(0)
                d.DwmGetWindowAttribute(h, 14,   # DWMWA_CLOAKED
                                        ctypes.byref(cloaked), 4)
                if not cloaked.value:
                    r = ctypes.wintypes.RECT()
                    if u.GetWindowRect(h, ctypes.byref(r)) \
                            and r.left <= x < r.right \
                            and r.top <= y < r.bottom:
                        return h
        h = u.GetWindow(h, 2)   # GW_HWNDNEXT
    # 兜底（理论上走不到）
    u.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
    u.WindowFromPoint.restype = ctypes.c_void_p
    return u.WindowFromPoint(ctypes.wintypes.POINT(x, y)) or 0


def _menu_target_at(x, y):
    """光标处的标题栏是否属于被接管窗口，是则返回目标 hwnd，否则 0。"""
    u = ctypes.windll.user32
    w = _input_window_at(x, y)
    if not w:
        return 0
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetAncestor.restype = ctypes.c_void_p
    root = u.GetAncestor(w, GA_ROOT) or w
    # WM_NCHITTEST：负坐标打包进 lparam 要按 16 位截断回绕。
    # 必须用 SendMessageTimeout：本函数在低级鼠标钩子线程里跑，
    # 同步 SendMessage 撞上忙/挂死的窗口会卡住钩子线程——那会拖死
    # 全系统的鼠标键盘输入（2026-11-05 凌晨全线冻结事故的钩子侧风险）
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    u.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_size_t, ctypes.c_ssize_t,
                                      ctypes.c_uint, ctypes.c_uint,
                                      ctypes.POINTER(ctypes.c_size_t)]
    u.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    result = ctypes.c_size_t(0)
    SMTO_ABORTIFHUNG = 0x0002
    ok = u.SendMessageTimeoutW(root, WM_NCHITTEST, 0, lp,
                               SMTO_ABORTIFHUNG, 200,
                               ctypes.byref(result))
    if not ok:
        return 0
    ht = result.value & 0xFFFF
    if ht in (HTCAPTION, HTSYSMENU) and int(root) in _menu_targets():
        return int(root)
    return 0


def _sysmenu_popup(target):
    """弹出替代菜单并执行所选动作（在宿主窗口线程里跑，模态阻塞无妨）。"""
    u = ctypes.windll.user32
    # 原生菜单若有残影一并取消
    u.PostMessageW(ctypes.c_void_p(target), WM_CANCELMODE, 0, 0)
    menu = u.CreatePopupMenu()
    mon = extscreen_cast.MANAGER.by_hwnd(target)
    if mon:
        u.AppendMenuW(menu, 0, 1, '停止窗口投屏')
        u.AppendMenuW(menu, 0, 2, '复制投屏地址')
    else:
        u.AppendMenuW(menu, 0, 3, '开始窗口投屏')
    u.AppendMenuW(menu, 0x00000800, 0, None)   # MF_SEPARATOR
    u.AppendMenuW(menu, 0, SC_RESTORE, '还原(R)')
    u.AppendMenuW(menu, 0, SC_MOVE, '移动(M)')
    u.AppendMenuW(menu, 0, SC_SIZE, '大小(S)')
    u.AppendMenuW(menu, 0, SC_MINIMIZE, '最小化(N)')
    u.AppendMenuW(menu, 0, SC_MAXIMIZE, '最大化(X)')
    u.AppendMenuW(menu, 0x00000800, 0, None)
    u.AppendMenuW(menu, 0, SC_CLOSE, '关闭(C)\tAlt+F4')
    pt = ctypes.wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    owner = _menu_state['owner']
    # TrackPopupMenu 要求所有者在前台，否则点外面菜单不消失；ALT 技巧解锁
    u.keybd_event(0x12, 0, 0, 0)
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(ctypes.c_void_p(owner))
    cmd = u.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                           pt.x, pt.y, 0, ctypes.c_void_p(owner), None)
    u.DestroyMenu(menu)
    try:
        if cmd == 1 and mon:
            do_async(_stop_one_mon, mon)
        elif cmd == 2 and mon:
            # 剪贴板可能被其他进程占着导致 OpenClipboard 阻塞——
            # 本函数跑在低级钩子线程，阻塞会拖死全系统输入，必须丢后台
            def _copy_url(u_):
                s = u_.snapshot()
                if s.get('url') and _copy_to_clipboard(s['url']):
                    ICON.notify('已复制 %s' % s['url'], APP_NAME)
            do_async(_copy_url, mon)
        elif cmd == 3:
            _start_cast_worker(target)
        elif cmd:
            u.PostMessageW(ctypes.c_void_p(target), WM_SYSCOMMAND, cmd, 0)
    finally:
        _menu_state['active'] = False


def _host_wndproc(hwnd, msg, wparam, lparam):
    """宿主窗口过程：收到 WM_APP_SYSMENU 就弹替代菜单（模态在此处）。"""
    user32 = ctypes.windll.user32
    try:
        if msg == WM_APP_SYSMENU:
            target = int(wparam or 0)
            if target:
                _menu_state['active'] = True
                _sysmenu_popup(target)
            return 0
    except Exception as e:
        _menu_state['active'] = False
        log(f'sysmenu popup: {e}')
    return user32.DefWindowProcW(ctypes.c_void_p(hwnd), ctypes.c_uint(msg),
                                 ctypes.c_size_t(wparam),
                                 ctypes.c_ssize_t(lparam))


def _ll_mouse_cb(ncode, wparam, lparam):
    """低级鼠标钩子：目标窗口标题栏的右键抬起 → 吞掉 + 弹我们的菜单。"""
    user32 = ctypes.windll.user32
    try:
        if ncode == 0 and not _menu_state['active'] \
                and wparam == WM_RBUTTONUP:
            ms = ctypes.cast(ctypes.c_void_p(lparam),
                             ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            target = _menu_target_at(ms.pt.x, ms.pt.y)
            if target:
                _menu_state['active'] = True
                user32.PostMessageW(ctypes.c_void_p(_menu_state['owner']),
                                    WM_APP_SYSMENU, target, 0)
                return 1   # 吞掉：原生菜单不弹出
    except Exception as e:
        log(f'll mouse: {e}')
    return user32.CallNextHookEx(None, ncode, wparam, lparam)


def _ll_key_cb(ncode, wparam, lparam):
    """低级键盘钩子：目标窗口前台时 Alt+Space → 吞掉 + 弹我们的菜单。"""
    user32 = ctypes.windll.user32
    try:
        if ncode == 0 and not _menu_state['active'] \
                and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(ctypes.c_void_p(lparam),
                             ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if kb.vk == 0x20 and (kb.flags & LLKHF_ALTDOWN):
                fg = user32.GetForegroundWindow() or 0
                if fg and int(fg) in _menu_targets():
                    _menu_state['active'] = True
                    user32.PostMessageW(ctypes.c_void_p(_menu_state['owner']),
                                        WM_APP_SYSMENU, int(fg), 0)
                    return 1   # 吞掉：原生 Alt+Space 菜单不弹出
    except Exception as e:
        log(f'll key: {e}')
    return user32.CallNextHookEx(None, ncode, wparam, lparam)


def _menu_hook_thread():
    """宿主窗口 + 低级鼠标/键盘钩子线程（必须有消息泵）。"""
    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    # 显式 argtypes：64 位下默认转换会把句柄/长整型搞溢出（实测踩过）
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassW.restype = ctypes.c_uint16
    user32.CreateWindowExW.argtypes = [
        ctypes.c_uint32, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_size_t, ctypes.c_ssize_t]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_uint, ctypes.c_uint]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_size_t, ctypes.c_ssize_t)
    proc_ref = WNDPROC(_host_wndproc)
    _menu_state['proc_ref'] = proc_ref   # 防 GC（窗口过程 + 两个钩子共用）
    class WNDCLASSW(ctypes.Structure):
        _fields_ = [('style', ctypes.c_uint), ('lpfnWndProc', WNDPROC),
                    ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int),
                    ('hInstance', ctypes.c_void_p), ('hIcon', ctypes.c_void_p),
                    ('hCursor', ctypes.c_void_p),
                    ('hbrBackground', ctypes.c_void_p),
                    ('lpszMenuName', ctypes.c_wchar_p),
                    ('lpszClassName', ctypes.c_wchar_p)]
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc_ref
    wc.lpszClassName = 'ExtScreenMenuHost'
    wc.hInstance = k32.GetModuleHandleW(None)
    if not user32.RegisterClassW(ctypes.byref(wc)):
        log('sysmenu host: RegisterClass failed')
        return
    owner = user32.CreateWindowExW(
        0, wc.lpszClassName, 'ExtScreenMenuHost', 0, 0, 0, 0, 0,
        None, None, wc.hInstance, None)
    _menu_state['owner'] = owner

    MOUSEPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
    mproc = MOUSEPROC(_ll_mouse_cb)
    kproc = MOUSEPROC(_ll_key_cb)
    _menu_state['mproc_ref'] = mproc
    _menu_state['kproc_ref'] = kproc
    mh = user32.SetWindowsHookExW(WH_MOUSE_LL, mproc, None, 0)
    kh = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kproc, None, 0)
    if not mh:
        log('sysmenu hook: SetWindowsHookEx(mouse) failed')
    if not kh:
        log('sysmenu hook: SetWindowsHookEx(keyboard) failed')
    if mh or kh:
        log('sysmenu hooks installed (mouse=%s keyboard=%s)' % (mh, kh))
    # MSG 结构 + 消息泵（低级钩子回调依赖线程消息循环）
    class MSG(ctypes.Structure):
        _fields_ = [('hwnd', ctypes.c_void_p), ('message', ctypes.c_uint),
                    ('wParam', ctypes.c_size_t), ('lParam', ctypes.c_ssize_t),
                    ('time', ctypes.c_uint32),
                    ('ptX', ctypes.c_long), ('ptY', ctypes.c_long)]
    m = MSG()
    while user32.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(m))
        user32.DispatchMessageW(ctypes.byref(m))


CMD_IN = os.path.join(APP_DIR, 'cmd.txt')
CMD_OUT = os.path.join(APP_DIR, 'cmd.out.txt')


def cmd_loop():
    """调试通道：把命令写进 cmd.txt，本程序以管理员身份执行，结果追加到 cmd.out.txt。

    用途是排障时不用一次次手工点菜单。命令执行完 cmd.txt 会自动删除。
    """
    # 启动时丢弃残留命令：「写入时 Dock 没在跑」的命令会残留到下次启动，
    # 一开机就执行——曾发生残留 taskkill 令新实例秒自杀（2026-11-05 实测）
    try:
        if os.path.exists(CMD_IN):
            os.remove(CMD_IN)
            log('cmd: dropped stale command from previous session')
    except Exception:
        pass
    while True:
        try:
            if os.path.exists(CMD_IN):
                with open(CMD_IN, 'r', encoding='utf-8', errors='ignore') as f:
                    cmd = f.read().strip()
                try:
                    os.remove(CMD_IN)
                except Exception:
                    pass
                if cmd:
                    log(f'cmd: {cmd}')
                    proc = subprocess.run(cmd, shell=True, capture_output=True,
                                          timeout=600, creationflags=0x08000000)
                    txt = (proc.stdout or b'').decode('utf-8', 'replace')
                    txt += (proc.stderr or b'').decode('utf-8', 'replace')
                    with open(CMD_OUT, 'a', encoding='utf-8') as f:
                        f.write(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} rc={proc.returncode}\n'
                                f'$ {cmd}\n{txt}\n')
                    log(f'cmd done rc={proc.returncode}')
                    refresh_state()
                    update_icon()
        except Exception as e:
            log(f'cmd_loop error: {e}')
        time.sleep(1.5)


def poll_loop():
    tick = 0
    miss = 0
    last_heal = 0.0
    last_status = ''
    while True:
        try:
            refresh_state(full=(tick % 3 == 0))
            update_icon()
            _caption_ensure()
            _caption_write_state()

            # 状态变化记一笔，排障时不用猜图标/菜单当时是什么状态
            txt = status_text()
            if txt != last_status:
                last_status = txt
                log('status: %s' % txt)

            # 投屏会话逐路监测「运行中→停止」的跳变（菜单/快捷按钮/系统
            # 菜单/出错自停都会走到这）：那一路拉起过的手机逐台 force-stop
            # 恢复原界面，撤 USB 回环；其他路继续投的不受影响
            for m in extscreen_cast.MANAGER.mons:
                r = bool(m.snapshot()['run'])
                if getattr(m, '_wasRun', False) and not r:
                    stop_viewer_for_serials(
                        getattr(m, 'launched_serials', []))
                    m.launched_serials = []   # 清零，下次全新会话
                m._wasRun = r

            # 守护：虚拟屏掉了就重启设备节点（5 分钟内最多一次）
            if STATE['svc'] == SERVICE_RUNNING and not STATE['vdd']:
                miss += 1
                if miss >= 3 and not STATE['busy'] and time.time() - last_heal > 300:
                    last_heal = time.time()
                    miss = 0
                    log('watchdog: 虚拟屏掉线，重启设备节点')
                    do_async(restart_vdd)
            else:
                miss = 0
        except Exception as e:
            log(f'poll: {e}')
        tick += 1
        time.sleep(4)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def dump_driver_log(tag=''):
    """把虚拟显示驱动自己写的日志尾部抄进本程序日志，便于定位挂不出屏的原因。"""
    try:
        d = r'C:\VirtualDisplayDriver\Logs'
        if not os.path.isdir(d):
            log(f'driver log: no Logs dir ({tag})')
            return
        files = [os.path.join(d, f) for f in os.listdir(d)]
        if not files:
            log('driver log: Logs dir is empty')
            return
        newest = max(files, key=os.path.getmtime)
        with open(newest, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        log(f'driver log [{os.path.basename(newest)}] tail:')
        for ln in lines[-40:]:
            log('  | ' + ln.rstrip())
    except Exception as e:
        log(f'driver log read error: {e}')


def startup_selfheal():
    """启动自检：校正配置 → 虚拟屏没挂出就重启设备 → 摆正布局与串流目标。"""
    try:
        ok, msg = ensure_vdd_config()
        log(f'selfheal config: {msg}')
        if not vdd_attached():
            log('selfheal: vdd not attached, restarting device')
            ok2, _ = restart_vdd()
            log(f'selfheal: restart -> attached={ok2}')
            if not ok2:
                log('selfheal: trying device node recreation')
                ok3 = recreate_vdd_device()
                log(f'selfheal: recreate -> attached={ok3}')
                if not ok3 and not vdd_attached():
                    # 重建后驱动是新的，拓扑里还是没有：补一击显式挂出
                    ok3b, m3 = _force_attach_vdd()
                    log(f'selfheal: force-attach -> {m3}')
            dump_driver_log('after selfheal')
        else:
            log('selfheal: vdd already attached')

        # 虚拟屏在就顺手把分辨率/位置和串流目标摆正，省得用户手动开一次
        if vdd_attached():
            ok4, msgs = ensure_layout()
            for m in msgs:
                log('selfheal layout: ' + m)
            ok5, bmsg, changed = bind_sunshine_output()
            log('selfheal bind: ' + bmsg)
            if changed and service_state(SUNSHINE_SVC) == SERVICE_RUNNING:
                log('selfheal: 重启 Sunshine 以应用新的串流目标')
                stop_service()
                # 后台自愈不阻塞界面，这里不等就绪，轮询会自己跟上
                start_service(wait_ready=False)
    except Exception as e:
        log(f'selfheal error: {e}')


def main():
    global ICON

    # 必须在任何坐标运算之前声明 DPI 感知，否则分辨率/摆位会被系统缩放虚拟化，
    # 表现为“设置成功但窗口跑到屏幕外”。
    mode = disp.set_dpi_aware()
    log(f'=== launched admin={is_admin()} dpi={mode} ===')

    if not is_admin() and not os.environ.get('EXTSCREEN_NO_ELEVATE'):
        try:
            args = ' '.join(f'"{a}"' for a in sys.argv)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', sys.executable, args, None, 1)
            log(f'elevate request rc={rc}')
            if rc <= 32:
                log('elevation cancelled or blocked')
                ctypes.windll.user32.MessageBoxW(
                    None,
                    '需要管理员权限才能启动 Sunshine 服务和虚拟显示器。\n\n'
                    '刚才的授权请求被取消或拦截了。请再运行一次，并在弹出的\n'
                    '「用户账户控制」窗口里点“是”。\n\n'
                    f'（错误码 {rc}）',
                    'ExtScreen 副屏助手', 0x30)
        except Exception as e:
            log(f'elevate failed: {e}')
        return

    log('=== start ===')
    refresh_state()
    ICON = pystray.Icon('ExtScreenDock', make_image(status_color()), APP_NAME, MENU)
    threading.Thread(target=startup_selfheal, daemon=True).start()
    threading.Thread(target=cmd_loop, daemon=True).start()
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=_menu_hook_thread, daemon=True).start()
    threading.Thread(target=probe_usb_phone, daemon=True).start()

    log('icon.run() enter')
    try:
        ICON.run()
    except Exception as e:
        log('icon.run() error: %r' % (e,))
        raise
    log('icon.run() exit')


if __name__ == '__main__':
    main()
