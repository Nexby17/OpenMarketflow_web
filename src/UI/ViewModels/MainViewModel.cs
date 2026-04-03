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
        SettingsVM = new SettingsViewModel();
        BacktestVM.ApplyToTradingRequested += OnApplyBacktestToTrading;
        WireUpNewVMs();

        // Команды
        ConnectBrokerCommand = new RelayCommand(ConnectBroker, () => !IsBrokerConnected);
        StartBotCommand = new RelayCommand(StartBot, () => IsBrokerConnected && !IsRunning);
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

        Instruments = new ObservableCollection<string> { "SiM6", "SiU6", "SBER", "GAZP", "BRM6", "GDM6" };
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
    public SettingsViewModel SettingsVM { get; }

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

    public ICommand ConnectBrokerCommand { get; }
    public ICommand StartBotCommand { get; }
    public ICommand StopCommand { get; }

    private bool _isBrokerConnected;
    public bool IsBrokerConnected
    {
        get => _isBrokerConnected;
        set { if (SetField(ref _isBrokerConnected, value)) OnPropertyChanged(nameof(BrokerStatusText)); }
    }
    public string BrokerStatusText => IsBrokerConnected ? "✅ Подключён" : "❌ Не подключён";
    public ICommand ToggleMartingaleCommand { get; }
    public ICommand ToggleStopLossCommand { get; }
    public ICommand LoadTokenCommand { get; }

    // === Логика ===

    /// <summary>Подключение к брокеру (кнопка 1)</summary>
    private async void ConnectBroker()
    {
        if (IsBrokerConnected) return;
        await ConnectFinamAsync();
    }

    /// <summary>Запуск торгового робота (кнопка 2)</summary>
    private void StartBot()
    {
        if (IsRunning || !IsBrokerConnected) return;

        AddLog($"🤖 Робот запущен: {SelectedStrategy} на {SelectedInstrument}");
        AddLog($"Усреднение: {AveragingModeText}, лимит: {MaxAveragingText}");
        if (StopLossEnabled)
            AddLog($"Стоп-лосс: {StopLossValue} {SelectedStopLossMode}");

        IsRunning = true;
        _uiTimer.Start();
        MonitoringVM.StartRefresh();

        // TODO: запуск выбранной стратегии через StrategyRunner
        AddLog("✅ Стратегия активна. Ожидание сигналов...");
    }

    private async Task ConnectFinamAsync()
    {
        try
        {
            // Токен: сначала из Настроек, потом из поля ввода, потом ENV
            var token = !string.IsNullOrWhiteSpace(SettingsVM.FinamToken)
                ? SettingsVM.FinamToken
                : !string.IsNullOrWhiteSpace(ApiToken)
                    ? ApiToken
                    : Environment.GetEnvironmentVariable("FINAM_TOKEN") ?? "";

            if (string.IsNullOrWhiteSpace(token))
            {
                AddLog("⚠ Токен Финам не указан. Укажите во вкладке Настройки.");
                return;
            }

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

            await _finamConnector.ConnectAsync(token);
            IsBrokerConnected = true;
            AddLog("✅ Финам подключён!");

            // Подписываемся на ВСЕ инструменты
            foreach (var instrument in Instruments)
            {
                await SubscribeToInstrument(instrument);
            }

            // Подписка на дефолтный инструмент в Стакане (котировки + стакан + свечи)
            await ResubscribeOrderBookInstrumentAsync(OrderBookVM.SelectedInstrument);

            // Подписка на обновления счёта (equity, позиции) через gRPC
            await _finamConnector.SubscribeAccountAsync((equity, positions) =>
            {
                App.Current?.Dispatcher.Invoke(() =>
                {
                    // Equity и баланс
                    MonitoringVM.Equity = equity;
                    if (MonitoringVM.InitialBalance == 0)
                        MonitoringVM.InitialBalance = equity;
                    
                    // PnL
                    MonitoringVM.PnLToday = equity - MonitoringVM.InitialBalance;
                    Balance = equity;

                    // Equity curve
                    MonitoringVM.AddEquityPoint(equity);

                    // Позиции
                    var openPositions = positions.Where(p => p.qty != 0).ToList();
                    MonitoringVM.OpenPositionsCount = openPositions.Count;
                    MonitoringVM.OpenPositions.Clear();
                    foreach (var pos in openPositions)
                    {
                        var position = new Position
                        {
                            Ticker = pos.symbol.Split('@')[0],
                            Direction = pos.qty > 0 ? SignalDirection.Buy : SignalDirection.Sell,
                            Entries = new List<PositionEntry>
                            {
                                new() { Price = pos.avgPrice, Volume = (int)Math.Abs(pos.qty) }
                            }
                        };
                        MonitoringVM.OpenPositions.Add(new PositionViewModel(position));
                    }
                });
            });

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
            // Свечи через gRPC стрим (фильтруем по выбранному инструменту)
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

            AddLog($"📊 gRPC свечи {ticker}");
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
            var tfMinutes = OrderBookVM.SelectedTimeframe switch
            {
                "1m" => 1, "5m" => 5, "15m" => 15, "1h" => 60, "4h" => 240, "D" => 1440, _ => 5
            };
            var days = tfMinutes <= 5 ? 3 : tfMinutes <= 60 ? 7 : 30;
            var history = await _finamConnector.GetHistoricalCandlesAsync(
                ticker, TimeSpan.FromMinutes(tfMinutes), DateTime.UtcNow.AddDays(-days), DateTime.UtcNow);
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

    // ConnectFinamAsync — единственная версия выше (строка ~255)

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

        // Кнопка 🔄 в стакане — переподписка
        OrderBookVM.SubscribeBookRequested += async (ticker) =>
        {
            await ResubscribeOrderBookInstrumentAsync(ticker);
        };
        OrderBookVM.TimeframeChangeRequested += async (tf) =>
        {
            if (_finamConnector != null && _finamConnector.IsConnected)
            {
                try { await LoadHistoricalCandles(OrderBookVM.SelectedInstrument); }
                catch (Exception ex) { AddLog($"⚠️ Смена TF: {ex.Message}"); }
            }
            else if (_hubClient != null && IsServerConnected)
            {
                try { await _hubClient.GetCandlesAsync(OrderBookVM.SelectedInstrument, tf); } catch { }
            }
        };

        // При смене инструмента на вкладке Стакан — переподписываемся
        OrderBookVM.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == nameof(OrderBookVM.SelectedInstrument) && !string.IsNullOrEmpty(OrderBookVM.SelectedInstrument))
            {
                _ = ResubscribeOrderBookInstrumentAsync(OrderBookVM.SelectedInstrument);
            }
        };
    }

    private CancellationTokenSource? _orderBookCts;

    /// <summary>Переподписка на котировки + стакан + свечи при смене инструмента в Стакане</summary>
    private async Task ResubscribeOrderBookInstrumentAsync(string ticker)
    {
        // Отменяем предыдущие подписки на стакан/котировки
        _orderBookCts?.Cancel();
        _orderBookCts = new CancellationTokenSource();
        var ct = _orderBookCts.Token;

        // Очищаем стакан при смене
        App.Current?.Dispatcher.Invoke(() =>
        {
            OrderBookVM.OrderBookRows.Clear();
            OrderBookVM.LastPrice = 0;
            OrderBookVM.BestBid = 0;
            OrderBookVM.BestAsk = 0;
            OrderBookVM.SpreadValue = 0;
        });

        try
        {
            if (_finamConnector?.IsConnected == true)
            {
                // 1. Котировки gRPC (только для этого инструмента, отменяется при смене)
                await _finamConnector.SubscribeQuotesAsync(ticker, (bid, ask, last) =>
                {
                    if (ct.IsCancellationRequested) return;
                    // Пропускаем нулевые значения (нет активных котировок)
                    if (last <= 0) return;
                    App.Current?.Dispatcher.Invoke(() =>
                    {
                        if (OrderBookVM.SelectedInstrument != ticker) return;
                        OrderBookVM.LastPrice = last;
                        if (bid > 0) OrderBookVM.BestBid = bid;
                        if (ask > 0) OrderBookVM.BestAsk = ask;
                        if (bid > 0 && ask > 0) OrderBookVM.SpreadValue = ask - bid;
                    });
                }, ct);

                // 2. Стакан gRPC (инкрементальный апдейт без мигания)
                _ = Task.Run(async () =>
                {
                    // Локальный снимок стакана (накапливаем incremental обновления)
                    var book = new SortedDictionary<double, (long bidVol, long askVol)>(Comparer<double>.Create((a, b) => b.CompareTo(a)));
                    try
                    {
                        await _finamConnector.SubscribeOrderBookAsync(ticker, (rows) =>
                        {
                            if (ct.IsCancellationRequested) return;

                            // Обновляем локальный снимок
                            foreach (var (price, bidVol, askVol) in rows)
                            {
                                if (bidVol == 0 && askVol == 0)
                                    book.Remove(price);
                                else
                                    book[price] = (bidVol, askVol);
                            }

                            // Отправляем в UI
                            App.Current?.Dispatcher.BeginInvoke(() =>
                            {
                                if (OrderBookVM.SelectedInstrument != ticker) return;

                                // Ограничиваем стакан ±25 уровней от спреда (не нужно скроллить)
                                var allRows = book.ToList();
                                // Находим границу bid/ask (первый bid сверху)
                                int spreadIdx = allRows.FindIndex(r => r.Value.bidVol > 0);
                                if (spreadIdx < 0) spreadIdx = allRows.Count / 2;
                                int showAbove = 25; // асков сверху
                                int showBelow = 25; // бидов снизу
                                int startIdx = Math.Max(0, spreadIdx - showAbove);
                                int endIdx = Math.Min(allRows.Count, spreadIdx + showBelow);
                                var snapshot = allRows.GetRange(startIdx, endIdx - startIdx);
                                long maxVol = snapshot.Count > 0
                                    ? Math.Max(
                                        snapshot.Max(r => r.Value.bidVol),
                                        snapshot.Max(r => r.Value.askVol))
                                    : 1;
                                if (maxVol == 0) maxVol = 1;

                                // Smart update: совмещаем с существующими строками
                                // Если кол-во строк сильно изменилось — перестроить
                                if (Math.Abs(OrderBookVM.OrderBookRows.Count - snapshot.Count) > 10
                                    || OrderBookVM.OrderBookRows.Count == 0)
                                {
                                    OrderBookVM.OrderBookRows.Clear();
                                    foreach (var (price, (bidVol, askVol)) in snapshot)
                                    {
                                        OrderBookVM.OrderBookRows.Add(new OrderBookRowViewModel
                                        {
                                            Price = price, BidVolume = bidVol, AskVolume = askVol,
                                            BidBarWidth = (double)bidVol / maxVol * 80,
                                            AskBarWidth = (double)askVol / maxVol * 80,
                                            IsLastPrice = Math.Abs(price - OrderBookVM.LastPrice) < 1
                                        });
                                    }
                                    // Авто-скролл к спреду
                                    OrderBookVM.NotifyScrollNeeded();
                                }
                                else
                                {
                                    // In-place update существующих строк
                                    for (int i = 0; i < Math.Min(snapshot.Count, OrderBookVM.OrderBookRows.Count); i++)
                                    {
                                        var row = OrderBookVM.OrderBookRows[i];
                                        var (price, (bidVol, askVol)) = snapshot[i];
                                        row.Price = price;
                                        row.BidVolume = bidVol;
                                        row.AskVolume = askVol;
                                        row.BidBarWidth = (double)bidVol / maxVol * 80;
                                        row.AskBarWidth = (double)askVol / maxVol * 80;
                                        row.IsLastPrice = Math.Abs(price - OrderBookVM.LastPrice) < 1;
                                    }
                                }
                            });
                        }, ct);
                    }
                    catch (OperationCanceledException) { }
                    catch (Exception ex)
                    {
                        App.Current?.Dispatcher.Invoke(() => AddLog($"⚠️ Стакан {ticker}: {ex.Message}"));
                    }
                }, ct);

                // 3. Исторические свечи
                await LoadHistoricalCandles(ticker);

                AddLog($"📊 Переключено на {ticker}");
            }
            else if (_hubClient != null && IsServerConnected)
            {
                await _hubClient.SubscribeOrderBookAsync(ticker);
                await _hubClient.GetCandlesAsync(ticker, OrderBookVM.SelectedTimeframe);
            }
        }
        catch (Exception ex)
        {
            App.Current?.Dispatcher.Invoke(() => AddLog($"⚠️ Переключение {ticker}: {ex.Message}"));
        }
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
