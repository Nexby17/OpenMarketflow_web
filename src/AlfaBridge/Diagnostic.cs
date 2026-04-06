using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using ADClientSDK;
using AD.Common.DataStructures;
using AD.Common.Helpers;

namespace HedgeFund.AlfaBridge
{
    /// <summary>
    /// Полная диагностика ADClientSDK.
    /// Запуск: AlfaBridge.exe --diag
    /// Результат сохраняется в alfa_diag.txt на рабочем столе.
    /// </summary>
    class Diagnostic
    {
        static List<string> _log = new List<string>();
        static List<string> _events = new List<string>();

        public static void Run(string login, string password)
        {
            Log("═══════════════════════════════════════════════════");
            Log("  ADClientSDK Diagnostic — " + DateTime.Now);
            Log("═══════════════════════════════════════════════════");
            Log("");

            // 1. Создание клиента
            AdClient client = null;
            Log("--- STEP 1: Create AdClient ---");
            try
            {
                client = new AdClient();
                Log("OK: AdClient created");
            }
            catch (Exception ex)
            {
                Log($"FAIL: {ex.GetType().Name}: {ex.Message}");
                Log(ex.ToString());
                Save();
                return;
            }

            // 2. Версии
            Log("");
            Log("--- STEP 2: Versions ---");
            try { Log($"Version: {AdClient.Version}"); } catch (Exception ex) { Log($"Version ERROR: {ex.Message}"); }
            try { Log($"TerminalVersion: {AdClient.TerminalVersion}"); } catch (Exception ex) { Log($"TerminalVersion ERROR: {ex.Message}"); }
            try { Log($"ProtocolVersion: {AdClient.ProtocolVersion}"); } catch (Exception ex) { Log($"ProtocolVersion ERROR: {ex.Message}"); }

            // 3. Подписка на события ДО Connect
            Log("");
            Log("--- STEP 3: Subscribe events ---");
            try
            {
                client.OnConnectionChanged += (fe, status) =>
                {
                    var msg = $"EVENT OnConnectionChanged: frontEnd={fe}, status={status}";
                    _events.Add(msg);
                    Console.WriteLine(msg);
                };
                Log("OK: OnConnectionChanged subscribed");
            }
            catch (Exception ex) { Log($"FAIL: {ex.Message}"); }

            try
            {
                client.ConnectionError += (error) =>
                {
                    var msg = $"EVENT ConnectionError: {error}";
                    _events.Add(msg);
                    Console.WriteLine(msg);
                };
                Log("OK: ConnectionError subscribed");
            }
            catch (Exception ex) { Log($"FAIL: {ex.Message}"); }

            try
            {
                client.OnServerMessage += (msgType, text) =>
                {
                    var msg = $"EVENT OnServerMessage: type={msgType}, text={text}";
                    _events.Add(msg);
                    Console.WriteLine(msg);
                };
                Log("OK: OnServerMessage subscribed");
            }
            catch (Exception ex) { Log($"FAIL: {ex.Message}"); }

            // 4. Connect
            Log("");
            Log($"--- STEP 4: Connect('{login}', '***') ---");
            try
            {
                var connectDone = false;
                Exception connectEx = null;
                var t = new Thread(() =>
                {
                    try
                    {
                        client.Connect(login, password);
                    }
                    catch (Exception ex)
                    {
                        connectEx = ex;
                    }
                    connectDone = true;
                });
                t.IsBackground = true;
                t.Start();

                // Ждём до 15 секунд
                for (int i = 0; i < 30; i++)
                {
                    Thread.Sleep(500);
                    if (connectDone) break;
                    if (i % 4 == 0) Log($"  waiting... {i / 2}s");
                }

                if (!connectDone)
                {
                    Log("TIMEOUT: Connect did not return in 15 seconds");
                }
                else if (connectEx != null)
                {
                    Log($"FAIL: {connectEx.GetType().Name}: {connectEx.Message}");
                    Log(connectEx.ToString());
                }
                else
                {
                    Log("OK: Connect returned without exception");
                }
            }
            catch (Exception ex)
            {
                Log($"OUTER FAIL: {ex.GetType().Name}: {ex.Message}");
            }

            // 5. Собираем события
            Thread.Sleep(3000);
            Log("");
            Log("--- STEP 5: Events received ---");
            foreach (var e in _events)
                Log($"  {e}");
            if (_events.Count == 0)
                Log("  (no events)");

            // 6. Connection Status
            Log("");
            Log("--- STEP 6: Connection Status ---");
            foreach (FrontEndType fe in Enum.GetValues(typeof(FrontEndType)))
            {
                try
                {
                    var status = client.GetConnectionStatus(fe);
                    Log($"  {fe} = {status}");
                }
                catch (Exception ex) { Log($"  {fe} = ERROR: {ex.Message}"); }
            }

            // 7. Dictionaries
            Log("");
            Log("--- STEP 7: Dictionaries ---");
            
            try
            {
                var fi = client.Dictionaries.GetFinInstruments();
                Log($"FinInstruments: {(fi != null ? fi.Length.ToString() : "null")}");
                if (fi != null && fi.Length > 0)
                {
                    for (int i = 0; i < Math.Min(10, fi.Length); i++)
                    {
                        var f = fi[i];
                        Log($"  [{i}] IdFi={f.IdFi} IdObject={f.IdObject} Board={f.IdMarketBoard} Gate={f.IdGate}");
                    }
                }
            }
            catch (Exception ex) { Log($"GetFinInstruments ERROR: {ex.Message}"); }

            try
            {
                var objs = client.Dictionaries.GetObjects();
                Log($"Objects: {(objs != null ? objs.Length.ToString() : "null")}");
                if (objs != null && objs.Length > 0)
                {
                    for (int i = 0; i < Math.Min(10, objs.Length); i++)
                    {
                        var o = objs[i];
                        Log($"  [{i}] IdObject={o.IdObject} Symbol={o.SymbolObject} Name={o.NameObject} Type={o.IdObjectType}");
                    }
                }
            }
            catch (Exception ex) { Log($"GetObjects ERROR: {ex.Message}"); }

            // Поиск наших инструментов
            var searchList = new[] { "SBER", "GAZP", "ROSN", "TATN", "ALRS", "SBRF", "GAZR" };
            foreach (var name in searchList)
            {
                try
                {
                    var found = client.Dictionaries.SearchInstruments(name, ObjectGroup.None);
                    if (found == null)
                    {
                        // Попробуем другой ObjectGroup
                        found = client.Dictionaries.SearchInstruments(name, ObjectGroup.Stocks);
                    }
                    var count = found?.Length ?? 0;
                    Log($"Search '{name}': {count} results");
                    if (found != null)
                    {
                        for (int i = 0; i < Math.Min(5, found.Length); i++)
                        {
                            var f = found[i];
                            var obj = client.Dictionaries.GetObjectByIdFi(f.IdFi);
                            Log($"  IdFi={f.IdFi} IdObj={f.IdObject} Board={f.IdMarketBoard} Symbol={obj?.SymbolObject} Name={obj?.NameObject}");
                        }
                    }
                }
                catch (Exception ex) { Log($"Search '{name}' ERROR: {ex.Message}"); }
            }

            // SubAccountRazdels
            try
            {
                var sar = client.Dictionaries.GetSubAccountRazdels();
                Log($"SubAccountRazdels: {(sar != null ? sar.Length.ToString() : "null")}");
                if (sar != null)
                {
                    for (int i = 0; i < Math.Min(10, sar.Length); i++)
                    {
                        var r = sar[i];
                        Log($"  [{i}] Sub={r.IdSubAccount} Razdel={r.IdRazdel} Code={r.CodeSubAccount} Account={r.IdAccount} Gate={r.IdGate} Type={r.IdRazdelType}");
                    }
                }
            }
            catch (Exception ex) { Log($"GetSubAccountRazdels ERROR: {ex.Message}"); }

            // 8. Portfolio
            Log("");
            Log("--- STEP 8: Portfolio ---");
            try
            {
                var pos = client.Portfolio.GetPositions();
                Log($"Positions: {(pos != null ? pos.Length.ToString() : "null")}");
                if (pos != null)
                {
                    for (int i = 0; i < Math.Min(10, pos.Length); i++)
                    {
                        var p = pos[i];
                        var obj = client.Dictionaries.GetObjectByIdFi(p.IdFiBalance);
                        Log($"  [{i}] IdObj={p.IdObject} Sub={p.IdSubAccount} Razdel={p.IdRazdel} " +
                            $"BackPos={p.BackPos} Buy={p.BuyQty} Sell={p.SellQty} PL={p.TrdPL} " +
                            $"Symbol={obj?.SymbolObject}");
                    }
                }
            }
            catch (Exception ex) { Log($"GetPositions ERROR: {ex.Message}"); }

            // 9. Orders
            Log("");
            Log("--- STEP 9: Orders ---");
            try
            {
                var orders = client.Trading.GetOrders();
                Log($"Orders type: {orders?.GetType().FullName ?? "null"}");
                // OrderEntity может быть не IEnumerable — проверяем
                if (orders != null)
                {
                    Log($"Orders properties:");
                    foreach (var prop in orders.GetType().GetProperties())
                    {
                        try
                        {
                            var val = prop.GetValue(orders);
                            Log($"  {prop.Name} = {val}");
                        }
                        catch { }
                    }
                }
            }
            catch (Exception ex) { Log($"GetOrders ERROR: {ex.Message}"); }

            // 10. RealTime — попробуем подписаться
            Log("");
            Log("--- STEP 10: RealTime ---");
            // Сначала найдём idFi для SBER
            try
            {
                var sber = client.Dictionaries.SearchInstruments("SBER", ObjectGroup.Stocks);
                if (sber != null && sber.Length > 0)
                {
                    var idFi = sber[0].IdFi;
                    Log($"Subscribing to FinInfo for SBER idFi={idFi}...");
                    client.RealTime.SubscribeFinInfo(idFi);
                    Thread.Sleep(2000);
                    var fi = client.RealTime.GetFinInfo(idFi);
                    if (fi != null)
                    {
                        Log($"FinInfo type: {fi.GetType().FullName}");
                        Log($"  IdFI={fi.IdFI} Last={fi.Last} Bid={fi.Bid} Ask={fi.Ask}");
                        Log($"  High={fi.High} Low={fi.Low} Open={fi.Open} Vol={fi.VolToday}");
                    }
                    else
                    {
                        Log("GetFinInfo returned null");
                    }
                    client.RealTime.UnSubscribeFinInfo(idFi);
                }
                else
                {
                    Log("Could not find SBER to test RealTime");
                }
            }
            catch (Exception ex) { Log($"RealTime ERROR: {ex.Message}"); }

            // 11. Archive
            Log("");
            Log("--- STEP 11: Archive ---");
            try
            {
                var news = client.Archive.GetNews();
                Log($"News: {(news != null ? news.Length.ToString() : "null")}");
            }
            catch (Exception ex) { Log($"GetNews ERROR: {ex.Message}"); }

            // Final events
            Thread.Sleep(2000);
            Log("");
            Log("--- FINAL: All events ---");
            foreach (var e in _events)
                Log($"  {e}");

            Log("");
            Log("═══ DIAGNOSTIC COMPLETE ═══");
            Save();
        }

        static void Log(string msg)
        {
            Console.WriteLine(msg);
            _log.Add(msg);
        }

        static void Save()
        {
            var path = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Desktop),
                "alfa_diag.txt");
            File.WriteAllLines(path, _log, Encoding.UTF8);
            Console.WriteLine($"\nСохранено: {path}");
            Console.WriteLine("Нажмите Enter...");
            Console.ReadLine();
        }
    }
}
