using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using ADClientSDK;
using Newtonsoft.Json;

namespace HedgeFund.AlfaBridge
{
    /// <summary>
    /// Мост между ADClientSDK (.NET Framework x86) и OpenMarketflow Server (.NET 8 x64).
    /// Запускается на ПК с терминалом Альфа-Директ.
    /// Слушает HTTP порт 15200 и транслирует команды в SDK.
    /// 
    /// Архитектура:
    ///   Браузер → OpenMarketflow Server → HTTP → AlfaBridge → ADClientSDK → Альфа-Директ
    /// </summary>
    class Program
    {
        static AdClient _client;
        static HttpListener _listener;
        static bool _connected = false;
        static string _account = "";
        static readonly ConcurrentDictionary<int, FinInfoData> _finInfoCache = new ConcurrentDictionary<int, FinInfoData>();
        static readonly ConcurrentQueue<string> _logQueue = new ConcurrentQueue<string>();
        static readonly ConcurrentQueue<OrderEvent> _orderEvents = new ConcurrentQueue<OrderEvent>();

        const int HTTP_PORT = 15200;

        static void Main(string[] args)
        {
            Console.Title = "AlfaBridge — OpenMarketflow";
            Log("═══════════════════════════════════════");
            Log("  AlfaBridge v1.0 — Альфа-Директ ↔ OpenMarketflow");
            Log($"  HTTP: http://localhost:{HTTP_PORT}/");
            Log("═══════════════════════════════════════");

            _client = new AdClient();

            // События подключения
            _client.OnConnectionChanged += (frontEnd, status) =>
            {
                Log($"[CONN] {frontEnd}: {status}");
                _connected = status.ToString().Contains("Connected");
            };

            _client.ConnectionError += (error) =>
            {
                Log($"[ERROR] {error}");
            };

            // Подключение к терминалу (пустые логин/пароль = через запущенный терминал)
            Log("🔌 Подключение к Альфа-Директ...");
            _client.Connect("", "");
            Thread.Sleep(2000);

            if (_client.GetConnectionStatus(FrontEndType.Terminal).ToString().Contains("Connected"))
            {
                _connected = true;
                Log("✅ Подключён к терминалу!");

                // Получаем счёт
                var positions = _client.Portfolio.GetPositions();
                if (positions != null && positions.Length > 0)
                {
                    _account = positions[0].CodeSubAccount ?? "";
                    Log($"📋 Счёт: {_account}");
                }

                // Подписка на изменения ордеров
                _client.Trading.OnOrderChanged += (orders) =>
                {
                    foreach (var o in orders)
                    {
                        var ev = new OrderEvent
                        {
                            NumEDocument = o.NumEDocument,
                            Status = o.OrderStatus?.ToString() ?? "",
                            Direction = o.Direction.ToString(),
                            IdFi = o.IdFi,
                            Quantity = o.Quantity,
                            Price = o.Price,
                            Comment = o.Comment ?? ""
                        };
                        _orderEvents.Enqueue(ev);
                        Log($"[ORDER] #{o.NumEDocument} {o.Direction} {o.Quantity}x idFi={o.IdFi} @ {o.Price} → {o.OrderStatus}");
                    }
                };

                // Подписка на баланс
                _client.Portfolio.OnBalanceChanged += (balances) =>
                {
                    foreach (var b in balances)
                    {
                        Log($"[BALANCE] {b.CodeSubAccount}: {b.Balance}");
                    }
                };
            }
            else
            {
                Log("⚠️ Не удалось подключиться. Терминал запущен?");
            }

            // Запускаем HTTP сервер
            StartHttpServer();
        }

        static void StartHttpServer()
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{HTTP_PORT}/");
            _listener.Prefixes.Add($"http://127.0.0.1:{HTTP_PORT}/");
            _listener.Start();
            Log($"📡 HTTP сервер запущен на порту {HTTP_PORT}");

            while (true)
            {
                try
                {
                    var ctx = _listener.GetContext();
                    ThreadPool.QueueUserWorkItem(_ => HandleRequest(ctx));
                }
                catch (Exception ex)
                {
                    Log($"[HTTP ERROR] {ex.Message}");
                }
            }
        }

        static void HandleRequest(HttpListenerContext ctx)
        {
            var path = ctx.Request.Url.AbsolutePath.ToLower().TrimEnd('/');
            var method = ctx.Request.HttpMethod;
            object result = null;

            try
            {
                switch (path)
                {
                    case "/status":
                        result = new
                        {
                            connected = _connected,
                            account = _account,
                            version = _client.Version ?? "",
                            terminalVersion = _client.TerminalVersion ?? "",
                        };
                        break;

                    case "/instruments":
                        var name = GetParam(ctx, "name");
                        if (!string.IsNullOrEmpty(name))
                        {
                            var found = _client.Dictionaries.SearchInstruments(name, ObjectGroup.All);
                            result = found?.Select(f => new
                            {
                                idFi = f.IdFi,
                                code = f.Code,
                                shortName = f.ShortName,
                                name = f.Name,
                            }).ToArray();
                        }
                        else
                        {
                            result = new { error = "param 'name' required" };
                        }
                        break;

                    case "/fininfo":
                        var idFi = GetIntParam(ctx, "idFi");
                        if (idFi > 0)
                        {
                            _client.RealTime.SubscribeFinInfo(idFi);
                            Thread.Sleep(500);
                            var fi = _client.RealTime.GetFinInfo(idFi);
                            if (fi != null)
                            {
                                result = new
                                {
                                    idFi = fi.IdFi,
                                    last = fi.LastPrice,
                                    bid = fi.Bid,
                                    ask = fi.Ask,
                                    high = fi.HighPrice,
                                    low = fi.LowPrice,
                                    open = fi.OpenPrice,
                                    close = fi.ClosePrice,
                                    volume = fi.Volume,
                                };
                            }
                        }
                        break;

                    case "/queue":
                        var qIdFi = GetIntParam(ctx, "idFi");
                        if (qIdFi > 0)
                        {
                            _client.RealTime.SubscribeQueue(qIdFi);
                            Thread.Sleep(300);
                            var q = _client.RealTime.GetQueue(qIdFi);
                            if (q != null)
                            {
                                result = new
                                {
                                    idFi = qIdFi,
                                    rows = q.Select(r => new { price = r.Price, buy = r.Buy, sell = r.Sell }).ToArray()
                                };
                            }
                        }
                        break;

                    case "/positions":
                        var pos = _client.Portfolio.GetPositions();
                        result = pos?.Select(p => new
                        {
                            account = p.CodeSubAccount,
                            idFi = p.IdFi,
                            quantity = p.Quantity,
                            avgPrice = p.AveragePrice,
                            pnl = p.PnL,
                        }).ToArray();
                        break;

                    case "/orders":
                        var orders = _client.Trading.GetOrders();
                        result = orders?.Select(o => new
                        {
                            numEDocument = o.NumEDocument,
                            idFi = o.IdFi,
                            direction = o.Direction.ToString(),
                            quantity = o.Quantity,
                            price = o.Price,
                            status = o.OrderStatus?.ToString() ?? "",
                            comment = o.Comment ?? "",
                        }).ToArray();
                        break;

                    case "/order/market":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JsonConvert.DeserializeObject<MarketOrderRequest>(body);
                            string errorMsg = "";
                            var dir = req.Direction.ToLower() == "buy" ? OrderDirection.Buy : OrderDirection.Sell;
                            
                            _client.Trading.CreateMarketOrder(
                                req.Account ?? _account,
                                req.IdFi,
                                dir,
                                req.Quantity,
                                LifeTime.Day,
                                req.Comment ?? "AlfaBridge",
                                out errorMsg);

                            result = new { success = string.IsNullOrEmpty(errorMsg), error = errorMsg };
                            Log($"[MARKET] {dir} {req.Quantity}x idFi={req.IdFi} → {(string.IsNullOrEmpty(errorMsg) ? "OK" : errorMsg)}");
                        }
                        break;

                    case "/order/limit":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JsonConvert.DeserializeObject<LimitOrderRequest>(body);
                            string errorMsg = "";
                            var dir = req.Direction.ToLower() == "buy" ? OrderDirection.Buy : OrderDirection.Sell;

                            _client.Trading.CreateLimitOrder(
                                req.Account ?? _account,
                                req.IdFi,
                                dir,
                                req.Quantity,
                                req.Price,
                                LifeTime.Day,
                                req.Comment ?? "AlfaBridge",
                                out errorMsg);

                            result = new { success = string.IsNullOrEmpty(errorMsg), error = errorMsg };
                            Log($"[LIMIT] {dir} {req.Quantity}x idFi={req.IdFi} @ {req.Price} → {(string.IsNullOrEmpty(errorMsg) ? "OK" : errorMsg)}");
                        }
                        break;

                    case "/order/cancel":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JsonConvert.DeserializeObject<CancelRequest>(body);
                            _client.Trading.CancelOrder(req.NumEDocument);
                            result = new { success = true, numEDocument = req.NumEDocument };
                            Log($"[CANCEL] #{req.NumEDocument}");
                        }
                        break;

                    case "/events":
                        // Long-poll: вернуть накопленные события ордеров
                        var events = new List<OrderEvent>();
                        while (_orderEvents.TryDequeue(out var ev))
                            events.Add(ev);
                        result = events;
                        break;

                    case "/log":
                        var logs = new List<string>();
                        while (_logQueue.TryDequeue(out var l))
                            logs.Add(l);
                        result = logs;
                        break;

                    case "/candles":
                        var cIdFi = GetIntParam(ctx, "idFi");
                        var cDays = GetIntParam(ctx, "days");
                        if (cDays == 0) cDays = 5;
                        var tfStr = GetParam(ctx, "tf") ?? "M5";
                        
                        BaseTimeFrame tf;
                        switch (tfStr.ToUpper())
                        {
                            case "M1": tf = BaseTimeFrame.M1; break;
                            case "M5": tf = BaseTimeFrame.M5; break;
                            case "M15": tf = BaseTimeFrame.M15; break;
                            case "M30": tf = BaseTimeFrame.M30; break;
                            case "H1": tf = BaseTimeFrame.H1; break;
                            default: tf = BaseTimeFrame.M5; break;
                        }

                        if (cIdFi > 0)
                        {
                            var candlesReady = new ManualResetEventSlim(false);
                            object[] candles = null;

                            Action<int, object> handler = null;
                            handler = (id, data) =>
                            {
                                // Сохраняем свечи из callback
                                candles = new object[] { data };
                                candlesReady.Set();
                            };

                            _client.Archive.OnChartArchive += handler;
                            _client.Archive.RequestChartArchive(CandleType.TimeFrame, cIdFi, DateTime.Now, tf, cDays);
                            candlesReady.Wait(5000);
                            _client.Archive.OnChartArchive -= handler;

                            result = candles ?? new object[0];
                        }
                        break;

                    default:
                        result = new
                        {
                            endpoints = new[]
                            {
                                "GET /status",
                                "GET /instruments?name=SBER",
                                "GET /fininfo?idFi=12345",
                                "GET /queue?idFi=12345",
                                "GET /positions",
                                "GET /orders",
                                "POST /order/market {idFi, direction, quantity, comment}",
                                "POST /order/limit {idFi, direction, quantity, price, comment}",
                                "POST /order/cancel {numEDocument}",
                                "GET /events",
                                "GET /candles?idFi=123&tf=M5&days=5",
                                "GET /log",
                            }
                        };
                        break;
                }
            }
            catch (Exception ex)
            {
                result = new { error = ex.Message, stack = ex.StackTrace };
                Log($"[ERROR] {path}: {ex.Message}");
            }

            var json = JsonConvert.SerializeObject(result ?? new { }, Formatting.Indented);
            var buffer = Encoding.UTF8.GetBytes(json);
            ctx.Response.ContentType = "application/json; charset=utf-8";
            ctx.Response.ContentLength64 = buffer.Length;
            ctx.Response.Headers.Add("Access-Control-Allow-Origin", "*");
            ctx.Response.OutputStream.Write(buffer, 0, buffer.Length);
            ctx.Response.Close();
        }

        static string GetParam(HttpListenerContext ctx, string name)
        {
            return ctx.Request.QueryString[name];
        }

        static int GetIntParam(HttpListenerContext ctx, string name)
        {
            int.TryParse(ctx.Request.QueryString[name], out var val);
            return val;
        }

        static string ReadBody(HttpListenerContext ctx)
        {
            using (var reader = new StreamReader(ctx.Request.InputStream, ctx.Request.ContentEncoding))
                return reader.ReadToEnd();
        }

        static void Log(string msg)
        {
            var line = $"[{DateTime.Now:HH:mm:ss}] {msg}";
            Console.WriteLine(line);
            _logQueue.Enqueue(line);
            while (_logQueue.Count > 500)
                _logQueue.TryDequeue(out _);
        }
    }

    // === DTO ===
    class MarketOrderRequest
    {
        public string Account { get; set; }
        public int IdFi { get; set; }
        public string Direction { get; set; }
        public int Quantity { get; set; }
        public string Comment { get; set; }
    }

    class LimitOrderRequest
    {
        public string Account { get; set; }
        public int IdFi { get; set; }
        public string Direction { get; set; }
        public int Quantity { get; set; }
        public double Price { get; set; }
        public string Comment { get; set; }
    }

    class CancelRequest
    {
        public long NumEDocument { get; set; }
    }

    class OrderEvent
    {
        public long NumEDocument { get; set; }
        public string Status { get; set; }
        public string Direction { get; set; }
        public int IdFi { get; set; }
        public int Quantity { get; set; }
        public double Price { get; set; }
        public string Comment { get; set; }
    }

    class FinInfoData
    {
        public int IdFi { get; set; }
        public double Last { get; set; }
        public double Bid { get; set; }
        public double Ask { get; set; }
    }
}
