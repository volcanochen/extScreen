# -*- coding: utf-8 -*-
"""
Deskreen 模拟后端：联调「副屏接收器」用（不依赖真实 Deskreen）。

行为对齐真实 Deskreen CE：
  - HTTP 3131 提供一个观众页（这里用测试页代替 viewer bundle）
  - mDNS 广播 _extscreen-deskreen._tcp，TXT path=/<room>

用法：
  python tools/deskreen_mock_server.py            # 房间号默认 123456
  python tools/deskreen_mock_server.py --room 654321 --port 3131
"""
import argparse
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
from extscreen_deskreen import MdnsAnnouncer  # noqa: E402

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Deskreen CE Viewer (mock)</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{background:#000;color:#0f8;font:32px monospace;display:flex;
flex-direction:column;justify-content:center;align-items:center;height:95vh}}</style>
</head><body><div>MOCK VIEWER</div><div>room={room}</div>
<div id="t"></div><script>setInterval(()=>document.getElementById('t').textContent
=new Date().toLocaleTimeString(),500)</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    room = '123456'

    def do_GET(self):
        body = PAGE.format(room=self.room).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--room', default='123456')
    ap.add_argument('--port', type=int, default=3131)
    args = ap.parse_args()

    Handler.room = args.room
    httpd = ThreadingHTTPServer(('0.0.0.0', args.port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print('http on %d, room %s' % (args.port, args.room), flush=True)

    a = MdnsAnnouncer('ExtScreen-mock', args.port,
                      {'path': '/%s' % args.room, 'ver': args.room,
                       'proto': 'http', 'name': 'ExtScreen-mock'})
    if not a.start():
        print('mdns start failed', flush=True)
        return
    print('mdns announcing', flush=True)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        a.stop()


if __name__ == '__main__':
    main()
