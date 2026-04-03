namespace HedgeFund.Core;

/// <summary>
/// Работа с тикерами фьючерсов MOEX FORTS.
/// 
/// Формат тикера: {PREFIX}{MONTH_CODE}{YEAR_DIGIT}
/// Месяцы: H=март, M=июнь, U=сентябрь, Z=декабрь
/// Пример: GZM6 = GAZR июнь 2026
/// 
/// Маппинг акция → префикс фьючерса:
/// SBER→SR, GAZP→GZ, ROSN→RN, TATN→TT, ALRS→AL
/// </summary>
public static class FuturesContractHelper
{
    private static readonly Dictionary<string, string> SpotToFutPrefix = new()
    {
        ["SBER"] = "SR",
        ["GAZP"] = "GZ",
        ["ROSN"] = "RN",
        ["TATN"] = "TT",
        ["ALRS"] = "AL",
        ["LKOH"] = "LK",
        ["VTBR"] = "VB",
        ["GMKN"] = "GK",
        ["NVTK"] = "NK",
        ["YDEX"] = "YD",
        ["MGNT"] = "MN",
    };

    private static readonly Dictionary<char, int> MonthCodeToMonth = new()
    {
        ['H'] = 3,  // Март
        ['M'] = 6,  // Июнь
        ['U'] = 9,  // Сентябрь
        ['Z'] = 12, // Декабрь
    };

    private static readonly char[] MonthCodes = { 'H', 'M', 'U', 'Z' };

    /// <summary>Получить префикс фьючерса по тикеру акции</summary>
    public static string? GetFuturesPrefix(string spotTicker) =>
        SpotToFutPrefix.TryGetValue(spotTicker.ToUpper(), out var prefix) ? prefix : null;

    /// <summary>Сгенерировать тикер фьючерса</summary>
    public static string MakeFuturesTicker(string prefix, int month, int year)
    {
        char monthCode = MonthCodes.FirstOrDefault(c => MonthCodeToMonth[c] == month);
        if (monthCode == default) throw new ArgumentException($"Invalid month: {month}");
        int yearDigit = year % 10;
        return $"{prefix}{monthCode}{yearDigit}";
    }

    /// <summary>Получить дату экспирации фьючерса (~15 число месяца)</summary>
    public static DateTime GetExpiry(string futuresTicker)
    {
        if (futuresTicker.Length < 3)
            throw new ArgumentException($"Invalid futures ticker: {futuresTicker}");

        char monthCode = futuresTicker[^2];
        int yearDigit = futuresTicker[^1] - '0';

        if (!MonthCodeToMonth.TryGetValue(monthCode, out int month))
            throw new ArgumentException($"Invalid month code: {monthCode}");

        // Предполагаем 2020-е
        int year = 2020 + yearDigit;
        // Экспирация ~15 числа (третий четверг в реальности)
        return new DateTime(year, month, 15);
    }

    /// <summary>Дней до экспирации</summary>
    public static int DaysToExpiry(string futuresTicker, DateTime now) =>
        Math.Max(0, (GetExpiry(futuresTicker) - now).Days);

    /// <summary>
    /// Определить ближайший контракт для торговли.
    /// Если до экспирации меньше rollDays — берём следующий.
    /// </summary>
    public static string GetNearestContract(string spotTicker, DateTime now, int rollDays = 20)
    {
        var prefix = GetFuturesPrefix(spotTicker);
        if (prefix == null) throw new ArgumentException($"Unknown spot ticker: {spotTicker}");

        // Генерируем все контракты на ближайшие 2 года
        var candidates = new List<(string ticker, DateTime expiry, int dte)>();

        for (int year = now.Year; year <= now.Year + 1; year++)
        {
            foreach (int month in new[] { 3, 6, 9, 12 })
            {
                var ticker = MakeFuturesTicker(prefix, month, year);
                var expiry = GetExpiry(ticker);
                int dte = (expiry - now).Days;

                if (dte > rollDays) // Только если до экспирации > rollDays
                    candidates.Add((ticker, expiry, dte));
            }
        }

        candidates.Sort((a, b) => a.dte.CompareTo(b.dte));
        return candidates.FirstOrDefault().ticker ?? throw new InvalidOperationException("No contracts found");
    }

    /// <summary>Получить следующий контракт после текущего</summary>
    public static string GetNextContract(string currentFuturesTicker)
    {
        var expiry = GetExpiry(currentFuturesTicker);
        string prefix = currentFuturesTicker[..^2];

        // Следующий квартал
        int nextMonth = expiry.Month + 3;
        int nextYear = expiry.Year;
        if (nextMonth > 12) { nextMonth -= 12; nextYear++; }

        return MakeFuturesTicker(prefix, nextMonth, nextYear);
    }

    /// <summary>Человекочитаемое имя контракта</summary>
    public static string GetDisplayName(string futuresTicker)
    {
        try
        {
            var expiry = GetExpiry(futuresTicker);
            string prefix = futuresTicker[..^2];
            var spotName = SpotToFutPrefix.FirstOrDefault(kv => kv.Value == prefix).Key ?? prefix;
            return $"{spotName} {expiry:MMM yyyy}";
        }
        catch
        {
            return futuresTicker;
        }
    }
}
