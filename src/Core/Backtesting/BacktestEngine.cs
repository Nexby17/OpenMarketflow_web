using System.Diagnostics;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;

namespace HedgeFund.Core.Backtesting;

/// <summary>
/// Бэктестер. Прогоняет стратегию по историческим свечам,
/// считает PnL, equity curve, метрики.
/// </summary>
public class BacktestEngine
{
    public BacktestResult Run(IStrategy strategy, List<Candle> candles, BacktestSettings settings)
    {
        var sw = Stopwatch.StartNew();
        strategy.Reset();

        var trades = new List<TradeRecord>();
        var equityCurve = new List<double>();
        double equity = 0;
        double peakEquity = 0;
        double maxDrawdown = 0;

        // Состояние позиции
        bool inPosition = false;
        SignalDirection posDir = SignalDirection.None;
        double entryPrice = 0;
        DateTime entryTime = DateTime.MinValue;
        DateTime prevDay = DateTime.MinValue;

        for (int i = 0; i < candles.Count; i++)
        {
            var candle = candles[i];

            // Закрытие в конце дня
            if (settings.CloseEndOfDay && inPosition && prevDay != DateTime.MinValue
                && candle.Timestamp.Date != prevDay)
            {
                // Используем Close предыдущей свечи (последнюю цену дня)
                double exitPrice = i > 0 ? candles[i - 1].Close : candle.Close;
                double pnl = CalculatePnL(posDir, entryPrice, exitPrice, settings.Lots, settings.Commission);
                equity += pnl;

                trades.Add(new TradeRecord
                {
                    EntryTime = entryTime,
                    ExitTime = candles[i - 1].Timestamp,
                    Ticker = settings.Ticker,
                    Direction = posDir,
                    EntryPrice = entryPrice,
                    ExitPrice = exitPrice,
                    Volume = settings.Lots,
                    PnL = pnl,
                    Strategy = strategy.Name,
                    Comment = "Закрытие в конце дня"
                });

                inPosition = false;
            }

            prevDay = candle.Timestamp.Date;

            // Проверка TP/SL для открытой позиции
            if (inPosition)
            {
                double unrealizedPnL = posDir == SignalDirection.Buy
                    ? (candle.Close - entryPrice) * settings.Lots
                    : (entryPrice - candle.Close) * settings.Lots;

                bool tpHit = settings.TakeProfit > 0 && unrealizedPnL >= settings.TakeProfit;
                bool slHit = settings.StopLoss > 0 && unrealizedPnL <= -settings.StopLoss;

                if (tpHit || slHit)
                {
                    double pnl = unrealizedPnL - settings.Commission * 2;
                    equity += pnl;

                    trades.Add(new TradeRecord
                    {
                        EntryTime = entryTime,
                        ExitTime = candle.Timestamp,
                        Ticker = settings.Ticker,
                        Direction = posDir,
                        EntryPrice = entryPrice,
                        ExitPrice = candle.Close,
                        Volume = settings.Lots,
                        PnL = pnl,
                        Strategy = strategy.Name,
                        Comment = tpHit ? "Take Profit" : "Stop Loss"
                    });

                    inPosition = false;
                    equityCurve.Add(equity);

                    if (equity > peakEquity) peakEquity = equity;
                    double dd = peakEquity - equity;
                    if (dd > maxDrawdown) maxDrawdown = dd;

                    continue;
                }
            }

            // Получаем сигнал от стратегии
            var signal = strategy.OnCandle(candle, settings.Ticker);

            if (signal != null)
            {
                if (!inPosition && (signal.Direction == SignalDirection.Buy || signal.Direction == SignalDirection.Sell)
                    && signal.Source == SignalSource.Strategy)
                {
                    // Открытие позиции
                    inPosition = true;
                    posDir = signal.Direction;
                    entryPrice = candle.Close;
                    entryTime = candle.Timestamp;
                }
                else if (inPosition && signal.Source == SignalSource.Exit)
                {
                    // Закрытие позиции
                    double pnl = CalculatePnL(posDir, entryPrice, candle.Close, settings.Lots, settings.Commission);
                    equity += pnl;

                    trades.Add(new TradeRecord
                    {
                        EntryTime = entryTime,
                        ExitTime = candle.Timestamp,
                        Ticker = settings.Ticker,
                        Direction = posDir,
                        EntryPrice = entryPrice,
                        ExitPrice = candle.Close,
                        Volume = settings.Lots,
                        PnL = pnl,
                        Strategy = strategy.Name,
                        Comment = signal.Comment
                    });

                    inPosition = false;
                }
            }

            equityCurve.Add(equity);

            if (equity > peakEquity) peakEquity = equity;
            double drawdown = peakEquity - equity;
            if (drawdown > maxDrawdown) maxDrawdown = drawdown;
        }

        // Закрытие незавершённой позиции
        if (inPosition && candles.Count > 0)
        {
            var lastCandle = candles[^1];
            double pnl = CalculatePnL(posDir, entryPrice, lastCandle.Close, settings.Lots, settings.Commission);
            equity += pnl;

            trades.Add(new TradeRecord
            {
                EntryTime = entryTime,
                ExitTime = lastCandle.Timestamp,
                Ticker = settings.Ticker,
                Direction = posDir,
                EntryPrice = entryPrice,
                ExitPrice = lastCandle.Close,
                Volume = settings.Lots,
                PnL = pnl,
                Strategy = strategy.Name,
                Comment = "Закрытие: конец данных"
            });

            equityCurve[^1] = equity;
        }

        sw.Stop();

        // Расчёт метрик
        var wins = trades.Where(t => t.PnL > 0).ToList();
        var losses = trades.Where(t => t.PnL <= 0).ToList();

        double totalWin = wins.Sum(t => t.PnL);
        double totalLoss = Math.Abs(losses.Sum(t => t.PnL));
        double winRate = trades.Count > 0 ? (double)wins.Count / trades.Count * 100 : 0;
        double profitFactor = totalLoss > 0 ? totalWin / totalLoss : totalWin > 0 ? double.PositiveInfinity : 0;
        double avgWin = wins.Count > 0 ? wins.Average(t => t.PnL) : 0;
        double avgLoss = losses.Count > 0 ? losses.Average(t => t.PnL) : 0;

        // Sharpe Ratio (по сделкам)
        double sharpe = 0;
        if (trades.Count > 1)
        {
            double avgPnl = trades.Average(t => t.PnL);
            double stdPnl = Math.Sqrt(trades.Sum(t => (t.PnL - avgPnl) * (t.PnL - avgPnl)) / (trades.Count - 1));
            sharpe = stdPnl > 0 ? avgPnl / stdPnl * Math.Sqrt(252) : 0;
        }

        double maxDdPercent = peakEquity > 0 ? maxDrawdown / peakEquity * 100 : 0;

        return new BacktestResult
        {
            StrategyName = strategy.Name,
            TotalPnL = equity,
            TotalTrades = trades.Count,
            WinRate = winRate,
            ProfitFactor = profitFactor,
            SharpeRatio = sharpe,
            MaxDrawdown = maxDrawdown,
            MaxDrawdownPercent = maxDdPercent,
            AvgWin = avgWin,
            AvgLoss = avgLoss,
            Trades = trades,
            EquityCurve = equityCurve,
            Settings = settings,
            Duration = sw.Elapsed
        };
    }

    private static double CalculatePnL(SignalDirection dir, double entry, double exit, int lots, double commission)
    {
        double raw = dir == SignalDirection.Buy
            ? (exit - entry) * lots
            : (entry - exit) * lots;
        return raw - commission * 2; // комиссия на вход и выход
    }
}
