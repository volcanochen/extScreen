# -*- coding: utf-8 -*-
"""ExtScreen 副屏助手 —— 托盘主程序。"""
import os
import re
import sys
import json
import time
import threading
import subprocess
import ctypes

import pystray
from PIL import Image, ImageDraw

import extscreen_display as disp
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
    for _ in range(24):
        if service_state(SUNSHINE_SVC) == SERVICE_STOPPED:
            return True, 'Sunshine 已停止'
        time.sleep(0.5)
    return False, '停止 Sunshine 超时'


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
    return False, '虚拟屏重启后仍未挂出'


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
    pystray.MenuItem('退出', lambda icon=None, item=None: icon.stop()),
)


CMD_IN = os.path.join(APP_DIR, 'cmd.txt')
CMD_OUT = os.path.join(APP_DIR, 'cmd.out.txt')


def cmd_loop():
    """调试通道：把命令写进 cmd.txt，本程序以管理员身份执行，结果追加到 cmd.out.txt。

    用途是排障时不用一次次手工点菜单。命令执行完 cmd.txt 会自动删除。
    """
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

            # 状态变化记一笔，排障时不用猜图标/菜单当时是什么状态
            txt = status_text()
            if txt != last_status:
                last_status = txt
                log('status: %s' % txt)

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
    log('icon.run() enter')
    try:
        ICON.run()
    except Exception as e:
        log('icon.run() error: %r' % (e,))
        raise
    log('icon.run() exit')


if __name__ == '__main__':
    main()
