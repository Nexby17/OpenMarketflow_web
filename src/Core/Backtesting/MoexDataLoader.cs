using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using HedgeFund.Core.Models;

namespace HedgeFund.Core.Backtesting;

/// <summary>
/// Загрузчик исторических свечей с MOEX ISS API.
/// Поддержка фьючерсов (FORTS) и акций (TQBR).
/// </summary>
public static class MoexDataLoader
{
    private static readonly HttpClient _http = new()
    {
        Timeout = TimeSpan.FromSeconds(30)
    };

    // Тикеры акций (TQBR)
    private static readonly HashSet<string> StockTickers = new(StringComparer.OrdinalIgnoreCase)
    {
        "SBER", "GAZP", "LKOH", "ROSN", "GMKN", "NVTK", "SNGS", "MTLR",
        "VTBR", "PLZL", "YNDX", "OZON", "MGNT", "CHMF", "NLMK", "ALRS",
        "MOEX", "TATN", "PHOR", "RUAL", "AFLT", "HYDR", "IRAO", "MAGN",
        "POLY", "PIKK", "RTKM", "TRNFP", "FEES", "MTSS"
    };

    /// <summary>
    /// Загрузить свечи с MOEX ISS.
    /// interval: 1=1мин, 5=5мин, 10=10мин, 60=час, 24=день
    /// </summary>
    public static async Task<List<Candle>> LoadCandlesAsync(
        string ticker, int interval, DateTime from, DateTime to,
        IProgress<string>? progress = null, CancellationToken ct = default)
    {
        var (engine, market, board) = GetMarketParams(ticker);
        var candles = new List<Candle>();
        int offset = 0;
        const int pageSize = 500;

        string fromStr = from.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        string toStr = to.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);

        while (true)
        {
            ct.ThrowIfCancellationRequested();

            string url = $"https://iss.moex.com/iss/engines/{engine}/markets/{market}/boards/{board}" +
                         $"/securities/{ticker}/candles.json" +
                         $"?interval={interval}&from={fromStr}&till={toStr}&start={offset}";

            progress?.Report($"Загрузка: {ticker}, offset={offset}...");

            string json;
            try
            {
                json = await _http.GetStringAsync(url, ct);
            }
            catch (Exception ex)
            {
                progress?.Report($"Ошибка загрузки: {ex.Message}");
                break;
            }

            var parsed = ParseCandles(json);
            if (parsed.Count == 0) break;

            candles.AddRange(parsed);
            offset += pageSize;

            // Небольшая пауза чтобы не перегружать API
            await Task.Delay(100, ct);
        }

        progress?.Report($"Загружено {candles.Count} свечей для {ticker}");
        return candles.OrderBy(c => c.Timestamp).ToList();
    }

    private static (string engine, string market, string board) GetMarketParams(string ticker)
    {
        if (StockTickers.Contains(ticker))
            return ("stock", "shares", "TQBR");

        // Фьючерсы FORTS
        return ("futures", "forts", "RFUD");
    }

    private static List<Candle> ParseCandles(string json)
    {
        var result = new List<Candle>();

        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            if (!root.TryGetProperty("candles", out var candlesSection))
                return result;

            if (!candlesSection.TryGetProperty("columns", out var columns) ||
                !candlesSection.TryGetProperty("data", out var data))
                return result;

            // Находим индексы колонок
            var colNames = new List<string>();
            foreach (var col in columns.EnumerateArray())
                colNames.Add(col.GetString() ?? "");

            int iOpen = colNames.IndexOf("open");
            int iClose = colNames.IndexOf("close");
            int iHigh = colNames.IndexOf("high");
            int iLow = colNames.IndexOf("low");
            int iVolume = colNames.IndexOf("value"); // "value" — объём в рублях
            int iVolumeContracts = colNames.IndexOf("volume"); // "volume" — объём в контрактах
            int iBegin = colNames.IndexOf("begin");

            if (iOpen < 0 || iClose < 0 || iHigh < 0 || iLow < 0 || iBegin < 0)
                return result;

            foreach (var row in data.EnumerateArray())
            {
                try
                {
                    var candle = new Candle
                    {
                        Open = row[iOpen].GetDouble(),
                        Close = row[iClose].GetDouble(),
                        High = row[iHigh].GetDouble(),
                        Low = row[iLow].GetDouble(),
                        Volume = iVolumeContracts >= 0 
                            ? (long)row[iVolumeContracts].GetDouble()
                            : (iVolume >= 0 ? (long)row[iVolume].GetDouble() : 0)
                    };

                    var beginStr = row[iBegin].GetString();
                    if (beginStr != null && DateTime.TryParse(beginStr, CultureInfo.InvariantCulture,
                            DateTimeStyles.None, out var ts))
                    {
                        candle.Timestamp = ts;
                    }

                    result.Add(candle);
                }
                catch
                {
                    // Пропускаем битые строки
                }
            }
        }
        catch
        {
            // JSON parse error
        }

        return result;
    }
}
