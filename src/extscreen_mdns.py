# -*- coding: utf-8 -*-
"""
ExtScreen 局域网 mDNS / DNS-SD 应答器（纯标准库，Dock 公共设施）
--------------------------------------------------------------
给窗口投屏（extscreen_cast）与 Deskreen 后端（extscreen_deskreen）共用：
  - 被动应答 PTR/SRV/TXT/A 查询（多播 + 单播回源双发）
  - 主动公告 + TTL 一半时刷新 + stop 时 TTL=0 goodbye
Windows 上必须 IP_ADD_MEMBERSHIP + IP_MULTICAST_IF 显式锚定真实网卡，
否则会被 192.168.176.x 之类的虚拟网卡抢走多播（实测踩过）。
"""
import re
import secrets
import socket
import struct
import threading
import time

MDNS_PROTO = '_tcp'
MDNS_DOMAIN = 'local'
MDNS_GROUP = '224.0.0.251'
MDNS_PORT = 5353
MDNS_TTL = 120

def _lan_ip():
    """取本机局域网 IPv4（不真正发包，只路由一次 UDP）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('223.5.5.5', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def _host_label():
    label = re.sub(r'[^A-Za-z0-9-]', '', socket.gethostname()) or 'PC'
    return label[:30]


# --------------------------------------------------------------------------
# mDNS / DNS-SD 应答器（纯标准库）
# --------------------------------------------------------------------------
def _enc_name(name):
    """把 'a.b.local' 编成 DNS 名（无压缩）。name='.' 表示根。"""
    out = b''
    name = name.rstrip('.')
    if name:
        for label in name.split('.'):
            raw = label.encode('idna') if not label.isascii() else label.encode()
            out += bytes([len(raw)]) + raw
    return out + b'\x00'


class MdnsAnnouncer:
    """宣告 _extscreen-deskreen._tcp 服务；被动应答查询 + 主动公告。

    Windows 上 dock 是提权进程，绑定 5353 没问题；沙箱测试时若 10013
    则说明跑在受限环境里（此时 start() 返回 False，不抛异常）。
    """

    def __init__(self, instance, port, txt, ttl=MDNS_TTL,
                 svc_type='extscreen-deskreen'):
        self.instance = instance                      # 实例标签
        self.port = port
        self.txt = dict(txt or {})
        self.ttl = ttl
        self.svc_type = svc_type.lstrip('_')          # 不带前导下划线
        # SRV 目标名必须每次会话唯一（跟随机实例名绑定）：手机端会独立
        # 解析这个名字拿 A 记录，若用共享的 PC 主机名，陈旧/其他来源的
        # 解析结果（如 192.168.50.16）会盖掉正确 IP，页面变成连接拒绝
        # （实测踩过：ViewerService 8s 一拍重新解析，把能用的页面覆盖成死页）
        host = re.sub(r'[^A-Za-z0-9-]', '', instance).lower() or 'pc'
        self.srv_target = 'x-%s.%s' % (host[:40], MDNS_DOMAIN)
        self.full = '%s._%s.%s.%s' % (instance, self.svc_type, MDNS_PROTO, MDNS_DOMAIN)
        self.type_ptr = '_%s.%s.%s' % (self.svc_type, MDNS_PROTO, MDNS_DOMAIN)
        self.meta = '_services._dns-sd._udp.%s' % MDNS_DOMAIN
        self._sock = None
        self._run = False
        self._thread = None

    # ---- 生命周期 ----
    def start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._sock.bind(('', MDNS_PORT))
            except OSError as e:
                log('mdns bind failed: %s' % e)
                self._sock.close()
                self._sock = None
                return False
            # 多播必须锚定在真实局域网口上：机器上常有多块虚拟网卡
            # （WSL/Hyper-V/VMware 的 192.168.176.x 之类），不指定接口时
            # Windows 会把加组和发包都落到默认口上，手机所在的 WiFi 网段
            # 就收不到广播、它的查询也到不了我们。
            self._if_ip = _lan_ip()
            if_ip = socket.inet_aton(self._if_ip)
            mreq = socket.inet_aton(MDNS_GROUP) + if_ip
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, if_ip)
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            try:
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            except OSError:
                pass
            self._run = True
            self._thread = threading.Thread(
                target=self._serve, name='mdns-announce', daemon=True)
            self._thread.start()
            self.announce()
            return True
        except Exception as e:
            log('mdns start failed: %s' % e)
            return False

    def stop(self):
        self._run = False
        if self._sock:
            try:
                # TTL=0 的 goodbye 包，让手机立刻下线
                self._announce_once(goodbye=True)
            except Exception:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def txt_matches(self, txt):
        return self.txt == dict(txt or {})

    # ---- 收发 ----
    def _serve(self):
        self._announce_at = time.time() + 30
        while self._run:
            try:
                self._sock.settimeout(2.0)
                try:
                    data, addr = self._sock.recvfrom(9000)
                except socket.timeout:
                    if time.time() >= self._announce_at:
                        self._announce_once()
                        self._announce_at = time.time() + MDNS_TTL // 2
                    continue
                self._handle(data, addr)
                if time.time() >= self._announce_at:
                    self._announce_once()
                    self._announce_at = time.time() + MDNS_TTL // 2
            except OSError:
                if not self._run:
                    return
                time.sleep(0.2)
            except Exception as e:
                log('mdns serve: %s' % e)

    def _handle(self, data, addr):
        if len(data) < 12:
            return
        tid, flags, qd, _an, _ns, _ar = struct.unpack('>6H', data[:12])
        if flags & 0x8000:            # 响应包不理
            return
        off = 12
        wanted = False
        for _ in range(qd):
            name, off = _read_name(data, off)
            if off + 4 > len(data):
                return
            qtype, qclass = struct.unpack('>HH', data[off:off + 4])
            off += 4
            if name in (self.full, self.type_ptr, self.meta) or \
                    name == self.srv_target:
                wanted = True
        if not wanted:
            return
        resp = self._build_response(tid)
        try:
            # 多播给所有绑定 5353 的查询方（Android mdnssd 就是这种），
            # 同时单播回源——查询方可能绑在临时端口上（RFC 6762 允许双发）
            self._sock.sendto(resp, (MDNS_GROUP, MDNS_PORT))
            self._sock.sendto(resp, addr)
        except OSError as e:
            log('mdns send: %s' % e)

    def announce(self):
        for _ in range(2):        # 连发两次（丢包容错）
            self._announce_once()
            time.sleep(0.12)

    def _announce_once(self, goodbye=False):
        try:
            self._sock.sendto(self._build_response(0, goodbye=goodbye),
                              (MDNS_GROUP, MDNS_PORT))
        except Exception:
            pass

    # ---- 报文构造 ----
    def _records(self, goodbye=False):
        ttl = 0 if goodbye else self.ttl
        txt_bytes = b''
        for k in sorted(self.txt):
            v = str(self.txt[k])
            item = ('%s=%s' % (k, v)).encode('utf-8')
            if len(item) > 255:
                item = item[:255]
            txt_bytes += bytes([len(item)]) + item
        # unique 记录（SRV/TXT/A）带 cache-flush 位（0x8001），PTR 保持 0x0001
        recs = [
            (self.meta, 12, ttl, _enc_name(self.type_ptr)),                # PTR 枚举
            (self.type_ptr, 12, ttl, _enc_name(self.full)),                # PTR
            (self.full, 33, ttl,
             struct.pack('>HHH', 0, 0, self.port) + _enc_name(self.srv_target)),
            (self.full, 16, ttl,
             bytes([len(txt_bytes)]) + txt_bytes if txt_bytes else b'\x00'),
            (self.srv_target, 1, ttl,
             socket.inet_aton(getattr(self, '_if_ip', None) or _lan_ip())),  # A
        ]
        return recs

    def _build_response(self, tid, goodbye=False):
        recs = self._records(goodbye)
        # 所有记录塞进 ANCOUNT（mDNS 允许；不搞名压缩，包不大）
        out = struct.pack('>6H', tid, 0x8400, 0, len(recs), 0, 0)
        for name, rtype, ttl, rdata in recs:
            # unique 记录（SRV/TXT/A）带 cache-flush 位
            qclass = 0x8001 if rtype in (33, 16, 1) else 0x0001
            out += _enc_name(name)
            out += struct.pack('>HHIH', rtype, qclass, ttl, len(rdata))
            out += rdata
        return out


def _read_name(data, off):
    """解析 DNS 名（支持压缩指针），返回 (name, next_off)。"""
    labels = []
    jumps = 0
    orig = off
    while True:
        if off >= len(data) or jumps > 8:
            return '.', orig + 1
        ln = data[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            if off + 2 > len(data):
                return '.', orig + 1
            ptr = struct.unpack('>H', data[off:off + 2])[0] & 0x3FFF
            sub, _ = _read_name(data, ptr)
            labels.append(sub.rstrip('.'))
            off += 2
            return '.'.join(l for l in labels if l), off
        off += 1
        raw = data[off:off + ln]
        off += ln
        try:
            labels.append(raw.decode('ascii', 'ignore'))
        except Exception:
            labels.append('')
    return '.'.join(l for l in labels if l) or '.', off
