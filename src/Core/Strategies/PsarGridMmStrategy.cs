using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// PSAR Grid MM — маркетмейкер без стопов.
/// 
/// Логика:
/// 1. PSAR×EMA крест → вход 1 лот @ market + сетка лимиток против позиции
/// 2. Каждый grid-уровень: TP = +Spread пт → round trip, уровень recycling
/// 3. Закрытие сессии:
///    A) Обратный PSAR×EMA крест → close all
///    B) 50%+ peak volume закрыто round trips + PnL/лот ≥ MinProfitPerLot
/// 4. Нет стопов. Нет trailing. Нет dist.
///
/// Оптимальные параметры (2 года SI, 8/8 OOS, 10/10 IS):
///   SAR: 0.005/0.01/0.2 (одинаковый для Long и Short)
///   EMA: 500
///   Grid Step: 20 пт
///   Grid Spread: 40 пт
///   Max Levels: 50
///   MinProfitPerLot: 20 пт
///   Close %: 50%
/// </summary>
public class PsarGridMmStrategy : IStrategy
{
    public class Config
    {
        // SAR — одинаковый для Long и Short
        public double SarStart { get; set; } = 0.005;
        public double SarStep { get; set; } = 0.01;
        public double SarMax { get; set; } = 0.2;

        // EMA
        public int EmaPeriod { get; set; } = 500;

        // Grid MM
        public double GridStep { get; set; } = 20;       // пт между лимитками
        public double GridSpread { get; set; } = 40;      // пт TP на round trip
        public int MaxLevels { get; set; } = 50;          // max grid уровней
        public double MinProfitPerLot { get; set; } = 20; // пт для close all
        public double ClosePct { get; set; } = 0.50;      // 50% peak закрыто
        public double Commission { get; set; } = 0.60;    // пт round trip
        public int BaseLots { get; set; } = 1;
    }

    public enum StrategyMode { Running, Paused, Stopped }

    // ═══ Публичные свойства ═══
    public string Name => "PSAR Grid MM v1";
    public Config Params { get; }
    public StrategyMode Mode { get; set; } = StrategyMode.Paused;

    // Статистика
    public int TotalSessions { get; private set; }
    public double TotalPnL { get; private set; }
    public int TotalRoundTrips { get; private set; }
    public int CurrentLots => _nLots + (_entryFilled ? 1 : 0);
    public int PeakLots => _peakLots;
    public int MaxLotsEver { get; private set; }
    public int PositionDirection => _posDir;
    public double SessionRealizedPnL => _sessionRealized;
    public double SessionUnrealizedPnL => _lastUnrealized;

    // Лог
    private readonly List<string> _log = new();
    public IReadOnlyList<string> Log => _log;

    // ═══ Индикаторы ═══
    private readonly ParabolicSAR _sar;
    private readonly EMA _ema;
    private double _prevSar, _prevEma;
    private bool _prevValid;

    // ═══ Состояние сессии ═══
    private int _posDir;           // 0=flat, 1=long, -1=short
    private double _entryPrice;
    private bool _entryFilled;     // entry lot ещё в позиции?
    private bool _gridActive;      // grid activated (PnL went negative)

    private int _nLots;              // число лотов в grid (без entry)

    // Grid levels
    private readonly double[] _gridPrice;    // limit price
    private readonly bool[] _gridFilled;     // заполнена ли лимитка
    private readonly int[] _gridFillBar;     // на каком баре заполнена (для same-bar protection)
    private int _nGrid;                      // сколько grid-уровней размещено
    private double _lastGridPrice;           // цена последнего размещённого уровня

    // Session tracking
    private int _sessionRt;          // round trips в текущей сессии
    private double _sessionRealized; // realized PnL от round trips
    private int _peakLots;           // max одновременно открытых лотов
    private double _lastUnrealized;  // для отображения
    private int _barCount;           // номер бара (для same-bar protection)

    public PsarGridMmStrategy(Config? config = null)
    {
        Params = config ?? new Config();
        _sar = new ParabolicSAR(Params.SarStart, Params.SarStep, Params.SarMax);
        _ema = new EMA(Params.EmaPeriod);
        _gridPrice = new double[Params.MaxLevels];
        _gridFilled = new bool[Params.MaxLevels];
        _gridFillBar = new int[Params.MaxLevels];
    }

    /// <summary>
    /// Обработка новой свечи. Возвращает сигнал на действие или null.
    /// Для Grid MM может возвращать несколько сигналов за бар — 
    /// вызывайте повторно пока не вернёт null (или обрабатывайте внутри).
    /// 
    /// В текущей реализации — один сигнал за вызов (приоритет: close > TP > grid fill > entry).
    /// </summary>
    public Signal? OnCandle(Candle candle, string ticker)
    {
        _barCount++;

        // Обновляем индикаторы ВСЕГДА
        double sarVal = _sar.Update(candle);
        double emaVal = _ema.Update(candle);

        if (double.IsNaN(sarVal) || double.IsNaN(emaVal) || !_prevValid)
        {
            _prevSar = sarVal;
            _prevEma = emaVal;
            _prevValid = !double.IsNaN(sarVal) && !double.IsNaN(emaVal);
            return null;
        }

        double c = candle.Close;
        double hi = candle.High;
        double lo = candle.Low;

        // Кроссы
        bool longEntry = (_prevSar >= _prevEma && sarVal < emaVal);
        bool longExit = (_prevSar <= _prevEma && sarVal > emaVal);
        bool shortEntry = (_prevSar <= _prevEma && sarVal > emaVal);
        bool shortExit = (_prevSar >= _prevEma && sarVal < emaVal);

        _prevSar = sarVal;
        _prevEma = emaVal;

        // ═══ STOP MODE ═══
        if (Mode == StrategyMode.Stopped && _posDir != 0)
            return CloseAll(c, ticker, "СТОП ТОРГИ");

        // ═══ Активная сессия ═══
        if (_posDir != 0)
        {
            int d = _posDir;

            // --- Grid activation: PnL ушёл в минус ---
            if (!_gridActive && _entryFilled)
            {
                double entryUr = d == 1 ? c - _entryPrice : _entryPrice - c;
                if (entryUr < 0)
                {
                    _gridActive = true;
                    // Размещаем первый grid-уровень
                    double gp = d == 1 ? _entryPrice - Params.GridStep : _entryPrice + Params.GridStep;
                    _gridPrice[0] = gp;
                    _gridFilled[0] = false;
                    _gridFillBar[0] = 0;
                    _nGrid = 1;
                    _lastGridPrice = gp;
                    LogMsg($"[{ticker}] Grid ON: first level @ {gp:F0}");
                }
            }

            if (_gridActive)
            {
                // --- Добавляем новые grid-уровни по мере движения цены ---
                while (_nGrid < Params.MaxLevels)
                {
                    double nextGp = d == 1
                        ? _lastGridPrice - Params.GridStep
                        : _lastGridPrice + Params.GridStep;

                    bool reached = d == 1 ? lo <= nextGp : hi >= nextGp;
                    if (!reached) break;

                    _gridPrice[_nGrid] = nextGp;
                    _gridFilled[_nGrid] = false;
                    _gridFillBar[_nGrid] = 0;
                    _nGrid++;
                    _lastGridPrice = nextGp;
                }

                // --- Заполнение grid лимиток ---
                for (int j = 0; j < _nGrid; j++)
                {
                    if (_gridFilled[j]) continue;
                    bool filled = d == 1 ? lo <= _gridPrice[j] : hi >= _gridPrice[j];
                    if (filled)
                    {
                        _gridFilled[j] = true;
                        _gridFillBar[j] = _barCount;
                        LogMsg($"[{ticker}] Grid FILL #{j}: @ {_gridPrice[j]:F0}");
                    }
                }

                // --- Round trips (TP) на grid-уровнях (заполненных ДО текущего бара) ---
                for (int j = 0; j < _nGrid; j++)
                {
                    if (!_gridFilled[j] || _gridFillBar[j] >= _barCount) continue;

                    bool tpHit = d == 1
                        ? hi >= _gridPrice[j] + Params.GridSpread
                        : lo <= _gridPrice[j] - Params.GridSpread;

                    if (tpHit)
                    {
                        double rtPnl = Params.GridSpread - 2.0 * Params.Commission;
                        _gridFilled[j] = false; // recycle
                        _sessionRealized += rtPnl;
                        _sessionRt++;
                        TotalRoundTrips++;
                        LogMsg($"[{ticker}] RT #{_sessionRt}: +{rtPnl:F1} пт (level {j} @ {_gridPrice[j]:F0})");
                    }
                }

                // --- Entry lot TP ---
                if (_entryFilled)
                {
                    bool entryTp = d == 1
                        ? hi >= _entryPrice + Params.GridSpread
                        : lo <= _entryPrice - Params.GridSpread;

                    if (entryTp)
                    {
                        double rtPnl = Params.GridSpread - 2.0 * Params.Commission;
                        _sessionRealized += rtPnl;
                        _sessionRt++;
                        TotalRoundTrips++;
                        _entryFilled = false;
                        LogMsg($"[{ticker}] RT entry: +{rtPnl:F1} пт (entry @ {_entryPrice:F0})");
                    }
                }
            }

            // --- Подсчёт текущих лотов и PnL ---
            int curLots = 0;
            double ur = 0;
            if (_entryFilled)
            {
                curLots++;
                ur += d == 1 ? c - _entryPrice : _entryPrice - c;
            }
            for (int j = 0; j < _nGrid; j++)
            {
                if (_gridFilled[j])
                {
                    curLots++;
                    ur += d == 1 ? c - _gridPrice[j] : _gridPrice[j] - c;
                }
            }
            if (curLots > _peakLots) _peakLots = curLots;
            if (curLots > MaxLotsEver) MaxLotsEver = curLots;
            _lastUnrealized = ur - curLots * Params.Commission;

            double totalPnl = _sessionRealized + ur - curLots * Params.Commission;

            // ═══ Закрытие ═══

            // A) Обратный PSAR×EMA крест
            bool exitCross = (d == 1 && longExit) || (d == -1 && shortExit);

            // B) 50%+ peak закрыто + PnL/лот OK
            bool gridClose = false;
            if (_gridActive && _peakLots > 1 && curLots > 0)
            {
                int closed = _peakLots - curLots;
                if (closed >= _peakLots * Params.ClosePct && totalPnl > 0)
                {
                    double ppl = totalPnl / curLots;
                    if (ppl >= Params.MinProfitPerLot)
                        gridClose = true;
                }
            }

            if (exitCross || gridClose)
            {
                string reason = gridClose
                    ? $"Grid close: {_peakLots - curLots}/{_peakLots} RT'd, PnL={totalPnl:F0}, ppl={totalPnl / Math.Max(curLots, 1):F0}"
                    : $"SAR×EMA крест, PnL={totalPnl:F0}, lots={curLots}";
                return CloseAll(c, ticker, reason);
            }
        }

        // ═══ Новый вход (только flat + running) ═══
        if (_posDir == 0 && Mode == StrategyMode.Running)
        {
            if (longEntry)
                return OpenSession(1, c, candle, ticker, "Long: SAR < EMA");
            if (shortEntry)
                return OpenSession(-1, c, candle, ticker, "Short: SAR > EMA");
        }

        return null;
    }

    private Signal OpenSession(int direction, double price, Candle candle, string ticker, string comment)
    {
        _posDir = direction;
        _entryPrice = price;
        _entryFilled = true;
        _gridActive = false;
        _nGrid = 0;
        _sessionRt = 0;
        _sessionRealized = 0;
        _peakLots = 1;
        _lastGridPrice = price;
        _lastUnrealized = 0;

        LogMsg($"[{ticker}] {comment} @ {price:F0}");

        return new Signal
        {
            Timestamp = candle.Timestamp,
            Ticker = ticker,
            Direction = direction == 1 ? SignalDirection.Buy : SignalDirection.Sell,
            Source = SignalSource.Strategy,
            Volume = Params.BaseLots,
            StrategyName = Name,
            Price = price,
            Comment = comment
        };
    }

    private Signal CloseAll(double price, string ticker, string reason)
    {
        int curLots = 0;
        double ur = 0;
        if (_entryFilled)
        {
            curLots++;
            ur += _posDir == 1 ? price - _entryPrice : _entryPrice - price;
        }
        for (int j = 0; j < _nGrid; j++)
        {
            if (_gridFilled[j])
            {
                curLots++;
                ur += _posDir == 1 ? price - _gridPrice[j] : _gridPrice[j] - price;
            }
        }
        double totalPnl = _sessionRealized + ur - curLots * Params.Commission;

        LogMsg($"[{ticker}] CLOSE ALL: {reason} | lots={curLots} peak={_peakLots} RT={_sessionRt} | realized={_sessionRealized:F0} ur={ur - curLots * Params.Commission:F0} total={totalPnl:F0}");

        TotalSessions++;
        TotalPnL += totalPnl;

        var signal = new Signal
        {
            Timestamp = DateTime.UtcNow,
            Ticker = ticker,
            Direction = _posDir == 1 ? SignalDirection.Sell : SignalDirection.Buy,
            Source = SignalSource.Exit,
            Volume = curLots * Params.BaseLots,
            StrategyName = Name,
            Price = price,
            Comment = reason
        };

        // Reset session state
        _posDir = 0;
        _entryFilled = false;
        _gridActive = false;
        _nGrid = 0;
        _sessionRt = 0;
        _sessionRealized = 0;
        _peakLots = 0;
        _lastUnrealized = 0;

        return signal;
    }

    public void Reset()
    {
        _sar.Reset();
        _ema.Reset();
        _posDir = 0;
        _entryFilled = false;
        _gridActive = false;
        _nGrid = 0;
        _sessionRt = 0;
        _sessionRealized = 0;
        _peakLots = 0;
        _prevValid = false;
        _barCount = 0;
        _lastUnrealized = 0;
        _log.Clear();
    }

    private void LogMsg(string msg)
    {
        _log.Add($"[{DateTime.UtcNow:HH:mm:ss}] {msg}");
        if (_log.Count > 2000) _log.RemoveAt(0);
    }

    /// <summary>Строка статуса для мониторинга</summary>
    public string GetStatus()
    {
        var dir = _posDir switch { 1 => "LONG", -1 => "SHORT", _ => "FLAT" };
        return $"{Name} [{Mode}] {dir} lots={CurrentLots} peak={_peakLots} max_ever={MaxLotsEver} " +
               $"RT={_sessionRt} realized={_sessionRealized:F0} ur={_lastUnrealized:F0} " +
               $"| sessions={TotalSessions} totalPnL={TotalPnL:F0} totalRT={TotalRoundTrips}";
    }
}
