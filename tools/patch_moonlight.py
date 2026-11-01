# -*- coding: utf-8 -*-
"""把 Moonlight 的 Game Activity 方向锁成竖屏。

目标方法：com.limelight.Game.setPreferredOrientationForCurrentDisplay()V
反汇编里的三条 setRequestedOrientation 参数：
    0x000c  const/16 v1, 11   FULL_SENSOR       -> 13 USER_PORTRAIT
    0x0038  const/16 v0, 12   USER_LANDSCAPE    -> 13 USER_PORTRAIT
    0x003e  const/16 v0, 13   USER_PORTRAIT     （本来就是，不动）

只改常量字节，方法结构/长度完全不变，不触发 dex 校验问题。
改完重算 dex 的 SHA-1 签名和 Adler-32 校验和。
"""
import hashlib
import os
import shutil
import sys
import zipfile
import zlib

SRC_APK = os.path.join(os.path.dirname(__file__), 'moonlight-12.2.apk')
WORK = os.path.join(os.path.dirname(__file__), 'ml_patch')
OUT_APK = os.path.join(os.path.dirname(__file__), 'moonlight-12.2-portrait.apk')

# (文件偏移, 原字节, 新字节, 说明)
PATCHES = [
    (0x06D240, b'\x13\x01\x0b\x00', b'\x13\x01\x0d\x00', 'const/16 v1, 11 FULL_SENSOR -> 13 USER_PORTRAIT'),
    (0x06D298, b'\x13\x00\x0c\x00', b'\x13\x00\x0d\x00', 'const/16 v0, 12 USER_LANDSCAPE -> 13 USER_PORTRAIT'),
]


def patch_dex(data: bytearray) -> bytearray:
    for off, old, new, desc in PATCHES:
        cur = bytes(data[off:off + 4])
        if cur != old:
            raise SystemExit('偏移 0x%06X 不匹配：期望 %s，实际 %s（%s）'
                             % (off, old.hex(' '), cur.hex(' '), desc))
        data[off:off + 4] = new
        print('  [OK] 0x%06X  %s' % (off, desc))
    # 重算校验：signature = SHA1(data[32:])，checksum = Adler32(data[12:])
    data[12:32] = hashlib.sha1(bytes(data[32:])).digest()
    data[8:12] = zlib.adler32(bytes(data[12:])).to_bytes(4, 'little')
    return data


def main():
    if not os.path.exists(SRC_APK):
        raise SystemExit('找不到 %s' % SRC_APK)

    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)

    print('解开 APK ...')
    with zipfile.ZipFile(SRC_APK) as z:
        z.extractall(WORK)

    dex_path = os.path.join(WORK, 'classes.dex')
    data = bytearray(open(dex_path, 'rb').read())
    print('修改 classes.dex ...')
    data = patch_dex(data)
    open(dex_path, 'wb').write(data)

    print('删除旧签名 ...')
    for root, _dirs, files in os.walk(os.path.join(WORK, 'META-INF')):
        for f in files:
            p = os.path.join(root, f)
            os.remove(p)
            print('  -', os.path.relpath(p, WORK))

    print('重新打包 ...')
    if os.path.exists(OUT_APK):
        os.remove(OUT_APK)
    src_zip = zipfile.ZipFile(SRC_APK)
    names = [n for n in src_zip.namelist() if not n.startswith('META-INF/')]

    with zipfile.ZipFile(OUT_APK, 'w', zipfile.ZIP_DEFLATED) as out:
        for name in names:
            full = os.path.join(WORK, name)
            if not os.path.exists(full) or os.path.isdir(full):
                continue
            # resources.arsc 必须不压缩（Android 11+ 要求，9 上也更稳）
            comp = zipfile.ZIP_STORED if name == 'resources.arsc' else zipfile.ZIP_DEFLATED
            out.write(full, name, compress_type=comp)
    src_zip.close()

    print('产出：%s  (%d 字节)' % (OUT_APK, os.path.getsize(OUT_APK)))


if __name__ == '__main__':
    main()
