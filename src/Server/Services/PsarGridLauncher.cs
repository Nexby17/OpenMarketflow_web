using HedgeFund.Core;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
using HedgeFund.Brokers.Finam;

namespace HedgeFund.Server.Services;

/// <summary>
/// PSAR Grid MM + RV/HV regime filter.
/// RV < HV → grid ON. RV >= HV → close all, wait.
/// </summary>
public class PsarGridLauncher : IDisposable
{
    private readonly FinamConnector _broker;
    private readonly PsarGridStrategy _strategy;
    private readonly string _ticker;
    private readonly TimeSpan _timeframe = TimeSpan.FromMinutes(1);

    public PsarGridStrategy Strategy => _strategy;
    public bool IsConnected => _broker.IsConnected;

    public PsarGridLauncher(string finamToken, string ticker = "SiM6", string accountId = "")
    {
        _ticker = ticker;
        _broker = new FinamConnector();
        _strategy = new PsarGridStrategy(new PsarGridStrategy.Config
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

        _broker.OnError += msg => Console.WriteLine($"[PSAR-GRID] BROKER ERROR: {msg}");
        _broker.OnTrade += trade => Console.WriteLine($"[PSAR-GRID] TRADE: {trade.Direction} {trade.Volume}x @ {trade.Price:F0}");

        _ = ConnectAndWarm(finamToken, accountId);
    }

    private async Task ConnectAndWarm(string token, string accountId)
    {
        Console.WriteLine("[PSAR-GRID] Подключение к Финам...");
        bool ok = await _broker.ConnectAsync(token, accountId);
        if (!ok) { Console.WriteLine("[PSAR-GRID] ❌ Не подключён"); return; }
        Console.WriteLine("[PSAR-GRID] ✅ Подключён");

        Console.WriteLine($"[PSAR-GRID] 📐 Прогрев ({_ticker}, 1-мин)...");
        var history = await _broker.GetHistoricalCandlesAsync(_ticker, _timeframe,
            DateTime.UtcNow.AddDays(-7), DateTime.UtcNow);

        if (history.Length > 0)
        {
            Console.WriteLine($"[PSAR-GRID] Загружено {history.Length} свечей");
            try
            {
                for (int i = 0; i < history.Length; i++)
                {
                    _strategy.OnCandle(history[i], _ticker);
                    if (i % 1000 == 999) Console.WriteLine($"[PSAR-GRID] Прогрев: {i + 1}/{history.Length}...");
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[PSAR-GRID] ❌ Ошибка прогрева: {ex.Message}");
            }
            Console.WriteLine($"[PSAR-GRID] ✅ Прогрето. RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4}");
        }
        else
        {
            Console.WriteLine("[PSAR-GRID] ⚠️ Нет данных");
        }

        Console.WriteLine($"[PSAR-GRID] 📡 Подписка {_ticker}...");
        await _broker.SubscribeCandlesAsync(_ticker, _timeframe, OnNewCandle);
        Console.WriteLine("[PSAR-GRID] 📡 Активен");
    }

    private int _candleCount = 0;
    private void OnNewCandle(Candle candle)
    {
        _candleCount++;
        if (_candleCount % 60 == 1)
            Console.WriteLine($"[PSAR-GRID] 🕯️ #{_candleCount}: {candle.Timestamp:HH:mm:ss} O={candle.Open:F0} C={candle.Close:F0} | " +
                $"SAR={_strategy.CurrentSar:F0} EMA={_strategy.CurrentEma:F0} | " +
                $"RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4} " +
                $"({(_strategy.IsLowVol ? "LOW→TRADE" : "HIGH→WAIT")})");

        var signal = _strategy.OnCandle(candle, _ticker);

        while (signal != null && signal.Direction != SignalDirection.None)
        {
            Console.WriteLine($"[SIGNAL] {signal.Comment} | Dir={signal.Direction} Vol={signal.Volume} @ {signal.Price:F0}");

            if (_strategy.Mode == PsarGridStrategy.StrategyMode.Running
                || _strategy.Mode == PsarGridStrategy.StrategyMode.Stopped)
            {
                _ = ExecuteSignal(signal);
            }

            // Проверяем queue
            signal = _strategy.OnCandle(candle, _ticker);
        }

        if (_strategy.PositionDirection != 0 && _candleCount % 60 == 0)
        {
            Console.WriteLine($"[STATE] Pos={_strategy.PositionDirection} Lots={_strategy.CurrentLotLevel} " +
                $"Trades={_strategy.TotalTrades} PnL={_strategy.TotalPnL:F0} " +
                $"GridPnL={_strategy.CurrentGridPnL:F0} OpenLots={_strategy.OpenGridLevels}");
        }
    }

    private async Task ExecuteSignal(Signal signal)
    {
        try
        {
            await _broker.PlaceOrderAsync(new Order
            {
                Ticker = _ticker,
                Direction = signal.Direction,
                Type = OrderType.Market,
                Volume = signal.Volume,
                Comment = signal.Comment
            });
            Console.WriteLine($"[EXEC] ✅ {signal.Direction} {signal.Volume}x {_ticker}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[EXEC] ❌ {ex.Message}");
        }
    }

    public void Start()
    {
        _strategy.Mode = PsarGridStrategy.StrategyMode.Running;
        Console.WriteLine("[PSAR-GRID] ▶️ СТАРТ");
    }

    public void StopTrading()
    {
        _strategy.Mode = PsarGridStrategy.StrategyMode.Stopped;
        Console.WriteLine("[PSAR-GRID] ⏹️ СТОП — закрытие позиций");
    }

    public void Pause()
    {
        _strategy.Mode = PsarGridStrategy.StrategyMode.Paused;
        Console.WriteLine("[PSAR-GRID] ⏸️ ПАУЗА");
    }

    public string GetStatus()
    {
        string regime;
        if (_strategy.CurrentHv <= 0)
            regime = "warming...";
        else if (_strategy.IsLowVol)
            regime = $"RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4} (LOW→TRADE)";
        else
            regime = $"RV={_strategy.CurrentRv:F4} HV={_strategy.CurrentHv:F4} (HIGH→WAIT)";

        string grid = _strategy.PositionDirection != 0
            ? $"GridPnL={_strategy.CurrentGridPnL:F0} OpenLots={_strategy.OpenGridLevels} "
            : "";

        return $"Mode={_strategy.Mode} | Pos={_strategy.PositionDirection} | " +
               $"Lots={_strategy.CurrentLotLevel} | Trades={_strategy.TotalTrades} | PnL={_strategy.TotalPnL:F0} | " +
               $"{grid}{regime} | Connected={IsConnected}";
    }

    public void Dispose()
    {
        _broker.Dispose();
    }
}
