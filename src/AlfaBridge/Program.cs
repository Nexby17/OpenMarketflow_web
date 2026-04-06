using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using WebSocketSharp;

namespace HedgeFund.AlfaBridge
{
    /// <summary>
    /// AlfaBridge v2 — WebSocket PRO API.
    /// Подключается к ws://127.0.0.1:3366/router/ (терминал Альфа-Директ).
    /// Слушает HTTP :15200 для OpenMarketflow Server.
    /// </summary>
    class Program
    {
        static WebSocket _ws;
        static HttpListener _http;
        static bool _connected = false;
        static bool _authorized = false;
        static int _requestId = 0;

        // Кэш данных из терминала
        static readonly ConcurrentDictionary<string, JToken> _subscriptionData = new ConcurrentDictionary<string, JToken>();
        static readonly ConcurrentDictionary<string, ManualResetEventSlim> _pendingRequests = new ConcurrentDictionary<string, ManualResetEventSlim>();
        static readonly ConcurrentDictionary<string, JToken> _responseData = new ConcurrentDictionary<string, JToken>();
        static readonly ConcurrentQueue<string> _logQueue = new ConcurrentQueue<string>();
        static readonly ConcurrentQueue<object> _orderEvents = new ConcurrentQueue<object>();

        // Данные клиента
        static int _idAccount = 0;
        static int _idSubAccount = 0;
        static int _idRazdel = 0;
        static int _idRazdelForts = 0;
        static readonly ConcurrentDictionary<string, JObject> _assets = new ConcurrentDictionary<string, JObject>();

        const int HTTP_PORT = 15200;
        const string WS_URL = "ws://127.0.0.1:3366/router/";

        static void Main(string[] args)
        {
            Console.Title = "AlfaBridge v2 — WebSocket PRO API";
            Log("═══════════════════════════════════════");
            Log("  AlfaBridge v2 — Альфа-Директ WebSocket PRO API");
            Log($"  Terminal WS: {WS_URL}");
            Log($"  HTTP Bridge: http://localhost:{HTTP_PORT}/");
            Log("═══════════════════════════════════════");

            // Подключаемся к роутеру терминала
            ConnectWebSocket();

            // Запускаем HTTP сервер
            StartHttpServer();
        }

        static void ConnectWebSocket()
        {
            Log("🔌 Подключение к терминалу...");
            _ws = new WebSocket(WS_URL);

            _ws.OnOpen += (s, e) =>
            {
                _connected = true;
                Log("✅ WebSocket подключён к роутеру терминала");
                InitSubscriptions();
            };

            _ws.OnMessage += (s, e) =>
            {
                try { HandleMessage(e.Data); }
                catch (Exception ex) { Log($"[MSG ERROR] {ex.Message}"); }
            };

            _ws.OnError += (s, e) =>
            {
                Log($"[WS ERROR] {e.Message}");
            };

            _ws.OnClose += (s, e) =>
            {
                _connected = false;
                _authorized = false;
                Log($"[WS CLOSED] {e.Code} {e.Reason}");
                // Переподключение через 3 сек
                Thread.Sleep(3000);
                Log("🔄 Переподключение...");
                try { _ws.Connect(); } catch { }
            };

            _ws.Connect();
            Thread.Sleep(2000);

            if (!_connected)
            {
                Log("⚠️ Не удалось подключиться. Терминал запущен?");
            }
        }

        static void InitSubscriptions()
        {
            // 1. Монитор состояния
            SendWs(new { Command = "listen", Channel = "#ConnectionState.Bus" });

            // 2. Подписка на справочники
            SubscribeEntity("AssetInfoEntity", init: true);
            SubscribeEntity("ClientAccountEntity", init: true);
            SubscribeEntity("ClientSubAccountEntity", init: true);
            SubscribeEntity("SubAccountRazdelEntity", init: true);
            SubscribeEntity("AllowedOrderParamEntity", init: true);

            // 3. Подписка на заявки и позиции
            SubscribeEntity("OrderEntity", init: true);
            SubscribeEntity("ClientPositionEntity", init: true);
            SubscribeEntity("ClientBalanceEntity", init: true);
            SubscribeEntity("ClientOperationEntity", init: true);

            Log("📡 Подписки отправлены");
        }

        static void SubscribeEntity(string type, bool init = false, long[] keys = null)
        {
            // Слушаем шину
            SendWs(new { Command = "listen", Channel = $"#Data.Bus.{type}" });

            // Запрос подписки
            var payload = new JObject { ["Type"] = type };
            if (init) payload["Init"] = true;
            if (keys != null) payload["Keys"] = new JArray(keys);

            var id = NextId();
            SendWs(new { Command = "request", Channel = "#Data.Query", Id = id, Payload = payload.ToString(Formatting.None) });
        }

        static void HandleMessage(string raw)
        {
            var msg = JObject.Parse(raw);
            var command = msg["Command"]?.ToString();
            var channel = msg["Channel"]?.ToString() ?? "";
            var id = msg["Id"]?.ToString();
            var payloadStr = msg["Payload"]?.ToString();

            JObject payload = null;
            if (!string.IsNullOrEmpty(payloadStr))
            {
                try { payload = JObject.Parse(payloadStr); } catch { }
            }

            // Ответ на наш запрос
            if (command == "response" && id != null && payload != null)
            {
                var type = payload["Type"]?.ToString() ?? channel;
                
                // Сохраняем данные
                var data = payload["Data"];
                if (data != null)
                {
                    _subscriptionData[type] = data;
                    ProcessEntityData(type, data);
                }

                // Разблокируем ожидающий запрос
                if (_pendingRequests.TryGetValue(id, out var evt))
                {
                    _responseData[id] = payload;
                    evt.Set();
                }
            }

            // Обновление шины
            if (command == "broadcast" && payload != null)
            {
                var type = payload["Type"]?.ToString() ?? "";
                var updated = payload["Updated"];
                var deleted = payload["Deleted"];

                if (updated != null && type != "")
                {
                    ProcessEntityData(type, updated);
                }

                // Обновления заявок
                if (type == "OrderEntity" && updated != null)
                {
                    foreach (var o in updated)
                    {
                        _orderEvents.Enqueue(new
                        {
                            numEDocument = o["NumEDocument"]?.Value<long>() ?? 0,
                            status = o["IdOrderStatus"]?.Value<int>() ?? 0,
                            direction = o["BuySell"]?.Value<int>() ?? 0,
                            idObject = o["IdObject"]?.Value<int>() ?? 0,
                            quantity = o["Quantity"]?.Value<int>() ?? 0,
                            price = o["LimitPrice"]?.Value<double>() ?? 0,
                            comment = o["Comment"]?.ToString() ?? ""
                        });
                    }
                }
            }

            // Состояние терминала
            if (channel == "#ConnectionState.Bus" && payload != null)
            {
                var authStatus = payload.SelectToken("States.User.AuthStatus")?.Value<int>() ?? 0;
                _authorized = authStatus == 2;
                var login = payload.SelectToken("States.User.Login")?.ToString() ?? "";
                var readyToSign = payload.SelectToken("States.SignService.ReadyToSign")?.Value<bool>() ?? false;
                Log($"[STATE] Login={login} Auth={authStatus} ReadyToSign={readyToSign}");
            }
        }

        static void ProcessEntityData(string type, JToken data)
        {
            if (data == null) return;

            switch (type)
            {
                case "AssetInfoEntity":
                    foreach (var item in data)
                    {
                        var ticker = item["Ticker"]?.ToString();
                        if (!string.IsNullOrEmpty(ticker))
                            _assets[ticker] = item as JObject ?? new JObject();
                    }
                    break;

                case "ClientAccountEntity":
                    foreach (var item in data)
                    {
                        _idAccount = item["IdAccount"]?.Value<int>() ?? 0;
                        Log($"📋 Account: {_idAccount}");
                    }
                    break;

                case "ClientSubAccountEntity":
                    foreach (var item in data)
                    {
                        _idSubAccount = item["IdSubAccount"]?.Value<int>() ?? 0;
                        Log($"📋 SubAccount: {_idSubAccount}");
                    }
                    break;

                case "SubAccountRazdelEntity":
                    foreach (var item in data)
                    {
                        var razdelGroup = item["IdRazdelGroup"]?.Value<int>() ?? 0;
                        var idRazdel = item["IdRazdel"]?.Value<int>() ?? 0;
                        var rcode = item["RCode"]?.ToString() ?? "";
                        Log($"📋 Razdel: {idRazdel} group={razdelGroup} rcode={rcode}");
                        if (razdelGroup == 1) _idRazdel = idRazdel;      // РЦБ
                        if (razdelGroup == 2) _idRazdelForts = idRazdel;  // ФОРТС
                    }
                    break;
            }
        }

        // === WebSocket отправка ===

        static void SendWs(object msg)
        {
            if (_ws?.IsAlive != true) return;
            var json = JsonConvert.SerializeObject(msg);
            _ws.Send(json);
        }

        static JToken SendRequest(string channel, JObject payload, int timeoutMs = 5000)
        {
            var id = NextId();
            var evt = new ManualResetEventSlim(false);
            _pendingRequests[id] = evt;

            SendWs(new { Command = "request", Channel = channel, Id = id, Payload = payload.ToString(Formatting.None) });

            if (evt.Wait(timeoutMs))
            {
                _responseData.TryRemove(id, out var result);
                _pendingRequests.TryRemove(id, out _);
                return result;
            }

            _pendingRequests.TryRemove(id, out _);
            return null;
        }

        static string NextId() => Interlocked.Increment(ref _requestId).ToString();

        // === HTTP сервер ===

        static void StartHttpServer()
        {
            _http = new HttpListener();
            _http.Prefixes.Add($"http://localhost:{HTTP_PORT}/");
            _http.Prefixes.Add($"http://127.0.0.1:{HTTP_PORT}/");
            _http.Start();
            Log($"📡 HTTP сервер: http://localhost:{HTTP_PORT}/");
            Log("Готов. Endpoints: /status, /instruments, /fininfo, /positions, /orders, /order/market...");

            while (true)
            {
                try
                {
                    var ctx = _http.GetContext();
                    ThreadPool.QueueUserWorkItem(_ => HandleHttp(ctx));
                }
                catch (Exception ex) { Log($"[HTTP] {ex.Message}"); }
            }
        }

        static void HandleHttp(HttpListenerContext ctx)
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
                            authorized = _authorized,
                            account = _idAccount,
                            subAccount = _idSubAccount,
                            razdelRcb = _idRazdel,
                            razdelForts = _idRazdelForts,
                            assetsLoaded = _assets.Count,
                        };
                        break;

                    case "/instruments":
                        var name = GetParam(ctx, "name")?.ToUpper();
                        if (!string.IsNullOrEmpty(name))
                        {
                            var found = _assets.Where(kv => 
                                kv.Key.Contains(name) || 
                                (kv.Value["Name"]?.ToString() ?? "").ToUpper().Contains(name))
                                .Take(20)
                                .Select(kv => new
                                {
                                    ticker = kv.Key,
                                    idObject = kv.Value["IdObject"]?.Value<int>() ?? 0,
                                    name = kv.Value["Name"]?.ToString() ?? "",
                                    idObjectGroup = kv.Value["IdObjectGroup"]?.Value<int>() ?? 0,
                                    instruments = kv.Value["Instruments"]?.Select(i => new
                                    {
                                        idFi = i["IdFi"]?.Value<int>() ?? 0,
                                        idMarketBoard = i["IdMarketBoard"]?.Value<int>() ?? 0,
                                        rcode = i["RCode"]?.ToString() ?? "",
                                        isLiquid = i["IsLiquid"]?.Value<bool>() ?? false,
                                    }).ToArray()
                                }).ToArray();
                            result = found;
                        }
                        else
                        {
                            result = new { error = "param 'name' required", example = "/instruments?name=SBER" };
                        }
                        break;

                    case "/fininfo":
                        var idFi = GetIntParam(ctx, "idFi");
                        if (idFi > 0)
                        {
                            // Подписываемся на FinInfoLastEntity + FinInfoOrderBookEntity
                            SubscribeEntity("FinInfoLastEntity", init: true, keys: new[] { (long)idFi });
                            SubscribeEntity("FinInfoOrderBookEntity", init: true, keys: new[] { (long)idFi });
                            Thread.Sleep(1500);

                            var last = GetCachedData("FinInfoLastEntity", idFi);
                            var book = GetCachedData("FinInfoOrderBookEntity", idFi);

                            result = new
                            {
                                idFi,
                                last = last?["Last"]?.Value<double>() ?? 0,
                                open = last?["Open"]?.Value<double>() ?? 0,
                                high = last?["High"]?.Value<double>() ?? 0,
                                low = last?["Low"]?.Value<double>() ?? 0,
                                volume = last?["VolToday"]?.Value<long>() ?? 0,
                                bid = book?["Bid"]?.Value<double>() ?? 0,
                                ask = book?["Ask"]?.Value<double>() ?? 0,
                                bidQty = book?["BidQty"]?.Value<int>() ?? 0,
                                askQty = book?["AskQty"]?.Value<int>() ?? 0,
                            };
                        }
                        break;

                    case "/positions":
                        if (_subscriptionData.TryGetValue("ClientPositionEntity", out var posData))
                        {
                            result = posData;
                        }
                        else
                        {
                            result = new object[0];
                        }
                        break;

                    case "/balance":
                        if (_subscriptionData.TryGetValue("ClientBalanceEntity", out var balData))
                        {
                            result = balData;
                        }
                        else
                        {
                            result = new { account = _idAccount };
                        }
                        break;

                    case "/orders":
                        if (_subscriptionData.TryGetValue("OrderEntity", out var ordData))
                        {
                            result = ordData;
                        }
                        else
                        {
                            result = new object[0];
                        }
                        break;

                    case "/order/market":
                    case "/order/limit":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JObject.Parse(body);

                            var ticker = req["ticker"]?.ToString();
                            var direction = req["direction"]?.ToString()?.ToLower();
                            var quantity = req["quantity"]?.Value<int>() ?? 0;
                            var price = req["price"]?.Value<double>() ?? 0;
                            var comment = req["comment"]?.ToString() ?? "AlfaBridge";

                            // Найти инструмент
                            JObject asset = null;
                            if (!string.IsNullOrEmpty(ticker) && _assets.TryGetValue(ticker, out asset)) { }

                            if (asset == null)
                            {
                                result = new { error = $"Instrument '{ticker}' not found" };
                                break;
                            }

                            var idObject = asset["IdObject"]?.Value<int>() ?? 0;
                            var instr = asset["Instruments"]?.FirstOrDefault(i => i["IsLiquid"]?.Value<bool>() == true)
                                       ?? asset["Instruments"]?.FirstOrDefault();
                            var idMarketBoard = instr?["IdMarketBoard"]?.Value<int>() ?? 0;
                            var idObjectGroup = asset["IdObjectGroup"]?.Value<int>() ?? 0;
                            var rcode = instr?["RCode"]?.ToString() ?? "";

                            // Выбираем razdel по rcode
                            var razdel = rcode == "FORTS" ? _idRazdelForts : _idRazdel;

                            // Ищем AllowedOrderParams
                            var isMarket = path.Contains("market");
                            var orderType = isMarket ? 1 : 2;
                            var idAllowed = FindAllowedOrderParams(idObjectGroup, idMarketBoard, orderType);

                            // Собираем заявку
                            var orderPayload = new JObject
                            {
                                ["IdAccount"] = _idAccount,
                                ["IdSubAccount"] = _idSubAccount,
                                ["IdRazdel"] = razdel,
                                ["IdPriceControlType"] = 3,
                                ["IdObject"] = idObject,
                                ["BuySell"] = direction == "buy" ? 1 : -1,
                                ["Quantity"] = quantity,
                                ["Comment"] = comment,
                                ["IdAllowedOrderParams"] = idAllowed,
                            };

                            if (!isMarket && price > 0)
                                orderPayload["LimitPrice"] = price;

                            var resp = SendRequest("#Order.Enter.Query", orderPayload, 10000);
                            var respStatus = resp?["ResponseStatus"]?.Value<int>() ?? -1;
                            var errMsg = resp?.SelectToken("Error.Message")?.ToString()
                                        ?? resp?.SelectToken("Value.ErrorText")?.ToString() ?? "";

                            result = new
                            {
                                success = respStatus == 0,
                                responseStatus = respStatus,
                                numEDocument = resp?.SelectToken("Value.NumEDocument")?.Value<long>() ?? 0,
                                error = errMsg,
                                ticker,
                                direction,
                                quantity,
                                price,
                                idAllowed,
                            };

                            var ok = respStatus == 0;
                            Log($"[{(isMarket ? "MARKET" : "LIMIT")}] {direction} {quantity}x {ticker} (obj={idObject}) @ {price} → {(ok ? "OK" : errMsg)}");
                        }
                        break;

                    case "/order/cancel":
                        if (method == "POST")
                        {
                            var body = ReadBody(ctx);
                            var req = JObject.Parse(body);
                            var numDoc = req["numEDocument"]?.Value<long>() ?? 0;

                            var cancelPayload = new JObject
                            {
                                ["IdAccount"] = _idAccount,
                                ["IdSubAccount"] = _idSubAccount,
                                ["IdRazdel"] = _idRazdel,
                                ["NumEDocumentBase"] = numDoc,
                            };

                            var resp = SendRequest("#Order.Cancel.Query", cancelPayload, 10000);
                            result = new
                            {
                                success = resp?["ResponseStatus"]?.Value<int>() == 0,
                                numEDocument = numDoc,
                            };
                            Log($"[CANCEL] #{numDoc}");
                        }
                        break;

                    case "/events":
                        var events = new List<object>();
                        while (_orderEvents.TryDequeue(out var ev)) events.Add(ev);
                        result = events;
                        break;

                    case "/candles":
                        var cIdFi = GetIntParam(ctx, "idFi");
                        var cDays = GetIntParam(ctx, "days");
                        if (cDays == 0) cDays = 5;
                        var interval = GetParam(ctx, "interval") ?? "minute";
                        var period = GetIntParam(ctx, "period");
                        if (period == 0) period = 5;

                        if (cIdFi > 0)
                        {
                            var archPayload = new JObject
                            {
                                ["IdFi"] = cIdFi,
                                ["CandleType"] = 0,
                                ["Interval"] = interval,
                                ["Period"] = period,
                                ["FirstDay"] = DateTime.UtcNow.AddDays(-cDays).ToString("yyyy-MM-ddT00:00:00"),
                                ["LastDay"] = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss+03:00"),
                                ["AtLeastOneCandle"] = false,
                                ["TakeLastNCandles"] = 0,
                            };

                            var resp = SendRequest("#Archive.Query", archPayload, 15000);
                            result = resp;
                        }
                        break;

                    case "/log":
                        var logs = new List<string>();
                        while (_logQueue.TryDequeue(out var l)) logs.Add(l);
                        result = logs;
                        break;

                    case "/ui":
                    case "":
                    case "/index.html":
                        // Встроенный UI
                        var html = GetEmbeddedHtml();
                        var htmlBuf = Encoding.UTF8.GetBytes(html);
                        ctx.Response.ContentType = "text/html; charset=utf-8";
                        ctx.Response.ContentLength64 = htmlBuf.Length;
                        ctx.Response.OutputStream.Write(htmlBuf, 0, htmlBuf.Length);
                        ctx.Response.Close();
                        return; // не идём в Respond()

                    default:
                        result = new
                        {
                            service = "AlfaBridge v2",
                            ui = "http://localhost:15200/ui",
                        };
                        break;
                }
            }
            catch (Exception ex)
            {
                result = new { error = ex.Message, type = ex.GetType().Name };
                Log($"[ERROR] {path}: {ex.Message}");
            }

            Respond(ctx, result);
        }

        static int FindAllowedOrderParams(int objectGroup, int marketBoard, int orderType)
        {
            if (_subscriptionData.TryGetValue("AllowedOrderParamEntity", out var data))
            {
                foreach (var item in data)
                {
                    if (item["IdObjectGroup"]?.Value<int>() == objectGroup &&
                        item["IdMarketBoard"]?.Value<int>() == marketBoard &&
                        item["IdOrderType"]?.Value<int>() == orderType &&
                        item["IdDocumentType"]?.Value<int>() == 1)
                    {
                        return item["IdAllowedOrderParams"]?.Value<int>() ?? 0;
                    }
                }
            }
            return 0;
        }

        static JToken GetCachedData(string type, int idFi)
        {
            if (_subscriptionData.TryGetValue(type, out var data))
            {
                return data.FirstOrDefault(d => d["IdFi"]?.Value<int>() == idFi);
            }
            return null;
        }

        static string GetParam(HttpListenerContext ctx, string name) => ctx.Request.QueryString[name];
        static int GetIntParam(HttpListenerContext ctx, string name)
        {
            int.TryParse(ctx.Request.QueryString[name], out var v);
            return v;
        }

        static string ReadBody(HttpListenerContext ctx)
        {
            using (var r = new StreamReader(ctx.Request.InputStream, ctx.Request.ContentEncoding))
                return r.ReadToEnd();
        }

        static void Respond(HttpListenerContext ctx, object result)
        {
            var json = JsonConvert.SerializeObject(result ?? new { }, Formatting.Indented);
            var buf = Encoding.UTF8.GetBytes(json);
            ctx.Response.ContentType = "application/json; charset=utf-8";
            ctx.Response.ContentLength64 = buf.Length;
            ctx.Response.Headers.Add("Access-Control-Allow-Origin", "*");
            ctx.Response.OutputStream.Write(buf, 0, buf.Length);
            ctx.Response.Close();
        }

        static void Log(string msg)
        {
            var line = $"[{DateTime.Now:HH:mm:ss}] {msg}";
            Console.WriteLine(line);
            _logQueue.Enqueue(line);
            while (_logQueue.Count > 500) _logQueue.TryDequeue(out _);
        }

        static string GetEmbeddedHtml() => @"<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>AlfaBridge</title>
<style>
body{background:#1a1a2e;color:#e0e0e0;font-family:system-ui;margin:0;padding:16px}
.card{background:#16213e;border-radius:8px;padding:12px;margin:8px 0}
.card h3{margin:0 0 8px;color:#00d4ff}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:4px 8px;text-align:left;border-bottom:1px solid #2d2d44}
th{color:#9ca3af}
.green{color:#22c55e}.red{color:#ef4444}.accent{color:#00d4ff}
input,button{background:#0f3460;color:#e0e0e0;border:1px solid #2d2d44;padding:6px 12px;border-radius:4px}
button{cursor:pointer}button:hover{background:#1a4a80}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.metric{background:#0f3460;padding:8px 16px;border-radius:6px;text-align:center}
.metric .val{font-size:18px;font-weight:bold}
.metric .lbl{font-size:11px;color:#9ca3af}
#log{background:#0a0a1a;padding:8px;font-family:monospace;font-size:11px;max-height:200px;overflow-y:auto;border-radius:4px}
</style></head><body>
<h2>⚡ AlfaBridge — Альфа-Директ 5</h2>

<div class='row' style='margin:12px 0'>
<div class='metric'><div class='lbl'>Статус</div><div class='val' id='st'>—</div></div>
<div class='metric'><div class='lbl'>Счёт</div><div class='val' id='acc'>—</div></div>
<div class='metric'><div class='lbl'>Инструментов</div><div class='val' id='assets'>—</div></div>
<div class='metric'><div class='lbl'>Подпись</div><div class='val' id='sign'>—</div></div>
</div>

<div class='card'><h3>🔍 Поиск инструментов</h3>
<div class='row'><input id='sq' placeholder='SBER, GAZP, ROSN...' onkeyup='if(event.key==""Enter"")search()'><button onclick='search()'>Найти</button></div>
<table><thead><tr><th>Тикер</th><th>Название</th><th>Группа</th><th>IdFi</th><th>Рынок</th><th></th></tr></thead><tbody id='sr'></tbody></table>
</div>

<div class='card'><h3>📊 Котировки</h3>
<table><thead><tr><th>Тикер</th><th>Last</th><th>Bid</th><th>Ask</th><th>High</th><th>Low</th><th>Volume</th></tr></thead><tbody id='qt'></tbody></table>
</div>

<div class='card'><h3>💼 Позиции</h3><div id='pos'><i>Загрузка...</i></div></div>
<div class='card'><h3>📝 Заявки</h3><div id='ord'><i>Загрузка...</i></div></div>
<div class='card'><h3>Лог</h3><div id='log'></div></div>

<script>
const B='';
const G={1:'Акции',2:'Облиг.',3:'Фонды',4:'Фьючерсы',5:'Опционы'};
let quotes={};

async function load(){
 try{
  const r=await(await fetch(B+'/status')).json();
  document.getElementById('st').innerHTML=r.authorized?'<span class=green>✅ ОК</span>':'<span class=red>❌</span>';
  document.getElementById('acc').textContent=r.account||'—';
  document.getElementById('assets').textContent=r.assetsLoaded||0;
  document.getElementById('sign').textContent=r.authorized?'Готова':'Нет';
  loadPos();loadOrd();refreshQ();
 }catch(e){document.getElementById('st').innerHTML='<span class=red>❌ '+e.message+'</span>';}
}

async function search(){
 const q=document.getElementById('sq').value;
 if(!q)return;
 const r=await(await fetch(B+'/instruments?name='+q)).json();
 if(!Array.isArray(r)){document.getElementById('sr').innerHTML='<tr><td colspan=6>'+JSON.stringify(r)+'</td></tr>';return;}
 document.getElementById('sr').innerHTML=r.map(a=>{
  const i=(a.instruments||[]).find(x=>x.isLiquid)||(a.instruments||[])[0]||{};
  return '<tr><td><b>'+a.ticker+'</b></td><td>'+a.name+'</td><td>'+(G[a.idObjectGroup]||a.idObjectGroup)+'</td><td>'+
   (i.idFi||'')+'</td><td>'+(i.rcode||'')+'</td><td><button onclick="\'getQ('+i.idFi+',\''+a.ticker+'\')\'">📊</button></td></tr>';
 }).join('');
}

async function getQ(idFi,t){
 const r=await(await fetch(B+'/fininfo?idFi='+idFi)).json();
 quotes[t]={...r,t,idFi};
 renderQ();
}
function renderQ(){
 document.getElementById('qt').innerHTML=Object.values(quotes).map(q=>
  '<tr><td><b>'+q.t+'</b></td><td class=accent>'+(q.last||'—')+'</td><td class=green>'+(q.bid||'—')+
  '</td><td class=red>'+(q.ask||'—')+'</td><td>'+(q.high||'—')+'</td><td>'+(q.low||'—')+
  '</td><td>'+(q.volume||'—')+'</td></tr>'
 ).join('');
}
async function refreshQ(){for(const[t,q]of Object.entries(quotes)){if(q.idFi)getQ(q.idFi,t);}}

async function loadPos(){
 try{
  const r=await(await fetch(B+'/positions')).json();
  if(Array.isArray(r)&&r.length){
   let h='<table><tr><th>ID</th><th>Поз.</th><th>Покупки</th><th>Продажи</th><th>PnL</th></tr>';
   r.forEach(p=>h+='<tr><td>'+(p.IdObject||'')+'</td><td>'+(p.TorgPos||p.BackPos||'')+'</td><td>'+(p.DailyBuyQuantity||'')+'</td><td>'+(p.DailySellQuantity||'')+'</td><td>'+(p.DailyPL||'')+'</td></tr>');
   document.getElementById('pos').innerHTML=h+'</table>';
  }else document.getElementById('pos').innerHTML='<i>Нет позиций</i>';
 }catch(e){document.getElementById('pos').textContent=e.message;}
}

async function loadOrd(){
 try{
  const r=await(await fetch(B+'/orders')).json();
  if(Array.isArray(r)&&r.length){
   let h='<table><tr><th>#</th><th>Instr</th><th>Dir</th><th>Qty</th><th>Price</th><th>Status</th></tr>';
   r.forEach(o=>h+='<tr><td>'+(o.NumEDocument||'')+'</td><td>'+(o.IdObject||'')+'</td><td>'+((o.BuySell||0)==1?'Buy':'Sell')+'</td><td>'+(o.Quantity||'')+'</td><td>'+(o.LimitPrice||o.Price||'')+'</td><td>'+(o.IdOrderStatus||'')+'</td></tr>');
   document.getElementById('ord').innerHTML=h+'</table>';
  }else document.getElementById('ord').textContent='Нет заявок';
 }catch(e){document.getElementById('ord').textContent=e.message;}
}

load();
setInterval(load,10000);
</script></body></html>";
    }
}
