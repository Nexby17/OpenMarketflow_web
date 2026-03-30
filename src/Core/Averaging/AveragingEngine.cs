using HedgeFund.Core.Models;

namespace HedgeFund.Core.Averaging;

/// <summary>
/// Движок усреднения. Принимает решения: усреднять, закрывать или ждать.
/// </summary>
public enum AveragingDecision
{
    /// <summary>Ничего не делать, ждать следующий сигнал</summary>
    Hold,
    
    /// <summary>Закрыть всю позицию (вышли в плюс)</summary>
    CloseAll,
    
    /// <summary>Усреднить (добавить к позиции)</summary>
    Average,
    
    /// <summary>Закрыть по стоп-лоссу</summary>
    StopLoss
}

public class AveragingResult
{
    public AveragingDecision Decision { get; set; }
    public int Volume { get; set; }
    public string Reason { get; set; } = string.Empty;
}

public class AveragingEngine
{
    private readonly AveragingSettings _settings;

    public AveragingEngine(AveragingSettings settings)
    {
        _settings = settings;
    }

    /// <summary>
    /// Основной метод принятия решений.
    /// Вызывается при каждом тике/свече или при новом сигнале стратегии.
    /// </summary>
    /// <param name="position">Текущая позиция</param>
    /// <param name="currentPrice">Текущая цена</param>
    /// <param name="hasNewSignal">Есть ли новый сигнал от стратегии</param>
    public AveragingResult Evaluate(Position position, double currentPrice, bool hasNewSignal)
    {
        if (!position.IsOpen)
        {
            return new AveragingResult 
            { 
                Decision = AveragingDecision.Hold, 
                Reason = "Позиция не открыта" 
            };
        }

        double pnl = position.GetPnL(currentPrice);

        // 1. Проверка стоп-лосса (если включён)
        if (_settings.StopLossEnabled)
        {
            double stopLossLevel = GetStopLossLevel(position);
            bool stopTriggered = position.Direction == SignalDirection.Buy
                ? currentPrice <= stopLossLevel
                : currentPrice >= stopLossLevel;

            if (stopTriggered)
            {
                return new AveragingResult
                {
                    Decision = AveragingDecision.StopLoss,
                    Volume = position.TotalVolume,
                    Reason = $"Стоп-лосс сработал. Цена: {currentPrice}, уровень: {stopLossLevel:F2}, PnL: {pnl:F2}"
                };
            }
        }

        // 2. Если PnL > 0 — закрываем всю позицию
        if (position.IsInProfit(currentPrice))
        {
            double minProfit = _settings.MinProfitToClose;
            if (minProfit <= 0 || pnl >= minProfit)
            {
                return new AveragingResult
                {
                    Decision = AveragingDecision.CloseAll,
                    Volume = position.TotalVolume,
                    Reason = $"Позиция в плюсе. PnL: {pnl:F2}, средняя: {position.AveragePrice:F2}, текущая: {currentPrice:F2}"
                };
            }
        }

        // 3. Если PnL < 0 и есть новый сигнал — усредняем
        if (pnl < 0 && hasNewSignal)
        {
            if (_settings.CanAverage(position.AveragingCount))
            {
                int nextLot = _settings.GetNextLotSize(position.AveragingCount);
                return new AveragingResult
                {
                    Decision = AveragingDecision.Average,
                    Volume = nextLot,
                    Reason = $"Усреднение #{position.AveragingCount + 1}. PnL: {pnl:F2}, добавка: {nextLot} лот(ов)"
                };
            }
            else
            {
                return new AveragingResult
                {
                    Decision = AveragingDecision.Hold,
                    Reason = $"Лимит усреднений достигнут ({_settings.MaxAveragingCount}). PnL: {pnl:F2}"
                };
            }
        }

        // 4. Иначе — ждём
        return new AveragingResult
        {
            Decision = AveragingDecision.Hold,
            Reason = $"Ожидание. PnL: {pnl:F2}, сигнал: {(hasNewSignal ? "да" : "нет")}"
        };
    }

    /// <summary>Рассчитать уровень стоп-лосса</summary>
    private double GetStopLossLevel(Position position)
    {
        double avg = position.AveragePrice;
        double offset;

        if (_settings.UsePercentStopLoss)
            offset = avg * _settings.StopLossPercent / 100.0;
        else
            offset = _settings.StopLossPoints;

        return position.Direction == SignalDirection.Buy
            ? avg - offset
            : avg + offset;
    }
}
