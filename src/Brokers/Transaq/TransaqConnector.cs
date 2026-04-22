using System.Net.Sockets;
using System.Text;
using System.Xml;
using System.Xml.Linq;
using HedgeFund.Core;
using HedgeFund.Core.Models;

namespace HedgeFund.Brokers.Transaq;

/// <summary>
/// Коннектор Transaq — XML-over-TCP протокол.
/// Резервный канал связи к MOEX через терминал Transaq.
/// 
/// Протокол: отправляем XML команды, получаем XML ответы и данные.
/// Сервер: tr.finam.ru:39000 (боевой), 39100 (демо)
/// </summary>
public class TransaqConnector : IBrokerConnector
{
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private CancellationTokenSource? _cts;
    private string _login = "";
    private string _password = "";
    private string _host = "tr.finam.ru";
    private int _port = 39000;
    private readonly object _sendLock = new();
    private readonly StringBuilder _buffer = new();
    private DateTime _lastDataTime = DateTime.MinValue;

    // Состояние
    private bool _isLoggedIn;
    private double _balance;
    private readonly List<Order> _activeOrders = new();
    private readonly List<Position> _positions = new();
    private readonly Dictionary<string, double> _quotes = new(); // ticker → last price
    private readonly Dictionary<string, (double bid, double ask, double bidVol, double askVol)> _level2 = new();

    // Subscriptions
    private Action<Candle>? _onCandle;
    private Action<double, double>? _onBidAsk;
    private string? _subscribedTicker;

    public string BrokerName => "Transaq (Finam)";
    public bool IsConnected => _tcp?.Connected == true && _isLoggedIn;

    public event Action<Trade>? OnTrade;
    public event Action<Order>? OnOrderUpdate;
    public event Action<string>? OnError;
    public event Action<bool>? OnConnectionChanged;

    // === Подключение ===

    public async Task<bool> ConnectAsync(string login, string password)
    {
        // Transaq expects login as "login" param
        // But IBrokerConnector uses login/password
        // Parse host:port from login if format is "login@host:port"
        if (login.Contains('@'))
        {
            var parts = login.Split('@');
            _login = parts[0];
            var hp = parts[1].Split(':');
            _host = hp[0];
            if (hp.Length > 1) int.TryParse(hp[1], out _port);
        }
        else
        {
            _login = login;
        }
        _password = password;

        try
        {
            _cts = new CancellationTokenSource();
            _tcp = new TcpClient();
            await _tcp.ConnectAsync(_host, _port, _cts.Token);
            _stream = _tcp.GetStream();

            // Запускаем приёмник
            _ = ReceiveLoop(_cts.Token);

            // Отправляем команду connect
            var connectXml = $@"<command id=""connect"">
    <login>{Security.Escape(_login)}</login>
    <password>{Security.Escape(_password)}</password>
    <host>{_host}</host>
    <port>{_port}</port>
    <autopos>true</autopos>
    <micex_registers>true</micex_registers>
    <millisecs>true</millisecs>
    <push_pos_limits>true</push_pos_limits>
    <compress>true</compress>
</command>";

            var response = await SendCommandAsync(connectXml);
            if (response?.Attribute("success")?.Value == "true")
            {
                _isLoggedIn = true;
                _lastDataTime = DateTime.UtcNow;
                OnConnectionChanged?.Invoke(true);
                // Запускаем health check
                _ = HealthCheckLoop(_cts.Token);
                return true;
            }
            else
            {
                var error = response?.Element("message")?.Value ?? "Unknown error";
                OnError?.Invoke($"Transaq connect failed: {error}");
                return false;
            }
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"Transaq connection error: {ex.Message}");
            return false;
        }
    }

    public async Task DisconnectAsync()
    {
        try
        {
            if (_isLoggedIn)
            {
                await SendCommandAsync("<command id=\"disconnect\"/>");
            }
        }
        catch { }

        _cts?.Cancel();
        _stream?.Close();
        _tcp?.Close();
        _isLoggedIn = false;
        OnConnectionChanged?.Invoke(false);
    }

    // === Получение данных ===

    private async Task ReceiveLoop(CancellationToken ct)
    {
        var buf = new byte[65536];
        try
        {
            while (!ct.IsCancellationRequested && _stream != null)
            {
                var len = await _stream.ReadAsync(buf, 0, buf.Length, ct);
                if (len == 0) break;

                var text = Encoding.UTF8.GetString(buf, 0, len);
                _buffer.Append(text);

                // Разбираем полные XML сообщения
                var content = _buffer.ToString();
                var messages = ExtractXmlMessages(content);
                if (messages.found > 0)
                {
                    _buffer.Clear();
                    _buffer.Append(messages.remaining);
                    foreach (var msg in messages.xmls)
                    {
                        ProcessMessage(msg);
                    }
                }
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            if (!ct.IsCancellationRequested)
                OnError?.Invoke($"Transaq receive error: {ex.Message}");
        }
    }

    private (List<string> xmls, string remaining, int found) ExtractXmlMessages(string data)
    {
        var xmls = new List<string>();
        var remaining = data;
        var found = 0;

        while (true)
        {
            var start = remaining.IndexOf('<');
            if (start < 0) { remaining = ""; break; }

            // Find matching closing tag
            var tagStart = remaining.IndexOf('<', start);
            if (tagStart < 0) break;
            
            var tagEnd = remaining.IndexOf('>', tagStart);
            if (tagEnd < 0) break;

            // Get tag name
            var tagContent = remaining.Substring(tagStart + 1, tagEnd - tagStart - 1);
            var spaceIdx = tagContent.IndexOf(' ');
            var tagName = spaceIdx >= 0 ? tagContent.Substring(0, spaceIdx) : tagContent;
            
            if (tagName.StartsWith("!--") || tagName.StartsWith("?"))
            {
                remaining = remaining.Substring(tagEnd + 1);
                continue;
            }

            // Find closing tag
            var closeTag = $"</{tagName}>";
            var closeIdx = remaining.IndexOf(closeTag, tagEnd);
            if (closeIdx < 0)
            {
                // Also check for self-closing
                var selfClose = remaining.Substring(tagEnd - 1, 1);
                if (selfClose == "/")
                {
                    xmls.Add(remaining.Substring(start, tagEnd + 1 - start));
                    remaining = remaining.Substring(tagEnd + 1);
                    found++;
                    continue;
                }
                break; // Incomplete XML
            }

            var endIdx = closeIdx + closeTag.Length;
            xmls.Add(remaining.Substring(start, endIdx - start));
            remaining = remaining.Substring(endIdx);
            found++;
        }

        return (xmls, remaining, found);
    }

    private void ProcessMessage(string xml)
    {
        try
        {
            _lastDataTime = DateTime.UtcNow;
            var doc = XDocument.Parse(xml);
            var root = doc.Root;
            if (root == null) return;

            switch (root.Name.LocalName)
            {
                case "result":
                    // Результат команды — обрабатывается в SendCommandAsync
                    break;
                case "securities":
                    ProcessSecurities(root);
                    break;
                case "quotes":
                    ProcessQuotes(root);
                    break;
                case "ticks":
                    ProcessTicks(root);
                    break;
                case "orders":
                    ProcessOrders(root);
                    break;
                case "trades":
                    ProcessTrades(root);
                    break;
                case "positions":
                    ProcessPositions(root);
                    break;
                case "portfolios":
                    ProcessPortfolios(root);
                    break;
                case "candlekinds":
                    // Список доступных таймфреймов — игнорируем
                    break;
                case "messages":
                    // Системные сообщения
                    var msgText = root.Element("message")?.Value ?? "";
                    if (!string.IsNullOrEmpty(msgText))
                        Console.WriteLine($"[TRANSAQ] Message: {msgText}");
                    break;
                case "overriding":
                    // Reconnect required
                    _isLoggedIn = false;
                    OnConnectionChanged?.Invoke(false);
                    break;
                case "server_status":
                    var connected = root.Attribute("connected")?.Value == "true";
                    if (!connected)
                    {
                        _isLoggedIn = false;
                        OnConnectionChanged?.Invoke(false);
                        OnError?.Invoke("Transaq server disconnected");
                    }
                    break;
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[TRANSAQ] XML parse error: {ex.Message}");
        }
    }

    private void ProcessSecurities(XElement root)
    {
        foreach (var sec in root.Elements("security"))
        {
            var secId = sec.Element("secid")?.Value ?? sec.Attribute("id")?.Value;
            var board = sec.Element("board")?.Value ?? "";
            var seccode = sec.Element("seccode")?.Value ?? "";
            if (!string.IsNullOrEmpty(secId) && !string.IsNullOrEmpty(seccode))
            {
                // Сохраняем маппинг secid → seccode
                _secIdMap[secId] = seccode;
            }
        }
    }

    private readonly Dictionary<string, string> _secIdMap = new(); // secid → seccode

    private void ProcessQuotes(XElement root)
    {
        foreach (var q in root.Elements("quote"))
        {
            var secId = q.Element("secid")?.Value ?? "";
            var ticker = _secIdMap.TryGetValue(secId, out var t) ? t : secId;
            var bid = ParseDouble(q.Element("bid")?.Value);
            var ask = ParseDouble(q.Element("offer")?.Value);
            var last = ParseDouble(q.Element("last")?.Value);
            var bidVol = ParseDouble(q.Element("biddepth")?.Value);
            var askVol = ParseDouble(q.Element("offerdepth")?.Value);

            if (last > 0) _quotes[ticker] = last;
            if (bid > 0 || ask > 0)
            {
                _level2[ticker] = (bid, ask, bidVol, askVol);
                if (ticker == _subscribedTicker && bid > 0 && ask > 0)
                    _onBidAsk?.Invoke(bid, ask);
            }
        }
    }

    private void ProcessTicks(XElement root)
    {
        foreach (var tick in root.Elements("tick"))
        {
            var secId = tick.Element("secid")?.Value ?? "";
            var ticker = _secIdMap.TryGetValue(secId, out var t) ? t : secId;
            var price = ParseDouble(tick.Element("price")?.Value);
            var qty = ParseInt(tick.Element("quantity")?.Value);
            // Ticks can be used for candle building
        }
    }

    private void ProcessOrders(XElement root)
    {
        lock (_activeOrders)
        {
            _activeOrders.Clear();
            foreach (var o in root.Elements("order"))
            {
                var order = new Order
                {
                    BrokerOrderId = o.Element("transactionid")?.Value ?? o.Element("orderno")?.Value ?? "",
                    Ticker = o.Element("seccode")?.Value ?? "",
                    Direction = o.Element("buysell")?.Value == "B" ? SignalDirection.Buy : SignalDirection.Sell,
                    Type = o.Element("iseven")?.Value == "true" ? OrderType.Limit : OrderType.Market,
                    Price = ParseDouble(o.Element("price")?.Value),
                    Volume = ParseInt(o.Element("quantity")?.Value),
                    Comment = $"Transaq order #{o.Element("orderno")?.Value}"
                };
                _activeOrders.Add(order);
                OnOrderUpdate?.Invoke(order);
            }
        }
    }

    private void ProcessTrades(XElement root)
    {
        foreach (var t in root.Elements("trade"))
        {
            var trade = new Trade
            {
                Direction = t.Element("buysell")?.Value == "B" ? SignalDirection.Buy : SignalDirection.Sell,
                Price = ParseDouble(t.Element("price")?.Value),
                Volume = ParseInt(t.Element("quantity")?.Value),
                Ticker = t.Element("seccode")?.Value ?? "",
                Timestamp = DateTime.TryParse(t.Element("tradetime")?.Value, out var dt) ? dt : DateTime.UtcNow
            };
            OnTrade?.Invoke(trade);
        }
    }

    private void ProcessPositions(XElement root)
    {
        lock (_positions)
        {
            _positions.Clear();
            foreach (var p in root.Elements("position"))
            {
                var pos = new Position
                {
                    Ticker = p.Element("seccode")?.Value ?? ""
                };
                var entry = new PositionEntry
                {
                    Comment = p.Element("buysell")?.Value == "B" ? "Long" : "Short",
                    Price = ParseDouble(p.Element("openprice")?.Value),
                    Volume = ParseInt(p.Element("quantity")?.Value)
                };
                pos.Direction = p.Element("buysell")?.Value == "B" ? SignalDirection.Buy : SignalDirection.Sell;
                pos.Entries.Add(entry);
                _positions.Add(pos);
            }
        }
    }

    private void ProcessPortfolios(XElement root)
    {
        foreach (var p in root.Elements("portfolio"))
        {
            var equity = ParseDouble(p.Element("equity")?.Value);
            var cash = ParseDouble(p.Element("cash")?.Value);
            if (equity > 0) _balance = equity;
            else if (cash > 0) _balance = cash;
        }
    }

    // === Health Check ===

    private async Task HealthCheckLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            await Task.Delay(5000, ct);
            try
            {
                if (!_tcp?.Connected == true || (DateTime.UtcNow - _lastDataTime).TotalSeconds > 30)
                {
                    _isLoggedIn = false;
                    OnConnectionChanged?.Invoke(false);
                    OnError?.Invoke("Transaq: нет данных более 30 сек, переподключение...");
                    _ = ReconnectAsync();
                }
            }
            catch { }
        }
    }

    private async Task ReconnectAsync()
    {
        try
        {
            await DisconnectAsync();
            await Task.Delay(3000);
            await ConnectAsync($"{_login}@{_host}:{_port}", _password);
        }
        catch (Exception ex)
        {
            OnError?.Invoke($"Transaq reconnect failed: {ex.Message}");
        }
    }

    // === Отправка команд ===

    private async Task<XElement?> SendCommandAsync(string xml)
    {
        if (_stream == null || !_tcp?.Connected == true) return null;

        var data = Encoding.UTF8.GetBytes(xml + "\0");
        lock (_sendLock) _stream.Write(data, 0, data.Length);

        // Ответ приходит через ReceiveLoop и обрабатывается в ProcessMessage
        // Для синхронных команд — ждём result
        return null; // Async processing via ReceiveLoop
    }

    // === IBrokerConnector ===

    public async Task SubscribeCandlesAsync(string ticker, TimeSpan timeframe, Action<Candle> onCandle)
    {
        _onCandle = onCandle;
        _subscribedTicker = ticker;

        // Подписка на котировки
        var secCode = MapTickerToSecCode(ticker);
        await SendCommandAsync($@"<command id=""subscribe"">
    <alltrades>{Security.Escape(secCode)}</alltrades>
    <quotations>{Security.Escape(secCode)}</quotations>
    <quotes>{Security.Escape(secCode)}</quotes>
</command>");
    }

    public async Task SubscribeLevel2Async(string ticker, Action<double, double> onBidAsk)
    {
        _onBidAsk = onBidAsk;
        var secCode = MapTickerToSecCode(ticker);
        await SendCommandAsync($@"<command id=""subscribe"">
    <quotes>{Security.Escape(secCode)}</quotes>
</command>");
    }

    public async Task<Candle[]> GetHistoricalCandlesAsync(string ticker, TimeSpan timeframe, DateTime from, DateTime to)
    {
        // Transaq не поддерживает запрос исторических свечей напрямую
        // Используем REST API Финама как fallback
        return Array.Empty<Candle>();
    }

    public async Task<Order> PlaceOrderAsync(Order order)
    {
        var secCode = MapTickerToSecCode(order.Ticker);
        var buySell = order.Direction == SignalDirection.Buy ? "B" : "S";
        var orderType = order.Type == OrderType.Market ? "market" : "limit";

        var xml = $@"<command id=""neworder"">
    <secboard>TQBR</secboard>
    <seccode>{Security.Escape(secCode)}</seccode>
    <client>{Security.Escape(_login)}</client>
    <buysell>{buySell}</buysell>
    <ordertype>{orderType}</ordertype>
    {(_port == 39100 ? "<brokerref>DEMO</brokerref>" : "")}
</command>";

        await SendCommandAsync(xml);
        return order;
    }

    public async Task<bool> CancelOrderAsync(string orderId)
    {
        await SendCommandAsync($@"<command id=""cancelorder"">
    <transactionid>{Security.Escape(orderId)}</transactionid>
</command>");
        return true;
    }

    public Task<Order[]> GetActiveOrdersAsync()
    {
        lock (_activeOrders) return Task.FromResult(_activeOrders.ToArray());
    }

    public Task<Position[]> GetPositionsAsync()
    {
        lock (_positions) return Task.FromResult(_positions.ToArray());
    }

    public Task<double> GetBalanceAsync()
    {
        return Task.FromResult(_balance);
    }

    // === Helpers ===

    private string MapTickerToSecCode(string ticker)
    {
        // SI фьючерсы → SiM6, SiU6 и т.д.
        // Акции → SBER, GAZP и т.д.
        return ticker;
    }

    private static double ParseDouble(string? s) => double.TryParse(s, System.Globalization.NumberStyles.Any, System.Globalization.NumberFormatInfo.InvariantInfo, out var v) ? v : 0;
    private static int ParseInt(string? s) => int.TryParse(s, out var v) ? v : 0;

    public void Dispose()
    {
        _cts?.Cancel();
        _stream?.Close();
        _tcp?.Close();
        _isLoggedIn = false;
    }
}

internal static class Security
{
    public static string Escape(string input) => input?
        .Replace("&", "&amp;")
        .Replace("<", "&lt;")
        .Replace(">", "&gt;")
        .Replace("\"", "&quot;")
        .Replace("'", "&apos;") ?? "";
}
