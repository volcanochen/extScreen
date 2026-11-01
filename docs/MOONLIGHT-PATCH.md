# Moonlight 竖屏补丁 —— 完整流程

手机当扩展屏用时，原版 Moonlight 有三个坑，全部要改掉画面才能竖着铺满：

| # | 现象 | 根因 | 改法 |
|---|---|---|---|
| 1 | 一进串流就被强制横屏，桌面横着显示 | `Game.setPreferredOrientationForCurrentDisplay()` 把「竖屏分辨率」判成 `USER_LANDSCAPE` | dex 字节级改方向常量 |
| 2 | 画面缩成屏幕中间一小块 | `PreferenceConfiguration` 请求的分辨率是横屏（宽高交换分支写反） | smali 改宽高取值 |
| 3 | 修好方向后左右仍留 9px 黑边 | 流分辨率与窗口差几十像素（导航栏），走 aspect-fit 分支 | smali 强制 `stretchVideo=true` |

## 一、dex 方向常量（`patch_moonlight.py`）

```bash
python tools/patch_moonlight.py     # 产出 tools/moonlight-12.2-portrait.apk
```

改的是 `com.limelight.Game.setPreferredOrientationForCurrentDisplay()` 里两条
`setRequestedOrientation` 的参数：

```
0x06D240  const/16 v1, 11  FULL_SENSOR     -> 13 USER_PORTRAIT
0x06D298  const/16 v0, 12  USER_LANDSCAPE  -> 13 USER_PORTRAIT
```

只动 4 个字节，方法长度不变，改完重算 `signature=SHA1(data[32:])`、
`checksum=Adler32(data[12:])`。

> 说明：这一层必须在**未反编译的原版 dex** 上做字节替换（`0x13 0x01 0x0b 0x00`
> 这种指令序列在 smali 里没有稳定锚点）。后面的两项改动用 apktool 在 smali 上做。

## 二、分辨率与拉伸（apktool + smali）

用 `moonlight-12.2-portrait.apk` 反编译出 `ml_src/`：

```bash
java -jar tools/apktool.jar d tools/moonlight-12.2-portrait.apk -o tools/ml_src -f
```

### 1. `smali/com/limelight/preferences/PreferenceConfiguration.smali`

在 `readPreferences` 的 `return-object v1` 之前插入（保持 `height >= width`）：

```smali
move-object/from16 v2, p0
invoke-virtual {v2}, Landroid/content/Context;->getResources()Landroid/content/res/Resources;
move-result-object v2
invoke-virtual {v2}, Landroid/content/res/Resources;->getDisplayMetrics()Landroid/util/DisplayMetrics;
move-result-object v2
iget v3, v2, Landroid/util/DisplayMetrics;->widthPixels:I
iget v4, v2, Landroid/util/DisplayMetrics;->heightPixels:I

# 关键：if-ge 才是「高>=宽就跳过交换」。写成 if-gt 会永远得到横屏
if-ge v4, v3, :cond_es_portrait
move v5, v3
move v3, v4
move v4, v5
:cond_es_portrait

iput v3, v1, Lcom/limelight/preferences/PreferenceConfiguration;->width:I
iput v4, v1, Lcom/limelight/preferences/PreferenceConfiguration;->height:I
return-object v1
```

> `.locals 22` 时 `p0` 落在 v22，普通 `invoke-virtual` 只能寻址 v0–v15，
> 所以必须先 `move-object/from16 v2, p0` 搬到低位寄存器。

### 2. 同一个文件，强制拉伸

`stretchVideo` 的 `iput-boolean` 前加一行：

```smali
const/4 v4, 0x1
iput-boolean v4, v1, Lcom/limelight/preferences/PreferenceConfiguration;->stretchVideo:Z
```

`Game.smali` 里的分支是：

```
if (!stretchVideo && !aspectCompatible) -> streamView.setDesiredAspectRatio(w/h)   # 会留黑边
else                                    -> holder.setFixedSize(w, h)               # 铺满
```

强制 `stretchVideo=true` 就走第二条，代价是纵向约 1.8% 的轻微压缩（因为窗口被
导航栏占掉 39px），肉眼看不出来。

## 三、打包签名

```bash
bash tools/build_moonlight.sh            # 产出 tools/ml_portrait.apk
adb install -r tools/ml_portrait.apk     # 同签名覆盖安装，配对数据不丢
```

脚本做四件事：`apktool b` → `zipalign -p -f 4` → `apksigner sign`（v1+v2，
`~/.android/debug.keystore` / `androiddebugkey` / 密码 `android`）→ verify。

踩过的坑：
- `apksigner.jar` 的主类是 `com.android.apksigner.ApkSignerTool`，不是 `ApkSigner`；
- Git Bash 里传 jar 路径会被 MSYS 改写，要 `MSYS2_ARG_CONV_EXCL="*"` + Windows 风格路径；
- 签名必须和之前一致，否则 `install -r` 会失败、配对数据（客户端证书）丢失。

## 四、验证

```bash
# 方向：串流中应为 rotation 0
adb shell dumpsys display | grep mOverrideDisplayInfo

# 分辨率：应该是竖的
adb logcat -d | grep "Configuring with format"
#   -> width=1080, height=2180  ✔
#   -> width=2192, height=1088  ✘ 说明宽高交换分支还是反的

# PC 端应看到
grep "Capture size" "C:\Program Files\Sunshine\config\sunshine.log"
#   -> Capture size : 1080x2220
```

## 五、手机上怎么点（重要）

这台的 sepolicy/输入注入有毛病：`adb shell input tap` 经常被当成**长按**，
在 Moonlight 的 AppView 里会弹出「恢复串流 / 退出串流 / 查看详情 / 创建快捷方式」，
点图标根本进不去串流。

所以自动化一律走 `ShortcutTrampoline`，不用点界面：

```bash
adb shell am start -n com.limelight/.ShortcutTrampoline \
  -e UUID 31D8D607-8EA8-1805-3498-573BF705B4E4 \
  -e AppId 881448767 \
  -e AppName Desktop
```

- `UUID` = PC 的 UUID，可从 `adb logcat -d | grep "is UUID"` 抓到；
- `AppId` / `AppName` = Sunshine `apps.json` 里的桌面项（默认 `881448767` / `Desktop`）；
- 三个 extra 都是 **字符串**（`getStringExtra`）。

托盘程序里的 `launch_stream_on_phone()` 就是封装的这条命令，UUID 会自动抓取并缓存到
`config.json` 的 `moonlightUuid`。
