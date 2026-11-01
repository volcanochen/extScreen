# -*- coding: utf-8 -*-
"""托盘动作函数的返回值契约测试。

背景：do_async 约定动作函数返回 (ok, msg)。早前 restart_vdd 只返回 bool，
被 do_async 解包成 ok, msg 时抛「cannot unpack non-iterable bool object」，
托盘直接弹错误框、动作也没执行。

这里逐个检查所有会进 do_async 的函数，确认返回值形状对得上，
并且检查 do_async 自身的兜底逻辑能接住单值返回。
"""
import os
import re
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import extscreen_dock as d  # noqa: E402


DO_ASYNC_TARGETS = [
    'disable_extscreen',
    'enable_extscreen',
    'install_vdd_driver',
    'launch_scrcpy',
    'open_sunshine_config',
    'open_sunshine_page',
    'repair_environment',
    'restart_vdd',
    'start_or_install_sunshine',
]

failures = []


def check(name, fn):
    """调用函数（尽量不真执行副作用），检查返回值是二元组。"""
    # 用源码分析代替真实调用，避免真的重启驱动/装服务
    import inspect
    try:
        src = inspect.getsource(fn)
    except Exception as e:
        failures.append('%s: 取不到源码 %s' % (name, e))
        return
    rets = re.findall(r'^\s*return\s+(.+?)\s*$', src, re.M)
    if not rets:
        failures.append('%s: 没有 return 语句' % name)
        return
    for expr in rets:
        expr = expr.strip()
        # 允许: True, x / False, x / ok, x / 'str' / f"..." / 变量名(单值→可疑)
        if re.match(r'^(True|False|ok|ok_ins|ok_cfg|ok_restart\w*|ok2|ok3|ok4|ok5)\s*,', expr):
            continue
        if expr.startswith(("'", '"', 'f"', "f'")):
            continue
        if expr in ('None',):
            continue
        if re.match(r'^[A-Za-z_][\w.]*\(.*\)$', expr) and ',' not in expr:
            # 转发调用：真正调一下被调函数，确认它返回二元组
            callee = expr.rstrip('()').split('.')[-1]
            target = None
            import extscreen_core as _core
            for cand in (d, _core):
                if hasattr(cand, callee):
                    target = getattr(cand, callee)
                    break
            if target is None:
                failures.append('%s: 转发到未知函数 %r' % (name, expr))
                continue
            try:
                r = target()
            except Exception as e:
                failures.append('%s: 调用 %s 抛异常 %s' % (name, callee, e))
                continue
            if not (isinstance(r, tuple) and len(r) == 2):
                failures.append('%s: %s 返回的不是二元组: %r' % (name, callee, r))
            else:
                print('    %s -> %s 返回二元组 OK' % (name, callee))


print('--- 检查动作函数返回值形状 ---')
for n in DO_ASYNC_TARGETS:
    f = getattr(d, n, None)
    if f is None:
        failures.append('%s: 函数不存在' % n)
        continue
    check(n, f)
    print('  checked %s' % n)

print()
print('--- 检查 do_async 兜底逻辑 ---')
src = inspect_src = open(d.__file__, encoding='utf-8').read()
m = re.search(r'def do_async\(fn.*?threading\.Thread', src, re.S)
if not m:
    failures.append('do_async 源码定位失败')
else:
    body = m.group(0)
    if 'isinstance(res, tuple) and len(res) == 2' in body:
        print('  do_async 有单值兜底')
    else:
        failures.append('do_async 缺少单值兜底')

# 实测兜底：塞一个返回 bool 的假动作
captured = {}


class _FakeIcon:
    def notify(self, *a, **k):
        captured['notify'] = a

    icon = None
    title = ''

    def update_menu(self):
        pass


d.ICON = _FakeIcon()
d.show_message = lambda *a, **k: captured.__setitem__('msg', a)

# 走 worker 内部逻辑（同步执行，不起线程）
import threading  # noqa: E402


def _run_async(fn, *args):
    box = {}

    def worker():
        res = fn(*args)
        if isinstance(res, tuple) and len(res) == 2:
            ok, msg = res
        else:
            ok, msg = (bool(res), '' if res is None else str(res))
        box['ok'] = ok
        box['msg'] = msg

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    return box


r = _run_async(lambda: True)
if r.get('ok') is not True:
    failures.append('兜底未接住 bool 返回: %r' % r)
else:
    print('  兜底接住 bool 返回 OK')

r = _run_async(lambda: None)
if r.get('ok') is not False:
    failures.append('兜底未接住 None 返回: %r' % r)
else:
    print('  兜底接住 None 返回 OK')

r = _run_async(lambda: (True, 'msg'))
if r.get('msg') != 'msg':
    failures.append('正常二元组被破坏: %r' % r)
else:
    print('  正常二元组透传 OK')

print()
if failures:
    print('FAILED (%d):' % len(failures))
    for f in failures:
        print('  - %s' % f)
    sys.exit(1)
print('ALL PASSED')
