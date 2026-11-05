# -*- coding: utf-8 -*-
"""
免 Gradle 离线构建「副屏接收器」APK（ExtScreenViewer）。

管线（已在本机验证过的老路子）：
  aapt2 compile/link → javac → d8 → dex 进 apk → zipalign → apksigner

依赖（全部本机已有）：
  - JDK 17:  C:\\Program Files\\Android\\Android Studio\\jbr
  - SDK:     %LOCALAPPDATA%\\Android\\Sdk（build-tools 34.0.0 / android-34）

用法：
  python tools/build_deskreen_viewer.py            # 构建到 build/ExtScreenViewer/
  python tools/build_deskreen_viewer.py --install  # 构建后 adb 安装到已连设备
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

JDK = r'C:\Program Files\Android\Android Studio\jbr'
SDK = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk')
BT = os.path.join(SDK, 'build-tools', '34.0.0')
AJAR = os.path.join(SDK, 'platforms', 'android-34', 'android.jar')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'tools', 'deskreen-viewer')
OUT = os.path.join(ROOT, 'build', 'ExtScreenViewer')
KS = os.path.join(ROOT, 'tools', 'extscreen_viewer.keystore')
APK_NAME = 'ExtScreenViewer.apk'

MIN_API = 24
KS_PASS = 'extscreen'


def sh(cmd, env=None):
    print('  $', ' '.join(os.path.basename(c) if i == 0 else str(c)
                          for i, c in enumerate(cmd)))
    r = subprocess.run(cmd, capture_output=True, text=True,
                       errors='replace', env=env)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit('command failed: %s' % cmd[0])
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--install', action='store_true', help='构建后 adb 安装')
    args = ap.parse_args()

    for p in (os.path.join(JDK, 'bin', 'java.exe'),
              os.path.join(BT, 'aapt2.exe'), AJAR):
        if not os.path.exists(p):
            raise SystemExit('missing tool: ' + p)

    javac = os.path.join(JDK, 'bin', 'javac.exe')
    keytool = os.path.join(JDK, 'bin', 'keytool.exe')
    aapt2 = os.path.join(BT, 'aapt2.exe')
    zipalign = os.path.join(BT, 'zipalign.exe')
    d8jar = os.path.join(BT, 'lib', 'd8.jar')
    signer = os.path.join(BT, 'apksigner.bat')
    java = os.path.join(JDK, 'bin', 'java.exe')

    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    for sub in ('gen', 'classes', 'dex', 'obj'):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)

    env = dict(os.environ)
    env['JAVA_HOME'] = JDK

    print('[1/6] aapt2 compile + link')
    res_zip = os.path.join(OUT, 'res.zip')
    sh([aapt2, 'compile', '--dir', os.path.join(SRC, 'res'),
        '-o', res_zip], env)
    base_apk = os.path.join(OUT, 'base.apk')
    sh([aapt2, 'link',
        '-o', base_apk,
        '-I', AJAR,
        '--manifest', os.path.join(SRC, 'AndroidManifest.xml'),
        '--min-sdk-version', str(MIN_API),
        '--target-sdk-version', '34',
        '--version-code', '1',
        '--version-name', '1.0',
        '--java', os.path.join(OUT, 'gen'),
        '--auto-add-overlay',
        res_zip], env)

    print('[2/6] javac')
    rjava = os.path.join(OUT, 'gen', 'cn', 'volcanochen', 'extscreenview',
                         'R.java')
    sh([javac, '-encoding', 'UTF-8',
        '-source', '8', '-target', '8',
        '-bootclasspath', AJAR,
        '-classpath', AJAR,
        '-d', os.path.join(OUT, 'classes'),
        rjava]
       + [os.path.join(SRC, 'java', 'cn', 'volcanochen', 'extscreenview', f)
          for f in ('ViewerBridge.java', 'ViewerService.java',
                    'MainActivity.java', 'BootReceiver.java')], env)

    print('[3/6] d8 dex')
    class_files = []
    for dirpath, _, names in os.walk(os.path.join(OUT, 'classes')):
        for n in names:
            if n.endswith('.class'):
                class_files.append(os.path.join(dirpath, n))
    sh([java, '-cp', d8jar, 'com.android.tools.r8.D8',
        '--lib', AJAR, '--min-api', str(MIN_API),
        '--output', os.path.join(OUT, 'dex')]
       + class_files, env)

    print('[4/6] pack classes.dex')
    dex = os.path.join(OUT, 'dex', 'classes.dex')
    with zipfile.ZipFile(base_apk, 'a', zipfile.ZIP_DEFLATED) as z:
        z.write(dex, 'classes.dex')

    print('[5/6] zipalign')
    aligned = os.path.join(OUT, 'aligned.apk')
    sh([zipalign, '-f', '4', base_apk, aligned], env)

    print('[6/6] apksigner')
    if not os.path.exists(KS):
        sh([keytool, '-genkeypair', '-keystore', KS,
            '-alias', 'extscreen', '-keyalg', 'RSA', '-keysize', '2048',
            '-validity', '10000',
            '-storepass', KS_PASS, '-keypass', KS_PASS,
            '-dname', 'CN=ExtScreen Viewer'], env)
    final = os.path.join(OUT, APK_NAME)
    sh([signer, 'sign', '--ks', KS,
        '--ks-pass', 'pass:' + KS_PASS,
        '--key-pass', 'pass:' + KS_PASS,
        '--out', final, aligned], env)

    print('OK ->', final)
    if args.install:
        adb = os.path.join(SDK, 'platform-tools', 'adb.exe')
        # 注意：adb 安装本地 APK 要用 Windows 风格路径
        sh([adb, 'install', '-r', final], env)
        sh([adb, 'shell', 'am', 'start', '-n',
            'cn.volcanochen.extscreenview/.MainActivity'], env)
    return 0


if __name__ == '__main__':
    sys.exit(main())
