using HedgeFund.Core;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// Лаунчер Grid MM Regime стратегии на Финам.
/// TF: 1 мин. Инструмент: SiM6 (или актуальный SI фьючерс).
/// Управление: Start() / Stop() / Pause()
/// </summary>
public class GridMmRegimeLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly GridMmRegimeStrategy _strategy;
    private readonly string _ticker;
    private readonly TimeSpan _timeframe = TimeSpan.FromMinutes(1);
    private CancellationTokenSource? _cts;

    public GridMmRegimeStrategy Strategy => _strategy;
    public bool IsConnected => _broker.IsConnected;

    public GridMmRegimeLauncher(string finamToken, string ticker = "SiM6", string accountId = "")
    {
        _ticker = ticker;
        _broker = new FinamConnector();
        _strategy = new GridMmRegimeStrategy(new GridMmRegimeStrategy.Config
        {
            SarStart = 0.005,
            SarStep = 0.01,
            SarMax = 0.2,
            EmaPeriod = 500,
            GridStep = 35.0,
            GridSpread = 35.0,
            MaxGridLevels = 50,
            MinProfitPerLot = 28.0,
            ClosePct = 0.50,
            RvWindow = 288,
            HvWindow = 1440,
            Commission = 0.90,
            LotStepProfit = 1000.0,
            MaxLots = 3
        });

        _broker.OnError += msg => Console.WriteLine($"[BROKER ERROR] {msg}");
        _broker.OnTrade += trade => Console.WriteLine($"[TRADE] {trade.Direction} {trade.Volume}x @ {trade.Price:F0}");

        _ = ConnectAndWarm(finamToken, accountId);
    }

    private async Task ConnectAndWarm(string token, string accountId)
    {
        Console.WriteLine($"[GRID-MM] Подключение к Финам...");
        bool ok = await _broker.ConnectAsync(token, accountId);
        if (!ok)
        {
            Console.WriteLine("[GRID-MM] ❌ Не удалось подключиться к Финам!");
            return;
        }
        Console.WriteLine($"[GRID-MM] ✅ Подключён.");

        // Прогрев: 1-мин свечи за последние 7 дней (нужно для EMA500 + RV/HV)
        Console.WriteLine($"[GRID-MM] 📐 Прогрев индикаторов ({_ticker}, 1-мин)...");
        var from = DateTime.UtcNow.AddDays(-7);
        var to = DateTime.UtcNow;
        var history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);

        if (history.Length > 0)
        {
            Console.WriteLine($"[GRID-MM] Загружено {history.Length} исторических свечей");
            try
            {
                for (int i = 0; i < history.Length; i++)
                {
                    _strategy.OnCandle(history[i], _ticker);
                    if (i % 1000 == 999) Console.WriteLine($"[GRID-MM] Прогрев: {i + 1}/{history.Length}...");
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[GRID-MM] ❌ ОШИБКА ПРОГРЕВА: {ex.Message} {ex.StackTrace}");
            }
            Console.WriteLine($"[GRID-MM] ✅ Прогрето. RV/HV ready. Лоты={_strategy.CurrentLotLevel}");
        }
        else
        {
            Console.WriteLine($"[GRID-MM] ⚠️ Нет исторических данных");
        }

        Console.WriteLine($"[GRID-MM] 📡 Подписка на {_ticker} 1-мин...");
        await _broker.SubscribeCandlesAsync(_ticker, _timeframe, OnNewCandle);
        Console.WriteLine($"[GRID-MM] 📡 Подписка на {_ticker} 1-мин активна");
    }

    private int _candleCount = 0;
    private void OnNewCandle(Candle candle)
    {
        _candleCount++;
        if (_candleCount % 60 == 1)
        {
            Console.WriteLine($"[GRID-MM] 🕯️ Свеча #{_candleCount}: {candle.Timestamp:HH:mm:ss} O={candle.Open:F0} H={candle.High:F0} L={candle.Low:F0} C={candle.Close:F0} | SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0}");
        }
        var signal = _strategy.OnCandle(candle, _ticker);

        if (signal != null && signal.Direction != SignalDirection.None)
        {
            string regime = _strategy.IsRegimeLowVol ? "LOW" : "HIGH";
            Console.WriteLine($"[SIGNAL] {signal.Comment} | Dir={signal.Direction} Vol={signal.Volume} @ {signal.Price:F0} | Regime={regime} RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4}");

            if (_strategy.Mode == GridMmRegimeStrategy.StrategyMode.Running
                || _strategy.Mode == GridMmRegimeStrategy.StrategyMode.Stopped)
            {
                _ = ExecuteSignal(signal);
            }
        }

        // Status log every 60 bars (~1 hour)
        if (_strategy.PositionDirection != 0)
        {
            Console.WriteLine($"[STATE] Pos={_strategy.PositionDirection} Lots={_strategy.CurrentLotLevel} " +
                              $"Trades={_strategy.TotalTrades} PnL={_strategy.TotalPnL:F0} " +
                              $"Regime={(_strategy.IsRegimeLowVol ? "LOW" : "HIGH")}");
        }
    }

    private async Task ExecuteSignal(Signal signal)
    {
        try
        {
            var order = new Order
            {
                Ticker = _ticker,
                Direction = signal.Direction,
                Type = OrderType.Market,
                Volume = signal.Volume,
                Comment = signal.Comment
            };
            await _broker.PlaceOrderAsync(order);
            Console.WriteLine($"[EXEC] ✅ {signal.Direction} {signal.Volume}x {_ticker}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXEC] ❌ {ex.Message}");
        }
    }

    public void Start()
    {
        _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Running;
        Console.WriteLine("[CMD] ▶️ СТАРТ — Grid MM торгует");
    }

    public void StopTrading()
    {
        _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Stopped;
        Console.WriteLine("[CMD] ⏹️ СТОП — закрытие позиций");
    }

    public void Pause()
    {
        _strategy.Mode = GridMmRegimeStrategy.StrategyMode.Paused;
        Console.WriteLine("[CMD] ⏸️ ПАУЗА");
    }

    public string GetStatus()
    {
        string regime;
        if (_strategy.CurrentHv <= 0)
            regime = "RV/HV warming...";
        else if (_strategy.IsRegimeLowVol)
            regime = $"RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4} ({_strategy.CurrentRv}<{_strategy.CurrentHv} → LOW)";
        else
            regime = $"RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4} ({_strategy.CurrentRv}>={_strategy.CurrentHv} → HIGH)";
        
        string gridInfo = _strategy.PositionDirection != 0 
            ? $"GridPnL={_strategy.CurrentGridPnL:F0} OpenLots={_strategy.OpenGridLevels} "
            : "";
            
        return $"Mode={_strategy.Mode} | Pos={_strategy.PositionDirection} | " +
               $"Lots={_strategy.CurrentLotLevel} | Trades={_strategy.TotalTrades} | PnL={_strategy.TotalPnL:F0} | " +
               $"{gridInfo}{regime} | Connected={IsConnected}";
    }

    public void Dispose()
    {
        _cts?.Cancel();
        _broker.Dispose();
    }
}
