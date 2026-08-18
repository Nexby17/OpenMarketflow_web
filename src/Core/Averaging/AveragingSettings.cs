namespace HedgeFund.Core.Averaging;

public enum AveragingMode
{
    /// <summary>Фиксированный объём каждый раз (по умолчанию 1 лот)</summary>
    Fixed,
    
    /// <summary>Мартингейл: каждая добавка удваивает объём (1→2→4→8...)</summary>
    Martingale
}

public class AveragingSettings
{
    /// <summary>Включено ли усреднение</summary>
    public bool Enabled { get; set; } = true;
    
    /// <summary>Режим усреднения: фикс или мартингейл</summary>
    public AveragingMode Mode { get; set; } = AveragingMode.Fixed;
    
    /// <summary>Базовый объём одной добавки (лоты)</summary>
    public int BaseLotSize { get; set; } = 1;
    
    /// <summary>Ограничение на количество усреднений (0 = без лимита)</summary>
    public int MaxAveragingCount { get; set; } = 0;
    
    /// <summary>Включён ли стоп-лосс на всю позицию</summary>
    public bool StopLossEnabled { get; set; } = false;
    
    /// <summary>Стоп-лосс в пунктах от средней цены входа</summary>
    public double StopLossPoints { get; set; } = 0;
    
    /// <summary>Стоп-лосс в процентах от средней цены (альтернатива пунктам)</summary>
    public double StopLossPercent { get; set; } = 0;
    
    /// <summary>Использовать проценты вместо пунктов для стоп-лосса</summary>
    public bool UsePercentStopLoss { get; set; } = false;
    
    /// <summary>Максимальная позиция в рублях для усреднения (0 = нет лимита). При мартингейле + MaxAveragingCount=0 используется как лимит.</summary>
    public double MaxPositionRub { get; set; } = 0;

    /// <summary>Минимальный PnL для закрытия (0 = любой положительный)</summary>
    public double MinProfitToClose { get; set; } = 0;

    /// <summary>Рассчитать объём следующей добавки</summary>
    public int GetNextLotSize(int currentAveragingCount)
    {
        return Mode switch
        {
            AveragingMode.Fixed => BaseLotSize,
            AveragingMode.Martingale => BaseLotSize * (int)Math.Pow(2, currentAveragingCount),
            _ => BaseLotSize
        };
    }
    
    /// <summary>Можно ли ещё усредняться?</summary>
    public bool CanAverage(int currentAveragingCount)
    {
        if (!Enabled) return false;
        if (MaxAveragingCount <= 0) return true; // без лимита
        return currentAveragingCount < MaxAveragingCount;
    }
}

