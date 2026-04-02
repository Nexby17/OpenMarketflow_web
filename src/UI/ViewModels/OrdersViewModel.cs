using System.Collections.ObjectModel;
using System.Windows.Input;
using HedgeFund.Core.Models;

namespace HedgeFund.UI.ViewModels;

/// <summary>ViewModel управления заявками — просмотр, редактирование, удаление</summary>
public class OrdersViewModel : BaseViewModel
{
    public OrdersViewModel()
    {
        CancelOrderCommand = new RelayCommand<OrderViewModel>(CancelOrder);
        ModifyOrderCommand = new RelayCommand<OrderViewModel>(ModifyOrder);
        CancelAllCommand = new RelayCommand(CancelAll);
        RefreshCommand = new RelayCommand(async () => await RefreshOrdersAsync());
    }

    // === Коллекции ===
    public ObservableCollection<OrderViewModel> ActiveOrders { get; } = new();

    // === События для MainViewModel ===
    public event Func<string, Task>? CancelOrderRequested;
    public event Func<string, double, int, Task>? ModifyOrderRequested;
    public event Func<Task>? CancelAllRequested;
    public event Func<Task>? RefreshRequested;

    // === Статистика ===
    private int _activeCount;
    public int ActiveCount { get => _activeCount; set => SetField(ref _activeCount, value); }

    private int _filledTodayCount;
    public int FilledTodayCount { get => _filledTodayCount; set => SetField(ref _filledTodayCount, value); }

    // === Команды ===
    public ICommand CancelOrderCommand { get; }
    public ICommand ModifyOrderCommand { get; }
    public ICommand CancelAllCommand { get; }
    public ICommand RefreshCommand { get; }

    // === Методы ===

    private async void CancelOrder(OrderViewModel? order)
    {
        if (order == null) return;
        if (CancelOrderRequested != null)
            await CancelOrderRequested.Invoke(order.BrokerOrderId);
    }

    private async void ModifyOrder(OrderViewModel? order)
    {
        if (order == null || !order.IsModified) return;
        if (ModifyOrderRequested != null)
            await ModifyOrderRequested.Invoke(order.BrokerOrderId, order.Price, order.Volume);
        // Обновляем original values
        order.OriginalPrice = order.Price;
        order.OriginalVolume = order.Volume;
    }

    private async void CancelAll()
    {
        if (CancelAllRequested != null)
            await CancelAllRequested.Invoke();
    }

    private async Task RefreshOrdersAsync()
    {
        if (RefreshRequested != null)
            await RefreshRequested.Invoke();
    }

    /// <summary>Обновить список ордеров (вызывается из MainViewModel)</summary>
    public void UpdateOrders(IEnumerable<OrderViewModel> orders)
    {
        ActiveOrders.Clear();
        int active = 0;
        foreach (var o in orders)
        {
            ActiveOrders.Add(o);
            if (o.Status is "Active" or "PartiallyFilled")
                active++;
        }
        ActiveCount = active;
    }

    /// <summary>Обновить конкретный ордер по событию</summary>
    public void UpdateOrder(Order order)
    {
        var existing = ActiveOrders.FirstOrDefault(o => o.BrokerOrderId == order.BrokerOrderId || o.Id == order.Id);
        if (existing != null)
        {
            existing.Status = order.Status.ToString();
            existing.FilledVolume = order.FilledVolume;
            existing.Price = order.Price;
            existing.OriginalPrice = order.Price;
            // Удаляем исполненные/отменённые
            if (order.Status is OrderStatus.Filled or OrderStatus.Cancelled or OrderStatus.Rejected)
            {
                ActiveOrders.Remove(existing);
            }
        }
        else if (order.Status is OrderStatus.Active or OrderStatus.Pending or OrderStatus.PartiallyFilled)
        {
            ActiveOrders.Add(OrderViewModel.FromOrder(order));
        }
        ActiveCount = ActiveOrders.Count(o => o.Status is "Active" or "PartiallyFilled");
    }
}

/// <summary>RelayCommand с параметром</summary>
public class RelayCommand<T> : ICommand
{
    private readonly Action<T?> _execute;
    private readonly Func<T?, bool>? _canExecute;

    public RelayCommand(Action<T?> execute, Func<T?, bool>? canExecute = null)
    {
        _execute = execute;
        _canExecute = canExecute;
    }

    public event EventHandler? CanExecuteChanged
    {
        add => CommandManager.RequerySuggested += value;
        remove => CommandManager.RequerySuggested -= value;
    }

    public bool CanExecute(object? parameter) => _canExecute?.Invoke((T?)parameter) ?? true;
    public void Execute(object? parameter) => _execute((T?)parameter);
}
