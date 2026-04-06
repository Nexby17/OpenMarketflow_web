# Arbitrage Robot for Alfa-Direct 5 (WebSocket PRO API)
# Run: powershell -ExecutionPolicy Bypass -File arb_robot.ps1
# Requires: Alfa-Direct 5 terminal running and authorized

# === SETTINGS ===
$SPOT_TICKER  = "SBER"
$FUT_TICKER   = "SBRF"
$WINDOW       = 15
$ENTRY_Z      = 1.0
$EXIT_Z       = 0.0
$STOP_Z       = 3.5
$SPOT_LOTS    = 100
$FUT_LOTS     = 1
$LOT_SIZE     = 100
$CHECK_SEC    = 30
$DRY_RUN      = $true

$WS_URL = "ws://127.0.0.1:3366/router/"
$script:ws = $null
$script:rid = 0
$script:resp = @{}
$script:sdata = @{}
$script:position = "NONE"
$script:bh = @()
$script:spotIdFi=0; $script:futIdFi=0; $script:spotIdObj=0; $script:futIdObj=0
$script:spotBoard=0; $script:futBoard=0
$script:idAcc=0; $script:idSub=0; $script:idRaz=0; $script:idRazF=0
$script:totalPnL=0; $script:trades=0; $script:eSpot=0; $script:eFut=0

function L($m,$c="White"){Write-Host "[$([DateTime]::Now.ToString('HH:mm:ss'))] $m" -ForegroundColor $c}

function SW($o){
    if($script:ws.State -ne 'Open'){return}
    $j = $o | ConvertTo-Json -Compress -Depth 5
    $enc = [Text.Encoding]::UTF8
    $bytes = $enc.GetBytes($j)
    $t = New-Object Threading.Tasks.Task[] 0
    $aseg = [Type]::GetType("System.ArraySegment``1[[System.Byte]]")
    $seg = [Activator]::CreateInstance($aseg, @(,$bytes))
    $script:ws.SendAsync($seg, [Net.WebSockets.WebSocketMessageType]::Text, $true, [Threading.CancellationToken]::None).Wait()
}

function RecvMsg {
    $buf = New-Object byte[] 1048576
    $all = New-Object byte[] 0
    while($script:ws.State -eq 'Open'){
        $aseg = [Type]::GetType("System.ArraySegment``1[[System.Byte]]")
        $seg = [Activator]::CreateInstance($aseg, @(,$buf))
        $cts = New-Object Threading.CancellationTokenSource 200
        try {
            $task = $script:ws.ReceiveAsync($seg, $cts.Token)
            $task.Wait()
            $r = $task.Result
            if($r.MessageType -eq 'Close'){break}
            if($r.Count -gt 0){
                $chunk = New-Object byte[] $r.Count
                [Array]::Copy($buf, $chunk, $r.Count)
                $all = $all + $chunk
            }
            if($r.EndOfMessage -and $all.Length -gt 0){
                $txt = [Text.Encoding]::UTF8.GetString($all)
                $all = New-Object byte[] 0
                if($txt.Length -gt 0){ HM $txt }
            }
        } catch { break } finally { $cts.Dispose() }
    }
}

function SR($ch, $pl, $t=10) {
    $script:rid++
    $id = "$($script:rid)"
    $pj = $pl | ConvertTo-Json -Compress -Depth 5
    SW @{ Command="request"; Channel=$ch; Id=$id; Payload=$pj }
    $dl = [DateTime]::Now.AddSeconds($t)
    while([DateTime]::Now -lt $dl){
        RecvMsg
        if($script:resp.ContainsKey($id)){
            $r = $script:resp[$id]
            [void]$script:resp.Remove($id)
            return $r
        }
        Start-Sleep -Milliseconds 300
    }
    return $null
}

function HM($raw) {
    try {
        $m = $raw | ConvertFrom-Json
    } catch { return }
    $cmd = $m.Command
    $ch = $m.Channel
    $id = $m.Id
    $pl = $null
    if($m.Payload){ try { $pl = $m.Payload | ConvertFrom-Json } catch {} }
    
    if($cmd -eq "response" -and $id -and $pl){
        $script:resp[$id] = $pl
        if($pl.Data){ $script:sdata[$pl.Type] = $pl.Data; PD $pl.Type $pl.Data }
    }
    if($cmd -eq "broadcast" -and $pl){
        if($pl.Updated){ PD $pl.Type $pl.Updated }
    }
}

function PD($type, $data) {
    switch($type) {
        "ClientAccountEntity" { foreach($i in $data){ $script:idAcc = $i.IdAccount; L "  Account: $($i.IdAccount)" Cyan } }
        "ClientSubAccountEntity" { foreach($i in $data){ $script:idSub = $i.IdSubAccount; L "  SubAccount: $($i.IdSubAccount)" Cyan } }
        "SubAccountRazdelEntity" {
            foreach($i in $data){
                if($i.RCode -eq "MICEX" -or ($i.IdRazdelGroup -eq 1 -and $i.RCode -eq "ALFA")){ $script:idRaz = $i.IdRazdel }
                if($i.RCode -eq "FORTS"){ $script:idRazF = $i.IdRazdel }
            }
        }
        "AssetInfoEntity" {
            foreach($i in $data){
                $t = $i.Ticker
                if($t -eq $SPOT_TICKER){
                    $script:spotIdObj = $i.IdObject
                    $ins = $i.Instruments | Where-Object { $_.IsLiquid } | Select-Object -First 1
                    if(-not $ins){ $ins = $i.Instruments | Select-Object -First 1 }
                    if($ins){ $script:spotIdFi = $ins.IdFi; $script:spotBoard = $ins.IdMarketBoard }
                    L "  SPOT: $t obj=$($i.IdObject) fi=$($script:spotIdFi)" Green
                }
                if($t -like "$FUT_TICKER*"){
                    $script:futIdObj = $i.IdObject
                    $ins = $i.Instruments | Where-Object { $_.IsLiquid } | Select-Object -First 1
                    if(-not $ins){ $ins = $i.Instruments | Select-Object -First 1 }
                    if($ins){ $script:futIdFi = $ins.IdFi; $script:futBoard = $ins.IdMarketBoard }
                    L "  FUT:  $t obj=$($i.IdObject) fi=$($script:futIdFi)" Yellow
                }
            }
        }
    }
}

function GP($idFi) {
    SW @{ Command="listen"; Channel="#Data.Bus.FinInfoLastEntity" }
    $r = SR "#Data.Query" @{ Type="FinInfoLastEntity"; Keys=@($idFi); Init=$true } 5
    if($r -and $r.Data){
        $fi = $r.Data | Where-Object { $_.IdFi -eq $idFi } | Select-Object -First 1
        if($fi){ return $fi.Last }
    }
    return 0
}

function PO($idObj, $board, $bs, $qty, $cmt, $isSpot) {
    $raz = if($isSpot){ $script:idRaz } else { $script:idRazF }
    $grp = if($isSpot){ 1 } else { 4 }
    $idA = 0
    if($script:sdata.ContainsKey("AllowedOrderParamEntity")){
        foreach($p in $script:sdata["AllowedOrderParamEntity"]){
            if($p.IdObjectGroup -eq $grp -and $p.IdMarketBoard -eq $board -and $p.IdOrderType -eq 1 -and $p.IdDocumentType -eq 1){
                $idA = $p.IdAllowedOrderParams; break
            }
        }
    }
    $ord = @{ IdAccount=$script:idAcc; IdSubAccount=$script:idSub; IdRazdel=$raz; IdPriceControlType=3; IdObject=$idObj; BuySell=$bs; Quantity=$qty; Comment=$cmt; IdAllowedOrderParams=$idA }
    if($DRY_RUN){
        $d = if($bs -eq 1){"BUY"} else {"SELL"}
        L "  [DRY] $d $qty pcs obj=$idObj" Magenta
        return @{ ResponseStatus=0 }
    }
    return SR "#Order.Enter.Query" $ord 15
}

# === MAIN ===
L "=======================================" Cyan
L "  ARB ROBOT v1.0" Cyan
L "  Pair: $SPOT_TICKER / $FUT_TICKER" Cyan
L "  Z: entry=$ENTRY_Z exit=$EXIT_Z stop=$STOP_Z" Cyan
L "  Lots: spot=$SPOT_LOTS fut=$FUT_LOTS" Cyan
$mt = if($DRY_RUN){"DRY RUN"} else {"LIVE"}
$mc = if($DRY_RUN){"Yellow"} else {"Red"}
L "  Mode: $mt" $mc
L "=======================================" Cyan

L "Connecting..."
$script:ws = New-Object Net.WebSockets.ClientWebSocket
try {
    $script:ws.ConnectAsync([Uri]$WS_URL, [Threading.CancellationToken]::None).Wait()
    L "OK: Connected!" Green
} catch {
    L "FAIL: $($_.Exception.Message)" Red
    Read-Host "Press Enter"
    exit
}

L "Subscribing..."
SW @{ Command="listen"; Channel="#ConnectionState.Bus" }
SW @{ Command="listen"; Channel="#Data.Bus.AssetInfoEntity" }
SW @{ Command="listen"; Channel="#Data.Bus.ClientAccountEntity" }
SW @{ Command="listen"; Channel="#Data.Bus.ClientSubAccountEntity" }
SW @{ Command="listen"; Channel="#Data.Bus.SubAccountRazdelEntity" }
SW @{ Command="listen"; Channel="#Data.Bus.AllowedOrderParamEntity" }

L "Loading accounts..."
$null = SR "#Data.Query" @{ Type="ClientAccountEntity"; Init=$true } 5
$null = SR "#Data.Query" @{ Type="ClientSubAccountEntity"; Init=$true } 5
$null = SR "#Data.Query" @{ Type="SubAccountRazdelEntity"; Init=$true } 5
$null = SR "#Data.Query" @{ Type="AllowedOrderParamEntity"; Init=$true } 5

Start-Sleep 2
RecvMsg

L "Loading instruments (this may take 20-30 sec)..."
$null = SR "#Data.Query" @{ Type="AssetInfoEntity"; Init=$true } 60
Start-Sleep 5
RecvMsg
L "Loaded: spot=$($script:spotIdFi) fut=$($script:futIdFi)"

if($script:spotIdFi -eq 0){ L "FAIL: $SPOT_TICKER not found" Red; Read-Host; exit }
if($script:futIdFi -eq 0){ L "FAIL: $FUT_TICKER not found" Red; Read-Host; exit }

L ""
L "=== READY ===" Green
L "  Spot: $SPOT_TICKER fi=$($script:spotIdFi) obj=$($script:spotIdObj)"
L "  Fut:  $FUT_TICKER fi=$($script:futIdFi) obj=$($script:futIdObj)"
L "  Acc=$($script:idAcc) Sub=$($script:idSub) Raz=$($script:idRaz) FORTS=$($script:idRazF)"
L ""

$iter = 0
while($true){
    $iter++
    RecvMsg
    $sp = GP $script:spotIdFi
    $fp = GP $script:futIdFi
    if($sp -le 0 -or $fp -le 0){ L "[#$iter] No prices spot=$sp fut=$fp" Yellow; Start-Sleep $CHECK_SEC; continue }
    
    $bPct = ($fp / ($sp * $LOT_SIZE) - 1) * 100
    $bAnn = $bPct * 365 / 60
    $script:bh += $bAnn
    if($script:bh.Length -gt $WINDOW){ $script:bh = $script:bh[1..$script:bh.Length] }
    
    $z = 0
    if($script:bh.Length -ge $WINDOW){
        $avg = ($script:bh | Measure-Object -Average).Average
        $variance = ($script:bh | ForEach-Object { ($_ - $avg) * ($_ - $avg) } | Measure-Object -Sum).Sum / $WINDOW
        $std = [Math]::Sqrt($variance)
        if($std -gt 0.001){ $z = ($bAnn - $avg) / $std }
    }
    
    $pe = switch($script:position){"NONE"{"--"};"LONG_SPREAD"{"LG"};"SHORT_SPREAD"{"SH"}}
    L "[#$iter][$pe] S=$([Math]::Round($sp,2)) F=$([Math]::Round($fp,2)) B=$([Math]::Round($bAnn,1))% Z=$([Math]::Round($z,2)) PnL=$($script:totalPnL) T=$($script:trades)" Cyan
    
    if($script:bh.Length -lt $WINDOW){ L "  Warmup $($script:bh.Length)/$WINDOW" Gray; Start-Sleep $CHECK_SEC; continue }
    
    if($script:position -eq "NONE"){
        if($z -gt $ENTRY_Z){
            L ">> LONG SPREAD Z=$([Math]::Round($z,2))" Green
            PO $script:spotIdObj $script:spotBoard 1 $SPOT_LOTS "ARB" $true | Out-Null
            PO $script:futIdObj $script:futBoard (-1) $FUT_LOTS "ARB" $false | Out-Null
            $script:position="LONG_SPREAD"; $script:eSpot=$sp; $script:eFut=$fp; $script:trades++
        } elseif($z -lt (-$ENTRY_Z)){
            L ">> SHORT SPREAD Z=$([Math]::Round($z,2))" Red
            PO $script:spotIdObj $script:spotBoard (-1) $SPOT_LOTS "ARB" $true | Out-Null
            PO $script:futIdObj $script:futBoard 1 $FUT_LOTS "ARB" $false | Out-Null
            $script:position="SHORT_SPREAD"; $script:eSpot=$sp; $script:eFut=$fp; $script:trades++
        }
    } elseif($script:position -eq "LONG_SPREAD"){
        if($z -le $EXIT_Z){
            $pnl = ($sp - $script:eSpot)*$SPOT_LOTS + ($script:eFut - $fp)*$FUT_LOTS
            $script:totalPnL += $pnl
            L "<< EXIT LONG PnL=$([Math]::Round($pnl,0)) Total=$($script:totalPnL)" Green
            PO $script:spotIdObj $script:spotBoard (-1) $SPOT_LOTS "CLO" $true | Out-Null
            PO $script:futIdObj $script:futBoard 1 $FUT_LOTS "CLO" $false | Out-Null
            $script:position="NONE"
        } elseif($z -gt $STOP_Z){
            $pnl = ($sp - $script:eSpot)*$SPOT_LOTS + ($script:eFut - $fp)*$FUT_LOTS
            $script:totalPnL += $pnl
            L "!! STOP LONG PnL=$([Math]::Round($pnl,0))" Red
            PO $script:spotIdObj $script:spotBoard (-1) $SPOT_LOTS "STP" $true | Out-Null
            PO $script:futIdObj $script:futBoard 1 $FUT_LOTS "STP" $false | Out-Null
            $script:position="NONE"
        }
    } elseif($script:position -eq "SHORT_SPREAD"){
        if($z -ge (-$EXIT_Z)){
            $pnl = ($script:eSpot - $sp)*$SPOT_LOTS + ($fp - $script:eFut)*$FUT_LOTS
            $script:totalPnL += $pnl
            L "<< EXIT SHORT PnL=$([Math]::Round($pnl,0)) Total=$($script:totalPnL)" Green
            PO $script:spotIdObj $script:spotBoard 1 $SPOT_LOTS "CLO" $true | Out-Null
            PO $script:futIdObj $script:futBoard (-1) $FUT_LOTS "CLO" $false | Out-Null
            $script:position="NONE"
        } elseif($z -lt (-$STOP_Z)){
            $pnl = ($script:eSpot - $sp)*$SPOT_LOTS + ($fp - $script:eFut)*$FUT_LOTS
            $script:totalPnL += $pnl
            L "!! STOP SHORT PnL=$([Math]::Round($pnl,0))" Red
            PO $script:spotIdObj $script:spotBoard 1 $SPOT_LOTS "STP" $true | Out-Null
            PO $script:futIdObj $script:futBoard (-1) $FUT_LOTS "STP" $false | Out-Null
            $script:position="NONE"
        }
    }
    Start-Sleep $CHECK_SEC
}
