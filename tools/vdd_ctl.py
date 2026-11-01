# -*- coding: utf-8 -*-
"""短命提权脚本：操作虚拟显示器并计时。

用法：
    python vdd_ctl.py off     # 关虚拟屏
    python vdd_ctl.py on      # 开虚拟屏（含布局）
    python vdd_ctl.py status

结果追加写入 tools/vdd_out.txt（提权进程无法重定向 stdout）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'src'))

OUT = os.path.join(HERE, 'vdd_out.txt')


def out(msg):
    try:
        with open(OUT, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except Exception:
        pass
    print(msg)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    t0 = time.time()
    try:
        from extscreen_core import powershell, VDISPLAY_PS1
        rc, text = powershell(VDISPLAY_PS1, action, timeout=90)
        dt = time.time() - t0
        out('[VDD %s] rc=%s elapsed=%.1fs' % (action, rc, dt))
        tail = (text or '').strip().replace('\n', ' | ')[-400:]
        out('[VDD %s] out: %s' % (action, tail))
    except Exception as e:
        out('[VDD %s] ERROR after %.1fs: %s' % (action, time.time() - t0, e))


if __name__ == '__main__':
    main()
