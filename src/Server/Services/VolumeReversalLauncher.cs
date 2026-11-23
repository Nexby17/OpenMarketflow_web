using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;
using HedgeFund.Core.Risk;

namespace HedgeFund.Server.Services;

/// <summary>
/// Лаунчер Volume Reversal стратегии.
/// Использует общий FinamConnector из TradingService.
/// TF: 1 мин. Инструмент: RIM6 (RTS фьючерс).
/// WF OOS: 2/2 wins, +43,310, 1338 trades
/// </summary>
public class VolumeReversalLauncher : IDisposable
{
    private RiskGate? _riskGate;
    public void SetRiskGate(RiskGate gate) => _riskGate = gate;

    private readonly FinamConnector _broker;
    private readonly VolumeReversalStrategy _strategy;
    private readonly string _ticker;
    private readonly TimeSpan _timeframe = TimeSpan.FromMinutes(1);

    public VolumeReversalStrategy Strategy => _strategy;
    public bool IsConnected => _broker.IsConnected;
    public bool IsInitialized { get; private set; }

    public VolumeReversalLauncher(FinamConnector broker, string ticker = "RIM6")
    {
        _ticker = ticker;
        _broker = broker;
        _strategy = new VolumeReversalStrategy(new VolumeReversalStrategy.Config
        {
            NBars = 2,
            VolMult = 1.5,
            StopLoss = 133.0,
            TpMult = 2.5,
            TrailPct = 0.7,
            AvgVolWindow = 30,
            Commission = 0.90
        });

        _ = InitAsync();
    }

    private async Task InitAsync()
    {
        if (!_broker.IsConnected)
        {
            Console.WriteLine("[VR] ⏳ Ожидание подключения к Финам...");
            int wait = 0;
            while (!_broker.IsConnected && wait < 60)
            {
                await Task.Delay(1000);
                wait++;
            }
            if (!_broker.IsConnected)
            {
                Console.WriteLine("[VR] ❌ Финам не подключён");
                return;
            }
        }

        Console.WriteLine("[VR] ✅ Финам подключён. Прогрев...");
        var from = DateTime.UtcNow.AddDays(-3);
        var to = DateTime.UtcNow;
        var history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe, from, to);

        if (history.Length > 0)
        {
            Console.WriteLine($"[VR] Загружено {history.Length} исторических свечей");
            for (int i = 0; i < history.Length; i++)
            {
                _strategy.OnCandle(history[i], _ticker);
                if (i % 1000 == 999) Console.WriteLine($"[VR] Прогрев: {i + 1}/{history.Length}...");
            }
            Console.WriteLine($"[VR] ✅ Прогрето. AvgVol={_strategy.CurrentAvgVolume:F0}");
        }
        else
        {
            Console.WriteLine("[VR] ⚠️ Нет исторических данных");
        }

        Console.WriteLine($"[VR] 📡 Подписка на {_ticker} 1-мин...");
        await _broker.SubscribeCandlesAsync(_ticker, _timeframe, OnNewCandle);
        Console.WriteLine($"[VR] 📡 Подписка активна");
        IsInitialized = true;
    }

    private int _candleCount = 0;
    private void OnNewCandle(Candle candle)
    {
        _candleCount++;
        if (_candleCount % 60 == 1)
        {
            string pos = _strategy.PositionDirection != 0
                ? $"Pos={_strategy.PositionDirection} SL={_strategy.StopLoss:F0} TP={_strategy.TakeProfit:F0}"
                : "Flat";
            Console.WriteLine($"[VR] 🕯️ #{_candleCount}: {candle.Timestamp:HH:mm:ss} O={candle.Open:F0} H={candle.High:F0} L={candle.Low:F0} C={candle.Close:F0} V={candle.Volume:F0} | {pos}");
        }

        var signal = _strategy.OnCandle(candle, _ticker);

        if (signal != null && signal.Direction != SignalDirection.None)
        {
            Console.WriteLine($"[VR SIGNAL] {signal.Comment} | Dir={signal.Direction} Vol={signal.Volume} @ {signal.Price:F0} | Trades={_strategy.TotalTrades} PnL={_strategy.TotalPnL:F0}");

            if (_strategy.Mode == VolumeReversalStrategy.StrategyMode.Running
                || _strategy.Mode == VolumeReversalStrategy.StrategyMode.Stopped)
            {
                _ = ExecuteSignal(signal);
            }
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
            if (RiskIntegrationHelper.IsTradingBlocked(_riskGate)) { Console.WriteLine("[VR] Order blocked by circuit breaker"); return; }
            await _broker.PlaceOrderAsync(order);
            Console.WriteLine($"[VR EXEC] ✅ {signal.Direction} {signal.Volume}x {_ticker}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[VR EXEC] ❌ {ex.Message}");
        }
    }

    public void Start()
    {
        _strategy.Mode = VolumeReversalStrategy.StrategyMode.Running;
        Console.WriteLine("[VR CMD] ▶️ СТАРТ");
    }

    public void StopTrading()
    {
        _strategy.Mode = VolumeReversalStrategy.StrategyMode.Stopped;
        _strategy.ForceClose();
        Console.WriteLine("[VR CMD] ⏹️ СТОП");
    }

    public void Pause()
    {
        _strategy.Mode = VolumeReversalStrategy.StrategyMode.Paused;
        Console.WriteLine("[VR CMD] ⏸️ ПАУЗА");
    }

    public string GetStatus()
    {
        string pos = _strategy.PositionDirection != 0
            ? $"Pos={_strategy.PositionDirection} SL={_strategy.StopLoss:F0} TP={_strategy.TakeProfit:F0} Bars={_strategy.BarsInPosition}"
            : "Pos=0 (Flat)";

        return $"Mode={_strategy.Mode} | {pos} | Trades={_strategy.TotalTrades} | PnL={_strategy.TotalPnL:F0} | AvgVol={_strategy.CurrentAvgVolume:F0} | Connected={IsConnected}";
    }

    public void Dispose()
    {
        // Don't dispose broker — it's shared from TradingService
    }
}
