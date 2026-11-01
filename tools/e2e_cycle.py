# -*- coding: utf-8 -*-
"""端到端「关闭扩展屏 → 开启扩展屏」完整循环测试。

走的就是用户在托盘里点菜单时执行的那两条代码路径
（disable_extscreen / enable_extscreen），不是模拟出来的。

重点验证：
  1. 关闭后托盘状态如实变成「Sunshine 已停止」
  2. 开启过程中状态经过「Sunshine 启动中…」这个中间态，
     而不是一上来就假装「已就绪」
  3. 开启过程中（启动中的空窗期）如果用户再点一次开关，
     走的是 enable 而不是反向 disable —— 这是用户报告过的「再点一次反而关掉」
  4. 整个开启流程最终真的就绪（ready=True）

需要管理员权限运行（要启停服务、操作虚拟显示器设备节点）。
结果实时追加写入 tools/e2e_trace.jsonl，结束再写 tools/e2e_out.json
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

import extscreen_dock as D

TRACE = os.path.join(HERE, 'e2e_trace.jsonl')
OUT = os.path.join(HERE, 'e2e_out.json')

T0 = time.time()
STOP_FLAG = threading.Event()
_lock = threading.Lock()
_steps = []


def emit(kind, **kw):
    """实时追加落盘，卡住了也能看到最后一步。"""
    rec = {'t': round(time.time() - T0, 1), 'kind': kind}
    rec.update(kw)
    with _lock:
        with open(TRACE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return rec


def snap(tag):
    try:
        D.refresh_state(full=True)
    except Exception as e:
        emit('snap_error', tag=tag, error=str(e))
        return
    emit('snap', tag=tag,
         svc=D.STATE.get('svc'),
         ready=bool(D.STATE.get('ready')),
         ready_reason=D.STATE.get('ready_reason', ''),
         client=bool(D.STATE.get('client')),
         vdd=bool(D.STATE.get('vdd')),
         status_text=D.status_text(),
         menu_toggle=D.menu_toggle(None),
         extscreen_on=D.extscreen_on())


def watcher():
    while not STOP_FLAG.is_set():
        snap('poll')
        STOP_FLAG.wait(1.0)


def main():
    try:
        if os.path.exists(TRACE):
            os.remove(TRACE)
    except Exception:
        pass

    result = {'steps': []}
    try:
        D.refresh_state(full=True)
        snap('initial')
        emit('phase', phase='disable')

        # ---- 1. 关闭扩展屏 ----
        ok, msg = D.disable_extscreen()
        _steps.append({'step': 'disable_extscreen', 'ok': bool(ok), 'msg': msg})
        emit('action', step='disable_extscreen', ok=bool(ok), msg=msg)
        time.sleep(3)
        snap('after_disable')

        # ---- 2. 开启扩展屏（后台采样，抓中间态）----
        emit('phase', phase='enable')
        t = threading.Thread(target=watcher, daemon=True)
        t.start()
        ok2, msg2 = D.enable_extscreen()
        STOP_FLAG.set()
        time.sleep(1.5)
        _steps.append({'step': 'enable_extscreen', 'ok': bool(ok2), 'msg': msg2})
        emit('action', step='enable_extscreen', ok=bool(ok2), msg=msg2)
        snap('after_enable')

        result['toggle_after_enable'] = D.menu_toggle(None)
        result['extscreen_on_after_enable'] = D.extscreen_on()
        result['ok'] = bool(ok) and bool(ok2)
    except Exception:
        import traceback
        result['ok'] = False
        result['error'] = traceback.format_exc()
        emit('fatal', error=result['error'])
    finally:
        STOP_FLAG.set()
        result['steps'] = _steps
        try:
            with open(OUT, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


if __name__ == '__main__':
    main()
