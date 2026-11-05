# -*- coding: utf-8 -*-
"""
ExtScreen 窗口投屏（Deskreen CE 后端）
--------------------------------------
把 Deskreen CE（pavlobu/deskreen，Electron 应用）当作窗口级 WebRTC 投屏后端：

  Deskreen CE(主进程) ── HTTP+socket.io 信令 + WebRTC ──► 手机浏览器(WebView 壳)

职责：
  1. 定位 / 启动 / 停止 Deskreen CE（带 --inspect 让 Dock 能读取主进程状态）
  2. 通过 Node Inspector 协议读取会话状态：
       roomID / 是否有等待会话 / 是否在分享中
     必要时代为创建「等待连接会话」（幂等，与 Deskreen 自己的 IPC 等价）
  3. 探测 Deskreen 信令服务端口（3131 → 3132）
  4. 用 mDNS（DNS-SD, UDP 5353）广播 _extscreen-deskreen._tcp 服务：
       SRV 端口 = 信令端口, TXT path = /<roomID>, ver = roomID
     手机端接收器发现该服务后自动打开 http://<PC IP>:<端口>/<roomID>

设计要点（别退回去）：
  - Deskreen CE 本身【不做】mDNS 广播（源码无 bonjour 模块），广播层由我们实现
  - roomID 是随机 6 位数字，只会出现在主进程内存里（日志是 warn 级，不落盘），
    所以必须走 --inspect 从 global.sharingSessionService 里读
  - 「信息对齐」全靠 mDNS TXT 的 ver 变化推动手机端重载，房间号轮换后手机零操作
  - 全部用标准库（无 zeroconf/websocket 依赖），PyInstaller 打包零改动
"""
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import threading
import time

from extscreen_core import (
    log, run, load_ext_config, EXT_CONFIG, CREATE_NO_WINDOW,
)
from extscreen_mdns import (
    MDNS_DOMAIN, MDNS_GROUP, MDNS_PORT, MDNS_PROTO, MDNS_TTL,
    MdnsAnnouncer, _host_label, _lan_ip,
)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
DESKREEN_PRIMARY_PORT = 3131
DESKREEN_BACKUP_PORT = 3132
DEFAULT_INSPECT_PORT = 9229
DEFAULT_CDP_PORT = 9222          # --remote-debugging-port（主调试通道）


# Deskreen CE 安装位置的常见模式（electron-builder 默认装这里）
_DESKREEN_SEARCH_DIRS = (
    r'C:\Program Files',
    r'C:\Program Files (x86)',
    os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'),
)
_DESKREEN_EXE_NAMES = (
    'Deskreen CE.exe', 'Deskreen.exe', 'deskreen-ce.exe', 'DeskreenCE.exe',
)

# Deskreen 会话状态枚举（SharingSessionStatusEnum）
SS_NOT_CONNECTED, SS_CONNECTED, SS_SHARING, SS_ERROR, SS_DESTROYED = range(5)


# --------------------------------------------------------------------------
# 定位 Deskreen
# --------------------------------------------------------------------------
def deskreen_exe():
    """返回 deskreen 可执行文件路径；找不到返回空串。

    优先用配置 deskreenPath，其次在常见安装目录里按文件名匹配。
    """
    cfg = load_ext_config()
    p = (cfg.get('deskreenPath') or '').strip()
    if p and os.path.isfile(p):
        return p
    for base in _DESKREEN_SEARCH_DIRS:
        if not base or not os.path.isdir(base):
            continue
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for name in entries:
            if 'deskreen' not in name.lower():
                continue
            full = os.path.join(base, name)
            if os.path.isdir(full):
                for exe in _DESKREEN_EXE_NAMES:
                    cand = os.path.join(full, exe)
                    if os.path.isfile(cand):
                        return cand
            elif os.path.isfile(full) and name.lower().endswith('.exe'):
                return full
    return ''


def deskreen_installed():
    return bool(deskreen_exe())




# --------------------------------------------------------------------------
# 极简 Node Inspector 客户端（HTTP 发现 + WebSocket，纯标准库）
# --------------------------------------------------------------------------
class _WsError(Exception):
    pass


def _ws_connect(host, port, path):
    """建立 WebSocket 连接，返回已握手的 socket。"""
    raw = socket.create_connection((host, port), timeout=5)
    raw.settimeout(5)
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = (
        'GET %s HTTP/1.1\r\n'
        'Host: %s:%d\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\n'
        'Sec-WebSocket-Version: 13\r\n\r\n'
    ) % (path, host, port, key)
    raw.sendall(req.encode())
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = raw.recv(4096)
        if not chunk:
            raise _WsError('handshake closed')
        buf += chunk
    head, rest = buf.split(b'\r\n\r\n', 1)
    if b' 101 ' not in head.split(b'\r\n')[0]:
        raise _WsError('handshake rejected: %s' % head[:120])
    return raw, rest


def _ws_send_text(raw, text):
    payload = text.encode('utf-8')
    mask = secrets.token_bytes(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack('>H', n)
    else:
        header.append(0x80 | 127)
        header += struct.pack('>Q', n)
    header += mask
    raw.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv_frame(raw, buffered):
    """读一帧文本，返回 (opcode, payload bytes)。buffered 是握手残留。"""
    def need(n):
        while len(buffered[0]) < n:
            chunk = raw.recv(4096)
            if not chunk:
                raise _WsError('connection closed')
            buffered[0] += chunk
    need(2)
    b1, b2 = buffered[0][0], buffered[0][1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    ln = b2 & 0x7F
    off = 2
    if ln == 126:
        need(4)
        ln = struct.unpack('>H', buffered[0][2:4])[0]
        off = 4
    elif ln == 127:
        need(10)
        ln = struct.unpack('>Q', buffered[0][2:10])[0]
        off = 10
    mask = b''
    if masked:
        need(off + 4)
        mask = buffered[0][off:off + 4]
        off += 4
    need(off + ln)
    data = buffered[0][off:off + ln]
    buffered[0] = buffered[0][off + ln:]
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def _http_get_local(port, path, timeout=3):
    """对本机端口做一次裸 socket HTTP GET。

    不用 urllib：它走系统/环境代理，本机地址会被代理劫走（实测沙箱
    返回 502；用户机器配了系统代理同样会踩）。inspector / 端口探测
    都必须直连。
    """
    s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    try:
        s.sendall(('GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                   'Connection: close\r\n\r\n' % (path, port)).encode())
        buf = b''
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b'\r\n\r\n')
        status = head.split(b'\r\n')[0] if head else b''
        if b' 200 ' not in status and b' 101 ' not in status:
            raise _WsError('http status %s' % status[:60])
        return body
    finally:
        s.close()


def _evaluate_ws(port, ws_path, expression, timeout=6, await_promise=False):
    """连上调试 WebSocket 执行一次 Runtime.evaluate，返回 value。"""
    raw, buffered = _ws_connect('127.0.0.1', port, ws_path)
    try:
        buffered = [buffered]
        _ws_send_text(raw, json.dumps({
            'id': 1, 'method': 'Runtime.evaluate',
            'params': {'expression': expression,
                       'returnByValue': True,
                       'awaitPromise': bool(await_promise)},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                opcode, data = _ws_recv_frame(raw, buffered)
            except socket.timeout:
                continue
            if opcode == 0x8:
                raise _WsError('closed by peer')
            if opcode == 0x9:          # ping -> pong
                _ws_send_text_raw(raw, data)
                continue
            if opcode != 0x1:
                continue
            msg = json.loads(data.decode('utf-8', 'ignore'))
            if msg.get('id') != 1:
                continue
            if 'error' in msg:
                raise _WsError(str(msg['error']))
            res = msg.get('result', {}).get('result', {})
            if res.get('subtype') == 'error':
                raise _WsError(res.get('description', 'evaluate error'))
            return res.get('value')
        raise _WsError('evaluate timeout')
    finally:
        try:
            raw.close()
        except OSError:
            pass


def _list_debug_targets(port):
    """列出调试端口的全部 target（inspector 与 CDP 格式相同）。"""
    return json.loads(
        _http_get_local(port, '/json/list').decode('utf-8', 'ignore'))


class InspectorClient:
    """对 Deskreen 主进程（--inspect，Node inspector）执行 evaluate。"""

    def __init__(self, port):
        self.port = port

    def evaluate(self, expression, timeout=6):
        ws = None
        for t in _list_debug_targets(self.port):
            if t.get('webSocketDebuggerUrl') and t.get('type') in ('node', 'process'):
                ws = t['webSocketDebuggerUrl']
                break
        if not ws:
            for t in _list_debug_targets(self.port):
                if t.get('webSocketDebuggerUrl'):
                    ws = t['webSocketDebuggerUrl']
                    break
        if not ws:
            raise _WsError('no inspector target')
        path = ws.split('127.0.0.1:%d' % self.port, 1)[1]
        return _evaluate_ws(self.port, path, expression, timeout)


class CdpClient:
    """对 Deskreen 渲染页面（--remote-debugging-port，Chromium CDP）执行 evaluate。

    为什么用渲染进程：打包版 Electron 实测忽略 --inspect（主进程调试端口
    根本不开），但 --remote-debugging-port 一定有效。房间号虽然住在主进程，
    但 preload 暴露了 window.electron.ipcRenderer，渲染页里 invoke 一句
    'get-waiting-for-connection-sharing-session-room-id' 就能拿到。
    """

    def __init__(self, port):
        self.port = port

    def evaluate_on_any_page(self, expression, timeout=6, await_promise=True):
        """在任意 Deskreen 页面 target 上执行（第一个成功的生效）。"""
        last_err = None
        targets = _list_debug_targets(self.port)
        for t in targets:
            if t.get('type') != 'page' or not t.get('webSocketDebuggerUrl'):
                continue
            try:
                ws = t['webSocketDebuggerUrl']
                path = ws.split('127.0.0.1:%d' % self.port, 1)[1]
                return _evaluate_ws(self.port, path, expression, timeout,
                                    await_promise=await_promise)
            except Exception as e:
                last_err = e
        raise _WsError('no usable page target: %s' % (last_err or
                                                      'targets=%d' % len(targets)))


def _ws_send_text_raw(raw, data):
    """回 pong 帧（控制帧，FIN|0x0A）。"""
    mask = secrets.token_bytes(4)
    raw.sendall(bytes([0x8A, 0x80 | len(data)]) + mask
                + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


# Inspector 里执行的状态读取（全部包 try，别让一次 evaluate 崩掉轮询）
_JS_STATE = r"""
(function(){
  try{
    var s = global.sharingSessionService;
    var c = global.connectedDevicesService;
    if(!s) return JSON.stringify({ok:false, why:'no-service'});
    var w = s.waitingForConnectionSharingSession;
    var sharing = false;
    try{
      s.sharingSessions.forEach(function(v){
        if(v.status === %d) sharing = true;   // SHARING
      });
    }catch(e){}
    var slot = true;
    try{ slot = c ? c.isSlotAvailable() : true; }catch(e){}
    return JSON.stringify({
      ok:true,
      room: w ? w.roomID : null,
      src: w ? (w.desktopCapturerSourceID || '') : '',
      pending: !!(c && c.pendingConnectionDevice && c.pendingConnectionDevice.id),
      sharing: sharing,
      slot: slot
    });
  }catch(e){ return JSON.stringify({ok:false, why:String(e && e.message || e)}); }
})()
""" % SS_SHARING

_JS_ENSURE_SESSION = r"""
(function(){
  try{
    var s = global.sharingSessionService;
    var c = global.connectedDevicesService;
    if(!s) return 'no-service';
    if(s.waitingForConnectionSharingSession) return 'exists';
    if(c && !c.isSlotAvailable()) return 'slot-busy';
    s.createWaitingForConnectionSharingSession();
    return 'creating';
  }catch(e){ return 'err:' + String(e && e.message || e); }
})()
"""

# ---- CDP 版（渲染进程里执行，走 preload 暴露的 ipcRenderer）----
# 房间号在主进程内存里，渲染页通过 invoke 一句 IPC 就能拿到。
_JS_STATE_CDP = r"""
(async function(){
  try{
    var inv = function(ch){ return window.electron.ipcRenderer.invoke(ch); };
    var room = null, port = 0, sharing = false;
    try{ room = await inv('get-waiting-for-connection-sharing-session-room-id'); }
    catch(e){}
    try{ port = await inv('get-port'); }catch(e){}
    try{
      var devs = await inv('get-connected-devices-list');
      sharing = !!(devs && devs.length > 0);
    }catch(e){}
    return JSON.stringify({
      ok:true,
      room: room || null,
      port: port || 0,
      sharing: sharing,
      pending: false,
      slot: !sharing
    });
  }catch(e){ return JSON.stringify({ok:false, why:String(e && e.message || e)}); }
})()
"""

_JS_ENSURE_SESSION_CDP = r"""
(async function(){
  try{
    var r = await window.electron.ipcRenderer.invoke(
        'create-waiting-for-connection-sharing-session');
    return 'invoked';
  }catch(e){ return 'err:' + String(e && e.message || e); }
})()
"""


# --------------------------------------------------------------------------
# Deskreen 进程 / 端口
# --------------------------------------------------------------------------
class DeskreenMonitor:
    """Deskreen 窗口投屏后端的总管（单实例，由 Dock 持有）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._inspector_port = DEFAULT_INSPECT_PORT
        self._debug_port = DEFAULT_CDP_PORT
        self._http_port = 0
        self._room = ''
        self._mdns = None
        self._stop = False
        self._thread = None
        self._consecutive_fail = 0
        self.state = {
            'run': False,        # Dock 执行过 start 且进程还活着
            'room': '',
            'port': 0,
            'url': '',           # http://<lan-ip>:<port>/<room>
            'sharing': False,
            'pending': False,
            'err': '',
        }

    # ---- 状态快照（托盘线程读） ----
    def snapshot(self):
        with self._lock:
            return dict(self.state)

    # ---- 启动 / 停止 ----
    def start(self):
        """启动 Deskreen 并开始广播。返回 (ok, msg)。"""
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return True, '窗口投屏已在运行'
            exe = deskreen_exe()
            if not exe:
                return False, (
                    '没有找到 Deskreen CE。\n'
                    '请先安装（官网 deskreen.com 或 GitHub pavlobu/deskreen Releases，'
                    '需要 3.x 版本），\n安装后在设置文件 config.json 里加 '
                    '"deskreenPath": "安装路径\\\\xxx.exe" 也可以指定。')
            cfg = load_ext_config()
            self._inspector_port = int(cfg.get('deskreenInspectPort')
                                       or DEFAULT_INSPECT_PORT)
            self._debug_port = int(cfg.get('deskreenDebugPort')
                                   or DEFAULT_CDP_PORT)
            # 先杀掉手动开过的旧实例：Electron 有单实例锁，不杀的话
            # 带调试参数的新副本会直接退出，留下的旧进程没有调试口，
            # Dock 永远读不到房间号（这是首装后最典型的坑）
            run(['taskkill', '/IM', os.path.basename(exe), '/F'], timeout=20)
            time.sleep(1)
            ip = _lan_ip()
            # --remote-debugging-port：CDP，打包版 Electron 一定支持（主通道）
            # --inspect：Node inspector，部分版本支持（兜底通道）
            args = [exe, '--ip', ip,
                    '--remote-debugging-port=%d' % self._debug_port,
                    '--inspect=127.0.0.1:%d' % self._inspector_port]
            try:
                flags = 0
                if os.name == 'nt':
                    flags = CREATE_NO_WINDOW
                self._proc = subprocess.Popen(
                    args, creationflags=flags,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                self.state['err'] = str(e)
                return False, '启动 Deskreen 失败：%s' % e
            log('deskreen started pid=%s exe=%s' % (self._proc.pid, exe))
            self._ensure_firewall(exe)
            self._stop = False
            self.state.update(run=True, err='', room='', port=0, url='',
                              sharing=False, pending=False)
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name='deskreen-monitor', daemon=True)
                self._thread.start()
            return True, ('窗口投屏已启动（Deskreen CE）。\n'
                          '在 Deskreen 窗口里选择要分享的窗口即可；'
                          '手机端会自动连接。')

    def stop(self):
        with self._lock:
            self._stop = True
            proc, self._proc = self._proc, None
            self._publish_mdns(None)
            self.state.update(run=False, room='', port=0, url='',
                              sharing=False, pending=False)
        if proc and proc.poll() is None:
            try:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               capture_output=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW)
            except Exception as e:
                log('deskreen taskkill: %s' % e)
        return True, '窗口投屏已停止'

    def running(self):
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    # ---- 防火墙（失败不致命，只记日志） ----
    def _ensure_firewall(self, exe):
        rules = [
            ('ExtScreen Deskreen TCP', 'TCP', None),
            ('ExtScreen mDNS', 'UDP', MDNS_PORT),
        ]
        for name, proto, port in rules:
            cmd = ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
                   'name=%s' % name, 'dir=in', 'action=allow',
                   'protocol=%s' % proto, 'program=%s' % exe]
            if port:
                cmd.append('localport=%d' % port)
            rc, out = run(cmd, timeout=30)
            if rc != 0:
                # 已存在同名规则会失败，删了重建一次
                run(['netsh', 'advfirewall', 'firewall', 'delete', 'rule',
                     'name=%s' % name], timeout=30)
                rc2, _ = run(cmd, timeout=30)
                if rc2 != 0:
                    log('firewall rule failed: %s' % name)

    # ---- 后台轮询 ----
    def _loop(self):
        backoff = 0
        while not self._stop:
            time.sleep(2)
            try:
                self._tick()
                backoff = 0
            except Exception as e:
                log('deskreen tick: %s' % e)
                backoff += 1
                if backoff > 30:
                    time.sleep(10)

    def _tick(self):
        with self._lock:
            if self._stop:
                return
            alive = bool(self._proc and self._proc.poll() is None)
            if not alive:
                # Deskreen 被用户关掉了：如实下线
                if self.state['run']:
                    log('deskreen process exited (code=%s)'
                        % (self._proc.poll() if self._proc else '?'))
                    self._publish_mdns(None)
                    self.state.update(run=False, room='', port=0, url='',
                                      sharing=False, pending=False,
                                      err='Deskreen 已退出')
                return

        # 1) 端口：优先信令服务自报（get-port IPC），探测兜底
        port = self._probe_port()
        if not port:
            with self._lock:
                self.state['err'] = 'Deskreen 信令端口未就绪'
            return

        # 2) 通过 CDP（渲染页）读取会话状态，inspector 兜底
        info = self._read_state()
        if info is None:
            with self._lock:
                self.state['err'] = ('无法读取 Deskreen 会话（调试端口不通）'
                                     '——请停止后再启动一次')
            return
        try:
            port = int(info.get('port') or 0) or port
        except (TypeError, ValueError):
            pass

        room = info.get('room') or ''
        if not room and info.get('slot', True):
            self._ensure_session()
            return   # 下一轮 tick 再读
        url = 'http://%s:%d/%s' % (_lan_ip(), port, room)
        changed = (room != self._room) or (port != self._http_port)
        with self._lock:
            self._room, self._http_port = room, port
            self.state.update(run=True, room=room, port=port, url=url,
                              sharing=bool(info.get('sharing')),
                              pending=bool(info.get('pending')), err='')
        if changed and room:
            self._publish_mdns({
                'port': port, 'room': room,
                'name': 'ExtScreen-%s' % _host_label(),
            })
            log('deskreen room published: %s (port %d)' % (room, port))

    def _read_state(self):
        """读会话状态。优先 CDP（渲染页，实测可用），inspector 兜底。"""
        val = None
        try:
            val = CdpClient(self._debug_port).evaluate_on_any_page(
                _JS_STATE_CDP, timeout=6)
        except Exception as e:
            self._consecutive_fail += 1
            if self._consecutive_fail % 10 == 1:
                log('deskreen cdp: %s' % e)
            # 兜底：万一哪天打包版又支持 --inspect 了
            try:
                val = InspectorClient(self._inspector_port).evaluate(_JS_STATE)
            except Exception:
                return None
        try:
            data = json.loads(val)
            self._consecutive_fail = 0
            return data if data.get('ok') else None
        except Exception:
            return None

    def _ensure_session(self):
        try:
            try:
                res = CdpClient(self._debug_port).evaluate_on_any_page(
                    _JS_ENSURE_SESSION_CDP, timeout=6)
            except Exception:
                res = InspectorClient(self._inspector_port).evaluate(
                    _JS_ENSURE_SESSION)
            if res != getattr(self, '_last_ensure_res', None):
                log('deskreen ensure-session -> %s' % res)
                self._last_ensure_res = res
        except Exception as e:
            log('deskreen ensure-session: %s' % e)

    def _probe_port(self):
        with self._lock:
            cached = self._http_port
        for port in ([cached] if cached else []) + \
                [DESKREEN_PRIMARY_PORT, DESKREEN_BACKUP_PORT]:
            try:
                body = _http_get_local(port, '/', timeout=2)[:2048]
                body = body.lower()
                # 注意 body 是 bytes：标记必须用字节字面量，
                # 否则 'str' in bytes 每次抛 TypeError → 永远探测失败
                if (b'deskreen' in body or b'"ready"' in body
                        or b'<!doctype' in body):
                    return port
            except Exception:
                continue
        return 0

    # ---- mDNS ----
    def _publish_mdns(self, info):
        old = self._mdns
        if info is None:
            if old:
                old.stop()
            self._mdns = None
            return
        txt = {'path': '/%s' % info['room'], 'ver': info['room'],
               'proto': 'http', 'name': info['name']}
        if old:
            if old.txt_matches(txt):
                return
            old.stop()
        self._mdns = MdnsAnnouncer(info['name'], info['port'], txt)
        self._mdns.start()






# --------------------------------------------------------------------------
# 单例（供 Dock 使用）
# --------------------------------------------------------------------------
MONITOR = DeskreenMonitor()


# Dock 动作包装：契约是 (ok, msg)
def action_start():
    return MONITOR.start()


def action_stop():
    return MONITOR.stop()


# --------------------------------------------------------------------------
# 自检（python -m extscreen_deskreen）
# --------------------------------------------------------------------------
def _selftest():
    ok = True
    # DNS 名编解码（实现已迁到 extscreen_mdns）
    from extscreen_mdns import _enc_name, _read_name
    n = _enc_name('_extscreen-deskreen._tcp.local')
    assert n == b'\x13_extscreen-deskreen\x04_tcp\x05local\x00', n
    name, off = _read_name(n + b'\x00\x01\x00\x01', 0)
    assert name == '_extscreen-deskreen._tcp.local', name
    # 记录构造
    a = MdnsAnnouncer('ExtScreen-test', 3131,
                      {'path': '/123456', 'ver': '123456',
                       'proto': 'http', 'name': 'ExtScreen-test'})
    pkt = a._build_response(0)
    assert pkt[:12] == struct.pack('>6H', 0, 0x8400, 0, 5, 0, 0)
    assert b'123456' in pkt
    # WebSocket 帧编解码（环回自测：本地线程当服务器）
    import socket as _s
    srv = _s.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    result = {}

    def server():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\n'
                     b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Accept: x\r\n\r\n' + b'\x81\x02hi')
        import time as _t
        _t.sleep(0.3)
        conn.close()

    th = threading.Thread(target=server, daemon=True)
    th.start()
    raw, rest = _ws_connect('127.0.0.1', port, '/')
    _ws_send_text(raw, 'hello')
    opcode, data = _ws_recv_frame(raw, [rest])
    result['echo'] = (opcode, data)
    raw.close()
    srv.close()
    assert result['echo'] == (1, b'hi'), result
    print('extscreen_deskreen selftest OK')
    return ok


if __name__ == '__main__':
    _selftest()
