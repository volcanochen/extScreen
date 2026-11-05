# -*- coding: utf-8 -*-
"""真机 mDNS 发现测试：起 cast 服务 90 秒，手机侧观察。结果写 e2e_result.txt"""
import sys, time, os
sys.path.insert(0, r'C:\VOLCANO\myws\extScreen\src')
out = open(r'C:\VOLCANO\myws\extScreen\tools\e2e_result.txt', 'w', encoding='utf-8')
def P(*a):
    s = ' '.join(str(x) for x in a)
    print(s, flush=True)
    out.write(s + '\n'); out.flush()

import extscreen_cast as cast
wins = cast.list_windows()
P('windows:', len(wins))
if not wins:
    P('NO WINDOW'); out.close(); sys.exit(1)
hwnd, title = wins[0]
P('sharing window:', title)
ok, msg = cast.MONITOR.start(hwnd, title)
P('start:', ok, msg)
P('state:', cast.MONITOR.snapshot())
time.sleep(90)
P('final state:', cast.MONITOR.snapshot())
cast.MONITOR.stop()
P('DONE')
out.close()
