using System.Globalization;
using System.Windows.Data;

namespace HedgeFund.UI.Converters;

/// <summary>Инвертирует bool (для блокировки элементов при IsRunning)</summary>
public class InverseBoolConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        => value is bool b ? !b : value;

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => value is bool b ? !b : value;
}
