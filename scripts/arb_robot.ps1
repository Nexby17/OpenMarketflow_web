# ═══════════════════════════════════════════════════════════════
#  Арбитражный робот для Альфа-Директ 5 (WebSocket PRO API)
#  Запуск: powershell -ExecutionPolicy Bypass -File arb_robot.ps1
#  Требование: терминал Альфа-Директ 5 запущен и авторизован
# ═══════════════════════════════════════════════════════════════

# === НАСТРОЙКИ (МЕНЯЙ ПОД СЕБЯ) ===
$SPOT_TICKER  = "SBER"          # Акция
$FUT_TICKER   = "SBRF"          # Фьючерс (ищем по тикеру)
$WINDOW       = 15              # Окно Z-score (количество наблюдений)
$ENTRY_Z      = 1.0             # Z-score порог входа
$EXIT_Z       = 0.0             # Z-score порог выхода
$STOP_Z       = 3.5             # Z-score стоп
$SPOT_LOTS    = 100             # Лоты акций (в штуках)
$FUT_LOTS     = 1               # Контракты фьючерса
$LOT_SIZE     = 100             # Акций в 1 контракте фьючерса
$CHECK_INTERVAL = 30            # Секунд между проверками
$DRY_RUN      = $true           # $true = только смотрим, $false = торгуем

# === WebSocket ===
$WS_URL = "ws://127.0.0.1:3366/router/"
$ws = $null
$requestId = 0
$responses = @{}
$subscriptionData = @{}
$connected = $false

# Состояние стратегии
$position = "NONE"  # NONE, LONG_SPREAD, SHORT_SPREAD
$basisHistory = [System.Collections.ArrayList]::new()
$spotIdFi = 0
$futIdFi = 0
$spotIdObject = 0
$futIdObject = 0
$spotIdMarketBoard = 0
$futIdMarketBoard = 0
$idAccount = 0
$idSubAccount = 0
$idRazdel = 0
$idRazdelForts = 0
$totalPnL = 0
$trades = 0

function Log($msg, $color = "White") {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $msg" -ForegroundColor $color
}

function NextId { $script:requestId++; return "$($script:requestId)" }

function SendWs($obj) {
    if ($ws.State -ne 'Open') { return }
    $json = $obj | ConvertTo-Json -Compress -Depth 5
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $segment = [System.ArraySegment[byte]]::new($bytes)
    $ws.SendAsync($segment, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, [System.Threading.CancellationToken]::None).Wait()
}

function SendRequest($channel, $payload, $timeoutSec = 10) {
    $id = NextId
    $payloadJson = $payload | ConvertTo-Json -Compress -Depth 5
    SendWs @{ Command = "request"; Channel = $channel; Id = $id; Payload = $payloadJson }
    
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        ReceiveMessages
        if ($responses.ContainsKey($id)) {
            $result = $responses[$id]
            $responses.Remove($id)
            return $result
        }
        Start-Sleep -Milliseconds 200
    }
    return $null
}

function ReceiveMessages {
    $buffer = [byte[]]::new(65536)
    while ($ws.State -eq 'Open') {
        $segment = [System.ArraySegment[byte]]::new($buffer)
        $cts = [System.Threading.CancellationTokenSource]::new(100)
        try {
            $result = $ws.ReceiveAsync($segment, $cts.Token).GetAwaiter().GetResult()
            if ($result.MessageType -eq 'Text') {
                $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $result.Count)
                HandleMessage $text
            }
        } catch {
            break  # таймаут — нет сообщений
        } finally {
            $cts.Dispose()
        }
    }
}

function HandleMessage($raw) {
    $msg = $raw | ConvertFrom-Json
    $command = $msg.Command
    $channel = $msg.Channel
    $id = $msg.Id
    
    $payload = $null
    if ($msg.Payload) {
        try { $payload = $msg.Payload | ConvertFrom-Json } catch {}
    }
    
    # Ответ на запрос
    if ($command -eq "response" -and $id -and $payload) {
        $responses[$id] = $payload
        $type = $payload.Type
        if ($payload.Data) {
            $subscriptionData[$type] = $payload.Data
            ProcessData $type $payload.Data
        }
    }
    
    # Broadcast (обновления)
    if ($command -eq "broadcast" -and $payload) {
        $type = $payload.Type
        if ($payload.Updated) {
            ProcessData $type $payload.Updated
        }
    }
    
    # Состояние терминала
    if ($channel -eq "#ConnectionState.Bus" -and $payload) {
        $auth = $payload.States.User.AuthStatus
        $login = $payload.States.User.Login
        if ($auth -eq 2) { $script:connected = $true }
    }
}

function ProcessData($type, $data) {
    switch ($type) {
        "ClientAccountEntity" {
            foreach ($item in $data) {
                $script:idAccount = $item.IdAccount
                Log "  Счёт: $($item.IdAccount)" Cyan
            }
        }
        "ClientSubAccountEntity" {
            foreach ($item in $data) {
                $script:idSubAccount = $item.IdSubAccount
                Log "  Субсчёт: $($item.IdSubAccount)" Cyan
            }
        }
        "SubAccountRazdelEntity" {
            foreach ($item in $data) {
                $group = $item.IdRazdelGroup
                $rcode = $item.RCode
                if ($rcode -eq "MICEX" -or ($group -eq 1 -and $rcode -eq "ALFA")) {
                    $script:idRazdel = $item.IdRazdel
                }
                if ($rcode -eq "FORTS") {
                    $script:idRazdelForts = $item.IdRazdel
                }
            }
        }
        "AssetInfoEntity" {
            foreach ($item in $data) {
                $ticker = $item.Ticker
                if ($ticker -eq $SPOT_TICKER) {
                    $script:spotIdObject = $item.IdObject
                    $instr = $item.Instruments | Where-Object { $_.IsLiquid } | Select-Object -First 1
                    if (-not $instr) { $instr = $item.Instruments | Select-Object -First 1 }
                    if ($instr) {
                        $script:spotIdFi = $instr.IdFi
                        $script:spotIdMarketBoard = $instr.IdMarketBoard
                    }
                    Log "  СПОТ: $ticker IdObj=$($item.IdObject) IdFi=$($script:spotIdFi) Board=$($script:spotIdMarketBoard)" Green
                }
                if ($ticker -like "$FUT_TICKER*") {
                    $script:futIdObject = $item.IdObject
                    $instr = $item.Instruments | Where-Object { $_.IsLiquid } | Select-Object -First 1
                    if (-not $instr) { $instr = $item.Instruments | Select-Object -First 1 }
                    if ($instr) {
                        $script:futIdFi = $instr.IdFi
                        $script:futIdMarketBoard = $instr.IdMarketBoard
                    }
                    Log "  ФЬЮЧЕРС: $ticker IdObj=$($item.IdObject) IdFi=$($script:futIdFi) Board=$($script:futIdMarketBoard)" Yellow
                }
            }
        }
    }
}

function GetPrice($idFi) {
    # Подписка + получение
    SendWs @{ Command = "listen"; Channel = "#Data.Bus.FinInfoLastEntity" }
    $resp = SendRequest "#Data.Query" @{ Type = "FinInfoLastEntity"; Keys = @($idFi); Init = $true }
    if ($resp -and $resp.Data) {
        $fi = $resp.Data | Where-Object { $_.IdFi -eq $idFi } | Select-Object -First 1
        if ($fi) { return $fi.Last }
    }
    return 0
}

function PlaceOrder($idObject, $idMarketBoard, $buySell, $quantity, $comment, $isSpot) {
    $razdel = if ($isSpot) { $idRazdel } else { $idRazdelForts }
    $group = if ($isSpot) { 1 } else { 4 }  # Stocks / Futures
    
    # Ищем AllowedOrderParams (рыночная заявка)
    $idAllowed = 0
    if ($subscriptionData.ContainsKey("AllowedOrderParamEntity")) {
        foreach ($p in $subscriptionData["AllowedOrderParamEntity"]) {
            if ($p.IdObjectGroup -eq $group -and $p.IdMarketBoard -eq $idMarketBoard -and $p.IdOrderType -eq 1 -and $p.IdDocumentType -eq 1) {
                $idAllowed = $p.IdAllowedOrderParams
                break
            }
        }
    }
    
    $order = @{
        IdAccount = $idAccount
        IdSubAccount = $idSubAccount
        IdRazdel = $razdel
        IdPriceControlType = 3
        IdObject = $idObject
        BuySell = $buySell  # 1=buy, -1=sell
        Quantity = $quantity
        Comment = $comment
        IdAllowedOrderParams = $idAllowed
    }
    
    if ($DRY_RUN) {
        $dir = if ($buySell -eq 1) { "BUY" } else { "SELL" }
        Log "  [DRY RUN] $dir $quantity шт obj=$idObject allowed=$idAllowed razdel=$razdel" Magenta
        return @{ ResponseStatus = 0; DryRun = $true }
    }
    
    $resp = SendRequest "#Order.Enter.Query" $order 15
    return $resp
}

# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

Log "═══════════════════════════════════════════════" Cyan
Log "  АРБИТРАЖНЫЙ РОБОТ v1.0" Cyan
Log "  Пара: $SPOT_TICKER / $FUT_TICKER" Cyan
Log "  Z-score: вход=$ENTRY_Z выход=$EXIT_Z стоп=$STOP_Z" Cyan
Log "  Лоты: спот=$SPOT_LOTS фьючерс=$FUT_LOTS" Cyan
Log "  Режим: $(if ($DRY_RUN) {'БУМАЖНАЯ ТОРГОВЛЯ'} else {'РЕАЛЬНАЯ ТОРГОВЛЯ'})" $(if ($DRY_RUN) {'Yellow'} else {'Red'})
Log "═══════════════════════════════════════════════" Cyan

# 1. Подключение
Log "🔌 Подключение к ws://127.0.0.1:3366/router/..."
$ws = [System.Net.WebSockets.ClientWebSocket]::new()
try {
    $ws.ConnectAsync([Uri]$WS_URL, [System.Threading.CancellationToken]::None).Wait()
    Log "✅ WebSocket подключён!" Green
} catch {
    Log "❌ Не удалось подключиться. Терминал v5 запущен?" Red
    Read-Host "Нажмите Enter"
    exit
}

# 2. Подписки
Log "📡 Подписка на данные..."
SendWs @{ Command = "listen"; Channel = "#ConnectionState.Bus" }
SendWs @{ Command = "listen"; Channel = "#Data.Bus.AssetInfoEntity" }
SendWs @{ Command = "listen"; Channel = "#Data.Bus.ClientAccountEntity" }
SendWs @{ Command = "listen"; Channel = "#Data.Bus.ClientSubAccountEntity" }
SendWs @{ Command = "listen"; Channel = "#Data.Bus.SubAccountRazdelEntity" }
SendWs @{ Command = "listen"; Channel = "#Data.Bus.AllowedOrderParamEntity" }
SendWs @{ Command = "listen"; Channel = "#Data.Bus.OrderEntity" }

# Запрашиваем данные
SendRequest "#Data.Query" @{ Type = "AssetInfoEntity"; Init = $true } 15 | Out-Null
SendRequest "#Data.Query" @{ Type = "ClientAccountEntity"; Init = $true } 5 | Out-Null
SendRequest "#Data.Query" @{ Type = "ClientSubAccountEntity"; Init = $true } 5 | Out-Null
SendRequest "#Data.Query" @{ Type = "SubAccountRazdelEntity"; Init = $true } 5 | Out-Null
SendRequest "#Data.Query" @{ Type = "AllowedOrderParamEntity"; Init = $true } 5 | Out-Null

Start-Sleep 3
ReceiveMessages

# 3. Проверяем что нашли инструменты
if ($spotIdFi -eq 0) { Log "❌ Не нашёл $SPOT_TICKER! Проверь тикер." Red; Read-Host; exit }
if ($futIdFi -eq 0) { Log "❌ Не нашёл $FUT_TICKER! Проверь тикер." Red; Read-Host; exit }
if ($idAccount -eq 0) { Log "❌ Не получил счёт!" Red; Read-Host; exit }

Log "" 
Log "═══ ГОТОВ К РАБОТЕ ═══" Green
Log "  Спот: $SPOT_TICKER IdFi=$spotIdFi IdObj=$spotIdObject"
Log "  Фьючерс: $FUT_TICKER IdFi=$futIdFi IdObj=$futIdObject"
Log "  Счёт: $idAccount Субсчёт: $idSubAccount"
Log "  Раздел РЦБ: $idRazdel ФОРТС: $idRazdelForts"
Log ""

# 4. Главный цикл
$iteration = 0
while ($true) {
    $iteration++
    ReceiveMessages
    
    # Получаем цены
    $spotPrice = GetPrice $spotIdFi
    $futPrice = GetPrice $futIdFi
    
    if ($spotPrice -le 0 -or $futPrice -le 0) {
        Log "[#$iteration] Нет цен: спот=$spotPrice фьючерс=$futPrice" Yellow
        Start-Sleep $CHECK_INTERVAL
        continue
    }
    
    # Базис (% годовых, упрощённо)
    $basisPct = ($futPrice / ($spotPrice * $LOT_SIZE) - 1) * 100
    $basisAnnual = $basisPct * 365 / 60  # ~60 дней до экспирации
    
    # Добавляем в историю
    [void]$basisHistory.Add($basisAnnual)
    if ($basisHistory.Count -gt $WINDOW) { $basisHistory.RemoveAt(0) }
    
    # Z-score
    $zScore = 0
    if ($basisHistory.Count -ge $WINDOW) {
        $mean = ($basisHistory | Measure-Object -Average).Average
        $std = [Math]::Sqrt(($basisHistory | ForEach-Object { ($_ - $mean) * ($_ - $mean) } | Measure-Object -Sum).Sum / $WINDOW)
        if ($std -gt 0.001) { $zScore = ($basisAnnual - $mean) / $std }
    }
    
    # Статус
    $posEmoji = switch ($position) { "NONE" { "⚪" } "LONG_SPREAD" { "🟢" } "SHORT_SPREAD" { "🔴" } }
    Log "[#$iteration] $posEmoji Спот=$([Math]::Round($spotPrice,2)) Фьюч=$([Math]::Round($futPrice,2)) Базис=$([Math]::Round($basisAnnual,1))% Z=$([Math]::Round($zScore,2)) Поз=$position PnL=$totalPnL Сделок=$trades" Cyan
    
    # Торговая логика
    if ($basisHistory.Count -lt $WINDOW) {
        Log "  Прогрев: $($basisHistory.Count)/$WINDOW" Gray
        Start-Sleep $CHECK_INTERVAL
        continue
    }
    
    # === НЕТ ПОЗИЦИИ ===
    if ($position -eq "NONE") {
        if ($zScore -gt $ENTRY_Z) {
            Log "🟢 ВХОД: LONG SPREAD (Z=$([Math]::Round($zScore,2)) > $ENTRY_Z)" Green
            Log "  Покупаем $SPOT_LOTS шт $SPOT_TICKER + Продаём $FUT_LOTS контр $FUT_TICKER" Green
            
            $r1 = PlaceOrder $spotIdObject $spotIdMarketBoard 1 $SPOT_LOTS "ARB LONG SPOT" $true
            $r2 = PlaceOrder $futIdObject $futIdMarketBoard -1 $FUT_LOTS "ARB SHORT FUT" $false
            
            $position = "LONG_SPREAD"
            $entrySpot = $spotPrice
            $entryFut = $futPrice
            $trades++
        }
        elseif ($zScore -lt (-$ENTRY_Z)) {
            Log "🔴 ВХОД: SHORT SPREAD (Z=$([Math]::Round($zScore,2)) < -$ENTRY_Z)" Red
            Log "  Продаём $SPOT_LOTS шт $SPOT_TICKER + Покупаем $FUT_LOTS контр $FUT_TICKER" Red
            
            $r1 = PlaceOrder $spotIdObject $spotIdMarketBoard -1 $SPOT_LOTS "ARB SHORT SPOT" $true
            $r2 = PlaceOrder $futIdObject $futIdMarketBoard 1 $FUT_LOTS "ARB LONG FUT" $false
            
            $position = "SHORT_SPREAD"
            $entrySpot = $spotPrice
            $entryFut = $futPrice
            $trades++
        }
    }
    # === LONG SPREAD ===
    elseif ($position -eq "LONG_SPREAD") {
        if ($zScore -le $EXIT_Z) {
            $pnl = ($spotPrice - $entrySpot) * $SPOT_LOTS + ($entryFut - $futPrice) * $FUT_LOTS
            $totalPnL += $pnl
            Log "✅ ВЫХОД LONG SPREAD: Z=$([Math]::Round($zScore,2)) PnL=$([Math]::Round($pnl,0)) Итого=$totalPnL" Green
            
            PlaceOrder $spotIdObject $spotIdMarketBoard -1 $SPOT_LOTS "ARB CLOSE SPOT" $true | Out-Null
            PlaceOrder $futIdObject $futIdMarketBoard 1 $FUT_LOTS "ARB CLOSE FUT" $false | Out-Null
            
            $position = "NONE"
        }
        elseif ($zScore -gt $STOP_Z) {
            $pnl = ($spotPrice - $entrySpot) * $SPOT_LOTS + ($entryFut - $futPrice) * $FUT_LOTS
            $totalPnL += $pnl
            Log "🛑 СТОП LONG: Z=$([Math]::Round($zScore,2)) > $STOP_Z PnL=$([Math]::Round($pnl,0))" Red
            
            PlaceOrder $spotIdObject $spotIdMarketBoard -1 $SPOT_LOTS "ARB STOP SPOT" $true | Out-Null
            PlaceOrder $futIdObject $futIdMarketBoard 1 $FUT_LOTS "ARB STOP FUT" $false | Out-Null
            
            $position = "NONE"
        }
    }
    # === SHORT SPREAD ===
    elseif ($position -eq "SHORT_SPREAD") {
        if ($zScore -ge (-$EXIT_Z)) {
            $pnl = ($entrySpot - $spotPrice) * $SPOT_LOTS + ($futPrice - $entryFut) * $FUT_LOTS
            $totalPnL += $pnl
            Log "✅ ВЫХОД SHORT SPREAD: Z=$([Math]::Round($zScore,2)) PnL=$([Math]::Round($pnl,0)) Итого=$totalPnL" Green
            
            PlaceOrder $spotIdObject $spotIdMarketBoard 1 $SPOT_LOTS "ARB CLOSE SPOT" $true | Out-Null
            PlaceOrder $futIdObject $futIdMarketBoard -1 $FUT_LOTS "ARB CLOSE FUT" $false | Out-Null
            
            $position = "NONE"
        }
        elseif ($zScore -lt (-$STOP_Z)) {
            $pnl = ($entrySpot - $spotPrice) * $SPOT_LOTS + ($futPrice - $entryFut) * $FUT_LOTS
            $totalPnL += $pnl
            Log "🛑 СТОП SHORT: Z=$([Math]::Round($zScore,2)) < -$STOP_Z PnL=$([Math]::Round($pnl,0))" Red
            
            PlaceOrder $spotIdObject $spotIdMarketBoard 1 $SPOT_LOTS "ARB CLOSE SPOT" $true | Out-Null
            PlaceOrder $futIdObject $futIdMarketBoard -1 $FUT_LOTS "ARB CLOSE FUT" $false | Out-Null
            
            $position = "NONE"
        }
    }
    
    Start-Sleep $CHECK_INTERVAL
}
