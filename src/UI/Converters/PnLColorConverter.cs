using System.Globalization;
using System.Windows.Data;
using System.Windows.Media;

namespace HedgeFund.UI.Converters;

/// <summary>PnL → зелёный/красный/нейтральный</summary>
public class PnLColorConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is double pnl)
        {
            if (pnl > 0) return new SolidColorBrush(Color.FromRgb(34, 197, 94));   // green
            if (pnl < 0) return new SolidColorBrush(Color.FromRgb(239, 68, 68));   // red
        }
        return new SolidColorBrush(Color.FromRgb(228, 228, 240)); // neutral
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotImplementedException();
}

/// <summary>bool → "ВКЛ"/"ВЫКЛ"</summary>
public class BoolToOnOffConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        => value is true ? "ВКЛ" : "ВЫКЛ";

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotImplementedException();
}

/// <summary>string hex color → SolidColorBrush</summary>
public class StringToBrushConverter : IValueConverter
{
    public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
    {
        if (value is string hex)
        {
            try { return (SolidColorBrush)new BrushConverter().ConvertFromString(hex)!; }
            catch { }
        }
        return new SolidColorBrush(Colors.White);
    }

    public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        => throw new NotImplementedException();
}
