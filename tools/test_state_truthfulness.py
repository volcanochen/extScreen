# -*- coding: utf-8 -*-
"""托盘状态真实性回归测试。

覆盖用户实际报告过的两个缺陷：

  Bug A：「dock 显示已启动时，实时还没起来」
        -> 服务进程活着（SCM=RUNNING）但 Sunshine 还没完成初始化时，
           托盘不能显示成「已就绪 / 扩展屏已挂出」这类"一切正常"的文案。

  Bug B：「过一会 sunshine 启动后，显示已关闭」
        -> 在上面那个「启动中」的空窗期里，用户再点一次开关，
           必须继续往前推（继续等就绪），不能反向把扩展屏关掉。

外加一条日志滚动陷阱的回归：
        sunshine.log 会滚动成 .1/.2/…，旧文件里的就绪标志属于上一次会话，
        拿来判「当前」会误报。

直接运行：python tools/test_state_truthfulness.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src'))

import extscreen_core as C
import extscreen_dock as D

FAILED = []
PASSED = 0


def check(name, cond, detail=''):
    global PASSED
    if cond:
        PASSED += 1
        print('  PASS  %s' % name)
    else:
        FAILED.append(name)
        print('  FAIL  %s   %s' % (name, detail))


def eq(name, got, want):
    check(name, got == want, 'got=%r want=%r' % (got, want))


# ---------------------------------------------------------------- 日志探针

def write_logs(files):
    """files: {filename: content}，写到临时 config 目录。"""
    d = tempfile.mkdtemp(prefix='sun_cfg_')
    for name, content in files.items():
        with open(os.path.join(d, name), 'w', encoding='utf-8') as f:
            f.write(content)
    return d


def with_config_dir(d, fn):
    orig = C.sunshine_config_dir
    C.sunshine_config_dir = lambda: d
    try:
        return fn()
    finally:
        C.sunshine_config_dir = orig
        shutil.rmtree(d, ignore_errors=True)


def test_ready_probe():
    print('\n[1] sunshine_ready() 日志探针')

    # 服务没跑 -> 一律判定未就绪
    orig_state = C.service_state
    C.service_state = lambda name: C.SERVICE_STOPPED
    try:
        ok, why = C.sunshine_ready()
        eq('服务未运行时判定未就绪', ok, False)
        check('未就绪原因说明了', '服务未运行' in why, why)
    finally:
        C.service_state = orig_state

    orig_state = C.service_state
    C.service_state = lambda name: C.SERVICE_RUNNING
    try:
        # 空日志：还没初始化完
        d = write_logs({'sunshine.log': '[2026-01-01] Info: Starting Sunshine\n'})
        ok, why = with_config_dir(d, C.sunshine_ready)
        eq('只有启动日志 -> 未就绪', ok, False)
        check('未就绪原因是"还在初始化"', '初始化' in why, why)
        shutil.rmtree(d, ignore_errors=True)

        # 有 mDNS 注册标志 -> 就绪
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Starting\n'
                            '[2026-01-01] Info: Registered Sunshine mDNS service\n'
        })
        ok, why = with_config_dir(d, C.sunshine_ready)
        eq('出现 mDNS 注册标志 -> 就绪', ok, True)
        shutil.rmtree(d, ignore_errors=True)

        # 退出标志在注册标志之后 -> 判为已退出
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Registered Sunshine mDNS service\n'
                            '[2026-01-01] Info: Unregistered Sunshine mDNS service\n'
        })
        ok, why = with_config_dir(d, C.sunshine_ready)
        eq('退出标志在后就绪之后 -> 未就绪', ok, False)
        check('退出时原因正确', '退出' in why, why)
        shutil.rmtree(d, ignore_errors=True)

        # 滚动日志陷阱：就绪标志只在旧的 .3 里，当前日志是新的空会话
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Starting Sunshine\n',
            'sunshine.log.1': '[2026-01-01] Info: Registered Sunshine mDNS service\n',
        })
        ok, why = with_config_dir(d, C.sunshine_ready)
        eq('就绪标志只在旧滚动文件里 -> 不得误报就绪', ok, False)
        shutil.rmtree(d, ignore_errors=True)

        # CLIENT CONNECTED 同样不能从旧文件里读出来
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Starting Sunshine\n',
            'sunshine.log.1': '[2026-01-01] Info: CLIENT CONNECTED\n',
        })
        eq('旧文件里的 CLIENT CONNECTED -> 不算已连接',
           with_config_dir(d, C.sunshine_client_connected), False)
        shutil.rmtree(d, ignore_errors=True)

        # 当前日志里的 CONNECTED / DISCONNECTED 顺序判定
        # （真实日志里客户端只可能在 Sunshine 就绪之后才连得上，所以要有 READY）
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Registered Sunshine mDNS service\n'
                            '[2026-01-01] Info: CLIENT CONNECTED\n'
                            '[2026-01-01] Info: CLIENT DISCONNECTED\n'
        })
        eq('后出现的 DISCONNECTED -> 视为已断开',
           with_config_dir(d, C.sunshine_client_connected), False)
        shutil.rmtree(d, ignore_errors=True)

        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Registered Sunshine mDNS service\n'
                            '[2026-01-01] Info: CLIENT DISCONNECTED\n'
                            '[2026-01-01] Info: CLIENT CONNECTED\n'
        })
        eq('后出现的 CONNECTED -> 视为已连接',
           with_config_dir(d, C.sunshine_client_connected), True)
        shutil.rmtree(d, ignore_errors=True)

        # Sunshine 被服务停止强杀：只打 Unregistered，不打 CLIENT DISCONNECTED
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: Registered Sunshine mDNS service\n'
                            '[2026-01-01] Info: CLIENT CONNECTED\n'
                            '[2026-01-01] Info: Unregistered Sunshine mDNS service\n'
        })
        eq('被强杀退出(无 DISCONNECTED) -> 不得残留为已连接',
           with_config_dir(d, C.sunshine_client_connected), False)
        shutil.rmtree(d, ignore_errors=True)

        # 重启后新会话还没连上：上一轮的 CONNECTED 不能算数
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: CLIENT CONNECTED\n'
                            '[2026-01-01] Info: Unregistered Sunshine mDNS service\n'
                            '[2026-01-01] Info: Registered Sunshine mDNS service\n'
        })
        eq('重启后新会话未连接 -> 不得用上一轮的 CONNECTED',
           with_config_dir(d, C.sunshine_client_connected), False)
        shutil.rmtree(d, ignore_errors=True)

        # 重启后新会话真的连上了
        d = write_logs({
            'sunshine.log': '[2026-01-01] Info: CLIENT CONNECTED\n'
                            '[2026-01-01] Info: Unregistered Sunshine mDNS service\n'
                            '[2026-01-01] Info: Registered Sunshine mDNS service\n'
                            '[2026-01-01] Info: CLIENT CONNECTED\n'
        })
        eq('重启后新会话已连接 -> 视为已连接',
           with_config_dir(d, C.sunshine_client_connected), True)
        shutil.rmtree(d, ignore_errors=True)
    finally:
        C.service_state = orig_state


# ------------------------------------------------------------ 托盘状态文案

def set_state(**kw):
    base = {
        'svc': C.SERVICE_RUNNING, 'installed': True, 'vdd': True,
        'monitors': 1, 'scrcpy': False, 'devices': [], 'busy': False,
        'ready': False, 'ready_reason': '', 'client': False,
    }
    base.update(kw)
    D.STATE.clear()
    D.STATE.update(base)


def test_status_text():
    print('\n[2] status_text() 不得把「服务在跑」说成「已就绪」')

    # Bug A：服务在跑但 Sunshine 还没初始化完
    set_state(svc=C.SERVICE_RUNNING, ready=False, vdd=True,
              ready_reason='服务已起，但 Sunshine 还在初始化（尚未完成）')
    txt = D.status_text()
    check('启动中显示为中间态而非"已就绪"', '启动中' in txt, 'got=%r' % txt)
    check('启动中不得显示"扩展屏已挂出"', '扩展屏已挂出' not in txt, 'got=%r' % txt)
    check('启动中不得显示"已就绪"', '已就绪' not in txt, 'got=%r' % txt)

    menu = D.menu_sunshine(None)
    check('Sunshine 菜单项在启动中如实说明', '启动中' in menu, 'got=%r' % menu)
    check('Sunshine 菜单项在启动中不得打勾', '✓' not in menu, 'got=%r' % menu)

    # Bug B：启动中的空窗期里，开关必须还是"开启"方向
    eq('启动中 -> extscreen_on() 为假', D.extscreen_on(), False)
    eq('启动中 -> 菜单仍是"开启扩展屏"', D.menu_toggle(None), '开启扩展屏')

    # 真就绪
    set_state(svc=C.SERVICE_RUNNING, ready=True, vdd=True, client=False)
    txt = D.status_text()
    check('已就绪 -> 显示等待连接', '等待手机连接' in txt, 'got=%r' % txt)
    eq('已就绪 -> extscreen_on() 为真', D.extscreen_on(), True)
    eq('已就绪 -> 菜单变为"关闭扩展屏"', D.menu_toggle(None), '关闭扩展屏')
    check('已就绪 -> Sunshine 菜单打勾', '✓' in D.menu_sunshine(None), D.menu_sunshine(None))

    # 正在串流
    set_state(svc=C.SERVICE_RUNNING, ready=True, vdd=True, client=True)
    eq('客户端已连接 -> 显示"手机正在接收画面"',
       D.status_text(), '手机正在接收画面')

    # 服务停止
    set_state(svc=C.SERVICE_STOPPED, ready=False, vdd=False)
    eq('服务停止 -> 显示"Sunshine 已停止"', D.status_text(), 'Sunshine 已停止')
    eq('服务停止 -> 开关方向为开启', D.menu_toggle(None), '开启扩展屏')

    # 未安装 / 服务未注册
    set_state(svc=None, installed=False, ready=False)
    eq('未安装 -> 显示"Sunshine 未安装"', D.status_text(), 'Sunshine 未安装')
    set_state(svc=None, installed=True, ready=False)
    eq('服务未注册 -> 显示"服务未注册"', D.status_text(), 'Sunshine 服务未注册')

    # 处理中优先
    set_state(svc=C.SERVICE_RUNNING, ready=True, busy=True)
    eq('忙碌中 -> 显示"处理中…"', D.status_text(), '处理中…')


# ------------------------------------------------------------ 动作返回值契约

def test_action_contract():
    print('\n[3] 动作函数返回值契约（cannot unpack 回归）')

    import inspect
    import re

    targets = ['enable_extscreen', 'disable_extscreen', 'restart_vdd',
               'repair_environment', 'install_vdd_driver', 'start_service',
               'stop_service', 'start_or_install_sunshine', 'launch_client']
    for name in targets:
        fn = getattr(D, name, None)
        if fn is None:
            continue
        src = inspect.getsource(fn)
        body = src.split('\n', 1)[1] if '\n' in src else src
        # 所有 return 语句都要是二元组
        bad = []
        for m in re.finditer(r'return\s+([^\n#]+)', body):
            expr = m.group(1).strip()
            if expr.startswith('(') and expr.endswith(')'):
                parts = expr[1:-1]
                if parts.count(',') != 1:
                    bad.append(expr)
            elif expr in ('True', 'False'):
                bad.append(expr)
        check('%s 的返回值是 (ok, msg) 二元组' % name, not bad, str(bad))


def test_do_async_fallback():
    print('\n[4] do_async 对单值返回的兜底')

    src = __import__('inspect').getsource(D.do_async)
    check('do_async 里有单值兜底分支',
          'isinstance(res, tuple)' in src and 'len(res) == 2' in src)


if __name__ == '__main__':
    test_ready_probe()
    test_status_text()
    test_action_contract()
    test_do_async_fallback()

    print('\n' + '=' * 58)
    if FAILED:
        print('FAILED %d / %d' % (len(FAILED), PASSED + len(FAILED)))
        for f in FAILED:
            print('  - %s' % f)
        sys.exit(1)
    print('ALL PASSED (%d checks)' % PASSED)
