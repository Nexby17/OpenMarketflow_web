using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using ADClientSDK;
using AD.Common.DataStructures;
using AD.Common.Helpers;
using Newtonsoft.Json;

namespace HedgeFund.AlfaBridge
{
    class Program
    {
        static AdClient _client;
        static HttpListener _listener;
        static bool _connected = false;
        static string _account = "";
        static int _idSubAccount = 0;
        static int _idRazdel = 0;
        static readonly ConcurrentQueue<string> _logQueue = new ConcurrentQueue<string>();
        static readonly ConcurrentQueue<object> _orderEvents = new ConcurrentQueue<object>();

        const int HTTP_PORT = 15200;

        static void Main(string[] args)
        {
            Console.Title = "AlfaBridge — OpenMarketflow";
            
            // --diag режим: полная диагностика SDK
            if (args.Length > 0 && args[0] == "--diag")
            {
                string dLogin = "", dPassword = "";
                var credsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), "alfa_creds.txt");
                if (File.Exists(credsPath))
                {
                    var lines = File.ReadAllLines(credsPath);
                    if (lines.Length >= 2) { dLogin = lines[0].Trim(); dPassword = lines[1].Trim(); }
                }
                if (args.Length >= 3) { dLogin = args[1]; dPassword = args[2]; }
                Diagnostic.Run(dLogin, dPassword);
                return;
            }
            
            Log("═══════════════════════════════════════");
            Log("  AlfaBridge v1.0 — Альфа-Директ ↔ OpenMarketflow");
            Log($"  HTTP: http://localhost:{HTTP_PORT}/");
            Log("═══════════════════════════════════════");

            _client = new AdClient();

            _client.OnConnectionChanged += (frontEnd, status) =>
            {
                Log($"[CONN] {frontEnd}: {status}");
            };

            _client.ConnectionError += (error) =>
            {
                Log($"[ERROR] {error}");
            };

            // Логин/пароль из аргументов или файла alfa_creds.txt
            string login = "";
            string password = "";
            
            if (args.Length >= 2)
            {
                login = args[0];
                password = args[1];
            }
            else
            {
                // Читаем из alfa_creds.txt рядом с exe
                var credsPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "alfa_creds.txt");
                if (!File.Exists(credsPath))
                    credsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), "alfa_creds.txt");
                
                if (File.Exists(credsPath))
                {
                    var lines = File.ReadAllLines(credsPath);
                    if (lines.Length >= 2)
                    {
                        login = lines[0].Trim();
                        password = lines[1].Trim();
                        Log($"   Логин из {credsPath}: {login}");
                    }
                }
                else
                {
                    Log("");
                    Log("!!! Создайте файл alfa_creds.txt на рабочем столе:");
                    Log("    Строка 1: логин");
                    Log("    Строка 2: пароль");
                    Log($"    Путь: {Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), "alfa_creds.txt")}");
                    Log("");
                    Log("    Или запустите: AlfaBridge.exe логин пароль");
                    Log("");
                    Log("Пробую подключиться без креденшлов (через терминал)...");
                }
            }
            
            Log("🔌 Подключение к Альфа-Директ...");
            Log($"   Connect('{login}', '***') — подключаемся...");
            try
            {
                var connectThread = new Thread(() =>
                {
                    try { _client.Connect(login, password); }
                    catch (Exception ex) { Log($"[CONNECT ERROR] {ex.Message}"); }
                });
                connectThread.IsBackground = true;
                connectThread.Start();
                
                if (!connectThread.Join(TimeSpan.FromSeconds(10)))
                {
                    Log("⚠️ Connect завис (10 сек). Терминал запущен и авторизован?");
                    Log("⚠️ Продолжаю без подключения... HTTP сервер всё равно запустится.");
                }
                else
                {
                    Log("✅ Connect() завершился");
                }
            }
            catch (Exception ex)
            {
                Log($"[CONNECT EXCEPTION] {ex.Message}");
            }
            
            Thread.Sleep(2000);
            Log("Проверяю статус подключения...");

            // Проверяем подключение
            try
            {
                var status = _client.GetConnectionStatus(FrontEndType.AuthAndOperInitServer);
                _connected = (status == ConnectionStatus.Connected || status == ConnectionStatus.Authorized);
                Log($"[STATUS] Auth: {status}");

                var rtStatus = _client.GetConnectionStatus(FrontEndType.RealTimeBirzInfoServer);
                Log($"[STATUS] RealTime: {rtStatus}");
            }
            catch (Exception ex)
            {
                Log($"[STATUS ERROR] {ex.Message}");
            }

            if (_connected)
            {
                Log("✅ Подключён к терминалу!");
                InitAccount();
                InitOrderCallback();
            }
            else
            {
                Log("⚠️ Не удалось определить статус. Пробуем работать...");
                // Попробуем всё равно — может подключение придёт позже
                try { InitAccount(); } catch { }
                try { InitOrderCallback(); } catch { }
            }

            StartHttpServer();
        }

        static void InitAccount()
        {
            try
            {
                var razdelEntities = _client.Dictionaries.GetSubAccountRazdels();
                if (razdelEntities != null)
                {
                    foreach (var r in razdelEntities)
                    {
                        if (!string.IsNullOrEmpty(r.CodeSubAccount))
                        {
                            _account = r.CodeSubAccount;
                            _idSubAccount = r.IdSubAccount;
                            _idRazdel = r.IdRazdel;
                            Log($"📋 Счёт: {_account} (sub={_idSubAccount}, razdel={_idRazdel})");
                            break;
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                Log($"[ACCOUNT ERROR] {ex.Message}");
            }
        }

        static void InitOrderCallback()
        {
            try
            {
                _client.Trading.OnOrderChanged += (order) =>
                {
                    if (order == null) return;
                    var ev = new
                    {
                        numEDocument = order.NumEDocument,
                        status = order.IdOrderStatus.ToString(),
                        direction = order.BuySell.ToString(),
                        idObject = order.IdObject,
                        quantity = order.Quantity,
                        price = order.LimitPrice,
                        comment = order.Comment ?? ""
                    };
                    _orderEvents.Enqueue(ev);
                    Log($"[ORDER] #{order.NumEDocument} {order.BuySell} {order.Quantity}x obj={order.IdObject} @ {order.LimitPrice} → {order.IdOrderStatus}");
                };

                _client.Portfolio.OnBalanceChanged += (balances) =>
                {
                    if (balances == null) return;
                    foreach (var b in balances)
                    {
                        Log($"[BALANCE] sub={b.IdSubAccount}: bal={b.Balance:F2} money={b.Money:F2} portfolio={b.PortfolioValue:F2}");
                    }
                };
            }
            catch (Exception ex)
            {
                Log($"[CALLBACK ERROR] {ex.Message}");
            }
        }

        static void StartHttpServer()
        {
            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{HTTP_PORT}/");
            _listener.Prefixes.Add($"http://127.0.0.1:{HTTP_PORT}/");
            _listener.Start();
            Log($"📡 HTTP сервер запущен на порту {HTTP_PORT}");
            Log("Готов к работе. Endpoints: GET /status, /instruments, /fininfo, /positions, /orders");
            Log("Торговля: POST /order/market, /order/limit, /order/cancel");

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
                        ConnectionStatus authSt = ConnectionStatus.Disconnected;
                        ConnectionStatus rtSt = ConnectionStatus.Disconnected;
                        try { authSt = _client.GetConnectionStatus(FrontEndType.AuthAndOperInitServer); } catch { }
                        try { rtSt = _client.GetConnectionStatus(FrontEndType.RealTimeBirzInfoServer); } catch { }
                        result = new
                        {
                            connected = _connected,
                            authStatus = authSt.ToString(),
                            realTimeStatus = rtSt.ToString(),
                            account = _account,
                            idSubAccount = _idSubAccount,
                            version = AdClient.Version?.ToString() ?? "",
                            terminalVersion = AdClient.TerminalVersion?.ToString() ?? "",
                        };
                        break;

                    case "/instruments":
                        var name = GetParam(ctx, "name");
                        if (!string.IsNullOrEmpty(name))
                        {
                            var found = _client.Dictionaries.SearchInstruments(name, ObjectGroup.None);
                            if (found != null)
                            {
                                result = found.Select(f =>
                                {
                                    var obj = _client.Dictionaries.GetObjectByIdFi(f.IdFi);
                                    return new
                                    {
                                        idFi = f.IdFi,
                                        idObject = f.IdObject,
                                        idMarketBoard = f.IdMarketBoard.ToString(),
                                        symbol = obj?.SymbolObject ?? "",
                                        name = obj?.NameObject ?? "",
                                        desc = obj?.DescObject ?? "",
                                    };
                                }).ToArray();
                            }
                        }
                        else
                        {
                            result = new { error = "param 'name' required. Example: /instruments?name=SBER" };
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
                                    idFi = fi.IdFI,
                                    last = fi.Last,
                                    bid = fi.Bid,
                                    ask = fi.Ask,
                                    high = fi.High,
                                    low = fi.Low,
                                    open = fi.Open,
                                    volume = fi.VolToday,
                                    numTrades = fi.NumTrades,
                                };
                            }
                            else
                            {
                                result = new { error = "FinInfo not available for idFi=" + idFi };
                            }
                        }
                        else
                        {
                            result = new { error = "param 'idFi' required" };
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
                                // GetQueue returns IQueue; cast to OrderBookEntity for Lines
                                var ob = q as OrderBookEntity;
                                if (ob?.Lines != null)
                                {
                                    result = new
                                    {
                                        idFi = qIdFi,
                                        rows = ob.Lines.Where(l => l != null).Select(r => new
                                        {
                                            price = r.Price,
                                            buy = r.BuyQty,
                                            sell = r.SellQty
                                        }).ToArray()
                                    };
                                }
                                else
                                {
                                    result = new { idFi = qIdFi, info = "Queue available but no Lines (type: " + q.GetType().Name + ")" };
                                }
                            }
                        }
                        break;

                    case "/positions":
                        var pos = _client.Portfolio.GetPositions();
                        if (pos != null)
                        {
                            result = pos.Select(p =>
                            {
                                var obj = _client.Dictionaries.GetObjectByIdFi(p.IdFiBalance);
                                return new
                                {
                                    idObject = p.IdObject,
                                    idSubAccount = p.IdSubAccount,
                                    idRazdel = p.IdRazdel,
                                    symbol = obj?.SymbolObject ?? "",
                                    backPos = p.BackPos,
                                    buyQty = p.BuyQty,
                                    sellQty = p.SellQty,
                                    trdPL = p.TrdPL,
                                    variationMargin = p.VariationMargin,
                                };
                            }).ToArray();
                        }
                        break;

                    case "/balance":
                        // Получаем баланс через OnBalanceChanged или напрямую
                        result = new
                        {
                            account = _account,
                            idSubAccount = _idSubAccount,
                        };
                        break;

                    case "/orders":
                        var orders = _client.Trading.GetOrders();
                        if (orders != null)
                        {
                            result = orders.Select(o =>
                            {
                                var obj = _client.Dictionaries.GetObjectByIdFi(o.IdObject);
                                return new
                                {
                                    numEDocument = o.NumEDocument,
                                    idObject = o.IdObject,
                                    symbol = obj?.SymbolObject ?? "",
                                    direction = o.BuySell.ToString(),
                                    quantity = o.Quantity,
                                    rest = o.Rest,
                                    price = o.LimitPrice,
                                    filledPrice = o.FilledPrice,
                                    status = o.IdOrderStatus.ToString(),
                                    comment = o.Comment ?? "",
                                    isActive = o.IsActiveStatus,
                                };
                            }).ToArray();
                        }
                        break;

                    case "/order/market":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JsonConvert.DeserializeObject<OrderRequest>(body);
                            string errorMsg = "";
                            var dir = req.Direction?.ToLower() == "buy"
                                ? OrderDirection.Buy
                                : OrderDirection.Sell;

                            _client.Trading.CreateMarketOrder(
                                req.Account ?? _account,
                                req.IdFi,
                                dir,
                                req.Quantity,
                                LifeTime.DAY,
                                req.Comment ?? "AlfaBridge",
                                out errorMsg);

                            var ok = string.IsNullOrEmpty(errorMsg);
                            result = new { success = ok, error = errorMsg };
                            Log($"[MARKET] {dir} {req.Quantity}x idFi={req.IdFi} → {(ok ? "OK" : errorMsg)}");
                        }
                        break;

                    case "/order/limit":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JsonConvert.DeserializeObject<OrderRequest>(body);
                            string errorMsg = "";
                            var dir = req.Direction?.ToLower() == "buy"
                                ? OrderDirection.Buy
                                : OrderDirection.Sell;

                            _client.Trading.CreateLimitOrder(
                                req.Account ?? _account,
                                req.IdFi,
                                dir,
                                req.Quantity,
                                req.Price,
                                LifeTime.DAY,
                                req.Comment ?? "AlfaBridge",
                                out errorMsg);

                            var ok = string.IsNullOrEmpty(errorMsg);
                            result = new { success = ok, error = errorMsg };
                            Log($"[LIMIT] {dir} {req.Quantity}x idFi={req.IdFi} @ {req.Price} → {(ok ? "OK" : errorMsg)}");
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
                        var events = new List<object>();
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

                    default:
                        result = new
                        {
                            service = "AlfaBridge v1.0",
                            endpoints = new[]
                            {
                                "GET  /status",
                                "GET  /instruments?name=SBER",
                                "GET  /fininfo?idFi=12345",
                                "GET  /queue?idFi=12345",
                                "GET  /positions",
                                "GET  /orders",
                                "POST /order/market  {idFi, direction, quantity, comment}",
                                "POST /order/limit   {idFi, direction, quantity, price, comment}",
                                "POST /order/cancel  {numEDocument}",
                                "GET  /events",
                                "GET  /log",
                            }
                        };
                        break;
                }
            }
            catch (Exception ex)
            {
                result = new { error = ex.Message, type = ex.GetType().Name };
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

        static string ReadPassword()
        {
            var sb = new StringBuilder();
            while (true)
            {
                var key = Console.ReadKey(true);
                if (key.Key == ConsoleKey.Enter) break;
                if (key.Key == ConsoleKey.Backspace && sb.Length > 0)
                {
                    sb.Remove(sb.Length - 1, 1);
                    Console.Write("\b \b");
                }
                else if (key.KeyChar != 0)
                {
                    sb.Append(key.KeyChar);
                    Console.Write("*");
                }
            }
            return sb.ToString();
        }
    }

    class OrderRequest
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
}
