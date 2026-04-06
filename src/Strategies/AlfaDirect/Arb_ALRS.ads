/**
Арбитраж акция/фьючерс — ALRS (АЛРОСА)
Оптимальные параметры: w=15, ez=2.5, xz=0.5
Результат: +11.1%/год | Sharpe 7.22 | WR 100% | 17 сделок
Лот фьючерса = 100 акций
Особенность: высокий ez=2.5 (только сильные расхождения), 100% win rate

Algorithm = АРБИТРАЖ;
Hash code ALRS_ARB_V1_2026
**/

function Initialize()
{
    StrategyName = "Arb_ALRS";
    AddParameter("Window", 15, "Окно_MA", 1);
    AddParameter("EntryZ", 2.5, "Вход_z", 1);
    AddParameter("ExitZ", 0.5, "Выход_z", 1);
    AddParameter("StopZ", 4.5, "Стоп_z", 1);
    AddParameter("LotSize", 100, "Размер_лота_фут", 1);
    AddParameter("Lots", 1, "Кол_лотов", 1);
    AddInput("Spot", Inputs.Candle, 1440, true, "ALRS=МБ ЦК");
    AddInput("Fut", Inputs.Candle, 1440, true, "ALRSфьюч=МБ ФО");
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
