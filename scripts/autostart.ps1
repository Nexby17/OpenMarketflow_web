# OpenMarketflow autostart: C# server + OF robots (paper)
# Запускается ярлыком из Startup при входе в Windows.

$ErrorActionPreference = "SilentlyContinue"
$log = "C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot\logs\autostart.log"
$proj = "C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web"

function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -Append -FilePath $log -Encoding utf8 }

# Не дублировать: если уже жив — не трогаем
$serverAlive = Get-Process HedgeFund.Server -ErrorAction SilentlyContinue
if (-not $serverAlive) {
    $bin = Join-Path $proj "src\Server\bin\Debug\net10.0"
    # .env и wwwroot на месте?
    if ((Test-Path "$bin\HedgeFund.Server.exe") -and (Test-Path "$bin\.env")) {
        Start-Process -FilePath "$bin\HedgeFund.Server.exe" -WorkingDirectory $bin -WindowStyle Hidden
        Log "server started"
    } else {
        Log "server bin/.env missing: $bin"
    }
} else { Log "server already running ($($serverAlive.Id))" }

Start-Sleep -Seconds 5

# OF-роботы (paper): только если не запущены
$mxAlive = Get-CimInstance Win32_Process -Filter "Name='python3.exe'" | Where-Object { $_.CommandLine -match "main_of_mx\.py" }
if (-not $mxAlive) {
    Start-Process -FilePath "python3" -ArgumentList "main_of_mx.py --paper" -WorkingDirectory "$proj\robot" -WindowStyle Hidden -RedirectStandardOutput "$proj\robot\logs\auto_mx.out" -RedirectStandardError "$proj\robot\logs\auto_mx.err"
    Log "MX robot started (paper)"
} else { Log "MX robot already running" }

$ofAlive = Get-CimInstance Win32_Process -Filter "Name='python3.exe'" | Where-Object { $_.CommandLine -match "main_of\.py" }
if (-not $ofAlive) {
    Start-Process -FilePath "python3" -ArgumentList "main_of.py --paper" -WorkingDirectory "$proj\robot" -WindowStyle Hidden -RedirectStandardOutput "$proj\robot\logs\auto_of.out" -RedirectStandardError "$proj\robot\logs\auto_of.err"
    Log "OF Si robot started (paper)"
} else { Log "OF Si robot already running" }

Log "autostart done"
