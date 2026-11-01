# Shared helpers, dot-sourced by every script in this folder.
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Config = Get-Content (Join-Path $ProjectRoot 'config.json') -Raw | ConvertFrom-Json
$LogDir = Join-Path $ProjectRoot 'logs'
$LogFile = Join-Path $LogDir 'extender.log'
New-Item -ItemType Directory -Force $LogDir | Out-Null

# Sunshine's install folder: config.json override, else from the service binary
# (<dir>\tools\sunshinesvc.exe), else the default location.
function Get-SunshineDir {
    if ($Config.sunshineDir) { return $Config.sunshineDir }
    $svc = Get-CimInstance Win32_Service -Filter "Name='$($Config.sunshineService)'" -ErrorAction SilentlyContinue
    if ($svc -and $svc.PathName) {
        $p = $svc.PathName
        $exe = if ($p.StartsWith('"')) { $p.Substring(1, $p.IndexOf('"', 1) - 1) } else { ($p -split ' ')[0] }
        return Split-Path -Parent (Split-Path -Parent $exe)
    }
    Join-Path $env:ProgramFiles 'Sunshine'
}
$SunshineDir = Get-SunshineDir
$SunshineLog = Join-Path $SunshineDir 'config\sunshine.log'

if (-not ('ExtDisplay' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'Display.cs')
}
[void][ExtDisplay]::MakeDpiAware()

function Write-Log([string]$Message) {
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 1MB) {
        Move-Item $LogFile "$LogFile.old" -Force
    }
    $line = '[{0:yyyy-MM-dd HH:mm:ss.fff}] [{1}] {2}' -f (Get-Date), $script:LogTag, $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Re-runs a script elevated, waits for it and echoes what it logged, so the
# result shows up in the window the user started it from. Returns the exit code.
function Invoke-Elevated([string]$ScriptPath, [string[]]$Arguments = @()) {
    $before = if (Test-Path $LogFile) { (Get-Item $LogFile).Length } else { 0 }
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$ScriptPath`"") + $Arguments
    try {
        $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList $argList
    } catch {
        Write-Host 'Cancelled: administrator rights are needed.' -ForegroundColor Yellow
        return 1
    }
    if (Test-Path $LogFile) {
        $fs = [IO.File]::Open($LogFile, 'Open', 'Read', 'ReadWrite')
        try {
            if ($fs.Length -ge $before) { [void]$fs.Seek($before, 'Begin') }
            Write-Host (New-Object IO.StreamReader($fs)).ReadToEnd()
        } finally { $fs.Dispose() }
    }
    $p.ExitCode
}

# The PnP device of the Virtual Display Driver (present whether enabled or disabled).
function Get-VddDevice {
    Get-PnpDevice -Class Display -ErrorAction SilentlyContinue |
        Where-Object { $_.HardwareID -contains $Config.vddHardwareId } |
        Select-Object -First 1
}

function Test-VddEnabled($Device) {
    # ConfigManagerErrorCode 22 = disabled by the user
    $Device.ConfigManagerErrorCode -ne 22
}

# Uses the PnpDevice cmdlets rather than pnputil: pnputil can lose track of the
# root-enumerated VDD ("/enum-devices /instanceid" finds nothing and
# "/enable-device" fails with 1167, device not connected) while these keep working.
function Set-VddEnabled([string]$InstanceId, [bool]$Enabled) {
    if ($Enabled) { Enable-PnpDevice -InstanceId $InstanceId -Confirm:$false }
    else {
        # Disable-PnpDevice waits for the device to drain and can block for
        # minutes when something still holds it open (Sunshine's DXGI
        # desktop-duplication handle does exactly that). It has no -Timeout
        # parameter in Windows PowerShell 5.1, so there is no way to cap it.
        # pnputil marks the device disabled right away and is measurably fast
        # (~0.3s), so use it as the primary path and keep the cmdlet only as
        # a fallback for the case pnputil cannot resolve the instance id.
        try {
            Invoke-Pnputil 'disable-device' $InstanceId
            Write-Log 'VDD disabled via pnputil'
        } catch {
            Write-Log "pnputil disable failed, falling back to Disable-PnpDevice: $_"
            Disable-PnpDevice -InstanceId $InstanceId -Confirm:$false
        }
    }
}

function Invoke-Pnputil([string]$Verb, [string]$InstanceId) {
    # Windows PowerShell turns native stderr into terminating errors under 'Stop'.
    $ErrorActionPreference = 'Continue'
    $out = & pnputil.exe "/$Verb" $InstanceId 2>&1 | Out-String
    # 3010 = success, reboot required (not expected for this driver)
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 3010) {
        throw "pnputil /$Verb failed (exit $LASTEXITCODE): $($out.Trim())"
    }
}

# The GDI adapter (\\.\DISPLAYn) that belongs to the VDD, or $null.
function Get-VddAdapter {
    [ExtDisplay]::ListAdapters() |
        Where-Object { $_.DeviceString -like '*Virtual Display Driver*' } |
        Select-Object -First 1
}

function Get-PrimaryAdapter {
    [ExtDisplay]::ListAdapters() | Where-Object { $_.Primary } | Select-Object -First 1
}
