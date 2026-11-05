# -*- coding: utf-8 -*-
"""
ExtScreen 窗口投屏（自研后端，不依赖 Deskreen）
------------------------------------------------
  PrintWindow 抓指定窗口 → JPEG → HTTP MJPEG 流 → 手机浏览器/接收器直接看

为什么自研：Deskreen CE 打包版把调试口全关了（--inspect / CDP 都不开），
无法自动读房间号；自研后整个链路零外部依赖、零内部黑盒。
  - 抓帧：Win32 PrintWindow + PW_RENDERFULLCONTENT（能抓被遮挡的窗口，
    DWM 合成内容），失败窗口（最小化/关掉）如实上状态
  - 传输：multipart/x-mixed-replace MJPEG——WebView/浏览器原生支持 <img>，
    无需 JS 信令、无需 WebRTC、无需房间号握手
  - 发现：复用 extscreen_mdns，广播 _extscreen-cast._tcp（TXT path=/）
  - 接收端：同一个「副屏接收器」App 自动发现并打开，手机零操作

延迟 ~100ms 级（LAN/TCP），看文档/终端/弹窗完全够用；画质/帧率/宽度可调。
"""
import ctypes
import io
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from extscreen_core import log, run, load_ext_config, CREATE_NO_WINDOW
from extscreen_mdns import MdnsAnnouncer, _host_label, _lan_ip

CAST_PORT_DEFAULT = 8123

# Win32 常量
PW_RENDERFULLCONTENT = 0x00000002
DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DIB_RGB_COLORS = 0
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
MONITOR_DEFAULTTONEAREST = 2

# 触摸 → 鼠标消息（PostMessage 只投给被投窗口）
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002

# 防激活（无感操作）
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20
LSFW_LOCK = 1
LSFW_UNLOCK = 2

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32
_dwmapi = ctypes.windll.dwmapi


class RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint32), ('rcMonitor', RECT),
                ('rcWork', RECT), ('dwFlags', ctypes.c_uint32)]


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                ('biYPelsPerMeter', ctypes.c_int32),
                ('biClrUsed', ctypes.c_uint32), ('biClrImportant', ctypes.c_uint32)]


# --------------------------------------------------------------------------
# 窗口枚举
# --------------------------------------------------------------------------
def _is_cloaked(hwnd):
    """UWP/虚拟桌面隐藏的窗口按 cloaked 过滤。"""
    val = ctypes.c_uint32(0)
    _dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), DWMWA_CLOAKED,
        ctypes.byref(val), ctypes.sizeof(val))
    return bool(val.value)


def _exe_of_hwnd(hwnd):
    """窗口所属进程的 exe 名（如 notepad.exe），失败返回空串。"""
    k32 = ctypes.windll.kernel32
    pid = ctypes.c_uint32(0)
    _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),
                                     ctypes.byref(pid))
    if not pid.value:
        return ''
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32)]
    hp = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                         pid.value)
    if not hp:
        return ''
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = ctypes.c_uint32(512)
        if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)):
            return buf.value.replace('\\', '/').rsplit('/', 1)[-1]
        return ''
    finally:
        k32.CloseHandle(hp)


def list_windows():
    """返回可分享的顶层窗口 [(hwnd, title, exe)]，按 Z 序（越靠前越新）。

    exe 用于选择器里标注窗口属于哪个应用（2026-11-05 用户要求）。
    """
    result = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def on_window(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if _is_cloaked(hwnd):
            return True
        # WS_EX_TOOLWINDOW（工具条/提示气泡）不列
        ex = _user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
        if ex & 0x00000080:
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        # 挂到扩展屏(7280,0)外的窗口也算。
        # WINDOWPLACEMENT: length(0) flags(1) showCmd(2)…——showCmd 是第 3 个
        # UINT（下标 2）。showCmd 语义：1=正常 2=SW_SHOWMINIMIZED
        # 3=SW_SHOWMAXIMIZED 7=SW_SHOWMINNOACTIVE——**3 是最大化不是最小化**，
        # 之前把 3 也当最小化排除，导致所有最大化的窗口从选择列表消失
        # （2026-11-05 用户报"能投的窗口漏了几个"，实测抓到）。
        buf2 = (ctypes.c_uint32 * 16)()
        buf2[0] = 60   # length
        ctypes.windll.user32.GetWindowPlacement(hwnd, buf2)
        if buf2[2] in (2, 7):   # SW_SHOWMINIMIZED / SW_SHOWMINNOACTIVE
            # 最小化窗口也列出（PrintWindow 能抓离屏内容，start 时会先还原），
            # 标题加后缀提示；之前直接排除导致最小化的浏览器全找不到
            title += '（最小化）'
        result.append((hwnd, title, _exe_of_hwnd(hwnd)))
        return True

    _user32.EnumWindows(on_window, None)
    return result


def window_still_valid(hwnd):
    return bool(_user32.IsWindow(hwnd)) and not _is_cloaked(hwnd)


def _window_rects(hwnd):
    """返回 (可见外框 rc, 真实窗口 rect wr)。DWM 失败时两者相同。"""
    rc, wr = RECT(), RECT()
    if _dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rc), ctypes.sizeof(rc)) != 0:
        _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rc))
        return rc, rc
    _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(wr))
    return rc, wr


def fit_window_to_aspect(hwnd, aspect_w, aspect_h, fit_w):
    """把窗口可见区域调成 aspect_w:aspect_h（可见宽 ≈ fit_w 像素）。

    保留窗口左上角位置；大小不超过所在显示器工作区（留 20px 边距）。
    返回是否成功。
    """
    if not hwnd or fit_w <= 0 or aspect_w <= 0 or aspect_h <= 0:
        return False
    if not window_still_valid(hwnd):
        return False
    rc, wr = _window_rects(hwnd)
    # 不可见边框/阴影差
    dx = max(0, (wr.right - wr.left) - (rc.right - rc.left))
    dy = max(0, (wr.bottom - wr.top) - (rc.bottom - rc.top))
    cur_w = max(1, rc.right - rc.left)
    cur_h = max(1, rc.bottom - rc.top)
    ratio = float(aspect_w) / float(aspect_h)
    # 尽量保持窗口大小：只动一边（2026-11-05 用户要求）。
    # 候选 A 保持宽调高，候选 B 保持高调宽，取总变化小的那个；
    # fit_w 仅作为窗口过小（<200）时的回退宽度
    h_from_w = int(round(cur_w / ratio))
    w_from_h = int(round(cur_h * ratio))
    if abs(h_from_w - cur_h) <= abs(w_from_h - cur_w):
        vis_w, vis_h = cur_w, h_from_w
    else:
        vis_w, vis_h = w_from_h, cur_h
    if min(vis_w, vis_h) < 120:
        vis_w = int(fit_w)
        vis_h = int(round(vis_w * ratio))
    out_w, out_h = vis_w + dx, vis_h + dy
    x, y = rc.left, rc.top
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = _user32.MonitorFromWindow(
        ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
    if hmon and _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        wa = mi.rcWork
        max_w = max(200, (wa.right - wa.left) - 40)
        max_h = max(200, (wa.bottom - wa.top) - 40)
        if out_w > max_w or out_h > max_h:
            k = min(max_w / float(out_w), max_h / float(out_h))
            vis_w, vis_h = int(vis_w * k), int(vis_h * k)
            out_w, out_h = int(out_w * k), int(out_h * k)
        # 位置夹回工作区
        x = min(max(rc.left, wa.left), max(wa.left, wa.right - out_w))
        y = min(max(rc.top, wa.top), max(wa.top, wa.bottom - out_h))
    ok = bool(_user32.SetWindowPos(
        ctypes.c_void_p(hwnd), None, x, y, out_w, out_h,
        SWP_NOZORDER | SWP_NOACTIVATE))
    if ok:
        log('cast: window fitted to %dx%d (aspect %dx%d, vis %dx%d)'
            % (out_w, out_h, aspect_w, aspect_h, vis_w, vis_h))
    return ok


# --------------------------------------------------------------------------
# 抓帧
# --------------------------------------------------------------------------
def _capture_window(hwnd):
    """PrintWindow 抓窗口，返回 PIL Image（RGB）或 None。"""
    rc = RECT()
    r = _dwmapi.DwmGetWindowAttribute(
        ctypes.c_void_p(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rc), ctypes.sizeof(rc))
    if r != 0:
        _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rc))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None

    hdc_window = _user32.GetWindowDC(hwnd)
    if not hdc_window:
        return None
    mem = _gdi32.CreateCompatibleDC(hdc_window)
    bmp = _gdi32.CreateCompatibleBitmap(hdc_window, w, h)
    old = _gdi32.SelectObject(mem, bmp)
    ok = _user32.PrintWindow(ctypes.c_void_p(hwnd), mem, PW_RENDERFULLCONTENT)
    img = None
    if ok:
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h          # 自上而下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0      # BI_RGB
        buf = ctypes.create_string_buffer(w * 4 * h)
        if _gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi),
                            DIB_RGB_COLORS):
            try:
                img = Image.frombuffer('RGB', (w, h), buf.raw, 'raw', 'BGRX')
            except Exception:
                img = None
    _gdi32.SelectObject(mem, old)
    _gdi32.DeleteObject(bmp)
    _gdi32.DeleteDC(mem)
    _user32.ReleaseDC(hwnd, hdc_window)
    return img


def _encode_frame(img, out_width, quality):
    """缩放到目标宽度并编码 JPEG，返回 (bytes, w, h)。"""
    w, h = img.size
    if out_width and w > out_width:
        h2 = round(h * out_width / w)
        img = img.resize((out_width, h2), Image.BILINEAR)
        w, h = out_width, h2
    bio = io.BytesIO()
    img.convert('RGB').save(bio, 'JPEG', quality=quality)
    return bio.getvalue(), w, h


class WindowCapture(threading.Thread):
    """持续抓取一个窗口，产出最新 JPEG 帧。"""

    def __init__(self, hwnd, out_width=720, fps=20, quality=70):
        super().__init__(name='cast-capture', daemon=True)
        self.hwnd = hwnd
        self.out_width = out_width
        self.fps = max(5, min(30, int(fps)))
        self.quality = quality
        self._lock = threading.Lock()
        self._jpeg = b''
        self._seq = 0
        self._w = self._h = 0
        self._stop = False
        self.err = ''
        self._ph_key = None
        self._ph_jpeg = None
        self._ph_wh = (0, 0)

    def get_frame(self):
        with self._lock:
            return self._seq, self._jpeg, self._w, self._h

    def _placeholder(self, msg):
        """提示帧：窗口最小化/关闭时手机端看到白字黑底说明，而不是黑屏。"""
        if self._ph_key == msg:
            return self._ph_jpeg, self._ph_wh
        from PIL import Image, ImageDraw, ImageFont
        w = self.out_width or 720
        h = round(w * 1.4)
        img = Image.new('RGB', (w, h), (15, 17, 23))
        d = ImageDraw.Draw(img)
        font = None
        size = max(22, w // 16)
        for cand in (r'C:\Windows\Fonts\msyh.ttc',
                     r'C:\Windows\Fonts\simhei.ttf'):
            try:
                font = ImageFont.truetype(cand, size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        lines = msg.split('\n')
        y = h / 2 - len(lines) * size
        for ln in lines:
            tw = d.textlength(ln, font=font)
            d.text(((w - tw) / 2, y), ln, fill=(235, 235, 235), font=font)
            y += size + size * 0.6
        bio = io.BytesIO()
        img.save(bio, 'JPEG', quality=80)
        self._ph_key, self._ph_jpeg, self._ph_wh = msg, bio.getvalue(), (w, h)
        return self._ph_jpeg, self._ph_wh

    def stop(self):
        self._stop = True

    def run(self):
        interval = 1.0 / self.fps
        fails = 0
        while not self._stop:
            t0 = time.time()
            # 窗口最小化/关闭时推提示帧，手机端能看到白字说明而不是纯黑
            if not window_still_valid(self.hwnd):
                msg = '投屏窗口已关闭\n请在电脑托盘停止或重新选择窗口'
            elif _user32.IsIconic(self.hwnd):
                msg = '窗口已最小化\n在电脑上还原该窗口即可继续观看'
            else:
                msg = None
            if msg:
                self.err = msg.replace('\n', ' ')
                jpeg, (w, h) = self._placeholder(msg)
                with self._lock:
                    self._jpeg, self._w, self._h = jpeg, w, h
                    self._seq += 1
                time.sleep(0.5)
                continue
            self.err = ''
            img = _capture_window(self.hwnd)
            if img is None:
                fails += 1
                if fails % 50 == 1:
                    log('cast: capture failed (hwnd=%s) x%d'
                        % (self.hwnd, fails))
                time.sleep(0.25)
                continue
            fails = 0
            try:
                jpeg, w, h = _encode_frame(img, self.out_width, self.quality)
            except Exception as e:
                log('cast encode: %s' % e)
                time.sleep(0.25)
                continue
            with self._lock:
                self._jpeg, self._w, self._h = jpeg, w, h
                self._seq += 1
            dt = time.time() - t0
            if dt < interval:
                time.sleep(interval - dt)


def _get_exstyle(hwnd):
    get = getattr(_user32, 'GetWindowLongPtrW', None) or _user32.GetWindowLongW
    return get(ctypes.c_void_p(hwnd), GWL_EXSTYLE)


def _set_exstyle(hwnd, ex):
    setf = getattr(_user32, 'SetWindowLongPtrW', None) or _user32.SetWindowLongW
    return setf(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)


CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPTRANSPARENT = 0x0002


def _resolve_input_target(hwnd, cx, cy):
    """把顶层窗口客户坐标解析到实际接收鼠标输入的子窗口，返回 (hwnd, x, y)。

    真实输入时系统 hit-test 会把鼠标消息直接送给光标下的子窗口（资源
    管理器的文件列表 DirectUIHWND、记事本的编辑框……），而 PostMessage
    到顶层窗口后 DefWindowProc 不会转发鼠标消息——表现为浏览器能控
    （整个 UI 一个 HWND）、其他窗口点了没反应。逐层走 ChildWindowFromPointEx
    找到最内层窗口，并把坐标映射到它的客户区。
    """
    pt = POINT(cx, cy)
    cur = hwnd
    for _ in range(8):   # 窗口链没那么深，防御个上限
        child = _user32.ChildWindowFromPointEx(
            ctypes.c_void_p(cur), pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
        if not child or int(child) == int(cur):
            break
        p = POINT(pt.x, pt.y)
        _user32.MapWindowPoints(ctypes.c_void_p(cur), child,
                                ctypes.byref(p), 1)
        cur = int(child)
        pt = p
    return cur, max(0, pt.x), max(0, pt.y)


def set_window_noactivate(hwnd, on):
    """挂/摘 WS_EX_NOACTIVATE：点击（包括真实点击）不再把窗口激活到前台。

    返回原 exstyle 供恢复。Chromium 等应用收到鼠标按下会自己调
    SetForegroundWindow 抢前台（实测复现），这个样式是第一道闸。
    """
    ex = _get_exstyle(hwnd)
    if on:
        _set_exstyle(hwnd, ex | WS_EX_NOACTIVATE)
    else:
        _set_exstyle(hwnd, ex & ~WS_EX_NOACTIVATE)
    return ex


def lock_foreground(lock):
    """LockSetForegroundWindow：锁住/解锁「程序化抢前台」的通道。

    用户按 ALT 或自己点别的窗口时系统会自动解锁——那时用户主动操作
    优先，属预期行为。返回是否成功。
    """
    return bool(_user32.LockSetForegroundWindow(
        LSFW_LOCK if lock else LSFW_UNLOCK))


# --------------------------------------------------------------------------
# MJPEG 服务
# --------------------------------------------------------------------------
# 手机端观众页：无屏幕标题；触摸事件归一化后 POST /touch 回传 PC，
# PC 只对被投窗口 PostMessage 鼠标消息（不影响桌面/不抢前台）
VIEWER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,
maximum-scale=1,user-scalable=no">
<title>{title}</title>
<style>
  html,body{{margin:0;height:100%;background:#000;overflow:hidden}}
  /* 不能用 inset 简写属性——Chrome 87 才支持，Note8 WebView 是 Chrome 74
     会整条忽略导致横屏时定位错乱、右侧留黑（2026-11-05 实测踩坑） */
  #wrap{{position:fixed;top:0;left:0;width:100%;height:100%}}
  /* object-fit: contain 任意朝向都铺满视口、比例不变形（Chrome 32+） */
  img{{width:100%;height:100%;object-fit:contain;display:block}}
</style></head><body>
<div id="wrap"><img id="v" src="/stream"></div>
<script>
// 断流自动重连：img 出错 1 秒后换缓存戳重拉
var v = document.getElementById('v');
v.onerror = function () {{
  setTimeout(function () {{
    v.src = '/stream?ts=' + Date.now();
  }}, 1000);
}};
// 触摸 → PC 鼠标：单指=左键，双指=右键；坐标按画面归一化
var lastMove = 0, twoFinger = false;
function sendTouch(o) {{
  var s = JSON.stringify(o);
  try {{
    // Chrome 74 的 fetch() 在部分 WebView 上同源 POST 也会
    // "TypeError: Failed to fetch"（实测踩过），XHR 走的是另一条
    // 更成熟的老路径，稳妥
    var x = new XMLHttpRequest();
    x.open('POST', '/touch', true);
    x.setRequestHeader('Content-Type', 'text/plain');
    x.send(s);
  }} catch (e) {{}}
}}
function norm(t) {{
  // object-fit:contain 的黑边在 img 元素盒内部，触摸坐标要按
  // 「元素盒内的实际内容区」归一化，不能直接用元素盒
  var r = v.getBoundingClientRect();
  var nw = v.naturalWidth, nh = v.naturalHeight;
  var ox = r.left, oy = r.top;
  var cw = r.width, ch = r.height;
  if (nw > 0 && nh > 0 && r.width > 0 && r.height > 0) {{
    var k = Math.min(r.width / nw, r.height / nh);
    cw = nw * k;
    ch = nh * k;
    ox = r.left + (r.width - cw) / 2;
    oy = r.top + (r.height - ch) / 2;
  }}
  var w = (cw > 0) ? cw : window.innerWidth;
  var h = (ch > 0) ? ch : window.innerHeight;
  return {{
    x: Math.max(0, Math.min(1, (t.clientX - ox) / w)),
    y: Math.max(0, Math.min(1, (t.clientY - oy) / h))
  }};
}}
// 监听挂 body：画面没出来（img 0x0）时触摸也不至于全部落空
window._ts = 0; window._up = 0;
document.body.addEventListener('touchstart', function (e) {{
  e.preventDefault();
  window._ts++;
  var p = norm(e.touches[0]);
  twoFinger = e.touches.length >= 2;
  sendTouch({{a: 'down', b: twoFinger ? 'r' : 'l', x: p.x, y: p.y}});
}}, {{passive: false}});
document.body.addEventListener('touchmove', function (e) {{
  e.preventDefault();
  var now = Date.now();
  if (now - lastMove < 40) return;   // 拖动节流 ~25Hz
  lastMove = now;
  var p = norm(e.touches[0]);
  sendTouch({{a: 'move', b: twoFinger ? 'r' : 'l', x: p.x, y: p.y}});
}}, {{passive: false}});
document.body.addEventListener('touchend', function (e) {{
  e.preventDefault();
  window._up++;
  var p = norm(e.changedTouches[0]);
  sendTouch({{a: 'up', b: twoFinger ? 'r' : 'l', x: p.x, y: p.y}});
  twoFinger = e.touches.length >= 2;
}}, {{passive: false}});
</script></body></html>"""


class CastHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    # server 上挂 monitor 引用
    @property
    def mon(self):
        return self.server.monitor

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/' or path == '/index.html':
            self._html()
        elif path == '/stream':
            self._stream()
        elif path == '/info':
            self._info()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split('?')[0]
        if path in ('/device', '/touch'):
            try:
                n = int(self.headers.get('Content-Length') or 0)
                data = json.loads(
                    self.rfile.read(n).decode('utf-8', 'ignore') or '{}')
                if not isinstance(data, dict):
                    data = {}
            except Exception:
                data = {}
            if self.mon:
                if path == '/device':
                    self.mon.set_device(data)
                else:
                    self.mon.handle_touch(data)
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def _html(self):
        body = VIEWER_HTML.format(title=self._title()).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _info(self):
        mon = self.mon
        seq, _, w, h = mon.caster.get_frame() if mon.caster else (0, b'', 0, 0)
        dev = dict(mon.device) if mon.device else {}
        body = json.dumps({
            'title': self._title(), 'w': w, 'h': h,
            'clients': mon.clients, 'fps': mon.fps,
            'device': dev,
        }).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _title(self):
        return self.mon.title if self.mon else 'ExtScreen Cast'

    def _stream(self):
        mon = self.mon
        caster = mon.caster if mon else None
        if not caster:
            self.send_error(503)
            return
        mon.clients += 1
        try:
            self.send_response(200)
            self.send_header('Content-Type',
                             'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            last_seq = -1
            # 注意引用局部 caster：stop() 会把 mon.caster 置 None，
            # 直接读 mon.caster.is_alive() 会竞态炸掉（实测踩过）
            while caster.is_alive():
                seq, jpeg, w, h = caster.get_frame()
                if seq != last_seq and jpeg:
                    last_seq = seq
                    self.wfile.write(
                        b'\r\n--frame\r\nContent-Type: image/jpeg\r\n'
                        b'Content-Length: %d\r\n\r\n' % len(jpeg))
                    self.wfile.write(jpeg)
                    self.wfile.flush()
                time.sleep(0.05)   # 帧率上限 20fps，无新帧时轮询等待
        except (ConnectionError, OSError):
            pass
        finally:
            mon.clients = max(0, mon.clients - 1)

    def log_message(self, fmt, *args):
        # 全量请求日志（/stream 一请求一行，量不大）；排障必备。
        # 带上端口：多会话时日志里分得清请求打到了哪一路（实测踩过）
        try:
            line = fmt % args
        except Exception:
            line = str(fmt)
        try:
            port = self.server.server_address[1]
        except Exception:
            port = 0
        log('cast http[%d]: %s' % (port, line.splitlines()[0][:120]) if port
            else 'cast http: %s' % line.splitlines()[0][:120])


class CastServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, monitor):
        super().__init__(addr, CastHandler)
        self.monitor = monitor


# --------------------------------------------------------------------------
# 总管（单例，由 Dock 持有）
# --------------------------------------------------------------------------
class CastMonitor:
    def __init__(self):
        self._lock = threading.RLock()
        self.caster = None
        self.server = None
        self._mdns = None
        self.title = ''
        self.hwnd = None
        self.fps = 20
        self.clients = 0
        self.device = None          # 手机上报 {'w','h','rot','ts'}
        self._fg_locked = False   # LockSetForegroundWindow 是否已挂
        self.state = {
            'run': False, 'title': '', 'port': 0, 'url': '',
            'clients': 0, 'err': '', 'device': '',
        }

    def snapshot(self):
        with self._lock:
            s = dict(self.state)
            s['clients'] = self.clients
            if self.caster:
                s['err'] = self.caster.err or s['err']
            return s

    def set_device(self, d):
        """接收器上报手机屏幕信息；比例变化时按配置适配窗口。"""
        try:
            w = int(d.get('w') or 0)
            h = int(d.get('h') or 0)
            rot = int(d.get('rot') or 0)
        except Exception:
            return
        if w <= 0 or h <= 0:
            return
        with self._lock:
            old = dict(self.device) if self.device else None
            self.device = {'w': w, 'h': h, 'rot': rot, 'ts': time.time()}
            self.state['device'] = '%dx%d%s' % (w, h, '横' if w > h else '竖')
            cfg = load_ext_config()
            follow = bool(cfg.get('castFitFollowRotate', True))
            if not old:
                # 首台设备：按它的屏幕定窗口比例
                do_fit = bool(cfg.get('castFitWindow', True)) and self.hwnd
            elif {old.get('w'), old.get('h')} == {w, h} \
                    and (old.get('w'), old.get('h')) != (w, h):
                # 同一台设备旋转（宽高互换）：跟随与否看配置
                do_fit = (follow and bool(cfg.get('castFitWindow', True))
                          and self.hwnd)
            else:
                # 另一台设备加入了观看：绝不挪窗口——多观众比例各异，
                # 谁后到谁说了算会把窗口拉来拉去（2026-11-05 实测两台
                # 手机交叉上报把两个窗口尺寸都搞乱）
                do_fit = False
                log('cast: 2nd device %dx%d joined, window not refitted'
                    % (w, h))
            hwnd, fit_w = self.hwnd, int(cfg.get('castFitWidth') or 700)
        if do_fit:
            fit_window_to_aspect(hwnd, w, h, fit_w)

    def handle_touch(self, d):
        """手机触摸 → 转成鼠标消息，只投递给被投窗口（PostMessage）。

        安全边界（用户明确要求）：
          - 绝不用 SendInput / SetCursorPos / mouse_event——那些动的是
            真实光标，会干扰桌面上的其他窗口；
          - PostMessage 不会激活窗口：被投窗口在后台时，点击不会把它
            顶到前台，Z 序完全不变；
          - 坐标按画面归一化值换算成窗口客户区坐标，超出夹回客户区。
        """
        with self._lock:
            hwnd = self.hwnd
            enabled = bool(load_ext_config().get('castTouchControl', True))
        if not enabled or not hwnd:
            return
        try:
            action = d.get('a')
            btn = 'r' if d.get('b') == 'r' else 'l'
            fx = float(d.get('x', 0))
            fy = float(d.get('y', 0))
        except Exception:
            return
        if action not in ('down', 'move', 'up'):
            return
        if not window_still_valid(hwnd):
            return
        fx = min(1.0, max(0.0, fx))
        fy = min(1.0, max(0.0, fy))
        # 画面=窗口可见外框（含标题栏），换算成客户区坐标
        rc, _ = _window_rects(hwnd)
        pt = POINT(0, 0)
        crect = RECT()
        _user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt))
        _user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(crect))
        cx = int(rc.left + fx * (rc.right - rc.left)) - pt.x
        cy = int(rc.top + fy * (rc.bottom - rc.top)) - pt.y
        cx = min(max(0, cx), max(0, crect.right - 1))
        cy = min(max(0, cy), max(0, crect.bottom - 1))
        # 命中解析：实际接收输入的可能是一个子窗口（资源管理器/记事本等），
        # 消息必须发给那个子窗口、坐标用它的客户区
        target, tx, ty = _resolve_input_target(hwnd, cx, cy)
        lparam = ((ty & 0xFFFF) << 16) | (tx & 0xFFFF)
        if btn == 'r':
            down, up, mk = WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON
        else:
            down, up, mk = WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON
        if action == 'down':
            _user32.PostMessageW(ctypes.c_void_p(target), down, mk, lparam)
            # 无感操作第二道闸：LockSetForegroundWindow 只有前台进程能调
            # （Dock 在后台调返回 False，实测无效），改用「恢复前台」——
            # 触摸后 0.2s 若前台被抢走，把原前台窗口拉回来
            fg = _user32.GetForegroundWindow()
            if fg and int(fg) not in (int(hwnd), int(target)):
                self._restore_fg_later(int(fg))
        elif action == 'up':
            _user32.PostMessageW(ctypes.c_void_p(target), up, 0, lparam)
        else:   # move（触摸拖动=按键按住）
            _user32.PostMessageW(ctypes.c_void_p(target),
                                 WM_MOUSEMOVE, mk, lparam)

    def _restore_fg_later(self, fg):
        """0.2s 后把被手机触摸抢走的前台还给原窗口。

        AttachThreadInput 让我们临时共享前台窗口的输入状态，
        SetForegroundWindow 就能成功（比按 ALT 键干净，不动真实键盘）。
        detach 放 finally：粘着不放手会让目标进程和我们的输入队列
        互相拖累（输入冻结类故障的经典来源）。
        """
        def run():
            me = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = False
            try:
                time.sleep(0.2)
                cur = _user32.GetForegroundWindow()
                if not fg or cur == fg:
                    return
                pid = ctypes.c_uint32(0)
                ftid = _user32.GetWindowThreadProcessId(
                    ctypes.c_void_p(fg), ctypes.byref(pid))
                if not ftid:
                    return
                attached = bool(_user32.AttachThreadInput(me, ftid, True))
                if attached:
                    _user32.SetForegroundWindow(ctypes.c_void_p(fg))
            except Exception:
                pass
            finally:
                if attached:
                    try:
                        _user32.AttachThreadInput(me, ftid, False)
                    except Exception:
                        pass
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _port_busy(port):
        """端口是否已有服务（Windows 下 SO_REUSEADDR 会让 bind「成功」，
        实际包全被先来的进程收走——必须先探测，实测踩过）。"""
        if not port:
            return False
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect(('127.0.0.1', port))
            s.close()
            return True
        except OSError:
            return False

    def start(self, hwnd, title, port=None):
        with self._lock:
            if self.server:
                # 直接切换目标窗口，不让用户先手动停止
                log('cast: switching window -> %s' % title)
                self.stop()
            cfg = load_ext_config()
            port = int(port or cfg.get('castPort') or CAST_PORT_DEFAULT)
            out_w = int(cfg.get('castWidth') or 720)
            fps = int(cfg.get('castFps') or 20)
            quality = int(cfg.get('castQuality') or 70)
            noactivate = bool(cfg.get('castNoActivate', True))
            # 端口被占则让系统分一个（先探测再绑，防 Windows 双绑假成功）
            try:
                if self._port_busy(port):
                    raise OSError('port busy')
                srv = CastServer(('0.0.0.0', port), self)
            except OSError:
                port = 0
                srv = CastServer(('0.0.0.0', 0), self)
                port = srv.server_address[1]
            self.server = srv
            self.hwnd, self.title = hwnd, title
            # 最小化的窗口先还原再抓：最小化状态下 PrintWindow 很多程序
            # 出旧帧/空白，还原后画面才可靠（选中即还原，副屏用法合理）
            try:
                pl = (ctypes.c_uint32 * 16)()
                pl[0] = 60
                _user32.GetWindowPlacement(ctypes.c_void_p(hwnd), pl)
                if pl[2] in (2, 7):   # SW_SHOWMINIMIZED / SW_SHOWMINNOACTIVE
                    _user32.ShowWindow(ctypes.c_void_p(hwnd), 9)   # SW_RESTORE
                    time.sleep(0.25)
                    log('cast: restored minimized window before capture')
            except Exception:
                pass
            self.fps = fps
            self.caster = WindowCapture(hwnd, out_w, fps, quality)
            self.caster.start()
            threading.Thread(target=srv.serve_forever, daemon=True,
                             name='cast-http').start()
            self._ensure_firewall(port)
            self._publish_mdns(port)
            # 无感操作：只锁「程序化抢前台」通道（LockSetForegroundWindow），
            # 手机点击不会把后台窗口顶到前台（Chromium 会自己抢，实测复现，
            # 也实测只靠这一招就挡得住）。
            # 千万不要挂 WS_EX_NOACTIVATE 窗口样式——它会同时把窗口从
            # 任务栏摘掉、并让用户自己点击窗口也无法激活（两个都是实测踩坑）。
            self._fg_locked = False
            if noactivate:
                self._fg_locked = lock_foreground(True)
                log('cast: fg lock=%s' % self._fg_locked)
            # 清掉历史残留的 WS_EX_NOACTIVATE：旧版本会挂这个样式且进程
            # 被强杀时无法恢复，表现为任务栏按钮消失 + 点击无反应
            try:
                ex_now = _get_exstyle(hwnd)
                if ex_now & WS_EX_NOACTIVATE:
                    _set_exstyle(hwnd, ex_now & ~WS_EX_NOACTIVATE)
                    log('cast: cleared stale WS_EX_NOACTIVATE')
            except Exception:
                pass
            ip = _lan_ip()
            self.state.update(run=True, title=title, port=port,
                              url='http://%s:%d/' % (ip, port),
                              clients=0, err='')
            log('cast started: hwnd=%s port=%d title=%s' % (hwnd, port, title))
            return True, ('窗口投屏已开始：%s\n手机端会自动打开画面。' % title)

    def stop(self):
        with self._lock:
            caster, server, self.caster, self.server = \
                self.caster, self.server, None, None
            if self._mdns:
                self._mdns.stop()
                self._mdns = None
            self.hwnd = None
            self.title = ''
            self.device = None
            self.state.update(run=False, title='', port=0, url='',
                              clients=0, err='', device='')
        if caster:
            caster.stop()
        if server:
            server.shutdown()
            server.server_close()   # 不 close 的话监听 socket 变僵尸：端口占着、连接只进 backlog 没人处理（实测 /info 超时）
        lock_foreground(False)
        return True, '窗口投屏已停止'

    def _publish_mdns(self, port):
        with self._lock:
            if self._mdns:
                self._mdns.stop()
            # 实例名带随机后缀：每次启动都是「新服务」，
            # 避免手机 mdnssd 缓存里上一轮（可能是坏的）记录压着新记录
            import secrets
            inst = 'ExtScreen-%s-%04x' % (_host_label(),
                                          secrets.randbelow(0xffff))
            self._mdns = MdnsAnnouncer(
                inst, port,
                {'path': '/', 'proto': 'http',
                 'name': 'ExtScreen Cast', 'ver': str(int(time.time()))},
                svc_type='extscreen-cast')
            if not self._mdns.start():
                log('cast: mdns announce failed')

    def _ensure_firewall(self, port):
        # 规则按「本程序」放行（Dock exe 即服务进程）；
        # UDP 5353 是 mDNS 应答口——手机查询包进来必须放行，否则永远发现不了
        prog = self._own_exe()
        if not prog:
            return
        rules = [
            ('ExtScreen Cast TCP', 'TCP', port),
            ('ExtScreen mDNS', 'UDP', 5353),
        ]
        for name, proto, lport in rules:
            cmd = ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                   'name=%s' % name, 'dir=in', 'action=allow',
                   'protocol=%s' % proto, 'program=%s' % prog]
            if lport:
                cmd.append('localport=%d' % lport)
            rc, _ = run(cmd, timeout=30)
            if rc != 0:
                run(['netsh', 'advfirewall', 'firewall', 'delete', 'rule',
                     'name=%s' % name], timeout=30)
                rc2, _ = run(cmd, timeout=30)
                if rc2 != 0:
                    log('cast: firewall rule failed: %s' % name)

    @staticmethod
    def _own_exe():
        if getattr(__import__('sys'), 'frozen', False):
            return __import__('sys').executable
        return ''   # 源码调试时不加规则，真机打包后才有意义


MONITOR = CastMonitor()


class CastManager:
    """多路投屏总管：每路一个 CastMonitor（各自端口/抓屏/mDNS/手机）。

    第 0 路沿用老单例 MONITOR（旧引用语义不变）；新会话追加新实例。
    端口从 castPort(8123) 起顺延分配，避开其他会话和被占端口。
    """
    MAX = 4

    def __init__(self):
        self.mons = [MONITOR]

    def running(self):
        return [m for m in self.mons if m.snapshot()['run']]

    def by_hwnd(self, hwnd):
        try:
            h = int(hwnd)
        except (TypeError, ValueError):
            return None
        for m in self.mons:
            if m.snapshot()['run'] and int(m.hwnd or 0) == h:
                return m
        return None

    def launched_serials(self):
        """所有「在投」会话拉起过的手机 serial 并集。"""
        out = []
        for m in self.running():
            out += list(getattr(m, 'launched_serials', []) or [])
        return out

    def acquire(self, hwnd, title):
        """为窗口拿一个投屏槽：已在投的窗口复用原路（切窗口语义），
        否则复用空闲槽，满了且未达上限则新建，达上限返回 None。"""
        m = self.by_hwnd(hwnd)
        if m:
            return m
        for m in self.mons:
            if not m.snapshot()['run']:
                return m
        if len(self.mons) >= self.MAX:
            return None
        m = CastMonitor()
        self.mons.append(m)
        return m

    def next_port(self):
        """新会话端口：castPort 起顺延，跳过本管理器内已占用的端口。"""
        cfg = load_ext_config()
        base = int(cfg.get('castPort') or CAST_PORT_DEFAULT)
        used = {int(m.snapshot().get('port') or 0) for m in self.mons}
        port = base
        for _ in range(self.MAX * 2):
            if port not in used and not self._port_busy(port):
                return port
            port += 1
        return 0   # 让 start() 自己走系统分配兜底

    @staticmethod
    def _port_busy(port):
        if not port:
            return False
        s = socket.socket()
        s.settimeout(0.6)
        try:
            s.connect(('127.0.0.1', port))
            s.close()
            return True
        except OSError:
            return False


MANAGER = CastManager()


def action_start(hwnd, title):
    return MONITOR.start(hwnd, title)


def action_stop():
    return MONITOR.stop()


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def _selftest():
    wins = list_windows()
    assert isinstance(wins, list)
    print('visible windows: %d' % len(wins))
    for hwnd, title, _exe in wins[:5]:
        print('  hwnd=%s %s' % (hwnd, title[:50]))
    # HTML 模板渲染
    html = VIEWER_HTML.format(title='测试')
    assert '/stream' in html and 'onerror' in html
    # 抓一张真窗口（抓桌面任一可见窗口）
    if wins:
        img = _capture_window(wins[0][0])
        assert img is not None and img.size[0] > 0, 'capture failed'
        jpeg, w, h = _encode_frame(img, 720, 70)
        assert jpeg[:2] == b'\xff\xd8', 'jpeg magic missing'
        print('capture %s -> %dx%d jpeg %dKB'
              % (wins[0][1][:30], w, h, len(jpeg) // 1024))
    print('extscreen_cast selftest OK')


if __name__ == '__main__':
    _selftest()
