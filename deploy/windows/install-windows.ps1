<#
.SYNOPSIS
  把大白装成 Windows 上「自己会活、自己会更新」的服务。

.DESCRIPTION
  Linux 上这套由 systemd 承担（deploy/systemd/ + deploy/release/install-update.sh）。
  Windows 没有 systemd，等价物是计划任务：

    DabaiServer          登录时拉起 launch.py；进程崩了自动重启（等价 Restart=always）
    DabaiWatchdog        每 N 分钟探一次端口，没在跑就拉起
    DabaiUpdate          每小时跑一次更新器 --apply（等价 dabai-update.timer）
    DabaiHealth          每 N 分钟体检（等价 dabai-health.timer）
    DabaiSecrets         每 N 分钟同步密钥（等价 dabai-secrets-sync.timer 的低频兜底）
    DabaiLongrun         长跑引擎，登录自启 + 崩溃重启（等价 dabai-longrun.service）
    DabaiLongrunWatchdog 长跑心跳看门狗（等价 dabai-longrun-watchdog.timer）

  全部注册在当前用户名下，不需要管理员权限 —— 大白只监听本机端口，不需要提权。

  为什么服务入口是 launch.py 而不是 server.py：Linux 上密钥由 systemd 的
  EnvironmentFile= 注入，Windows 计划任务没有等价物（Action 只能给一条命令行）。
  launch.py 负责把 secrets.env 读进环境再启动 server.py，让交互式启动与服务启动
  走同一条路径 —— 避免「手动跑有密钥、服务跑没密钥」这种最难查的差异。

  更新器为什么要拷一份副本到 %LOCALAPPDATA%：仓库正是被更新的对象，跑仓库里那份
  会在替换到一半时把正在执行的脚本换掉。可信度不能建立在被更新物之上（同 Linux）。

.PARAMETER Root
  安装目录。默认取本脚本的上两级目录（deploy\windows\ → 仓库根）。

.PARAMETER Port
  体检端口，必须与 settings.json 里服务实际监听的端口一致。

.PARAMETER WatchdogMinutes
  看门狗检查间隔（分钟），默认 5。

.PARAMETER Uninstall
  只卸载计划任务，保留配置与状态（回滚日志、备份都还在）。

.PARAMETER DryRun
  只打印将要做什么，不动系统。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-windows.ps1
  powershell -ExecutionPolicy Bypass -File deploy\windows\install-windows.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Root = "",
    [string]$Port = "8000",
    [int]$WatchdogMinutes = 5,
    [int]$HealthMinutes = 30,
    [int]$SecretsMinutes = 5,
    [switch]$WithoutLongrun,
    [switch]$Uninstall,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$TaskServer    = "DabaiServer"
$TaskWatchdog  = "DabaiWatchdog"
$TaskUpdate    = "DabaiUpdate"
$TaskHealth    = "DabaiHealth"
$TaskSecrets   = "DabaiSecrets"
$TaskLongrun   = "DabaiLongrun"
$TaskLongrunWd = "DabaiLongrunWatchdog"
$AllTasks      = @($TaskServer, $TaskWatchdog, $TaskUpdate, $TaskHealth,
                   $TaskSecrets, $TaskLongrun, $TaskLongrunWd)

function Say([string]$Msg) { Write-Host "  $Msg" }
function Step([string]$Msg) { Write-Host ""; Write-Host $Msg }

function Resolve-DabaiPython {
    param([string]$InstallRoot)
    $cands = @(
        (Join-Path $InstallRoot "venv\Scripts\python.exe"),
        (Join-Path $InstallRoot ".venv\Scripts\python.exe")
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "找不到 python.exe。先跑 dabai.bat --setup 建好环境，或用 -Root 指对目录。"
}

function Remove-DabaiTasks {
    foreach ($n in $AllTasks) {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if (-not $t) { continue }
        if ($DryRun) { Say "[演练] 会删除计划任务 $n" ; continue }
        Unregister-ScheduledTask -TaskName $n -Confirm:$false
        Say "已删除计划任务 $n"
    }
}

# ── 卸载 ────────────────────────────────────────────────────────────────
if ($Uninstall) {
    Write-Host "=== 大白 Windows 卸载（只删计划任务）==="
    if ($DryRun) { Write-Host "（演练模式：只打印计划）" }
    Remove-DabaiTasks
    Write-Host ""
    Write-Host "✓ 已卸载。保留：$env:APPDATA\dabai\update.conf、$env:LOCALAPPDATA\dabai-update"
    Write-Host "  要彻底清干净：删掉上面两个目录（回滚日志与备份会一起没）"
    exit 0
}

# ── 定位 ────────────────────────────────────────────────────────────────
if (-not $Root) { $Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path }
$Root = (Resolve-Path $Root).Path
if (-not (Test-Path (Join-Path $Root "server.py"))) {
    throw "在 $Root 里找不到 server.py —— 用 -Root 指定安装目录"
}

$LibDir  = Join-Path $env:LOCALAPPDATA "dabai-update"
$ConfDir = Join-Path $env:APPDATA "dabai"
$Updater = Join-Path $LibDir "update.py"
$Conf    = Join-Path $ConfDir "update.conf"
$Python  = Resolve-DabaiPython -InstallRoot $Root

# 仓库里的脚本（直接用绝对路径调，不做副本：它们不是被更新的对象）
$Launcher  = Join-Path $Root "deploy\windows\launch.py"
$Health    = Join-Path $Root "tools\linux_health.py"
$Secrets   = Join-Path $Root "deploy\secrets\sync_secrets.py"
$Longrun   = Join-Path $Root "tools\longrun\runner.py"
$LongrunWd = Join-Path $Root "tools\longrun\watchdog.py"

Write-Host "=== 大白 Windows 安装（安装目录 $Root）==="
if ($DryRun) { Write-Host "（演练模式：只打印计划，不动系统）" }

Step "① 前置检查"
Say "python: $Python"
Say "端口:   $Port"
if (Test-Path (Join-Path $Root "deploy\release\update.py")) {
    Say "更新器源码在"
} else {
    throw "找不到 deploy\release\update.py"
}
foreach ($f in @($Launcher, $Health, $Secrets)) {
    if (-not (Test-Path $f)) { throw "找不到 $f（仓库不完整？）" }
}
if (-not $WithoutLongrun) {
    foreach ($f in @($Longrun, $LongrunWd)) {
        if (-not (Test-Path $f)) { throw "找不到 $f（仓库不完整？）" }
    }
}

# ── 装更新器副本 ────────────────────────────────────────────────────────
Step "② 装更新器副本到 $LibDir"
if ($DryRun) {
    Say "[演练] 会拷 deploy\release\update.py → $Updater"
} else {
    New-Item -ItemType Directory -Force -Path $LibDir | Out-Null
    Copy-Item -Force (Join-Path $Root "deploy\release\update.py") $Updater
    Say "update.py 已就位（更新时不跑仓库里那份）"
}

# ── 写配置 ──────────────────────────────────────────────────────────────
Step "③ 写配置 $Conf"
$confBody = @"
# 大白自动更新配置（由 deploy\windows\install-windows.ps1 生成，手改也行）
# 改完不用重启任何东西：更新器每次运行都会重新读这个文件。
ROOT=$Root
REPO=geek-alpha/dabai-linux
SERVICE=$TaskServer
PORT=$Port
ENTRY=server.py
KEEP_BACKUPS=3
HTTP_TIMEOUT=60
"@
if ($DryRun) {
    Say "[演练] 会写入："
    $confBody -split "`n" | ForEach-Object { Say "    $_" }
} else {
    New-Item -ItemType Directory -Force -Path $ConfDir | Out-Null
    Set-Content -Path $Conf -Value $confBody -Encoding UTF8
    Say "配置已写入"
}

# ── 注册计划任务 ────────────────────────────────────────────────────────
Step "④ 注册计划任务（开机自启 + 崩溃自愈 + 定时更新）"

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$updateTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$healthTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(3) `
    -RepetitionInterval (New-TimeSpan -Minutes $HealthMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$secretsTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $SecretsMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$longrunWdTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# 服务要长跑：执行时限设 0 = 不限时，否则 Windows 会到点把它掐掉
$serverSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$jobSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$plans = @(
    @{ Name = $TaskServer; Desc = "大白服务（登录自启，崩溃自动重启）"
       Exe = $Python; ArgLine = "`"$Launcher`""
       Trigger = $logonTrigger; Settings = $serverSettings },
    @{ Name = $TaskWatchdog; Desc = "大白看门狗：端口不通就拉起（每 $WatchdogMinutes 分钟）"
       Exe = $Python; ArgLine = "`"$Updater`" --ensure-running"
       Trigger = $watchTrigger; Settings = $jobSettings },
    @{ Name = $TaskUpdate; Desc = "大白自动更新：每小时检查并装上最新版"
       Exe = $Python; ArgLine = "`"$Updater`" --apply"
       Trigger = $updateTrigger; Settings = $jobSettings },
    @{ Name = $TaskHealth; Desc = "大白体检（每 $HealthMinutes 分钟）"
       Exe = $Python; ArgLine = "`"$Health`""
       Trigger = $healthTrigger; Settings = $jobSettings },
    @{ Name = $TaskSecrets; Desc = "密钥同步（每 $SecretsMinutes 分钟，幂等：无变化不写盘）"
       Exe = $Python; ArgLine = "`"$Secrets`" sync --quiet"
       Trigger = $secretsTrigger; Settings = $jobSettings }
)

# 长跑引擎（无人值守推进长期目标）：Linux 侧是 dabai-longrun.service + 心跳看门狗。
# 它会持续消耗模型额度，所以给一个开关；默认装，与 Linux 侧保持对等。
if (-not $WithoutLongrun) {
    $plans += @{ Name = $TaskLongrun; Desc = "大白长跑引擎（登录自启，崩溃自动重启）"
                 Exe = $Python; ArgLine = "`"$Longrun`" --loop"
                 Trigger = $logonTrigger; Settings = $serverSettings }
    $plans += @{ Name = $TaskLongrunWd; Desc = "长跑心跳看门狗（每 10 分钟，卡死即重启）"
                 Exe = $Python; ArgLine = "`"$LongrunWd`""
                 Trigger = $longrunWdTrigger; Settings = $jobSettings }
}

foreach ($p in $plans) {
    if ($DryRun) {
        Say "[演练] 注册 $($p.Name)：$($p.Exe) $($p.ArgLine)"
        continue
    }
    $action = New-ScheduledTaskAction -Execute $p.Exe -Argument $p.ArgLine -WorkingDirectory $Root
    Register-ScheduledTask -TaskName $p.Name -Action $action -Trigger $p.Trigger `
        -Settings $p.Settings -Description $p.Desc -Force | Out-Null
    Say "已注册 $($p.Name)：$($p.Desc)"
}

# ── 接线自检 ────────────────────────────────────────────────────────────
Step "⑤ 接线自检"
if ($DryRun) {
    Say "[演练] 跳过自检"
    Write-Host ""
    Write-Host "✓ 演练完成，未动系统。正式安装：去掉 -DryRun"
    exit 0
}

Say "跑一次 --check（只查版本，不写盘）："
& $Python $Updater --check
if ($LASTEXITCODE -eq 0) {
    Say "更新器能跑起来"
} else {
    Say "! --check 返回 $LASTEXITCODE。常见原因：GITHUB_TOKEN 没配（仓库是私有的），"
    Say "  或网络到不了 api.github.com。token 放 $ConfDir\secrets.env，一行 GITHUB_TOKEN=..."
}

Say "探一次端口 $Port："
$listen = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listen) {
    Say "服务在跑（pid $($listen[0].OwningProcess)）"
} else {
    Say "现在没在跑 —— 看门狗会在 $WatchdogMinutes 分钟内把它拉起来，也可以立刻手动起："
    Say "  $Python `"$Updater`" --ensure-running"
}

Say "注册结果："
Get-ScheduledTask -TaskName "Dabai*" -ErrorAction SilentlyContinue |
    Sort-Object TaskName |
    ForEach-Object { Say ("  {0,-22} {1}" -f $_.TaskName, $_.State) }

Write-Host ""
Write-Host "✓ 安装完成"
Write-Host "  立刻起服务：  $Python `"$Updater`" --ensure-running"
Write-Host "  手动更新一次：$Python `"$Updater`" --apply"
Write-Host "  演练一次：    $Python `"$Updater`" --dry-run"
Write-Host "  体检一次：    $Python `"$Health`""
Write-Host "  同步密钥：    $Python `"$Secrets`" sync"
Write-Host "  看长跑心跳：  $Python `"$LongrunWd`" --dry-run"
Write-Host "  看任务状态：  Get-ScheduledTask -TaskName Dabai* | Format-Table TaskName,State"
Write-Host "  看更新日志：  Get-Content `"$LibDir\update.log`" -Tail 50"
Write-Host "  不装长跑引擎：加 -WithoutLongrun"
Write-Host "  卸载：        powershell -ExecutionPolicy Bypass -File $PSCommandPath -Uninstall"
