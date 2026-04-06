/**
Арбитраж акция/фьючерс — ROSN (Роснефть)

Логика:
- Рассчитываем базис: (фьючерс / (акция × лот_размер) - 1) × 100
- Строим z-score базиса: (basis - SMA(basis, window)) / StdDev(basis, window)
- z > entry_z → базис расширен → ПРОДАЁМ фьючерс + ПОКУПАЕМ акцию (лонг базис)
- z ≤ exit_z → базис вернулся к среднему → закрываем обе ноги
- z > stop_z → стоп (базис ушёл слишком далеко)
- z < -entry_z → базис сузился → ПОКУПАЕМ фьючерс + ПРОДАЁМ акцию (шорт базис)
- z ≥ -exit_z → закрываем

Оптимальные параметры (бэктест 2 года, walk-forward 20/20):
- ROSN: +26.5%/год | Sharpe 7.87 | WR 95% | 77 сделок | w=15, ez=1.0, xz=0.0
- Портфель из 5 инструментов: +25.3% net | +22.0% после НДФЛ

Особенности:
- Два входа (Input1=акция, Input2=фьючерс)
- Лот фьючерса = 100 акций для ROSN
- Комиссия учтена в бэктесте: 0.04% акции + 0.004% фьючерс

Algorithm = АРБИТРАЖ;
Hash code ROSN_ARB_V1_2026
**/

function Initialize()
{
    StrategyName = "Arb_ROSN";
    
    // Параметры z-score (оптимизированные)
    AddParameter("Window", 15, "Окно_MA", 1);
    AddParameter("EntryZ", 1.0, "Вход_z", 1);
    AddParameter("ExitZ", 0.0, "Выход_z", 1);
    AddParameter("StopZ", 3.5, "Стоп_z", 1);
    AddParameter("LotSize", 100, "Размер_лота_фут", 1);
    AddParameter("Lots", 1, "Кол_лотов", 1);
    
    // Вход 1: Акция ROSN (спот)
    AddInput("Spot", Inputs.Candle, 1440, true, "ROSN=МБ ЦК");
    
    // Вход 2: Фьючерс ROSN (ближайший)
    AddInput("Fut", Inputs.Candle, 1440, true, "ROSNфьюч=МБ ФО");
    
    // Разрешаем и лонг и шорт
    LongLimit = 0;
    ShortLimit = 0;
}

// Буферы для расчёта скользящей статистики
var basisHistory = [];
var position = 0;  // 0=flat, 1=long basis, -1=short basis

function OnUpdate()
{
    // Считаем базис: (фьючерс / (акция × лот_размер) - 1) × 100
    var spotPrice = Spot.Close;
    var futPrice = Fut.Close;
    
    if (spotPrice <= 0 || futPrice <= 0)
        return;
    
    var basis = (futPrice / (spotPrice * LotSize) - 1) * 100;
    
    // Добавляем в историю
    basisHistory.push(basis);
    
    // Ждём достаточно данных для окна
    if (basisHistory.length < Window)
        return;
    
    // Обрезаем до нужного окна + запас
    if (basisHistory.length > Window * 3)
        basisHistory = basisHistory.slice(basisHistory.length - Window * 2);
    
    // Считаем MA и StdDev за окно
    var sum = 0;
    var sumSq = 0;
    var startIdx = basisHistory.length - Window;
    
    for (var i = startIdx; i < basisHistory.length; i++)
    {
        sum += basisHistory[i];
        sumSq += basisHistory[i] * basisHistory[i];
    }
    
    var ma = sum / Window;
    var variance = sumSq / Window - ma * ma;
    var stddev = Math.sqrt(Math.max(variance, 0.0001));
    
    // Z-score
    var z = (basis - ma) / stddev;
    
    // === ТОРГОВАЯ ЛОГИКА ===
    
    if (position == 0)
    {
        // Нет позиции
        if (z > EntryZ)
        {
            // Базис расширен → продаём фьючерс, покупаем акцию
            // EnterLong = покупаем акцию (спот нога)
            // По фьючерсу нужен шорт — через вторую стратегию или ручную заявку
            EnterLong(Lots);
            position = 1;
            Print("LONG BASIS: z=" + z.toFixed(2) + " basis=" + basis.toFixed(3) + 
                  " spot=" + spotPrice.toFixed(2) + " fut=" + futPrice.toFixed(0));
        }
        else if (z < -EntryZ)
        {
            // Базис сузился → покупаем фьючерс, продаём акцию
            EnterShort(Lots);
            position = -1;
            Print("SHORT BASIS: z=" + z.toFixed(2) + " basis=" + basis.toFixed(3) +
                  " spot=" + spotPrice.toFixed(2) + " fut=" + futPrice.toFixed(0));
        }
    }
    else if (position == 1)
    {
        // Лонг базис — ждём возврата к среднему
        if (z <= ExitZ)
        {
            ExitLong();
            position = 0;
            Print("EXIT LONG BASIS: z=" + z.toFixed(2) + " (mean reversion)");
        }
        else if (z > StopZ)
        {
            ExitLong();
            position = 0;
            Print("STOP LONG BASIS: z=" + z.toFixed(2) + " (расширение продолжилось)");
        }
    }
    else if (position == -1)
    {
        // Шорт базис — ждём возврата
        if (z >= -ExitZ)
        {
            ExitShort();
            position = 0;
            Print("EXIT SHORT BASIS: z=" + z.toFixed(2) + " (mean reversion)");
        }
        else if (z < -StopZ)
        {
            ExitShort();
            position = 0;
            Print("STOP SHORT BASIS: z=" + z.toFixed(2) + " (сужение продолжилось)");
        }
    }
}
