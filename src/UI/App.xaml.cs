using System.Windows;

namespace HedgeFund.UI;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        
        // Глобальный обработчик ошибок
        DispatcherUnhandledException += (sender, args) =>
        {
            MessageBox.Show(
                $"Ошибка: {args.Exception.Message}\n\n{args.Exception.StackTrace}", 
                "OpenMarketflow — Ошибка", 
                MessageBoxButton.OK, 
                MessageBoxImage.Error);
            args.Handled = true;
        };
    }
}
