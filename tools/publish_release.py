# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传资产（通过 REST API，不依赖 gh CLI）。

用法：
    python tools/publish_release.py                       # 用下面的默认值
    python tools/publish_release.py --repo owner/name --tag v1.2.0
    python tools/publish_release.py --assets-dir D:/out  --notes notes.md
    python tools/publish_release.py --asset "名称=路径" --asset "名称=路径"

要点：
  * 资产名必须纯 ASCII —— GitHub 会把中文名转义成点号（`副屏` -> `.`），
    下载链接会失效。中文说明写进 label。
  * DELETE 资产返回 204 空体，解析要容错。
  * 同一个 tag 重复执行时会更新已有 release，不会重复创建。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse

DEFAULT_REPO = 'volcanochen/extScreen'
DEFAULT_TAG = 'v1.0.0'
DEFAULT_ASSETS_DIR = r'C:\VOLCANO\myws\extScreen\release_assets'

RELEASE_NAME = 'ExtScreen 副屏助手 v1.0.0'

BODY = """## ExtScreen 副屏助手 v1.0.0

把 Android 手机变成 Windows 桌面的**一块扩展屏** —— 不是投屏，不是远程控制，
而是真正可以拖窗口过去的第二块显示器。

```
Windows 真实显示器  +  VDD 虚拟屏 (1080x2220 @60Hz)
                          │  Sunshine 捕获 (DXGI Desktop Duplication)
                          ▼
                    RTSP 47989  ──►  Moonlight (手机, 竖屏补丁)
```

### 下载

| 文件 | 说明 |
|---|---|
| `ExtScreen-副屏助手-v1.0.0.exe` | 托盘主程序（**需要管理员权限运行**） |
| `MttVDD-25.7.23-signed-drivers-x86.zip` | 虚拟显示器驱动（官方签名版） |
| `sunshine-installer.msi` | Sunshine 串流主机端安装包 |

### 安装顺序

1. 安装 **Sunshine**（`sunshine-installer.msi`），装完重启
2. 安装 **MttVDD 虚拟显示器驱动**（解压 zip 后运行 `VDD Control.exe`，
   或用托盘菜单「环境与依赖 → 安装 / 修复虚拟显示器驱动…」）
3. 手机端装 **Moonlight 竖屏补丁版**（见仓库 `docs/MOONLIGHT-PATCH.md`）
4. 双击 `ExtScreen-副屏助手-v1.0.0.exe`，从托盘菜单点「开启扩展屏」

### 托盘状态对照

| 图标 | 文案 | 含义 |
|---|---|---|
| 灰 | `Sunshine 已停止` | 服务没跑 |
| 绿 | `Sunshine 启动中…` | 服务进程活着，但 Sunshine 还在初始化（实测 20~30 秒） |
| 绿 | `已就绪，等待手机连接` | 真正初始化完成，可以串流了 |
| 蓝 | `扩展屏已挂出，等待手机连接` | 虚拟屏已挂出，等手机连 |
| 蓝 | `手机正在接收画面` | 客户端已连上，正在推流 |
| 红 | `Sunshine 未安装` / `环境异常` | 需要安装或修复 |

> **「服务在跑」不等于「能串流」。** SCM 报 `SERVICE_RUNNING` 之后 Sunshine 还要
> 枚举显示器、编译 shader、建 NVENC 编码器、起 HTTP 线程、注册 mDNS。
> 这段空窗期托盘如实显示「Sunshine 启动中…」，判据取自 `sunshine.log` 里的
> 真实标志 `Registered Sunshine mDNS service`，不是时间差也不是状态码。

### 本版修复

- **托盘状态不再说谎**：区分「服务已起」与「已能串流」，中间态如实呈现
- **开关不再反向关闭**：初始化空窗期点「开启扩展屏」会继续往前推，
  而不是把已经开好的扩展屏关掉
- **关闭扩展屏不再卡死**：改为先停 Sunshine 再关虚拟屏，关屏走
  `pnputil /disable-device`。**实测 150 秒超时 → 2.2 秒**
- **托盘崩溃修复**：`cannot unpack non-iterable bool object`
- **虚拟屏按 EDID 匹配**：跨重启稳定，不需要为了拿 device_id 重启 Sunshine
- **服务启停用 advapi32 原生 API**：不再解析 `sc.exe` 的本地化输出

### 已知限制

- 左右各 9px 黑边未消除（流 2220 vs 窗口 2180）
- 托盘需要管理员权限

完整说明与设计细节见 [仓库 README](https://github.com/volcanochen/extScreen#readme)。
"""


def get_token():
    out = subprocess.run(['git', 'credential', 'fill'],
                         input='url=https://github.com\n',
                         capture_output=True, text=True, timeout=60).stdout
    for line in out.splitlines():
        if line.startswith('password='):
            return line.split('=', 1)[1].strip()
    raise SystemExit('拿不到 GitHub token')


def api(url, token, method='GET', data=None, raw=None, content_type=None):
    headers = {'Authorization': 'token ' + token,
               'Accept': 'application/vnd.github+json',
               'User-Agent': 'extScreen-release'}
    body = None
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    elif raw is not None:
        body = raw
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.status, r.read()


def parse_args():
    p = argparse.ArgumentParser(
        description='创建 GitHub Release 并上传资产（REST API，不依赖 gh CLI）')
    p.add_argument('--repo', default=DEFAULT_REPO,
                   help='owner/name，默认 %(default)s')
    p.add_argument('--tag', default=DEFAULT_TAG,
                   help='发布 tag，默认 %(default)s')
    p.add_argument('--assets-dir', default=DEFAULT_ASSETS_DIR,
                   help='资产目录，目录内的文件全部上传，默认 %(default)s')
    p.add_argument('--asset', action='append', metavar='名称=路径',
                   help='单个资产，name=path，可重复；给了它就忽略 --assets-dir')
    p.add_argument('--notes', help='release 正文文件（Markdown），默认用内置说明')
    p.add_argument('--name', help='release 标题，默认用内置标题')
    p.add_argument('--target', default='main',
                   help='目标分支，默认 %(default)s')
    p.add_argument('--prerelease', action='store_true', help='标记为预发布')
    p.add_argument('--dry-run', action='store_true', help='只打印计划，不实际上传')
    return p.parse_args()


def collect_assets(args):
    """返回 [(上传名, 本地路径, label), ...]"""
    out = []
    if args.asset:
        for spec in args.asset:
            if '=' not in spec:
                raise SystemExit('--asset 格式应为 名称=路径，收到: %r' % spec)
            name, path = spec.split('=', 1)
            out.append((name.strip(), path.strip(), ''))
        return out
    if not os.path.isdir(args.assets_dir):
        return out
    for fn in sorted(os.listdir(args.assets_dir)):
        p = os.path.join(args.assets_dir, fn)
        if os.path.isfile(p):
            out.append((fn, p, ''))
    return out


def main():
    args = parse_args()
    token = get_token()
    base = 'https://api.github.com/repos/' + args.repo

    body_text = BODY
    if args.notes:
        with open(args.notes, encoding='utf-8') as f:
            body_text = f.read()
    release_name = args.name or RELEASE_NAME

    # dry-run 必须完全只读：早期版本只在上传资产时判断 dry-run，
    # 结果「预演」把 release 真建出来了（2026-11-05 踩过）。这里提前返回，
    # 除了一次 GET 探查远端状态外不做任何写操作。
    if args.dry_run:
        print('=== DRY-RUN：不创建/修改 release，仅打印计划 ===')
        print('repo=%s  tag=%s  name=%s' % (args.repo, args.tag, release_name))
        print('notes=%s (%d chars)' % (args.notes or '内置说明',
                                      len(body_text)))
        try:
            api(base + '/releases/tags/' + args.tag, token)
            print('远端：release 已存在 → 正式执行时复用并更新正文')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print('远端：release 不存在 → 正式执行时新建')
            else:
                raise
        for name, path, label in collect_assets(args):
            exists = os.path.exists(path)
            mb = os.path.getsize(path) / 1048576 if exists else 0
            print('DRY-RUN  %-46s %8.2f MB  <- %s%s' % (
                name, mb, path, '' if exists else '（不存在，将跳过）'))
        return

    # 1. 看看 release 是否已存在
    try:
        st, body = api(base + '/releases/tags/' + args.tag, token)
        release = json.loads(body)
        release_id = release['id']
        print('release 已存在(id=%s)，将复用并更新正文' % release_id)
        st, body = api(base + '/releases/%d' % release_id, token, method='PATCH',
                       data={'name': release_name, 'body': body_text,
                             'prerelease': args.prerelease})
        print('release 正文已更新')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        payload = {'tag_name': args.tag, 'name': release_name, 'body': body_text,
                   'draft': False, 'prerelease': args.prerelease,
                   'target_commitish': args.target}
        st, body = api(base + '/releases', token, method='POST', data=payload)
        release = json.loads(body)
        release_id = release['id']
        print('release 创建成功 id=%s url=%s' % (release_id, release['html_url']))

    # 2. 上传资产
    assets = collect_assets(args)
    if not assets:
        print('没有找到资产可上传（目录：%s）' % args.assets_dir)
    upload_url = 'https://uploads.github.com/repos/%s/releases/%d/assets' % (
        args.repo, release_id)
    for name, path, label in assets:
        if not os.path.exists(path):
            print('跳过（不存在）:', path)
            continue
        if args.dry_run:
            print('DRY-RUN  %-46s %8.2f MB  <- %s' % (
                name, os.path.getsize(path) / 1048576, path))
            continue
        size = os.path.getsize(path)
        print('上传 %-46s %8.2f MB ...' % (name, size / 1048576), end='', flush=True)
        with open(path, 'rb') as f:
            raw = f.read()
        params = '?name=%s' % urllib.parse.quote(name)
        if label:
            params += '&label=' + urllib.parse.quote(label)
        try:
            st, body = api(upload_url + params, token, method='POST',
                           raw=raw, content_type='application/octet-stream')
            d = json.loads(body)
            print(' OK  id=%s  %s' % (d.get('id'), d.get('browser_download_url')))
        except urllib.error.HTTPError as e:
            print(' 失败 http=%s %s' % (e.code, e.read().decode('utf-8', 'ignore')[:300]))

    # 3. 汇总
    st, body = api(base + '/releases/tags/' + args.tag, token)
    d = json.loads(body)
    print('\n=== Release 汇总 ===')
    print('name    :', d.get('name'))
    print('tag     :', d.get('tag_name'))
    print('url     :', d.get('html_url'))
    print('draft   :', d.get('draft'), '| prerelease:', d.get('prerelease'))
    print('assets  :')
    for a in d.get('assets', []):
        print('   - %-46s %8.2f MB  %d downloads' % (
            a['name'], a['size'] / 1048576, a['download_count']))


if __name__ == '__main__':
    main()

