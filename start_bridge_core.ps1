# 桥的守护循环：崩溃/微信掉线就等一分钟重来。
# 由计划任务在登录时调用，也可以手动跑： powershell -File start_bridge.ps1
#
# 单实例：如果已经有桥在跑，本守护直接退出。否则计划任务被触发两次、或者手动
# 又点了一次，就会有两个桥同时轮询数据库 -> 同一条消息回两遍（等于主动刷屏）。
$ErrorActionPreference = "Continue"

# 单实例闸门：命名互斥体。计划任务每 5 分钟戳一次这个脚本，抢不到锁就说明已经有
# 一个守护在跑，本进程立刻退出。比"扫描进程命令行"可靠——命令行扫描会因为 CIM 瞬时
# 失败或时序抖动判断错，多起一个守护就会有两个桥同时回消息（同一条消息回两遍）。
$mutex = New-Object System.Threading.Mutex($false, "Local\DSHWeChatBridgeWrapper")
$haveLock = $false
try {
    $haveLock = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $haveLock = $true   # 上一个守护被强杀，锁被遗弃，这里接管
}
if (-not $haveLock) {
    Write-Host "已有守护在运行（互斥体被占用），本进程退出"
    exit 0
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Get-BridgeProcs {
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like "*run_v4.py*" })
    } catch {
        return @()
    }
}

# 面板（127.0.0.1:8765）：已经在跑就复用，没有就悄悄起一个，不弹浏览器。
try {
    $panels = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -like "*panel.py*" })
    if ($panels.Count -eq 0) {
        Start-Process -FilePath $py -ArgumentList "panel.py", "8765", "--no-open" `
            -WorkingDirectory $root -WindowStyle Hidden | Out-Null
    }
} catch {
}

# 再确认一次桥：互斥体只保护"守护"这一层，万一守护刚崩、桥还活着（孤儿进程），
# 这里就别再起第二个。连查三次，避免 CIM 瞬时失败导致误判。
$already = @()
for ($i = 0; $i -lt 3; $i++) {
    $already = Get-BridgeProcs
    if ($already.Count -gt 0) { break }
    Start-Sleep -Seconds 1
}
if ($already.Count -gt 0) {
    $pids = ($already | ForEach-Object { $_.ProcessId }) -join ","
    Write-Host "已有桥在运行（PID $pids），本守护进程退出"
    exit 0
}

# 已经迁到 AstrBot 了：只要这个标记文件在，旧桥就直接退出，避免两套同时在微信里回话。
$flagFile = Join-Path $root ".DISABLED_ASTRBOT_MODE"
if (Test-Path $flagFile) { Write-Host "旧桥已停用（AstrBot 接管），退出"; exit 0 }
while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $log = Join-Path $logs "bridge-$stamp.log"
    Set-Location $root
    & $py "run_v4.py" *>&1 | Tee-Object -FilePath $log -Append
    $code = $LASTEXITCODE
    # 0 = 正常退出（用户 Ctrl+C / 另一个实例已存在），别再拉起来
    if ($code -eq 0) { break }
    "$(Get-Date -Format 'HH:mm:ss') 退出码 $code，60 秒后重试" | Tee-Object -FilePath $log -Append
    Start-Sleep -Seconds 60
}
