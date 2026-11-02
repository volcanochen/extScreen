# -*- coding: utf-8 -*-
"""设置窗口：用 PowerShell + WinForms 原生对话框，避免依赖 tkinter。"""
import base64
import json
import os

from extscreen_core import EXT_CONFIG, load_ext_config, log, run

SETTINGS_PS = r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$cfgPath = 'C:\Tools\moonlight-desktop-extender\config.json'
$cfg = $null
if (Test-Path $cfgPath) {
    try { $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json } catch { $cfg = $null }
}

function Cfg($name, $default) {
    if ($cfg -and $cfg.PSObject.Properties[$name]) {
        $v = $cfg.$name
        if ($null -ne $v) { return $v }
    }
    return $default
}

$curRes   = [string](Cfg 'resolution' 'auto')
$curOrient= [string](Cfg 'orientation' 'portrait')
$curPos   = [string](Cfg 'position' 'right')
$curAlign = [string](Cfg 'align' 'start')
$curAudio = [bool](Cfg 'streamAudio' $false)

# W=0,H=0 表示「跟随手机屏幕」。其余为手动档位，必须与 extscreen_core.py 的
# RESOLUTION_PRESETS 保持一致，且都已在 extscreen_dock.py 的 VDD_XML 模板中声明。
$presets = @(
    [pscustomobject]@{ T = '自动：跟随手机屏幕铺满（推荐）'; W = 0;     H = 0     },
    [pscustomobject]@{ T = '1440 x 2960   竖屏 · 最细腻（字最小）'; W = 1440; H = 2960 },
    [pscustomobject]@{ T = '1080 x 2220   竖屏 · 默认';             W = 1080; H = 2220 },
    [pscustomobject]@{ T = '900 x 1850    竖屏 · 字稍大';           W = 900;  H = 1850 },
    [pscustomobject]@{ T = '800 x 1646    竖屏 · 字更大';           W = 800;  H = 1646 },
    [pscustomobject]@{ T = '720 x 1480    竖屏 · 字明显更大';        W = 720;  H = 1480 },
    [pscustomobject]@{ T = '640 x 1320    竖屏 · 字很大';           W = 640;  H = 1320 },
    [pscustomobject]@{ T = '540 x 1110    竖屏 · 字最大最清楚';      W = 540;  H = 1110 },
    [pscustomobject]@{ T = '1920 x 1080   横屏 · 标准';             W = 1920; H = 1080 },
    [pscustomobject]@{ T = '2220 x 1080   横屏 · 宽屏';             W = 2220; H = 1080 },
    [pscustomobject]@{ T = '1280 x 720    横屏 · 轻量';             W = 1280; H = 720  }
)
$orientMap = @('landscape', 'portrait')
$orientTxt = @('横屏', '竖屏')
$posMap    = @('right', 'left', 'above', 'below')
$posTxt    = @('右侧', '左侧', '上方', '下方')
$alignMap  = @('start', 'center', 'end')
$alignTxt  = @('顶部', '居中', '底部')

$form = New-Object System.Windows.Forms.Form
$form.Text = 'ExtScreen 副屏助手 — 设置'
$form.ClientSize = New-Object System.Drawing.Size(430, 348)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true
$form.AutoScaleMode = 'None'
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$form.BackColor = [System.Drawing.Color]::FromArgb(250, 250, 250)

function Add-Label($text, $x, $y) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text
    $l.Location = New-Object System.Drawing.Point($x, $y)
    $l.AutoSize = $true
    $l.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 9, [System.Drawing.FontStyle]::Bold)
    $l.ForeColor = [System.Drawing.Color]::FromArgb(50, 50, 50)
    $form.Controls.Add($l)
}

function Add-Combo($x, $y, $w, $items, $sel) {
    $c = New-Object System.Windows.Forms.ComboBox
    $c.Location = New-Object System.Drawing.Point($x, $y)
    $c.Size = New-Object System.Drawing.Size($w, 26)
    $c.DropDownStyle = 'DropDownList'
    $c.FlatStyle = 'Flat'
    foreach ($i in $items) { [void]$c.Items.Add($i) }
    $c.SelectedIndex = $sel
    $form.Controls.Add($c)
    return $c
}

# ---------------- 分辨率 ----------------
Add-Label '扩展屏分辨率' 20 14
$resSel = 0
if ($curRes -eq 'auto') {
    $resSel = 0
} else {
    for ($i = 1; $i -lt $presets.Count; $i++) {
        $tag = "$($presets[$i].W)x$($presets[$i].H)"
        if ($curRes -eq $tag) { $resSel = $i }
    }
}
$resItems = @()
foreach ($p in $presets) { $resItems += $p.T }
$cbRes = Add-Combo 20 38 390 $resItems $resSel

# ---------------- 方向 / 位置 / 对齐 ----------------
Add-Label '画面方向' 20 78
Add-Label '相对主屏位置' 160 78
Add-Label '对齐方式' 300 78

$orientSel = [array]::IndexOf($orientMap, $curOrient)
if ($orientSel -lt 0) { $orientSel = 0 }
$cbOrient = Add-Combo 20 102 120 $orientTxt $orientSel

$posSel = [array]::IndexOf($posMap, $curPos)
if ($posSel -lt 0) { $posSel = 0 }
$cbPos = Add-Combo 160 102 120 $posTxt $posSel

$alignSel = [array]::IndexOf($alignMap, $curAlign)
if ($alignSel -lt 0) { $alignSel = 0 }
$cbAlign = Add-Combo 300 102 110 $alignTxt $alignSel

# 手动指定分辨率时，方向没意义，置灰
$syncOrient = {
    if ($cbRes.SelectedIndex -eq 0) {
        $cbOrient.Enabled = $true
    } else {
        $cbOrient.Enabled = $false
    }
}
$cbRes.Add_SelectedIndexChanged($syncOrient)
& $syncOrient

$chkAudio = New-Object System.Windows.Forms.CheckBox
$chkAudio.Text = '把电脑声音一起传到手机'
$chkAudio.Location = New-Object System.Drawing.Point(22, 144)
$chkAudio.AutoSize = $true
$chkAudio.Checked = $curAudio
$form.Controls.Add($chkAudio)

$chkStart = New-Object System.Windows.Forms.CheckBox
$chkStart.Text = '开机自动启动托盘助手'
$chkStart.Location = New-Object System.Drawing.Point(22, 172)
$chkStart.AutoSize = $true
$form.Controls.Add($chkStart)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "手机 PPI 比电脑高，虚拟屏渲染像素越密，手机上越小越糊。" + `
             [Environment]::NewLine + `
             "把分辨率调低（如 720x1480），Windows 在更少像素上画同样桌面，" + `
             [Environment]::NewLine + `
             "手机端拉伸后文字更大更清楚——多试几个档位挑最舒服的。" + `
             [Environment]::NewLine + `
             "扩展屏是块独立虚拟显示器：鼠标从主屏边缘滑过去即可操作。"
$hint.Location = New-Object System.Drawing.Point(20, 200)
$hint.Size = New-Object System.Drawing.Size(390, 78)
$hint.ForeColor = [System.Drawing.Color]::FromArgb(120, 120, 120)
$form.Controls.Add($hint)

$sep = New-Object System.Windows.Forms.Label
$sep.BorderStyle = 'Fixed3D'
$sep.Location = New-Object System.Drawing.Point(20, 286)
$sep.Size = New-Object System.Drawing.Size(390, 2)
$form.Controls.Add($sep)

$btnOK = New-Object System.Windows.Forms.Button
$btnOK.Text = '保存'
$btnOK.Location = New-Object System.Drawing.Point(230, 304)
$btnOK.Size = New-Object System.Drawing.Size(88, 32)
$btnOK.BackColor = [System.Drawing.Color]::FromArgb(0, 120, 215)
$btnOK.ForeColor = [System.Drawing.Color]::White
$btnOK.FlatStyle = 'Flat'
$form.Controls.Add($btnOK)

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Text = '取消'
$btnCancel.Location = New-Object System.Drawing.Point(326, 304)
$btnCancel.Size = New-Object System.Drawing.Size(88, 32)
$btnCancel.FlatStyle = 'Flat'
$form.Controls.Add($btnCancel)

$global:EXT_RESULT = $null

$btnCancel.Add_Click({ $form.Close() })

$btnOK.Add_Click({
    $p = $presets[$cbRes.SelectedIndex]
    if ([int]$p.W -eq 0) {
        $res = 'auto'
    } else {
        $res = "$($p.W)x$($p.H)"
    }
    $obj = [ordered]@{
        action      = 'save'
        resolution  = [string]$res
        orientation = [string]$orientMap[$cbOrient.SelectedIndex]
        position    = [string]$posMap[$cbPos.SelectedIndex]
        align       = [string]$alignMap[$cbAlign.SelectedIndex]
        audio       = [bool]$chkAudio.Checked
        autostart   = [bool]$chkStart.Checked
    }
    $global:EXT_RESULT = ($obj | ConvertTo-Json -Compress)
    $form.Close()
})

[void]$form.ShowDialog()
$form.Dispose()

if ($global:EXT_RESULT) { Write-Output ('RESULT:' + $global:EXT_RESULT) }
'''


def show_settings():
    """弹出设置窗口。返回 (saved, payload_or_message)。"""
    encoded = base64.b64encode(SETTINGS_PS.encode('utf-16-le')).decode('ascii')
    rc, out = run(
        ['powershell.exe', '-NoProfile', '-Sta', '-ExecutionPolicy', 'Bypass',
         '-EncodedCommand', encoded],
        timeout=600,
    )
    log(f'settings rc={rc} out={out.strip()[:300]}')

    payload = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('RESULT:'):
            try:
                payload = json.loads(line[len('RESULT:'):])
            except Exception as e:
                log(f'settings parse error: {e}')
            break

    if not payload or payload.get('action') != 'save':
        return False, '已取消'

    cfg = load_ext_config()
    cfg['resolution'] = payload.get('resolution', 'auto')
    cfg['orientation'] = payload.get('orientation', 'portrait')
    cfg['position'] = payload.get('position', 'right')
    cfg['align'] = payload.get('align', 'start')
    cfg['streamAudio'] = payload.get('audio', False)
    # 同步旧的 fallback 字段，供未接手机时兜底
    if payload.get('resolution', 'auto') != 'auto':
        try:
            w, h = payload['resolution'].split('x')
            cfg['fallbackWidth'] = int(w)
            cfg['fallbackHeight'] = int(h)
        except Exception:
            pass

    try:
        os.makedirs(os.path.dirname(EXT_CONFIG), exist_ok=True)
        with open(EXT_CONFIG, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f'写入配置失败：{e}'

    return True, payload
