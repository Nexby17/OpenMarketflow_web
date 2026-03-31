using System.Collections.Specialized;
using System.Windows;
using System.Windows.Controls;
using HedgeFund.UI.ViewModels;

namespace HedgeFund.UI;

public partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        
        Loaded += OnLoaded;
    }

    private void OnLoaded(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
        {
            // Автопрокрутка лога вниз
            ((INotifyCollectionChanged)vm.LogEntries).CollectionChanged += (_, _) =>
            {
                if (LogListBox.Items.Count > 0)
                    LogListBox.ScrollIntoView(LogListBox.Items[^1]);
            };

            // PasswordBox → ViewModel (PasswordBox не поддерживает Binding)
            TokenBox.PasswordChanged += (_, _) =>
            {
                vm.ApiToken = TokenBox.Password;
            };

            // Если токен уже загружен из ENV, показать маску
            if (!string.IsNullOrEmpty(vm.ApiToken))
            {
                TokenBox.Password = vm.ApiToken;
            }

            vm.AddLog("OpenMarketflow загружен. Выберите параметры и нажмите СТАРТ.");
        }
    }
}
