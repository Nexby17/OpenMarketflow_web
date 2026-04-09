using HedgeFund.Core.Indicators;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Strategies;

/// <summary>
/// PSAR+EMA Combo Strategy — Long & Short без наложения.
/// Логика: SigAvg(2) → Grid ON → 50% closed + PnL≥28пт/лот → Close All.
/// Стоп: SAR×EMA обратный кросс.
/// Команды: Start / Stop / Pause через свойство Mode.
/// </summary>
public class PsarEmaComboStrategy : IStrategy
{
    // === Параметры ===
    public class Config
    {
        // Long
        public double L_SarStart { get; set; } = 0.03;
        public double L_SarStep { get; set; } = 0.02;
        public double L_SarMax { get; set; } = 0.2;
        public int L_EmaPeriod { get; set; } = 300;
        public double L_GridStep { get; set; } = 30;    // пт между уровнями grid
        public double L_GridSpread { get; set; } = 60;   // пт тейк на уровень

        // Short
        public double S_SarStart { get; set; } = 0.005;
        public double S_SarStep { get; set; } = 0.01;
        public double S_SarMax { get; set; } = 0.2;
        public int S_EmaPeriod { get; set; } = 300;
        public double S_GridStep { get; set; } = 20;
        public double S_GridSpread { get; set; } = 70;

        // Общие
        public int MaxGrid { get; set; } = 30;
        public double MinProfitPerLot { get; set; } = 28;  // 35 * 0.8
        public double Commission { get; set; } = 0.60;     // RT
        public int BaseLots { get; set; } = 1;

        // ATR фильтр
        public int AtrPeriod { get; set; } = 14;
        public double AtrFilter { get; set; } = 2.0;       // мин расстояние SAR-цена в единицах ATR
        public bool AtrFilterEnabled { get; set; } = true;

        // Динамические лоты
        public double LotStepProfit { get; set; } = 1000.0; // руб для повышения лота
        public int MaxDynamicLots { get; set; } = 5;
    }

    public enum StrategyMode { Running, Paused, Stopped }

    // === Состояние ===
    public string Name => "PSAR+EMA Combo v2";
    public Config Params { get; }
    public StrategyMode Mode { get; set; } = StrategyMode.Paused;

    // Индикаторы
    private readonly ParabolicSAR _sarLong;
    private readonly EMA _emaLong;
    private readonly ParabolicSAR _sarShort;
    private readonly EMA _emaShort;
    private readonly ATR _atr;

    // Позиция
    private int _posDir;              // 0=flat, 1=long, -1=short
    private readonly double[] _lots;  // entry prices
    private int _nLots;
    private int _maxLots;             // peak lots (для 50% check)
    private double _closedGridProfit;
    private int _sigAvgCount;
    private bool _gridOn;

    // Предыдущие значения
    private double _prevSarL, _prevEmaL;
    private double _prevSarS, _prevEmaS;
    private bool _prevValid;

    // Логирование
    private readonly List<string> _log = new();
    public IReadOnlyList<string> Log => _log;

    // Статистика
    public int TotalTrades { get; private set; }
    public double TotalPnL { get; private set; }
    public int CurrentLots => _nLots;
    public int MaxLotsEver { get; private set; }
    public int PositionDirection => _posDir;

    // Динамические лоты
    private int _currentLotLevel;
    private double _sessionProfit;
    private int _consecutiveLosses;
    public int CurrentLotLevel => _currentLotLevel;
    public double SessionProfit => _sessionProfit;
    public int ConsecutiveLosses => _consecutiveLosses;

    public PsarEmaComboStrategy(Config? config = null)
    {
        Params = config ?? new Config();
        _sarLong = new ParabolicSAR(Params.L_SarStart, Params.L_SarStep, Params.L_SarMax);
        _emaLong = new EMA(Params.L_EmaPeriod);
        _sarShort = new ParabolicSAR(Params.S_SarStart, Params.S_SarStep, Params.S_SarMax);
        _emaShort = new EMA(Params.S_EmaPeriod);
        _atr = new ATR(Params.AtrPeriod);
        _lots = new double[Params.MaxGrid];
        _currentLotLevel = Params.BaseLots;
    }

    public Signal? OnCandle(Candle candle, string ticker)
    {
        // Обновляем индикаторы ВСЕГДА (даже в паузе)
        double sarL = _sarLong.Update(candle);
        double emaL = _emaLong.Update(candle);
        double sarS = _sarShort.Update(candle);
        double emaS = _emaShort.Update(candle);
        double atrVal = _atr.Update(candle);

        if (double.IsNaN(sarL) || double.IsNaN(emaL) || double.IsNaN(sarS) || double.IsNaN(emaS)
            || !_prevValid)
        {
            _prevSarL = sarL; _prevEmaL = emaL;
            _prevSarS = sarS; _prevEmaS = emaS;
            _prevValid = !double.IsNaN(sarL) && !double.IsNaN(emaL)
                      && !double.IsNaN(sarS) && !double.IsNaN(emaS);
            return null;
        }

        double c = candle.Close;

        // Кроссы
        bool longEntry = (_prevSarL >= _prevEmaL && sarL < emaL);
        bool longExit = (_prevSarL <= _prevEmaL && sarL > emaL);
        bool longFlip = _sarLong.FlippedUp;

        bool shortEntry = (_prevSarS <= _prevEmaS && sarS > emaS);
        bool shortExit = (_prevSarS >= _prevEmaS && sarS < emaS);
        bool shortFlip = _sarShort.FlippedDown;

        _prevSarL = sarL; _prevEmaL = emaL;
        _prevSarS = sarS; _prevEmaS = emaS;

        // === STOP MODE: закрыть всё ===
        if (Mode == StrategyMode.Stopped && _posDir != 0)
        {
            return CloseAll(c, ticker, "СТОП ТОРГИ — закрытие позиции");
        }

        // === Позиция открыта ===
        if (_posDir != 0)
        {
            int d = _posDir;
            double gs = d == 1 ? Params.L_GridStep : Params.S_GridStep;
            double gsp = d == 1 ? Params.L_GridSpread : Params.S_GridSpread;

            // Unrealized PnL
            double ur = 0;
            for (int j = 0; j < _nLots; j++)
                ur += d == 1 ? c - _lots[j] : _lots[j] - c;
            double tp = ur + _closedGridProfit;
            double net = tp - _nLots * Params.Commission;

            // === Усреднение по сигналу (2 раза) ===
            bool ourFlip = (d == 1 && longFlip) || (d == -1 && shortFlip);
            if (ourFlip && _sigAvgCount < 2 && ur < 0 && _nLots < Params.MaxGrid
                && Mode != StrategyMode.Paused)
            {
                _lots[_nLots] = c;
                _nLots++;
                _maxLots = Math.Max(_maxLots, _nLots);
                if (_nLots > MaxLotsEver) MaxLotsEver = _nLots;
                _sigAvgCount++;
                if (_sigAvgCount >= 2) _gridOn = true;
                LogMsg($"[{ticker}] SigAvg #{_sigAvgCount}: +1 уровень @ {c:F0}, лотов={_nLots}, grid={(_gridOn ? "ON" : "OFF")}");

                return new Signal
                {
                    Timestamp = candle.Timestamp, Ticker = ticker,
                    Direction = d == 1 ? SignalDirection.Buy : SignalDirection.Sell,
                    Source = SignalSource.Averaging, Volume = _currentLotLevel,
                    StrategyName = Name, Price = c,
                    Comment = $"SigAvg #{_sigAvgCount}"
                };
            }

            // === Grid (после 2 усреднений) ===
            if (_gridOn && _nLots < Params.MaxGrid && _nLots > 0
                && Mode != StrategyMode.Paused)
            {
                double lastEntry = _lots[_nLots - 1];
                bool addGrid = (d == 1 && lastEntry - c >= gs) || (d == -1 && c - lastEntry >= gs);
                if (addGrid)
                {
                    _lots[_nLots] = c;
                    _nLots++;
                    _maxLots = Math.Max(_maxLots, _nLots);
                    if (_nLots > MaxLotsEver) MaxLotsEver = _nLots;
                    LogMsg($"[{ticker}] Grid: +1 @ {c:F0}, лотов={_nLots}");

                    return new Signal
                    {
                        Timestamp = candle.Timestamp, Ticker = ticker,
                        Direction = d == 1 ? SignalDirection.Buy : SignalDirection.Sell,
                        Source = SignalSource.Averaging, Volume = _currentLotLevel,
                        StrategyName = Name, Price = c,
                        Comment = $"Grid lvl {_nLots}"
                    };
                }
            }

            // === Grid тейк на уровень ===
            if (_gridOn)
            {
                for (int j = 1; j < _nLots; j++)
                {
                    double levelPnl = (d == 1 ? c - _lots[j] : _lots[j] - c) - Params.Commission;
                    if (levelPnl >= gsp)
                    {
                        _closedGridProfit += levelPnl;
                        LogMsg($"[{ticker}] Grid тейк lvl {j}: +{levelPnl:F0} пт @ {c:F0}");
                        // Сдвигаем массив
                        for (int k = j; k < _nLots - 1; k++) _lots[k] = _lots[k + 1];
                        _nLots--;

                        return new Signal
                        {
                            Timestamp = candle.Timestamp, Ticker = ticker,
                            Direction = d == 1 ? SignalDirection.Sell : SignalDirection.Buy,
                            Source = SignalSource.Exit, Volume = _currentLotLevel,
                            StrategyName = Name, Price = c,
                            Comment = $"Grid тейк +{levelPnl:F0}"
                        };
                    }
                }
            }

            // Recalc после grid ops
            ur = 0;
            for (int j = 0; j < _nLots; j++)
                ur += d == 1 ? c - _lots[j] : _lots[j] - c;
            tp = ur + _closedGridProfit;
            net = tp - _nLots * Params.Commission;
            int closed = _maxLots - _nLots;

            // === Close All: 50% grid closed + PnL OK ===
            if (_gridOn && _maxLots > 1 && closed >= _maxLots * 0.50 && net > 0)
            {
                double ppl = _nLots > 0 ? net / _nLots : net;
                if (ppl >= Params.MinProfitPerLot)
                {
                    return CloseAll(c, ticker, $"50% grid closed ({closed}/{_maxLots}), PnL={net:F0}, ppl={ppl:F0}");
                }
            }

            // === SAR×EMA обратный кросс ===
            bool exitCross = (d == 1 && longExit) || (d == -1 && shortExit);
            if (exitCross)
            {
                return CloseAll(c, ticker, $"SAR×EMA кросс, PnL={net:F0}");
            }
        }

        // === Вход (только flat + running) ===
        if (_posDir == 0 && Mode == StrategyMode.Running)
        {
            if (longEntry)
            {
                // ATR фильтр
                if (Params.AtrFilterEnabled && !double.IsNaN(atrVal) && atrVal > 0)
                {
                    double dist = Math.Abs(c - sarL);
                    if (dist < Params.AtrFilter * atrVal)
                    {
                        LogMsg($"[{ticker}] LONG пропущен: dist={dist:F0} < {Params.AtrFilter}*ATR={atrVal:F0}");
                        goto SkipEntry;
                    }
                }
                _posDir = 1; _lots[0] = c; _nLots = 1; _maxLots = 1;
                _closedGridProfit = 0; _sigAvgCount = 0; _gridOn = false;
                LogMsg($"[{ticker}] LONG @ {c:F0} (lots={_currentLotLevel})");
                return new Signal
                {
                    Timestamp = candle.Timestamp, Ticker = ticker,
                    Direction = SignalDirection.Buy, Source = SignalSource.Strategy,
                    Volume = _currentLotLevel, StrategyName = Name, Price = c,
                    Comment = "Long entry: SAR < EMA"
                };
            }
            if (shortEntry)
            {
                // ATR фильтр
                if (Params.AtrFilterEnabled && !double.IsNaN(atrVal) && atrVal > 0)
                {
                    double dist = Math.Abs(c - sarS);
                    if (dist < Params.AtrFilter * atrVal)
                    {
                        LogMsg($"[{ticker}] SHORT пропущен: dist={dist:F0} < {Params.AtrFilter}*ATR={atrVal:F0}");
                        goto SkipEntry;
                    }
                }
                _posDir = -1; _lots[0] = c; _nLots = 1; _maxLots = 1;
                _closedGridProfit = 0; _sigAvgCount = 0; _gridOn = false;
                LogMsg($"[{ticker}] SHORT @ {c:F0} (lots={_currentLotLevel})");
                return new Signal
                {
                    Timestamp = candle.Timestamp, Ticker = ticker,
                    Direction = SignalDirection.Sell, Source = SignalSource.Strategy,
                    Volume = _currentLotLevel, StrategyName = Name, Price = c,
                    Comment = "Short entry: SAR > EMA"
                };
            }
            SkipEntry:;
        }

        return null;
    }

    private void UpdateDynamicLots(double pnl)
    {
        if (pnl > 0)
        {
            _consecutiveLosses = 0;
            _sessionProfit += pnl;
            if (_sessionProfit >= Params.LotStepProfit)
            {
                _currentLotLevel = Math.Min(_currentLotLevel + 1, Params.MaxDynamicLots);
                _sessionProfit = 0;
                LogMsg($"[DYN] Лоты +1 → {_currentLotLevel}");
            }
        }
        else
        {
            _consecutiveLosses++;
            _currentLotLevel = Math.Max(_currentLotLevel - 1, 1);
            if (_consecutiveLosses >= 2)
            {
                _currentLotLevel = Math.Max(_currentLotLevel - 1, 1);
                _consecutiveLosses = 0;
            }
            _sessionProfit = 0;
            LogMsg($"[DYN] Убыток, лоты → {_currentLotLevel}, consec={_consecutiveLosses}");
        }
    }

    private Signal CloseAll(double price, string ticker, string reason)
    {
        double ur = 0;
        for (int j = 0; j < _nLots; j++)
            ur += _posDir == 1 ? price - _lots[j] : _lots[j] - price;
        double net = ur + _closedGridProfit - _nLots * Params.Commission;

        LogMsg($"[{ticker}] CLOSE ALL: {reason} | lots={_nLots} | net={net:F0}");
        TotalTrades++;
        TotalPnL += net;
        UpdateDynamicLots(net);

        var signal = new Signal
        {
            Timestamp = DateTime.UtcNow, Ticker = ticker,
            Direction = _posDir == 1 ? SignalDirection.Sell : SignalDirection.Buy,
            Source = SignalSource.Exit,
            Volume = _nLots * _currentLotLevel,
            StrategyName = Name, Price = price,
            Comment = reason
        };

        _posDir = 0; _nLots = 0; _maxLots = 0;
        _closedGridProfit = 0; _sigAvgCount = 0; _gridOn = false;

        return signal;
    }

    public void Reset()
    {
        _sarLong.Reset(); _emaLong.Reset();
        _sarShort.Reset(); _emaShort.Reset();
        _atr.Reset();
        _posDir = 0; _nLots = 0; _maxLots = 0;
        _closedGridProfit = 0; _sigAvgCount = 0; _gridOn = false;
        _prevValid = false;
        _currentLotLevel = Params.BaseLots;
        _sessionProfit = 0;
        _consecutiveLosses = 0;
        _log.Clear();
    }

    private void LogMsg(string msg)
    {
        _log.Add($"[{DateTime.UtcNow:HH:mm:ss}] {msg}");
        if (_log.Count > 1000) _log.RemoveAt(0);
    }
}
