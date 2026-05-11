using System.Text.Json;
using System.Text.Json.Serialization;

namespace HedgeFund.Brokers.Finam;

// === Assets ===

public class ClockResponse
{
    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = string.Empty;
}

public class ExchangesResponse
{
    [JsonPropertyName("exchanges")]
    public List<Exchange> Exchanges { get; set; } = new();
}

public class Exchange
{
    [JsonPropertyName("mic")]
    public string Mic { get; set; } = string.Empty;
    
    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;
}

public class AssetsResponse
{
    [JsonPropertyName("assets")]
    public List<Asset> Assets { get; set; } = new();
}

public class AllAssetsResponse
{
    [JsonPropertyName("assets")]
    public List<Asset> Assets { get; set; } = new();
    
    [JsonPropertyName("next_cursor")]
    public long NextCursor { get; set; }
}

public class Asset
{
    [JsonPropertyName("symbol")]
    public string Symbol { get; set; } = string.Empty;
    
    [JsonPropertyName("id")]
    public string Id { get; set; } = string.Empty;
    
    [JsonPropertyName("ticker")]
    public string Ticker { get; set; } = string.Empty;
    
    [JsonPropertyName("mic")]
    public string Mic { get; set; } = string.Empty;
    
    [JsonPropertyName("isin")]
    public string Isin { get; set; } = string.Empty;
    
    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;
    
    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;
    
    [JsonPropertyName("is_archived")]
    public bool IsArchived { get; set; }
}

public class AssetDetailResponse
{
    [JsonPropertyName("board")]
    public string Board { get; set; } = string.Empty;
    
    [JsonPropertyName("id")]
    public string Id { get; set; } = string.Empty;
    
    [JsonPropertyName("ticker")]
    public string Ticker { get; set; } = string.Empty;
    
    [JsonPropertyName("mic")]
    public string Mic { get; set; } = string.Empty;
    
    [JsonPropertyName("isin")]
    public string Isin { get; set; } = string.Empty;
    
    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;
    
    [JsonPropertyName("name")]
    public string Name { get; set; } = string.Empty;
    
    [JsonPropertyName("decimals")]
    public int Decimals { get; set; }
    
    [JsonPropertyName("min_step")]
    public long MinStep { get; set; }
    
    [JsonPropertyName("lot_size")]
    public DecimalValue? LotSize { get; set; }
    
    [JsonPropertyName("expiration_date")]
    public DateValue? ExpirationDate { get; set; }
    
    [JsonPropertyName("quote_currency")]
    public string QuoteCurrency { get; set; } = string.Empty;
}

public class ScheduleResponse
{
    [JsonPropertyName("symbol")]
    public string Symbol { get; set; } = string.Empty;
    
    [JsonPropertyName("sessions")]
    public List<TradingSession> Sessions { get; set; } = new();
}

public class TradingSession
{
    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;
    
    [JsonPropertyName("interval")]
    public TimeInterval? Interval { get; set; }
}

public class TimeInterval
{
    [JsonPropertyName("start_time")]
    public string StartTime { get; set; } = string.Empty;
    
    [JsonPropertyName("end_time")]
    public string EndTime { get; set; } = string.Empty;
}

// === Accounts ===

public class AccountsResponse
{
    [JsonPropertyName("accounts")]
    public List<Account> Accounts { get; set; } = new();
}

public class Account
{
    [JsonPropertyName("account_id")]
    public string AccountId { get; set; } = string.Empty;
    
    [JsonPropertyName("type")]
    public string Type { get; set; } = string.Empty;
    
    [JsonPropertyName("status")]
    public string Status { get; set; } = string.Empty;
}

public class AccountResponse
{
    [JsonPropertyName("account_id")]
    public string AccountId { get; set; } = string.Empty;
    
    [JsonPropertyName("positions")]
    public List<PositionRow> Positions { get; set; } = new();
    
    [JsonPropertyName("money")]
    public List<MoneyRow> Money { get; set; } = new();
}

public class PositionRow
{
    [JsonPropertyName("symbol")]
    public string Symbol { get; set; } = string.Empty;
    
    [JsonPropertyName("balance")]
    public long Balance { get; set; }
    
    [JsonPropertyName("quantity")]
    public object? QuantityRaw { get; set; }
    
    /// <summary>Effective quantity: Balance (old API) or quantity.value (new API)</summary>
    public long EffectiveQuantity
    {
        get
        {
            if (Balance != 0) return Balance;
            if (QuantityRaw is JsonElement je && je.ValueKind == JsonValueKind.Object && je.TryGetProperty("value", out var v))
                return long.TryParse(v.GetString(), out var l) ? l : 0;
            return 0;
        }
    }
    
    [JsonPropertyName("current_price")]
    public object? CurrentPriceRaw { get; set; }
    
    [JsonIgnore]
    public double? CurrentPrice => CurrentPriceRaw switch {
        JsonElement je => je.ValueKind == JsonValueKind.Object && je.TryGetProperty("value", out var v) ? double.TryParse(v.GetString(), out var d) ? d : (double?)null : je.ValueKind == JsonValueKind.Number ? je.GetDouble() : (double?)null,
        double d => d,
        _ => null
    };
    
    [JsonPropertyName("average_price")]
    public object? AveragePriceRaw { get; set; }
    
    [JsonIgnore]
    public double? AveragePrice => AveragePriceRaw switch {
        JsonElement je => je.ValueKind == JsonValueKind.Object && je.TryGetProperty("value", out var v) ? double.TryParse(v.GetString(), out var d) ? d : (double?)null : je.ValueKind == JsonValueKind.Number ? je.GetDouble() : (double?)null,
        double d => d,
        _ => null
    };
    
    [JsonPropertyName("unrealized_profit")]
    public double UnrealizedProfit { get; set; }
    
    [JsonPropertyName("max_buy")]
    public long MaxBuy { get; set; }
    
    [JsonPropertyName("max_sell")]
    public long MaxSell { get; set; }
}

public class MoneyRow
{
    [JsonPropertyName("currency")]
    public string Currency { get; set; } = string.Empty;
    
    [JsonPropertyName("balance")]
    public double Balance { get; set; }
}

// === MarketData ===

public class BarsResponse
{
    [JsonPropertyName("bars")]
    public List<Bar> Bars { get; set; } = new();
}

public class Bar
{
    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = string.Empty;
    
    [JsonPropertyName("open")]
    public DecimalValue? Open { get; set; }
    
    [JsonPropertyName("close")]
    public DecimalValue? Close { get; set; }
    
    [JsonPropertyName("high")]
    public DecimalValue? High { get; set; }
    
    [JsonPropertyName("low")]
    public DecimalValue? Low { get; set; }
    
    [JsonPropertyName("volume")]
    public DecimalValue? Volume { get; set; }
}

// === Orders ===

public class OrdersResponse
{
    [JsonPropertyName("orders")]
    public List<FinamOrder> Orders { get; set; } = new();
}

public class FinamOrder
{
    [JsonPropertyName("order_id")]
    public string OrderId { get; set; } = string.Empty;
    
    [JsonPropertyName("symbol")]
    public string Symbol { get; set; } = string.Empty;
    
    [JsonPropertyName("side")]
    public string Side { get; set; } = string.Empty;
    
    [JsonPropertyName("status")]
    public string Status { get; set; } = string.Empty;
    
    [JsonPropertyName("quantity")]
    public int Quantity { get; set; }
    
    [JsonPropertyName("filled_quantity")]
    public int FilledQuantity { get; set; }
    
    [JsonPropertyName("price")]
    public double Price { get; set; }
    
    [JsonPropertyName("created_at")]
    public string CreatedAt { get; set; } = string.Empty;
    
    [JsonPropertyName("order")]
    public FinamOrderDetails? Details { get; set; }
}

public class FinamOrderDetails
{
    [JsonPropertyName("comment")]
    public string Comment { get; set; } = string.Empty;
    
    [JsonPropertyName("limit_price")]
    public DecimalValue? LimitPrice { get; set; }
}

public class PlaceOrderRequest
{
    [JsonPropertyName("symbol")]
    public string Symbol { get; set; } = string.Empty;
    
    [JsonPropertyName("quantity")]
    public DecimalValue Quantity { get; set; } = new() { Value = "1" };
    
    [JsonPropertyName("side")]
    public string Side { get; set; } = string.Empty;  // SIDE_BUY / SIDE_SELL
    
    [JsonPropertyName("type")]
    public string OrderType { get; set; } = string.Empty;  // ORDER_TYPE_MARKET / ORDER_TYPE_LIMIT
    
    [JsonPropertyName("limit_price")]
    public DecimalValue? Price { get; set; }
    
    [JsonPropertyName("time_in_force")]
    public string? TimeInForce { get; set; }
    
    [JsonPropertyName("comment")]
    public string? Comment { get; set; }
}

public class PlaceOrderResponse
{
    [JsonPropertyName("order_id")]
    public string OrderId { get; set; } = string.Empty;
}

public class CancelOrderResponse
{
    [JsonPropertyName("order_id")]
    public string OrderId { get; set; } = string.Empty;
}

// === Auth ===

public class AuthRequest
{
    [JsonPropertyName("secret")]
    public string Secret { get; set; } = string.Empty;
}

public class AuthResponse
{
    [JsonPropertyName("token")]
    public string Token { get; set; } = string.Empty;
}

public class TokenDetailsRequest
{
    [JsonPropertyName("token")]
    public string Token { get; set; } = string.Empty;
}

public class TokenDetailsResponse
{
    [JsonPropertyName("account_ids")]
    public List<string> AccountIds { get; set; } = new();
    
    [JsonPropertyName("readonly")]
    public bool Readonly { get; set; }
    
    [JsonPropertyName("exchanges")]
    public List<string> Exchanges { get; set; } = new();
}

// === Shared types ===

public class DecimalValue
{
    [JsonPropertyName("value")]
    public string Value { get; set; } = string.Empty;
    
    public double ToDouble() => double.TryParse(Value, out var v) ? v : 0;
}

public class DateValue
{
    [JsonPropertyName("year")]
    public int Year { get; set; }
    
    [JsonPropertyName("month")]
    public int Month { get; set; }
    
    [JsonPropertyName("day")]
    public int Day { get; set; }
}

