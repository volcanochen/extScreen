# -*- coding: utf-8 -*-
"""创建 GitHub Release 并上传资产（通过 REST API，不依赖 gh CLI）。"""
import json
import os
import sys
import urllib.request
import urllib.error

REPO = 'volcanochen/extScreen'
TAG = 'v1.0.0'
ASSETS_DIR = r'C:\VOLCANO\myws\extScreen\release_assets'

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
    import subprocess
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


def main():
    token = get_token()
    base = 'https://api.github.com/repos/' + REPO

    # 1. 看看 release 是否已存在
    try:
        st, body = api(base + '/releases/tags/' + TAG, token)
        print('release 已存在，改为更新')
        release = json.loads(body)
        release_id = release['id']
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        payload = {'tag_name': TAG, 'name': RELEASE_NAME, 'body': BODY,
                   'draft': False, 'prerelease': False,
                   'target_commitish': 'main'}
        st, body = api(base + '/releases', token, method='POST', data=payload)
        release = json.loads(body)
        release_id = release['id']
        print('release 创建成功 id=%s url=%s' % (release_id, release['html_url']))

    # 2. 上传资产
    assets = [
        ('ExtScreen-副屏助手-v1.0.0.exe', '托盘主程序，需要管理员权限运行'),
        ('MttVDD-25.7.23-signed-drivers-x86.zip', '虚拟显示器驱动（官方签名版）'),
        ('sunshine-installer.msi', 'Sunshine 串流主机端安装包'),
    ]
    upload_url = 'https://uploads.github.com/repos/%s/releases/%d/assets' % (REPO, release_id)
    for name, label in assets:
        path = os.path.join(ASSETS_DIR, name)
        if not os.path.exists(path):
            print('跳过（不存在）:', name)
            continue
        size = os.path.getsize(path)
        print('上传 %-46s %8.2f MB ...' % (name, size / 1048576), end='', flush=True)
        with open(path, 'rb') as f:
            raw = f.read()
        params = '?name=%s&label=%s' % (
            urllib.parse.quote(name), urllib.parse.quote(label))
        try:
            st, body = api(upload_url + params, token, method='POST',
                           raw=raw, content_type='application/octet-stream')
            d = json.loads(body)
            print(' OK  id=%s  %s' % (d.get('id'), d.get('browser_download_url')))
        except urllib.error.HTTPError as e:
            print(' 失败 http=%s %s' % (e.code, e.read().decode('utf-8', 'ignore')[:300]))

    # 3. 汇总
    st, body = api(base + '/releases/tags/' + TAG, token)
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
    import urllib.parse
    main()
