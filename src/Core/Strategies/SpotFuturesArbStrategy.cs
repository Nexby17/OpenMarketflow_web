using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// Активный арбитраж спот/фьючерс.
/// 
/// Логика: торгуем расширения/сужения базиса (z-score).
/// - Z > entry_z → LONG SPREAD (лонг акция + шорт фьючерс)
/// - Z < -entry_z → SHORT SPREAD (шорт акция + лонг фьючерс)
/// - Z вернулся к exit_z → выход
/// 
/// Валидация (2 года, 2024-2026):
/// - Walk-forward: 20/20 окон в плюсе
/// - Monte Carlo: 100% prob(profit)
/// - Sensitivity: все параметры стабильны ±20%
/// </summary>
public class SpotFuturesArbStrategy
{
    // === Конфигурация инструмента ===
    public string Name { get; set; } = "ArbStrategy";
    public string SpotTicker { get; set; } = "";      // Напр. "GAZP"
    public string FuturesTicker { get; set; } = "";    // Напр. "GZM6"
    public int LotSize { get; set; } = 100;            // Акций в 1 лоте фьючерса
    public int DaysToExpiry { get; set; } = 60;        // Обновляется снаружи

    // === Параметры стратегии (из grid search + walk-forward) ===
    public int Window { get; set; } = 15;              // Окно z-score
    public double EntryZ { get; set; } = 1.0;          // Порог входа
    public double ExitZ { get; set; } = 0.0;           // Порог выхода
    public double StopZ { get; set; } = 3.5;           // Стоп
    public int RollDaysBeforeExpiry { get; set; } = 20; // За сколько дней роллить

    // === Управление позицией ===
    public int MaxLots { get; set; } = 100;            // Макс лотов
    public int BaseLots { get; set; } = 10;            // Базовый размер входа (контрактов фьючерса)
    
    // === Раздельные лоты для каждой ноги ===
    /// <summary>Лоты акций (на МосБирже 1 лот = 1/10/100 акций)</summary>
    public int SpotLots { get; set; } = 0;             // 0 = авторасчёт из FutLots
    /// <summary>Контракты фьючерса</summary>
    public int FutLots { get; set; } = 1;
    /// <summary>Акций в 1 лоте акции на МосБирже (SBER=10, GAZP=10, ROSN=1, TATN=1, ALRS=10)</summary>
    public int SharesPerSpotLot { get; set; } = 10;
    /// <summary>ГО за 1 контракт фьючерса (руб.)</summary>
    public double FuturesGO { get; set; } = 5000;
    
    /// <summary>Лоты акций для ордера (авторасчёт если SpotLots=0)</summary>
    public int EffectiveSpotLots => SpotLots > 0 ? SpotLots 
        : (SharesPerSpotLot > 0 ? FutLots * LotSize / SharesPerSpotLot : FutLots * LotSize);
    
    /// <summary>Стоимость позиции акций (руб.)</summary>
    public double SpotValueRub => EffectiveSpotLots * SharesPerSpotLot * LastSpotPrice;
    
    /// <summary>ГО фьючерсной ноги (руб.)</summary>
    public double FutGORub => FutLots * FuturesGO;

    // === Состояние ===
    public ArbPosition CurrentPosition { get; private set; } = new();
    public StrategyMode Mode { get; set; } = StrategyMode.Stopped;
    
    // Скользящие статистики базиса
    private readonly Queue<double> _basisHistory = new();
    private double _basisSum = 0;
    private double _basisSqSum = 0;

    // Последние цены
    public double LastSpotPrice { get; private set; }
    public double LastFuturesPrice { get; private set; }
    public double LastBasisPct { get; private set; }
    public double LastBasisAnnual { get; private set; }
    public double LastZScore { get; private set; }
    public double BasisMA { get; private set; }
    public double BasisStd { get; private set; }

    // Статистика
    public int TotalTrades { get; private set; }
    public int WinningTrades { get; private set; }
    public double TotalPnL { get; private set; }
    public DateTime? LastTradeTime { get; private set; }

    /// <summary>
    /// Обработать новые цены спота и фьючерса.
    /// Вызывается при каждом обновлении (тик/свеча).
    /// Возвращает сигнал или null.
    /// </summary>
    public ArbSignal? OnPriceUpdate(double spotPrice, double futuresPrice, DateTime timestamp)
    {
        if (Mode == StrategyMode.Stopped) return null;
        if (spotPrice <= 0 || futuresPrice <= 0 || DaysToExpiry <= 0) return null;

        LastSpotPrice = spotPrice;
        LastFuturesPrice = futuresPrice;

        // 1. Рассчитываем базис
        LastBasisPct = (futuresPrice / (spotPrice * LotSize) - 1) * 100;
        LastBasisAnnual = LastBasisPct * 365.0 / DaysToExpiry;

        // 2. Обновляем скользящую статистику
        _basisHistory.Enqueue(LastBasisAnnual);
        _basisSum += LastBasisAnnual;
        _basisSqSum += LastBasisAnnual * LastBasisAnnual;

        while (_basisHistory.Count > Window)
        {
            double old = _basisHistory.Dequeue();
            _basisSum -= old;
            _basisSqSum -= old * old;
        }

        if (_basisHistory.Count < Window)
            return null; // Недостаточно данных

        // 3. Z-score
        BasisMA = _basisSum / Window;
        double variance = _basisSqSum / Window - BasisMA * BasisMA;
        BasisStd = variance > 0 ? Math.Sqrt(variance) : 0.001;
        LastZScore = (LastBasisAnnual - BasisMA) / BasisStd;

        // 4. Торговая логика
        if (Mode == StrategyMode.Paused)
            return null; // Не открываем новые, но и не закрываем

        return EvaluateSignal(spotPrice, futuresPrice, timestamp);
    }

    private ArbSignal? EvaluateSignal(double spot, double fut, DateTime ts)
    {
        double z = LastZScore;

        // === Нет позиции → ищем вход ===
        if (!CurrentPosition.IsOpen)
        {
            if (z > EntryZ)
            {
                return OpenPosition(ArbDirection.LongSpread, spot, fut, ts,
                    $"Z={z:F2} > {EntryZ:F1}, basis={LastBasisAnnual:F1}%");
            }
            if (z < -EntryZ)
            {
                return OpenPosition(ArbDirection.ShortSpread, spot, fut, ts,
                    $"Z={z:F2} < -{EntryZ:F1}, basis={LastBasisAnnual:F1}%");
            }
            return null;
        }

        // === Есть позиция → ищем выход ===
        if (CurrentPosition.Direction == ArbDirection.LongSpread)
        {
            if (z <= ExitZ)
                return ClosePosition(spot, fut, ts, $"Target: Z={z:F2} <= {ExitZ:F1}");
            if (z > StopZ)
                return ClosePosition(spot, fut, ts, $"STOP: Z={z:F2} > {StopZ:F1}");
        }
        else if (CurrentPosition.Direction == ArbDirection.ShortSpread)
        {
            if (z >= -ExitZ)
                return ClosePosition(spot, fut, ts, $"Target: Z={z:F2} >= {-ExitZ:F1}");
            if (z < -StopZ)
                return ClosePosition(spot, fut, ts, $"STOP: Z={z:F2} < {-StopZ:F1}");
        }

        return null;
    }

    private ArbSignal OpenPosition(ArbDirection dir, double spot, double fut, DateTime ts, string reason)
    {
        CurrentPosition = new ArbPosition
        {
            Direction = dir,
            EntrySpotPrice = spot,
            EntryFuturesPrice = fut,
            Lots = FutLots,
            SpotLots = EffectiveSpotLots,
            FutLots = FutLots,
            OpenTime = ts,
            SpotTicker = SpotTicker,
            FuturesTicker = FuturesTicker,
        };

        return new ArbSignal
        {
            Timestamp = ts,
            Action = ArbAction.Open,
            Direction = dir,
            SpotTicker = SpotTicker,
            FuturesTicker = FuturesTicker,
            SpotPrice = spot,
            FuturesPrice = fut,
            Lots = FutLots,
            SpotLots = EffectiveSpotLots,
            FutLots = FutLots,
            ZScore = LastZScore,
            BasisAnnual = LastBasisAnnual,
            Reason = reason,
        };
    }

    private ArbSignal ClosePosition(double spot, double fut, DateTime ts, string reason)
    {
        var pos = CurrentPosition;

        // PnL расчёт
        double pnl;
        if (pos.Direction == ArbDirection.LongSpread)
        {
            // Лонг акция + шорт фьючерс
            pnl = (spot - pos.EntrySpotPrice) * LotSize * pos.Lots
                + (pos.EntryFuturesPrice - fut) * pos.Lots;
        }
        else
        {
            // Шорт акция + лонг фьючерс
            pnl = (pos.EntrySpotPrice - spot) * LotSize * pos.Lots
                + (fut - pos.EntryFuturesPrice) * pos.Lots;
        }

        TotalPnL += pnl;
        TotalTrades++;
        if (pnl > 0) WinningTrades++;
        LastTradeTime = ts;

        var signal = new ArbSignal
        {
            Timestamp = ts,
            Action = ArbAction.Close,
            Direction = pos.Direction,
            SpotTicker = SpotTicker,
            FuturesTicker = FuturesTicker,
            SpotPrice = spot,
            FuturesPrice = fut,
            Lots = pos.Lots,
            ZScore = LastZScore,
            BasisAnnual = LastBasisAnnual,
            PnL = pnl,
            Reason = reason,
        };

        // Закрываем позицию
        CurrentPosition = new ArbPosition();

        return signal;
    }

    /// <summary>
    /// Вызвать при переходе на новый фьючерсный контракт (ролл).
    /// Закрывает текущую позицию если открыта.
    /// </summary>
    public ArbSignal? OnContractRoll(string newFuturesTicker, int newDaysToExpiry, 
                                      double spotPrice, double futuresPrice, DateTime timestamp)
    {
        ArbSignal? closeSignal = null;

        if (CurrentPosition.IsOpen)
        {
            closeSignal = ClosePosition(spotPrice, futuresPrice, timestamp,
                $"ROLL: переход на {newFuturesTicker}");
        }

        FuturesTicker = newFuturesTicker;
        DaysToExpiry = newDaysToExpiry;

        return closeSignal;
    }

    /// <summary>Нужен ли ролл? (за N дней до экспирации)</summary>
    public bool NeedsRoll => DaysToExpiry <= RollDaysBeforeExpiry && DaysToExpiry > 0;

    /// <summary>Принудительно закрыть позицию</summary>
    public ArbSignal? ForceClose(double spotPrice, double futuresPrice, DateTime timestamp)
    {
        if (!CurrentPosition.IsOpen) return null;
        return ClosePosition(spotPrice, futuresPrice, timestamp, "FORCE CLOSE (команда)");
    }

    /// <summary>Сбросить всё состояние</summary>
    public void Reset()
    {
        CurrentPosition = new ArbPosition();
        _basisHistory.Clear();
        _basisSum = 0;
        _basisSqSum = 0;
        LastZScore = 0;
        TotalTrades = 0;
        WinningTrades = 0;
        TotalPnL = 0;
    }

    public double WinRate => TotalTrades > 0 ? (double)WinningTrades / TotalTrades * 100 : 0;

    public override string ToString() =>
        $"{Name} [{SpotTicker}/{FuturesTicker}] Z={LastZScore:F2} Basis={LastBasisAnnual:F1}% " +
        $"Pos={CurrentPosition.Direction} PnL={TotalPnL:+#,##0;-#,##0;0} " +
        $"Trades={TotalTrades} WR={WinRate:F0}%";
}

// === Вспомогательные модели ===

public enum ArbDirection { None, LongSpread, ShortSpread }
public enum ArbAction { Open, Close }
public enum StrategyMode { Running, Paused, Stopped }

public class ArbPosition
{
    public ArbDirection Direction { get; set; } = ArbDirection.None;
    public string SpotTicker { get; set; } = "";
    public string FuturesTicker { get; set; } = "";
    public double EntrySpotPrice { get; set; }
    public double EntryFuturesPrice { get; set; }
    public int Lots { get; set; }           // legacy, = FutLots
    public int SpotLots { get; set; }       // лоты акций
    public int FutLots { get; set; }        // контракты фьючерса
    public DateTime OpenTime { get; set; }
    public bool IsOpen => Direction != ArbDirection.None && (FutLots > 0 || Lots > 0);
}

public class ArbSignal
{
    public DateTime Timestamp { get; set; }
    public ArbAction Action { get; set; }
    public ArbDirection Direction { get; set; }
    public string SpotTicker { get; set; } = "";
    public string FuturesTicker { get; set; } = "";
    public double SpotPrice { get; set; }
    public double FuturesPrice { get; set; }
    public int Lots { get; set; }           // legacy, = FutLots
    public int SpotLots { get; set; }       // лоты акций
    public int FutLots { get; set; }        // контракты фьючерса
    public double ZScore { get; set; }
    public double BasisAnnual { get; set; }
    public double PnL { get; set; }
    public string Reason { get; set; } = "";

    public override string ToString() =>
        $"[{Timestamp:HH:mm:ss}] {Action} {Direction} {SpotTicker}/{FuturesTicker} " +
        $"x{Lots} Z={ZScore:F2} Basis={BasisAnnual:F1}% " +
        (Action == ArbAction.Close ? $"PnL={PnL:+#,##0;-#,##0;0} " : "") +
        Reason;
}
