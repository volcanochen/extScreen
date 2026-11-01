#!/usr/bin/env bash
# 重新打包打完补丁的 Moonlight APK
# 用法: bash build_moonlight.sh [输出名, 默认 ml_portrait]
set -e

cd "$(dirname "$0")"

OUT_NAME="${1:-ml_portrait}"
JAVA="C:/Program Files/Android/Android Studio/jbr/bin/java.exe"
BT="C:/Users/volcano/AppData/Local/Android/Sdk/build-tools/34.0.0"
KEYSTORE="C:/Users/volcano/.android/debug.keystore"

echo "==> 1/4 反编译资源回打包 ($OUT_NAME.unsigned.apk)"
rm -f "$OUT_NAME.unsigned.apk" "$OUT_NAME.aligned.apk" "$OUT_NAME.apk"
"$JAVA" -jar apktool.jar b ml_src -o "$OUT_NAME.unsigned.apk"

echo "==> 2/4 zipalign"
"$BT/zipalign.exe" -p -f 4 "$OUT_NAME.unsigned.apk" "$OUT_NAME.aligned.apk"

echo "==> 3/4 apksigner 签名 (v1+v2, debug key)"
MSYS2_ARG_CONV_EXCL="*" "$JAVA" -cp "$BT/lib/apksigner.jar" com.android.apksigner.ApkSignerTool \
  sign --ks "$KEYSTORE" --ks-key-alias androiddebugkey \
  --ks-pass pass:android --key-pass pass:android \
  --v1-signing-enabled true --v2-signing-enabled true \
  --out "$OUT_NAME.apk" "$OUT_NAME.aligned.apk"

echo "==> 4/4 校验"
MSYS2_ARG_CONV_EXCL="*" "$JAVA" -cp "$BT/lib/apksigner.jar" com.android.apksigner.ApkSignerTool verify "$OUT_NAME.apk"
ls -la "$OUT_NAME.apk"
echo "完成: $(pwd)/$OUT_NAME.apk"
