# -*- coding: utf-8 -*-
"""对照实验：用 zeroconf（标准实现）广播同样的 _extscreen-cast 服务。
如果手机能发现它，说明我们自己的应答器还有 bug；否则是环境问题。"""
import sys, time, socket
sys.path.insert(0, r'C:\VOLCANO\myws\extScreen\src')
out = open(r'C:\VOLCANO\myws\extScreen\tools\e2e_result.txt', 'w', encoding='utf-8')
def P(*a):
    s = ' '.join(str(x) for x in a)
    print(s, flush=True)
    out.write(s + '\n'); out.flush()

from zeroconf import Zeroconf, ServiceInfo
zc = Zeroconf()
ip = socket.gethostbyname(socket.gethostname())
# 取真实局域网 IP
import extscreen_mdns as m
ip = m._lan_ip()
P('announce via zeroconf, ip=%s' % ip)
info = ServiceInfo(
    '_extscreen-cast._tcp.local.',
    'Zeroconf-Test._extscreen-cast._tcp.local.',
    parsed_addresses=[ip], port=8123,
    properties={'path': '/', 'proto': 'http', 'name': 'Zeroconf Test'},
)
zc.register_service(info, ttl=120)
P('registered, waiting 60s...')
time.sleep(60)
zc.unregister_service(info)
zc.close()
P('DONE')
out.close()
