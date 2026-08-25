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

# ТОРГОВЫЕ РОБОТЫ НЕ автозапускаются (решение Дмитрия 2026-08-25):
# роботы работают только в режиме, явно выбранном Дмитрием (paper ИЛИ real),
# и запускаются вручную: кнопкой в UI или командой.
# Автозапуск поднимает ТОЛЬКО сервер (UI, данные, риск-панель).

Log "autostart done (server only; robots are manual by design)"
