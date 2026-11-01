<#
.SYNOPSIS
    Turns the virtual tablet screen on or off.

.DESCRIPTION
    on     - writes the requested mode into vdd_settings.xml, enables the VDD,
             sets resolution/refresh, places it next to the primary monitor and
             forces SDR. Sunshine passes the client's request in
             SUNSHINE_CLIENT_WIDTH / _HEIGHT / _FPS; config.json has fallbacks.
    off    - disables the VDD so the display disappears completely.
    status - prints what is going on.

    Needs admin for on/off. Sunshine runs it elevated as a prep-cmd.
#>
param(
    [ValidateSet('on', 'off', 'status')]
    [string]$Action = 'status'
)

$script:LogTag = "vdisplay $Action"
. "$PSScriptRoot\Common.ps1"

function Get-RequestedMode {
    $w = [int]$env:SUNSHINE_CLIENT_WIDTH
    $h = [int]$env:SUNSHINE_CLIENT_HEIGHT
    $f = [int]$env:SUNSHINE_CLIENT_FPS
    if ($w -le 0 -or $h -le 0) { $w = $Config.fallbackWidth; $h = $Config.fallbackHeight }
    if ($f -le 0) { $f = $Config.fallbackFps }
    [pscustomobject]@{ Width = $w; Height = $h; Hz = $f }
}

# Makes sure the VDD advertises WxH@Hz. Returns $true if the file changed
# (the driver only reads it when the device starts).
function Set-VddSettingsMode($Mode) {
    $path = $Config.vddSettingsPath
    if (-not (Test-Path $path)) { throw "VDD settings not found at $path - run install.ps1" }
    [xml]$xml = Get-Content $path -Raw
    $root = $xml.vdd_settings
    $changed = $false

    if ([int]$root.monitors.count -ne 1) {
        $root.monitors.count = '1'
        $changed = $true
    }

    $known = @($root.resolutions.resolution | Where-Object {
            [int]$_.width -eq $Mode.Width -and [int]$_.height -eq $Mode.Height })
    if ($known.Count -eq 0) {
        $res = $xml.CreateElement('resolution')
        foreach ($pair in @(('width', $Mode.Width), ('height', $Mode.Height), ('refresh_rate', $Mode.Hz))) {
            $el = $xml.CreateElement($pair[0])
            $el.InnerText = [string]$pair[1]
            [void]$res.AppendChild($el)
        }
        [void]$root.SelectSingleNode('resolutions').AppendChild($res)
        $changed = $true
    }

    $rates = @($root.global.g_refresh_rate | ForEach-Object { [int]$_ })
    if ($rates -notcontains $Mode.Hz) {
        $el = $xml.CreateElement('g_refresh_rate')
        $el.InnerText = [string]$Mode.Hz
        [void]$root.SelectSingleNode('global').AppendChild($el)
        $changed = $true
    }

    if ($changed) {
        $xml.Save($path)
        Write-Log "Added $($Mode.Width)x$($Mode.Height)@$($Mode.Hz) to vdd_settings.xml"
    }
    $changed
}

function Wait-VddAdapter {
    $deadline = (Get-Date).AddSeconds($Config.displayWaitSeconds)
    do {
        $a = Get-VddAdapter
        if ($a -and [ExtDisplay]::SupportedModes($a.DeviceName).Count -gt 0) { return $a }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "VDD display did not appear within $($Config.displayWaitSeconds)s"
}

function Select-BestMode($GdiName, $Want) {
    $modes = [ExtDisplay]::SupportedModes($GdiName)
    $sameRes = @($modes | Where-Object { $_.Width -eq $Want.Width -and $_.Height -eq $Want.Height })
    if ($sameRes.Count) {
        return $sameRes | Sort-Object { [math]::Abs($_.Hz - $Want.Hz) } | Select-Object -First 1
    }
    # Closest area at the closest refresh rate
    Write-Log "Driver does not offer $($Want.Width)x$($Want.Height); picking the nearest mode"
    $modes | Sort-Object { [math]::Abs($_.Width * $_.Height - $Want.Width * $Want.Height) },
                         { [math]::Abs($_.Hz - $Want.Hz) } | Select-Object -First 1
}

function Get-Placement($Primary, $Mode) {
    $p = [ExtDisplay]::CurrentMode($Primary.DeviceName)
    $alignX = switch ($Config.align) {
        'start' { $p.X }
        'end' { $p.X + $p.Width - $Mode.Width }
        default { $p.X + [int](($p.Width - $Mode.Width) / 2) }
    }
    $alignY = switch ($Config.align) {
        'start' { $p.Y }
        'end' { $p.Y + $p.Height - $Mode.Height }
        default { $p.Y + [int](($p.Height - $Mode.Height) / 2) }
    }
    switch ($Config.position) {
        'above' { @{ X = $alignX; Y = $p.Y - $Mode.Height } }
        'left' { @{ X = $p.X - $Mode.Width; Y = $alignY } }
        'right' { @{ X = $p.X + $p.Width; Y = $alignY } }
        default { @{ X = $alignX; Y = $p.Y + $p.Height } }   # below
    }
}

function Enable-ExtendedScreen {
    $dev = Get-VddDevice
    if (-not $dev) { throw 'Virtual Display Driver is not installed - run install.ps1' }

    $want = Get-RequestedMode
    Write-Log "Client asked for $($want.Width)x$($want.Height)@$($want.Hz)"
    $settingsChanged = Set-VddSettingsMode $want

    if (Test-VddEnabled $dev) {
        if ($settingsChanged) {
            Write-Log 'Restarting VDD to load new modes'
            Set-VddEnabled $dev.InstanceId $false
            Set-VddEnabled $dev.InstanceId $true
        }
    } else {
        Set-VddEnabled $dev.InstanceId $true
        Write-Log 'VDD enabled'
    }

    $vdd = Wait-VddAdapter
    $primary = Get-PrimaryAdapter
    if ($primary.DeviceName -eq $vdd.DeviceName) { throw 'The virtual display became primary; refusing to continue' }

    $mode = Select-BestMode $vdd.DeviceName $want
    $pos = Get-Placement $primary $mode

    $rc = -1
    for ($i = 0; $i -lt 5 -and $rc -ne 0; $i++) {
        if ($i) { Start-Sleep -Milliseconds 400 }
        $rc = [ExtDisplay]::SetMode($vdd.DeviceName, $mode.Width, $mode.Height, $mode.Hz, $pos.X, $pos.Y)
    }
    if ($rc -ne 0) { throw "ChangeDisplaySettingsEx failed with code $rc" }
    Write-Log "$($vdd.DeviceName) set to $([ExtDisplay]::CurrentMode($vdd.DeviceName))"

    if ($Config.forceSdr) {
        $supported = $false
        $hdr = [ExtDisplay]::HdrState($vdd.DeviceName, [ref]$supported)
        if ($hdr -eq 1) {
            $r = [ExtDisplay]::SetHdr($vdd.DeviceName, $false)
            Write-Log "HDR was on for the virtual display; turned off (rc $r)"
        }
    }
}

function Disable-ExtendedScreen {
    $dev = Get-VddDevice
    if (-not $dev) { Write-Log 'VDD not installed; nothing to do'; return }
    if (Test-VddEnabled $dev) {
        Set-VddEnabled $dev.InstanceId $false
        Write-Log 'VDD disabled'
    } else {
        Write-Log 'VDD already disabled'
    }
}

function Show-Status {
    $dev = Get-VddDevice
    if (-not $dev) { 'VDD device : not installed'; return }
    "VDD device : $($dev.InstanceId) ($(if (Test-VddEnabled $dev) { 'enabled' } else { 'disabled' }))"
    foreach ($a in [ExtDisplay]::ListAdapters() | Where-Object { $_.Attached -or $_.DeviceString -like '*Virtual Display Driver*' }) {
        $supported = $false
        $hdr = [ExtDisplay]::HdrState($a.DeviceName, [ref]$supported)
        $hdrText = switch ($hdr) { 1 { 'HDR' } 0 { 'SDR' } default { '?' } }
        '{0,-13} {1,-32} attached={2,-5} primary={3,-5} {4} {5}' -f $a.DeviceName, $a.DeviceString,
            $a.Attached, $a.Primary, [ExtDisplay]::CurrentMode($a.DeviceName), $hdrText
    }
}

switch ($Action) {
    'status' { Show-Status }
    'off' {
        try { Disable-ExtendedScreen } catch { Write-Log "ERROR: $_"; exit 1 }
    }
    'on' {
        try {
            Enable-ExtendedScreen
        } catch {
            # Sunshine does not run the undo command for a failed do command, so clean up here.
            Write-Log "ERROR: $_"
            try { Disable-ExtendedScreen } catch { Write-Log "Cleanup failed: $_" }
            exit 1
        }
    }
}
exit 0
