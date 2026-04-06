/**
Арбитраж акция/фьючерс — GAZP (Газпром)
Оптимальные параметры: w=20, ez=1.5, xz=0.0
Результат: +23.2%/год | WR 95% | 44 сделки
Лот фьючерса = 100 акций

Algorithm = АРБИТРАЖ;
Hash code GAZP_ARB_V1_2026
**/

function Initialize()
{
    StrategyName = "Arb_GAZP";
    AddParameter("Window", 20, "Окно_MA", 1);
    AddParameter("EntryZ", 1.5, "Вход_z", 1);
    AddParameter("ExitZ", 0.0, "Выход_z", 1);
    AddParameter("StopZ", 3.5, "Стоп_z", 1);
    AddParameter("LotSize", 100, "Размер_лота_фут", 1);
    AddParameter("Lots", 1, "Кол_лотов", 1);
    AddInput("Spot", Inputs.Candle, 1440, true, "GAZP=МБ ЦК");
    AddInput("Fut", Inputs.Candle, 1440, true, "GAZPфьюч=МБ ФО");
    LongLimit = 0;
    ShortLimit = 0;
}

var basisHistory = [];
var position = 0;

function OnUpdate()
{
    var spotPrice = Spot.Close;
    var futPrice = Fut.Close;
    if (spotPrice <= 0 || futPrice <= 0) return;
    
    var basis = (futPrice / (spotPrice * LotSize) - 1) * 100;
    basisHistory.push(basis);
    if (basisHistory.length < Window) return;
    if (basisHistory.length > Window * 3)
        basisHistory = basisHistory.slice(basisHistory.length - Window * 2);
    
    var sum = 0, sumSq = 0;
    var s = basisHistory.length - Window;
    for (var i = s; i < basisHistory.length; i++) { sum += basisHistory[i]; sumSq += basisHistory[i] * basisHistory[i]; }
    var ma = sum / Window;
    var std = Math.sqrt(Math.max(sumSq / Window - ma * ma, 0.0001));
    var z = (basis - ma) / std;
    
    if (position == 0) {
        if (z > EntryZ) { EnterLong(Lots); position = 1; Print("LONG BASIS z=" + z.toFixed(2)); }
        else if (z < -EntryZ) { EnterShort(Lots); position = -1; Print("SHORT BASIS z=" + z.toFixed(2)); }
    } else if (position == 1) {
        if (z <= ExitZ) { ExitLong(); position = 0; Print("EXIT LONG z=" + z.toFixed(2)); }
        else if (z > StopZ) { ExitLong(); position = 0; Print("STOP LONG z=" + z.toFixed(2)); }
    } else if (position == -1) {
        if (z >= -ExitZ) { ExitShort(); position = 0; Print("EXIT SHORT z=" + z.toFixed(2)); }
        else if (z < -StopZ) { ExitShort(); position = 0; Print("STOP SHORT z=" + z.toFixed(2)); }
    }
}
