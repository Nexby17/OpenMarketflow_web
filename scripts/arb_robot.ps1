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
$ws = $null
$rid = 0
$resp = @{}
$sdata = @{}
$position = "NONE"
$bh = [System.Collections.ArrayList]::new()
$spotIdFi=0; $futIdFi=0; $spotIdObj=0; $futIdObj=0; $spotBoard=0; $futBoard=0
$idAcc=0; $idSub=0; $idRaz=0; $idRazF=0; $totalPnL=0; $trades=0

function L($m,$c="White"){Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] $m" -F $c}
function NI{$script:rid++;return "$($script:rid)"}

function SW($o){
    if($ws.State-ne'Open'){return}
    $j=$o|ConvertTo-Json -Compress -Depth 5
    $b=[Text.Encoding]::UTF8.GetBytes($j)
    $ws.SendAsync([ArraySegment[byte]]::new($b),[Net.WebSockets.WebSocketMessageType]::Text,$true,[Threading.CancellationToken]::None).Wait()
}

function SR($ch,$pl,$t=10){
    $id=NI
    $pj=$pl|ConvertTo-Json -Compress -Depth 5
    SW @{Command="request";Channel=$ch;Id=$id;Payload=$pj}
    $dl=(Get-Date).AddSeconds($t)
    while((Get-Date)-lt$dl){
        RM
        if($resp.ContainsKey($id)){$r=$resp[$id];$resp.Remove($id);return $r}
        Start-Sleep -M 200
    }
    return $null
}

function RM{
    $buf=[byte[]]::new(1048576)  # 1MB buffer
    $ms=[IO.MemoryStream]::new()
    while($ws.State-eq'Open'){
        $seg=[System.ArraySegment[byte]]::new($buf)
        $cts=[Threading.CancellationTokenSource]::new(200)
        try{
            $r=$ws.ReceiveAsync($seg,$cts.Token).GetAwaiter().GetResult()
            if($r.MessageType-eq'Close'){break}
            if($r.MessageType-eq'Text'){
                $ms.Write($buf,0,$r.Count)
                if($r.EndOfMessage){
                    $txt=[Text.Encoding]::UTF8.GetString($ms.ToArray())
                    $ms.SetLength(0)
                    if($txt){HM $txt}
                }
            }
        }catch{break}finally{$cts.Dispose()}
    }
    $ms.Dispose()
}

function HM($raw){
    $m=$raw|ConvertFrom-Json
    $cmd=$m.Command;$ch=$m.Channel;$id=$m.Id
    $pl=$null
    if($m.Payload){try{$pl=$m.Payload|ConvertFrom-Json}catch{}}
    if($cmd-eq"response"-and$id-and$pl){
        $resp[$id]=$pl
        if($pl.Data){$sdata[$pl.Type]=$pl.Data;PD $pl.Type $pl.Data}
    }
    if($cmd-eq"broadcast"-and$pl){
        if($pl.Updated){PD $pl.Type $pl.Updated}
    }
}

function PD($type,$data){
    switch($type){
        "ClientAccountEntity"{foreach($i in $data){$script:idAcc=$i.IdAccount;L "  Account: $($i.IdAccount)" Cyan}}
        "ClientSubAccountEntity"{foreach($i in $data){$script:idSub=$i.IdSubAccount;L "  SubAccount: $($i.IdSubAccount)" Cyan}}
        "SubAccountRazdelEntity"{
            foreach($i in $data){
                if($i.RCode-eq"MICEX"-or($i.IdRazdelGroup-eq1-and$i.RCode-eq"ALFA")){$script:idRaz=$i.IdRazdel}
                if($i.RCode-eq"FORTS"){$script:idRazF=$i.IdRazdel}
            }
        }
        "AssetInfoEntity"{
            foreach($i in $data){
                $t=$i.Ticker
                if($t-eq$SPOT_TICKER){
                    $script:spotIdObj=$i.IdObject
                    $ins=$i.Instruments|Where-Object{$_.IsLiquid}|Select-Object -First 1
                    if(-not$ins){$ins=$i.Instruments|Select-Object -First 1}
                    if($ins){$script:spotIdFi=$ins.IdFi;$script:spotBoard=$ins.IdMarketBoard}
                    L "  SPOT: $t IdObj=$($i.IdObject) IdFi=$($script:spotIdFi)" Green
                }
                if($t-like"$FUT_TICKER*"){
                    $script:futIdObj=$i.IdObject
                    $ins=$i.Instruments|Where-Object{$_.IsLiquid}|Select-Object -First 1
                    if(-not$ins){$ins=$i.Instruments|Select-Object -First 1}
                    if($ins){$script:futIdFi=$ins.IdFi;$script:futBoard=$ins.IdMarketBoard}
                    L "  FUT:  $t IdObj=$($i.IdObject) IdFi=$($script:futIdFi)" Yellow
                }
            }
        }
    }
}

function GP($idFi){
    SW @{Command="listen";Channel="#Data.Bus.FinInfoLastEntity"}
    $r=SR "#Data.Query" @{Type="FinInfoLastEntity";Keys=@($idFi);Init=$true}
    if($r-and$r.Data){
        $fi=$r.Data|Where-Object{$_.IdFi-eq$idFi}|Select-Object -First 1
        if($fi){return $fi.Last}
    }
    return 0
}

function PO($idObj,$board,$bs,$qty,$cmt,$isSpot){
    $raz=if($isSpot){$idRaz}else{$idRazF}
    $grp=if($isSpot){1}else{4}
    $idA=0
    if($sdata.ContainsKey("AllowedOrderParamEntity")){
        foreach($p in $sdata["AllowedOrderParamEntity"]){
            if($p.IdObjectGroup-eq$grp-and$p.IdMarketBoard-eq$board-and$p.IdOrderType-eq1-and$p.IdDocumentType-eq1){
                $idA=$p.IdAllowedOrderParams;break
            }
        }
    }
    $ord=@{IdAccount=$idAcc;IdSubAccount=$idSub;IdRazdel=$raz;IdPriceControlType=3;IdObject=$idObj;BuySell=$bs;Quantity=$qty;Comment=$cmt;IdAllowedOrderParams=$idA}
    if($DRY_RUN){
        $d=if($bs-eq1){"BUY"}else{"SELL"}
        L "  [DRY] $d $qty pcs obj=$idObj allowed=$idA raz=$raz" Magenta
        return @{ResponseStatus=0}
    }
    return SR "#Order.Enter.Query" $ord 15
}

# === MAIN ===
L "=======================================" Cyan
L "  ARB ROBOT v1.0" Cyan
L "  Pair: $SPOT_TICKER / $FUT_TICKER" Cyan
L "  Z: entry=$ENTRY_Z exit=$EXIT_Z stop=$STOP_Z" Cyan
L "  Lots: spot=$SPOT_LOTS fut=$FUT_LOTS" Cyan
$mt=if($DRY_RUN){"DRY RUN"}else{"LIVE"}
$mc=if($DRY_RUN){"Yellow"}else{"Red"}
L "  Mode: $mt" $mc
L "=======================================" Cyan

L "Connecting to $WS_URL ..."
$ws=[Net.WebSockets.ClientWebSocket]::new()
try{
    $ws.ConnectAsync([Uri]$WS_URL,[Threading.CancellationToken]::None).Wait()
    L "OK: WebSocket connected!" Green
}catch{
    L "FAIL: Cannot connect. Is terminal v5 running?" Red
    Read-Host "Press Enter"
    exit
}

L "Subscribing..."
SW @{Command="listen";Channel="#ConnectionState.Bus"}
SW @{Command="listen";Channel="#Data.Bus.AssetInfoEntity"}
SW @{Command="listen";Channel="#Data.Bus.ClientAccountEntity"}
SW @{Command="listen";Channel="#Data.Bus.ClientSubAccountEntity"}
SW @{Command="listen";Channel="#Data.Bus.SubAccountRazdelEntity"}
SW @{Command="listen";Channel="#Data.Bus.AllowedOrderParamEntity"}
SW @{Command="listen";Channel="#Data.Bus.OrderEntity"}

# Request only what we need (not all 19K assets at once)
# First get accounts/razdels
$null = SR "#Data.Query" @{Type="ClientAccountEntity";Init=$true} 5
$null = SR "#Data.Query" @{Type="ClientSubAccountEntity";Init=$true} 5
$null = SR "#Data.Query" @{Type="SubAccountRazdelEntity";Init=$true} 5
$null = SR "#Data.Query" @{Type="AllowedOrderParamEntity";Init=$true} 5

L "Waiting for account data..."
Start-Sleep 3
RM

# Search specific instruments by name
L "Searching for $SPOT_TICKER..."
$null = SR "#Data.Query" @{Type="AssetInfoEntity";Init=$true} 30
Start-Sleep 5
RM
L "Assets loaded: spot=$spotIdFi fut=$futIdFi"

if($spotIdFi-eq0){L "FAIL: $SPOT_TICKER not found!" Red;Read-Host;exit}
if($futIdFi-eq0){L "FAIL: $FUT_TICKER not found!" Red;Read-Host;exit}
if($idAcc-eq0){L "FAIL: No account!" Red;Read-Host;exit}

L ""
L "=== READY ===" Green
L "  Spot: $SPOT_TICKER IdFi=$spotIdFi IdObj=$spotIdObj"
L "  Fut:  $FUT_TICKER IdFi=$futIdFi IdObj=$futIdObj"
L "  Acc=$idAcc Sub=$idSub RazRCB=$idRaz RazFORTS=$idRazF"
L ""

$iter=0
while($true){
    $iter++
    RM
    $sp=GP $spotIdFi
    $fp=GP $futIdFi
    if($sp-le0-or$fp-le0){L "[#$iter] No prices: spot=$sp fut=$fp" Yellow;Start-Sleep $CHECK_SEC;continue}
    
    $bPct=($fp/($sp*$LOT_SIZE)-1)*100
    $bAnn=$bPct*365/60
    [void]$bh.Add($bAnn)
    if($bh.Count-gt$WINDOW){$bh.RemoveAt(0)}
    
    $z=0
    if($bh.Count-ge$WINDOW){
        $avg=($bh|Measure-Object -Average).Average
        $std=[Math]::Sqrt(($bh|ForEach-Object{($_-$avg)*($_-$avg)}|Measure-Object -Sum).Sum/$WINDOW)
        if($std-gt0.001){$z=($bAnn-$avg)/$std}
    }
    
    $pe=switch($position){"NONE"{"  "};"LONG_SPREAD"{"LG"};"SHORT_SPREAD"{"SH"}}
    L "[#$iter] [$pe] Spot=$([Math]::Round($sp,2)) Fut=$([Math]::Round($fp,2)) Basis=$([Math]::Round($bAnn,1))% Z=$([Math]::Round($z,2)) PnL=$totalPnL T=$trades" Cyan
    
    if($bh.Count-lt$WINDOW){L "  Warmup: $($bh.Count)/$WINDOW" Gray;Start-Sleep $CHECK_SEC;continue}
    
    if($position-eq"NONE"){
        if($z-gt$ENTRY_Z){
            L ">> ENTER LONG SPREAD (Z=$([Math]::Round($z,2)))" Green
            PO $spotIdObj $spotBoard 1 $SPOT_LOTS "ARB LONG SPOT" $true|Out-Null
            PO $futIdObj $futBoard -1 $FUT_LOTS "ARB SHORT FUT" $false|Out-Null
            $position="LONG_SPREAD";$eSpot=$sp;$eFut=$fp;$trades++
        }
        elseif($z-lt(-$ENTRY_Z)){
            L ">> ENTER SHORT SPREAD (Z=$([Math]::Round($z,2)))" Red
            PO $spotIdObj $spotBoard -1 $SPOT_LOTS "ARB SHORT SPOT" $true|Out-Null
            PO $futIdObj $futBoard 1 $FUT_LOTS "ARB LONG FUT" $false|Out-Null
            $position="SHORT_SPREAD";$eSpot=$sp;$eFut=$fp;$trades++
        }
    }
    elseif($position-eq"LONG_SPREAD"){
        if($z-le$EXIT_Z){
            $pnl=($sp-$eSpot)*$SPOT_LOTS+($eFut-$fp)*$FUT_LOTS;$totalPnL+=$pnl
            L "<< EXIT LONG Z=$([Math]::Round($z,2)) PnL=$([Math]::Round($pnl,0)) Total=$totalPnL" Green
            PO $spotIdObj $spotBoard -1 $SPOT_LOTS "ARB CLOSE" $true|Out-Null
            PO $futIdObj $futBoard 1 $FUT_LOTS "ARB CLOSE" $false|Out-Null
            $position="NONE"
        }elseif($z-gt$STOP_Z){
            $pnl=($sp-$eSpot)*$SPOT_LOTS+($eFut-$fp)*$FUT_LOTS;$totalPnL+=$pnl
            L "!! STOP LONG Z=$([Math]::Round($z,2)) PnL=$([Math]::Round($pnl,0))" Red
            PO $spotIdObj $spotBoard -1 $SPOT_LOTS "ARB STOP" $true|Out-Null
            PO $futIdObj $futBoard 1 $FUT_LOTS "ARB STOP" $false|Out-Null
            $position="NONE"
        }
    }
    elseif($position-eq"SHORT_SPREAD"){
        if($z-ge(-$EXIT_Z)){
            $pnl=($eSpot-$sp)*$SPOT_LOTS+($fp-$eFut)*$FUT_LOTS;$totalPnL+=$pnl
            L "<< EXIT SHORT Z=$([Math]::Round($z,2)) PnL=$([Math]::Round($pnl,0)) Total=$totalPnL" Green
            PO $spotIdObj $spotBoard 1 $SPOT_LOTS "ARB CLOSE" $true|Out-Null
            PO $futIdObj $futBoard -1 $FUT_LOTS "ARB CLOSE" $false|Out-Null
            $position="NONE"
        }elseif($z-lt(-$STOP_Z)){
            $pnl=($eSpot-$sp)*$SPOT_LOTS+($fp-$eFut)*$FUT_LOTS;$totalPnL+=$pnl
            L "!! STOP SHORT Z=$([Math]::Round($z,2)) PnL=$([Math]::Round($pnl,0))" Red
            PO $spotIdObj $spotBoard 1 $SPOT_LOTS "ARB STOP" $true|Out-Null
            PO $futIdObj $futBoard -1 $FUT_LOTS "ARB STOP" $false|Out-Null
            $position="NONE"
        }
    }
    Start-Sleep $CHECK_SEC
}
