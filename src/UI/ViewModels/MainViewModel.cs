using System.Collections.ObjectModel;
using System.Windows.Input;
using System.Windows.Threading;
using HedgeFund.Brokers.Finam;
using HedgeFund.Core;
using HedgeFund.Core.Averaging;
using HedgeFund.Core.Models;
using HedgeFund.Core.Strategies;
// QuikConnector удалён — всё через Finam Trade API (REST + gRPC)
using HedgeFund.UI.Services;

namespace HedgeFund.UI.ViewModels;

public class MainViewModel : BaseViewModel
{
    private TradingEngine? _engine;
    private IBrokerConnector? _connector;
    private FinamConnector? _finamConnector;
    private readonly DispatcherTimer _uiTimer;

    public MainViewModel()
    {
        // Дочерние VM
        MonitoringVM = new MonitoringViewModel();
        TradesVM = new TradesViewModel();
        BacktestVM = new BacktestViewModel();
        OrdersVM = new OrdersViewModel();
        QuotesVM = new QuotesViewModel();
        StrategyManagerVM = new StrategyManagerViewModel();
        OrderBookVM = new OrderBookViewModel();
        BacktestVM.ApplyToTradingRequested += OnApplyBacktestToTrading;
        WireUpNewVMs();

        // Команды
        StartCommand = new RelayCommand(Start, () => !IsRunning);
        StopCommand = new RelayCommand(Stop, () => IsRunning);
        ToggleMartingaleCommand = new RelayCommand(ToggleMartingale);
        ToggleStopLossCommand = new RelayCommand(ToggleStopLoss);
        LoadTokenCommand = new RelayCommand(LoadTokenFromEnv);

        // Таймер обновления UI (200мс)
        _uiTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(200) };
        _uiTimer.Tick += (_, _) => RefreshUI();

        // Значения по умолчанию
        Brokers = new ObservableCollection<string> { "Финам (Trade API)" };
        SelectedBroker = Brokers[0];

        Instruments = new ObservableCollection<string> { "SiM6", "SiU6", "Si", "SBER", "GAZP", "BR", "GOLD" };
        SelectedInstrument = Instruments[0];

        Strategies = new ObservableCollection<string> { "PSAR Grid MM (1 мин)", "PSAR+EMA Combo (5 мин)", "VStop Pure (30 сек, бумага)" };
        SelectedStrategy = Strategies[0];

        StopLossModes = new ObservableCollection<string> { "Пункты", "%" };
        SelectedStopLossMode = StopLossModes[0];

        // Попробовать загрузить токен из env
        var envToken = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        if (!string.IsNullOrEmpty(envToken))
        {
            ApiToken = envToken;
            AddLog("Токен загружен из переменной окружения FINAM_TOKEN.");
        }
    }

    // === Дочерние ViewModel ===

    public MonitoringViewModel MonitoringVM { get; }
    public TradesViewModel TradesVM { get; }
    public BacktestViewModel BacktestVM { get; }
    public OrdersViewModel OrdersVM { get; }
    public QuotesViewModel QuotesVM { get; }
    public StrategyManagerViewModel StrategyManagerVM { get; }
    public OrderBookViewModel OrderBookVM { get; }

    // === Свойства привязки ===

    private bool _isRunning;
    public bool IsRunning
    {
        get => _isRunning;
        set
        {
            if (SetField(ref _isRunning, value))
            {
                OnPropertyChanged(nameof(StatusText));
                OnPropertyChanged(nameof(StatusColor));
                CommandManager.InvalidateRequerySuggested();
            }
        }
    }

    public string StatusText => IsRunning ? "● РАБОТАЕТ" : "○ ОСТАНОВЛЕН";
    public string StatusColor => IsRunning ? "#22C55E" : "#EF4444";

    // --- Токен API ---
    private string _apiToken = string.Empty;
    public string ApiToken
    {
        get => _apiToken;
        set => SetField(ref _apiToken, value);
    }

    // --- Выбор брокера/инструмента/стратегии ---
    public ObservableCollection<string> Brokers { get; }
    private string _selectedBroker = string.Empty;
    public string SelectedBroker
    {
        get => _selectedBroker;
        set => SetField(ref _selectedBroker, value);
    }

    public ObservableCollection<string> Instruments { get; }
    private string _selectedInstrument = string.Empty;
    public string SelectedInstrument
    {
        get => _selectedInstrument;
        set => SetField(ref _selectedInstrument, value);
    }

    public ObservableCollection<string> Strategies { get; }
    private string _selectedStrategy = string.Empty;
    public string SelectedStrategy
    {
        get => _selectedStrategy;
        set => SetField(ref _selectedStrategy, value);
    }

    // --- Усреднение ---
    private bool _isMartingale;
    public bool IsMartingale
    {
        get => _isMartingale;
        set
        {
            if (SetField(ref _isMartingale, value))
            {
                OnPropertyChanged(nameof(AveragingModeText));
                _engine?.ToggleMartingale();
            }
        }
    }
    public string AveragingModeText => IsMartingale ? "Мартингейл" : "Фикс (1 лот)";

    private int _baseLotSize = 1;
    public int BaseLotSize
    {
        get => _baseLotSize;
        set => SetField(ref _baseLotSize, value);
    }

    private int _maxAveraging = 0;
    public int MaxAveraging
    {
        get => _maxAveraging;
        set
        {
            if (SetField(ref _maxAveraging, value))
                _engine?.SetMaxAveraging(value);
        }
    }
    public string MaxAveragingText => MaxAveraging <= 0 ? "Без лимита" : MaxAveraging.ToString();

    // --- Стоп-лосс ---
    private bool _stopLossEnabled;
    public bool StopLossEnabled
    {
        get => _stopLossEnabled;
        set
        {
            if (SetField(ref _stopLossEnabled, value))
                _engine?.ToggleStopLoss();
        }
    }

    private double _stopLossValue = 100;
    public double StopLossValue
    {
        get => _stopLossValue;
        set
        {
            if (SetField(ref _stopLossValue, value))
                UpdateStopLoss();
        }
    }

    public ObservableCollection<string> StopLossModes { get; }
    private string _selectedStopLossMode = string.Empty;
    public string SelectedStopLossMode
    {
        get => _selectedStopLossMode;
        set
        {
            if (SetField(ref _selectedStopLossMode, value))
                UpdateStopLoss();
        }
    }

    // --- Баланс ---
    private double _balance;
    public double Balance
    {
        get => _balance;
        set => SetField(ref _balance, value);
    }

    // --- Позиции ---
    public ObservableCollection<PositionViewModel> Positions { get; } = new();

    // --- Лог ---
    public ObservableCollection<string> LogEntries { get; } = new();

    // === Команды ===

    public ICommand StartCommand { get; }
    public ICommand StopCommand { get; }
    public ICommand ToggleMartingaleCommand { get; }
    public ICommand ToggleStopLossCommand { get; }
    public ICommand LoadTokenCommand { get; }

    // === Логика ===

    private async void Start()
    {
        if (IsRunning) return;

        var settings = new AveragingSettings
        {
            Enabled = true,
            Mode = IsMartingale ? AveragingMode.Martingale : AveragingMode.Fixed,
            BaseLotSize = BaseLotSize,
            MaxAveragingCount = MaxAveraging,
            StopLossEnabled = StopLossEnabled,
            StopLossPoints = SelectedStopLossMode == "Пункты" ? StopLossValue : 0,
            StopLossPercent = SelectedStopLossMode == "%" ? StopLossValue : 0,
            UsePercentStopLoss = SelectedStopLossMode == "%"
        };

        AddLog("Робот запущен. Брокер: " + SelectedBroker + ", Инструмент: " + SelectedInstrument);
        AddLog("Стратегия: " + SelectedStrategy);
        AddLog($"Усреднение: {AveragingModeText}, лимит: {MaxAveragingText}");
        if (StopLossEnabled)
            AddLog($"Стоп-лосс: {StopLossValue} {SelectedStopLossMode}");

        // Подключение к брокеру
        // Всегда через Finam Trade API (REST + gRPC)
        await ConnectFinamAsync();

        IsRunning = true;
        _uiTimer.Start();
        MonitoringVM.StartRefresh();
    }

    private async Task ConnectFinamAsync()
    {
        try
        {
            AddLog("📡 Подключение к Финам (REST + gRPC)...");

            _finamConnector = new FinamConnector();
            _connector = _finamConnector;

            // События от коннектора
            _finamConnector.OnError += msg => App.Current?.Dispatcher.Invoke(() => AddLog(msg));
            _finamConnector.OnConnectionChanged += connected => App.Current?.Dispatcher.Invoke(() =>
            {
                AddLog(connected ? "✅ Финам подключён (gRPC + REST)" : "⚠️ Финам отключён");
                OnPropertyChanged(nameof(StatusText));
                OnPropertyChanged(nameof(StatusColor));
            });

            _finamConnector.OnTrade += trade => App.Current?.Dispatcher.Invoke(() =>
            {
                AddLog($"💹 Сделка: {trade.Ticker} {trade.Direction} {trade.Volume}@{trade.Price:F0}");
                MonitoringVM.TradesToday++;
            });

            _finamConnector.OnOrderUpdate += order => App.Current?.Dispatcher.Invoke(() =>
            {
                AddLog($"📝 Ордер: {order.Ticker} {order.Direction} {order.Status} @{order.Price:F0}");
                OrdersVM.UpdateOrder(order);
            });

            await _finamConnector.ConnectAsync(ApiToken);
            AddLog("✅ Финам подключён!");

            // Подписываемся на ВСЕ инструменты
            foreach (var instrument in Instruments)
            {
                await SubscribeToInstrument(instrument);
            }

            // Загружаем историю свечей для выбранного инструмента
            await LoadHistoricalCandles(SelectedInstrument);

            // Баланс
            var balance = await _finamConnector.GetBalanceAsync();
            Balance = balance;
            MonitoringVM.Balance = balance;
            MonitoringVM.Equity = balance;
        }
        catch (Exception ex)
        {
            AddLog($"❌ Ошибка Финам: {ex.Message}");
        }
    }

    private async Task SubscribeToInstrument(string ticker)
    {
        if (_finamConnector == null || string.IsNullOrEmpty(ticker)) return;

        try
        {
            // Подписка на стакан (bid/ask)
            await _finamConnector.SubscribeLevel2Async(ticker, (bid, ask) => { });

            // Свечи 5 мин через gRPC стрим
            await _finamConnector.SubscribeCandlesAsync(ticker, TimeSpan.FromMinutes(5), candle =>
                App.Current?.Dispatcher.Invoke(() =>
                {
                    if (ticker != OrderBookVM.SelectedInstrument) return;
                    var cluster = new ClusterCandle
                    {
                        Timestamp = candle.Timestamp, Open = candle.Open, High = candle.High,
                        Low = candle.Low, Close = candle.Close, Volume = candle.Volume
                    };
                    var list = new List<ClusterCandle>(OrderBookVM.Candles);
                    if (list.Count > 0 && list[^1].Timestamp == cluster.Timestamp)
                        list[^1] = cluster;
                    else
                        list.Add(cluster);
                    if (list.Count > 300) list.RemoveRange(0, list.Count - 300);
                    OrderBookVM.UpdateCandles(list);
                }));

            AddLog($"📊 Подписки на {ticker}: gRPC свечи, котировки");
        }
        catch (Exception ex)
        {
            AddLog($"⚠️ Подписка {ticker}: {ex.Message}");
        }
    }

    private async Task LoadHistoricalCandles(string ticker)
    {
        if (_finamConnector == null) return;
        try
        {
            var history = await _finamConnector.GetHistoricalCandlesAsync(
                ticker, TimeSpan.FromMinutes(5), DateTime.UtcNow.AddDays(-3), DateTime.UtcNow);
            if (history.Length > 0)
            {
                var candles = history.Select(c => new ClusterCandle
                {
                    Timestamp = c.Timestamp, Open = c.Open, High = c.High,
                    Low = c.Low, Close = c.Close, Volume = c.Volume
                }).ToList();
                OrderBookVM.UpdateCandles(candles);
                AddLog($"📊 Загружено {history.Length} свечей {ticker}");
            }
        }
        catch (Exception ex) { AddLog($"⚠️ История {ticker}: {ex.Message}"); }
    }

    private async Task ConnectFinamAsync()
    {
        if (string.IsNullOrWhiteSpace(ApiToken))
        {
            AddLog("⚠ Токен Финам не указан. Введите токен или установите FINAM_TOKEN.");
            return;
        }

        try
        {
            AddLog("Подключение к Финам Trade API...");
            var finam = new FinamConnector();

            finam.OnTrade += OnTradeReceived;
            finam.OnOrderUpdate += OnOrderReceived;
            finam.OnError += msg => AddLog($"⚠ Финам: {msg}");
            finam.OnConnectionChanged += connected =>
            {
                AddLog(connected ? "✓ Подключён к Финам" : "✗ Отключён от Финам");
            };

            var success = await finam.ConnectAsync(ApiToken);
            if (success)
            {
                _connector = finam;
                AddLog("✓ Финам: подключение успешно.");

                // Получаем начальный баланс
                var balance = await finam.GetBalanceAsync();
                Balance = balance;
                MonitoringVM.Balance = balance;
                MonitoringVM.Equity = balance;
                MonitoringVM.InitialBalance = balance;
                MonitoringVM.AddEquityPoint(balance);
            }
            else
            {
                AddLog("✗ Не удалось подключиться к Финам.");
            }
        }
        catch (Exception ex)
        {
            AddLog($"✗ Ошибка подключения Финам: {ex.Message}");
        }
    }

    private void OnTradeReceived(Trade trade)
    {
        App.Current?.Dispatcher.Invoke(() =>
        {
            AddLog($"Сделка: {trade.Ticker} {trade.Direction} {trade.Volume}@{trade.Price:F2}");

            // Обновляем equity curve
            MonitoringVM.TradesToday++;
            MonitoringVM.AddEquityPoint(MonitoringVM.Equity);
        });
    }

    private void OnOrderReceived(Order order)
    {
        App.Current?.Dispatcher.Invoke(() =>
        {
            AddLog($"Ордер: {order.Ticker} {order.Direction} статус={order.Status}");
        });
    }

    private void Stop()
    {
        if (!IsRunning) return;

        _engine?.Stop();
        _connector?.DisconnectAsync();
        _connector?.Dispose();
        _connector = null;
        _finamConnector = null;

        IsRunning = false;
        _uiTimer.Stop();
        MonitoringVM.StopRefresh();
        QuotesVM.StopRefresh();
        OrderBookVM.StopRefresh();
        AddLog("Робот остановлен.");
    }

    private void ToggleMartingale()
    {
        IsMartingale = !IsMartingale;
        AddLog($"Режим усреднения: {AveragingModeText}");
    }

    private void ToggleStopLoss()
    {
        StopLossEnabled = !StopLossEnabled;
        AddLog($"Стоп-лосс: {(StopLossEnabled ? "ВКЛ" : "ВЫКЛ")}");
    }

    private void LoadTokenFromEnv()
    {
        var token = Environment.GetEnvironmentVariable("FINAM_TOKEN");
        if (!string.IsNullOrEmpty(token))
        {
            ApiToken = token;
            AddLog("Токен загружен из переменной окружения FINAM_TOKEN.");
        }
        else
        {
            AddLog("⚠ Переменная FINAM_TOKEN не установлена.");
        }
    }

    private void UpdateStopLoss()
    {
        bool usePercent = SelectedStopLossMode == "%";
        _engine?.SetStopLoss(StopLossValue, usePercent);
    }

    private void RefreshUI()
    {
        // Обновление позиций и лога из движка
        if (_engine != null)
        {
            // Синхронизация лога
            while (LogEntries.Count < _engine.EventLog.Count)
                LogEntries.Add(_engine.EventLog[LogEntries.Count]);
        }

        // Синхронизация позиций с мониторингом
        MonitoringVM.OpenPositionsCount = Positions.Count;

        // Обновляем позиции в мониторинге
        MonitoringVM.OpenPositions.Clear();
        foreach (var p in Positions)
            MonitoringVM.OpenPositions.Add(p);
    }

    public void AddLog(string message)
    {
        var entry = $"[{DateTime.Now:HH:mm:ss}] {message}";
        LogEntries.Add(entry);
    }

    private void OnApplyBacktestToTrading(BacktestViewModel bt)
    {
        // Устанавливаем инструмент из бэктеста
        if (!string.IsNullOrEmpty(bt.SelectedTicker))
        {
            if (!Instruments.Contains(bt.SelectedTicker))
                Instruments.Add(bt.SelectedTicker);
            SelectedInstrument = bt.SelectedTicker;
        }

        // Находим стратегию
        var strategyMapping = new Dictionary<string, string>
        {
            ["Momentum Breakout"] = "Scalping (EMA Cross + RSI)",
            ["Scalping (EMA Cross + RSI)"] = "Scalping (EMA Cross + RSI)",
            ["Breakout (BB + ATR)"] = "Breakout (BB + ATR)"
        };

        if (strategyMapping.TryGetValue(bt.SelectedStrategyName, out var uiStrategy))
            SelectedStrategy = uiStrategy;

        // Устанавливаем стоп-лосс
        if (bt.StopLoss > 0)
        {
            StopLossEnabled = true;
            StopLossValue = bt.StopLoss;
        }

        AddLog($"Применены параметры из бэктеста: {bt.SelectedTicker}, " +
               $"SL={bt.StopLoss}, TP={bt.TakeProfit}, Комиссия={bt.Commission}");
    }

    // === СЕРВЕРНОЕ ПОДКЛЮЧЕНИЕ (SignalR) ===

    private TradingHubClient? _hubClient;

    private string _serverUrl = "http://216.57.106.32:5050/trading";
    public string ServerUrl
    {
        get => _serverUrl;
        set => SetField(ref _serverUrl, value);
    }

    private bool _isServerConnected;
    public bool IsServerConnected
    {
        get => _isServerConnected;
        set
        {
            if (SetField(ref _isServerConnected, value))
            {
                OnPropertyChanged(nameof(ServerConnectionColor));
                OnPropertyChanged(nameof(ConnectServerButtonText));
            }
        }
    }

    public string ServerConnectionColor => IsServerConnected ? "#26A69A" : "#EF5350";
    public string ConnectServerButtonText => IsServerConnected ? "Отключить" : "Подключить";

    public ICommand ConnectServerCommand => new RelayCommand(async () => await ToggleServerConnectionAsync());
    public ICommand SendTokenToServerCommand => new RelayCommand(async () => await SendTokenToServerAsync());
    public ICommand EmergencyStopCommand => new RelayCommand(async () => await EmergencyStopAsync());
    public ICommand PauseTradingCommand => new RelayCommand(async () => await PauseTradingAsync());

    private async Task ToggleServerConnectionAsync()
    {
        if (IsServerConnected)
        {
            if (_hubClient != null)
            {
                await _hubClient.DisconnectAsync();
                _hubClient = null;
            }
            IsServerConnected = false;
            AddLog("🔌 Отключён от сервера");
        }
        else
        {
            try
            {
                _hubClient = new TradingHubClient();
                _hubClient.OnLogMessage += (ts, level, msg) =>
                    System.Windows.Application.Current.Dispatcher.Invoke(() => AddLog($"[{level}] {msg}"));
                _hubClient.OnStatusUpdate += status =>
                    System.Windows.Application.Current.Dispatcher.Invoke(() =>
                    {
                        Balance = status.Balance;
                        MonitoringVM.Balance = status.Balance;
                        MonitoringVM.Equity = status.Equity;
                        MonitoringVM.PnLToday = status.TodayPnL;
                        MonitoringVM.PnLTotal = status.TotalPnL;
                        MonitoringVM.OpenPositionsCount = status.OpenPositions;
                        MonitoringVM.TradesToday = status.TodayTrades;
                    });
                _hubClient.OnError += msg =>
                    System.Windows.Application.Current.Dispatcher.Invoke(() => AddLog($"❌ {msg}"));

                await _hubClient.ConnectAsync(ServerUrl);
                IsServerConnected = true;
                WireUpHubEvents();
                AddLog($"✅ Подключён к серверу: {ServerUrl}");
                await _hubClient.RequestStatusAsync();
                QuotesVM.StartRefresh();
                OrderBookVM.StartRefresh();
            }
            catch (Exception ex)
            {
                AddLog($"❌ Ошибка подключения: {ex.Message}");
                _hubClient = null;
            }
        }
    }

    private async Task EmergencyStopAsync()
    {
        AddLog("🔴 ЭКСТРЕННАЯ ОСТАНОВКА!");
        if (_hubClient != null && IsServerConnected)
        {
            try { await _hubClient.EmergencyStopAsync(); }
            catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
        }
        // Также остановить локально
        Stop();
    }

    private async Task SendTokenToServerAsync()
    {
        if (string.IsNullOrWhiteSpace(ApiToken))
        {
            AddLog("⚠️ Введите токен Финам");
            return;
        }
        if (_hubClient == null || !IsServerConnected)
        {
            AddLog("⚠️ Сначала подключитесь к серверу");
            return;
        }
        try
        {
            await _hubClient.ConnectBrokerAsync(ApiToken);
            AddLog("📤 Токен отправлен на сервер");
        }
        catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
    }

    private async Task PauseTradingAsync()
    {
        if (_hubClient != null && IsServerConnected)
        {
            try
            {
                await _hubClient.PauseTradingAsync();
                AddLog("⏸️ Торговля приостановлена");
            }
            catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
        }
    }

    // === ПРОВОДКА НОВЫХ VM ===

    private void WireUpNewVMs()
    {
        // Заявки
        OrdersVM.CancelOrderRequested += async (id) =>
        {
            if (_finamConnector != null)
            {
                try { await _finamConnector.CancelOrderAsync(id); AddLog($"❌ Заявка {id} отменена"); }
                catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
            }
            else if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.CancelOrderAsync(id); AddLog($"❌ Заявка {id} отменена"); }
                catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
            }
        };
        OrdersVM.ModifyOrderRequested += async (id, price, vol) =>
        {
            // QUIK: cancel + re-place
            if (_finamConnector != null)
            {
                try
                {
                    await _finamConnector.CancelOrderAsync(id);
                    // TODO: re-place with new price/vol
                    AddLog($"✏️ Заявка {id} отменена (изменение через cancel+replace)");
                }
                catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
            }
            else if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.ModifyOrderAsync(id, price, vol); AddLog($"✏️ Заявка {id} изменена"); }
                catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
            }
        };
        OrdersVM.CancelAllRequested += async () =>
        {
            if (_finamConnector != null)
            {
                var orders = await _finamConnector.GetActiveOrdersAsync();
                foreach (var o in orders)
                    try { await _finamConnector.CancelOrderAsync(o.BrokerOrderId); } catch { }
                AddLog("❌ Все заявки отменены");
            }
            else if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.CancelAllOrdersAsync(); AddLog("❌ Все заявки отменены"); }
                catch (Exception ex) { AddLog($"❌ Ошибка: {ex.Message}"); }
            }
        };
        OrdersVM.RefreshRequested += async () =>
        {
            if (_finamConnector != null)
            {
                var orders = await _finamConnector.GetActiveOrdersAsync();
                OrdersVM.UpdateOrders(orders.Select(OrderViewModel.FromOrder));
            }
            else if (_hubClient != null && IsServerConnected)
                try { await _hubClient.GetActiveOrdersAsync(); } catch { }
        };

        // Котировки
        QuotesVM.SubscribeRequested += async (ticker) =>
        {
            if (_finamConnector != null)
            {
                try { await SubscribeToInstrument(ticker); }
                catch { }
            }
            else if (_hubClient != null && IsServerConnected)
                try { await _hubClient.SubscribeQuotesAsync(ticker); } catch { }
        };

        // Стратегии
        StrategyManagerVM.StartRequested += async (name, ticker, pars) =>
        {
            if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.StartStrategyAsync(name, ticker, pars); AddLog($"▶ Стратегия {name} запущена"); }
                catch (Exception ex) { AddLog($"❌ {ex.Message}"); }
            }
        };
        StrategyManagerVM.StopRequested += async (name) =>
        {
            if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.StopStrategyAsync(name); AddLog($"⏹ Стратегия {name} остановлена"); }
                catch (Exception ex) { AddLog($"❌ {ex.Message}"); }
            }
        };
        StrategyManagerVM.PauseRequested += async (name) =>
        {
            if (_hubClient != null && IsServerConnected)
                try { await _hubClient.PauseStrategyAsync(name); } catch { }
        };
        StrategyManagerVM.StopAndCloseRequested += async (name) =>
        {
            if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.StopAndCloseAllAsync(name); AddLog($"🔴 {name}: стоп торги + закрытие позиций"); }
                catch (Exception ex) { AddLog($"❌ {ex.Message}"); }
            }
        };
        StrategyManagerVM.EmergencyStopRequested += async () => await EmergencyStopAsync();

        // Стакан
        OrderBookVM.SubscribeBookRequested += async (ticker) =>
        {
            if (_hubClient != null && IsServerConnected)
                try { await _hubClient.SubscribeOrderBookAsync(ticker); } catch { }
        };
        OrderBookVM.TimeframeChangeRequested += async (tf) =>
        {
            if (_hubClient != null && IsServerConnected)
                try { await _hubClient.GetCandlesAsync(OrderBookVM.SelectedInstrument, tf); } catch { }
        };
    }

    private void WireUpHubEvents()
    {
        if (_hubClient == null) return;

        _hubClient.OnActiveOrdersReceived += orders =>
            App.Current?.Dispatcher.Invoke(() =>
                OrdersVM.UpdateOrders(orders.Select(OrderViewModel.FromOrder)));

        _hubClient.OnOrderUpdated += order =>
            App.Current?.Dispatcher.Invoke(() => OrdersVM.UpdateOrder(order));

        _hubClient.OnQuoteUpdate += quote =>
            App.Current?.Dispatcher.Invoke(() => QuotesVM.UpdateQuote(quote));

        _hubClient.OnStrategyStatusesReceived += statuses =>
            App.Current?.Dispatcher.Invoke(() =>
            {
                foreach (var s in statuses)
                    StrategyManagerVM.UpdateStrategyStatus(s);
            });

        _hubClient.OnOrderBookUpdate += snapshot =>
            App.Current?.Dispatcher.Invoke(() => OrderBookVM.UpdateOrderBook(snapshot));

        _hubClient.OnCandlesReceived += candles =>
            App.Current?.Dispatcher.Invoke(() => OrderBookVM.UpdateCandles(candles));
    }
}
