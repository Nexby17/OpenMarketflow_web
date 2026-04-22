using System.Text.Json;
using HedgeFund.Core.Models;

namespace HedgeFund.Server.Services;

/// <summary>
/// Provider для свечей из QUIK через HTTP endpoints
/// Заменяет FinamConnector для получения данных
/// </summary>
public class QuikCandleProvider
{
    private readonly HttpClient _http = new();
    private readonly string _baseUrl = "http://localhost:5050";

    public async Task<List<Candle>> GetHistoricalCandlesAsync()
    {
        try
        {
            var response = await _http.GetStringAsync($"{_baseUrl}/quik/candles");
            var candles = JsonSerializer.Deserialize<List<QuikCandleDto>>(response);
            return candles?.Select(ToCandle).Where(c => c != null).Cast<Candle>().ToList() ?? new List<Candle>();
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[QUIK] Error fetching candles: {ex.Message}");
            return new List<Candle>();
        }
    }

    public async Task<double?> GetCurrentPriceAsync()
    {
        try
        {
            var response = await _http.GetStringAsync($"{_baseUrl}/quik/price");
            var doc = JsonDocument.Parse(response);
            return doc.RootElement.TryGetProperty("price", out var price) ? price.GetDouble() : null;
        }
        catch
        {
            return null;
        }
    }

    private static Candle? ToCandle(QuikCandleDto dto)
    {
        if (!DateTimeOffset.TryParse(dto.Timestamp, out var ts))
            return null;
        return new Candle
        {
            Timestamp = ts.DateTime,
            Open = dto.Open,
            High = dto.High,
            Low = dto.Low,
            Close = dto.Close,
            Volume = (long)dto.Volume
        };
    }

    private record QuikCandleDto
    {
        public string Timestamp { get; init; } = "";
        public double Open { get; init; }
        public double High { get; init; }
        public double Low { get; init; }
        public double Close { get; init; }
        public double Volume { get; init; }
    }
}
