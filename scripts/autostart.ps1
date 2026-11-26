# OpenMarketflow autostart: C# server + robot supervisor
# Запускается ярлыком из Startup и дублируется ключом HKCU\...\Run.
# F-238 (26.11.2026): после переустановки .NET 10 остался только в "C:\Program Files\dotnet",
# а в PATH первым стоит комплектный dotnet 8 -> сервер падал "You must install or update .NET".
# Теперь DOTNET_ROOT выставляется явно. Плюс heartbeat-запись в начале, чтобы видеть сам факт запуска.

$ErrorActionPreference = "SilentlyContinue"
$log = "C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot\logs\autostart.log"
$proj = "C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web"

function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Out-File -Append -FilePath $log -Encoding utf8 }

Log "autostart begin (pid=$PID)"

# .NET 10 runtime lives in Program Files, not in the bundled vendor dotnet (8.x)
$dotnetRoot = "C:\Program Files\dotnet"
if (Test-Path (Join-Path $dotnetRoot "dotnet.exe")) {
    $env:DOTNET_ROOT = $dotnetRoot
    $env:PATH = "$dotnetRoot;" + $env:PATH
    Log "DOTNET_ROOT=$dotnetRoot"
} else {
    Log "WARN: $dotnetRoot not found - server may fail to start"
}

# Не дублировать: если уже жив — не трогаем
$serverAlive = Get-Process HedgeFund.Server -ErrorAction SilentlyContinue
if (-not $serverAlive) {
    $bin = Join-Path $proj "src\Server\bin\Debug\net10.0"
    if ((Test-Path "$bin\HedgeFund.Server.exe") -and (Test-Path "$bin\.env")) {
        Start-Process -FilePath "$bin\HedgeFund.Server.exe" -WorkingDirectory $bin -WindowStyle Hidden
        Log "server started"
    } else {
        Log "server bin/.env missing: $bin"
    }
} else { Log "server already running ($($serverAlive.Id))" }

# DataProvider :5060 — нужен роботам для заявок и позиций
$dpAlive = Get-NetTCPConnection -LocalPort 5060 -State Listen -ErrorAction SilentlyContinue
if (-not $dpAlive) {
    $dpDir = Join-Path $proj "DataProvider"
    $envFile = Join-Path $proj "src\Server\bin\Debug\net10.0\.env"
    if ((Test-Path "$dpDir\main.py") -and (Test-Path $envFile)) {
        Get-Content $envFile | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
                [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), 'Process')
            }
        }
        $env:PYTHONUTF8 = "1"
        $env:PYTHONIOENCODING = "utf-8"
        Start-Process -FilePath "D:\AutoClaw\resources\python\python3.exe" -ArgumentList "main.py" `
            -WorkingDirectory $dpDir -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $dpDir "dp_stdout_autostart.log") `
            -RedirectStandardError (Join-Path $dpDir "dp_stderr_autostart.log")
        Log "dataprovider started (:5060)"
    } else { Log "dataprovider skipped (main.py or .env missing)" }
} else { Log "dataprovider already running" }

# Локальный супервизор запуска роботов (:5099) — инфраструктура, не торговля
$supAlive = Get-NetTCPConnection -LocalPort 5099 -State Listen -ErrorAction SilentlyContinue
if (-not $supAlive) {
    Start-Process -FilePath "D:\AutoClaw\resources\python\python3.exe" `
        -ArgumentList (Join-Path $proj "scripts\robot_supervisor.py"), "--port", "5099" `
        -WorkingDirectory $proj -WindowStyle Hidden
    Log "robot supervisor started (:5099)"
} else { Log "robot supervisor already running" }

# ТОРГОВЫЕ РОБОТЫ НЕ АВТОЗАПУСКАЮТСЯ (решение Дмитрия 2026-08-25):
# они работают только в режиме, явно выбранном Дмитрием, и запускаются вручную.
Log "autostart done (server + dataprovider + supervisor; robots stay manual)"
