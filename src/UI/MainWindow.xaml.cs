using System.Collections.Specialized;
using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using HedgeFund.Core.Models;
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

            // Подписка на обновление свечей для перерисовки графика
            vm.OrderBookVM.PropertyChanged += OrderBookVM_PropertyChanged;
        }
    }

    private void OrderBookVM_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(OrderBookViewModel.Candles) || e.PropertyName == nameof(OrderBookViewModel.RefreshTick))
        {
            if (DataContext is MainViewModel vm && vm.OrderBookVM.Candles.Count > 0)
                DrawChart(vm.OrderBookVM.Candles, vm.OrderBookVM.ChartZoom, vm.OrderBookVM.ChartScrollOffset);
        }
    }

    private void ChartCanvas_MouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (DataContext is MainViewModel vm)
        {
            vm.OrderBookVM.ChartZoom += e.Delta > 0 ? 0.2 : -0.2;
            if (vm.OrderBookVM.Candles.Count > 0)
                DrawChart(vm.OrderBookVM.Candles, vm.OrderBookVM.ChartZoom, vm.OrderBookVM.ChartScrollOffset);
        }
    }

    /// <summary>Рисуем свечной график с кластерным объёмом на Canvas</summary>
    private void DrawChart(List<ClusterCandle> candles, double zoom, double scrollOffset)
    {
        var canvas = ChartCanvas;
        canvas.Children.Clear();

        if (candles.Count == 0) return;

        double w = canvas.ActualWidth;
        double h = canvas.ActualHeight;
        if (w < 10 || h < 10) return;

        double chartH = h * 0.75;  // 75% для свечей
        double volH = h * 0.20;    // 20% для объёма
        double volTop = chartH + h * 0.05;

        double candleW = Math.Max(2, 8 * zoom);
        double gap = Math.Max(1, 2 * zoom);
        double totalW = (candleW + gap) * candles.Count;

        // Показываем последние N свечей, влезающих на экран
        int visibleCount = Math.Max(1, (int)(w / (candleW + gap)));
        int startIdx = Math.Max(0, candles.Count - visibleCount);
        var visible = candles.Skip(startIdx).ToList();

        if (visible.Count == 0) return;

        double minPrice = visible.Min(c => c.Low);
        double maxPrice = visible.Max(c => c.High);
        double priceRange = maxPrice - minPrice;
        if (priceRange < 0.01) priceRange = 1;

        long maxVol = visible.Max(c => c.Volume);
        if (maxVol == 0) maxVol = 1;

        // Сетка цен
        var gridBrush = new SolidColorBrush(Color.FromArgb(40, 255, 255, 255));
        var textBrush = new SolidColorBrush(Color.FromRgb(150, 150, 170));
        int gridLines = 5;
        for (int g = 0; g <= gridLines; g++)
        {
            double py = chartH * g / gridLines;
            double price = maxPrice - (priceRange * g / gridLines);
            var line = new Line
            {
                X1 = 0, X2 = w, Y1 = py, Y2 = py,
                Stroke = gridBrush, StrokeThickness = 0.5,
                StrokeDashArray = new DoubleCollection { 4, 2 }
            };
            canvas.Children.Add(line);

            var label = new TextBlock
            {
                Text = price.ToString("F0"),
                FontSize = 9,
                Foreground = textBrush
            };
            Canvas.SetLeft(label, w - 50);
            Canvas.SetTop(label, py - 6);
            canvas.Children.Add(label);
        }

        // Свечи + объём
        var greenBrush = new SolidColorBrush(Color.FromRgb(34, 197, 94));
        var redBrush = new SolidColorBrush(Color.FromRgb(239, 68, 68));
        var greenTransBrush = new SolidColorBrush(Color.FromArgb(80, 34, 197, 94));
        var redTransBrush = new SolidColorBrush(Color.FromArgb(80, 239, 68, 68));
        var clusterBrush = new SolidColorBrush(Color.FromArgb(100, 59, 130, 246));

        for (int i = 0; i < visible.Count; i++)
        {
            var c = visible[i];
            double x = i * (candleW + gap);
            bool bull = c.IsBullish;
            var brush = bull ? greenBrush : redBrush;
            var volBrush = bull ? greenTransBrush : redTransBrush;

            // Тень (high-low)
            double yHigh = (maxPrice - c.High) / priceRange * chartH;
            double yLow = (maxPrice - c.Low) / priceRange * chartH;
            var wick = new Line
            {
                X1 = x + candleW / 2, X2 = x + candleW / 2,
                Y1 = yHigh, Y2 = yLow,
                Stroke = brush, StrokeThickness = 1
            };
            canvas.Children.Add(wick);

            // Тело (open-close)
            double yOpen = (maxPrice - c.Open) / priceRange * chartH;
            double yClose = (maxPrice - c.Close) / priceRange * chartH;
            double bodyTop = Math.Min(yOpen, yClose);
            double bodyH = Math.Max(1, Math.Abs(yOpen - yClose));
            var body = new System.Windows.Shapes.Rectangle
            {
                Width = candleW,
                Height = bodyH,
                Fill = bull ? brush : brush,
                Stroke = brush,
                StrokeThickness = 0.5
            };
            Canvas.SetLeft(body, x);
            Canvas.SetTop(body, bodyTop);
            canvas.Children.Add(body);

            // Объём
            double volBarH = (double)c.Volume / maxVol * volH;
            var volBar = new System.Windows.Shapes.Rectangle
            {
                Width = candleW,
                Height = Math.Max(1, volBarH),
                Fill = volBrush
            };
            Canvas.SetLeft(volBar, x);
            Canvas.SetTop(volBar, volTop + volH - volBarH);
            canvas.Children.Add(volBar);

            // Кластерный объём (VolumeProfile)
            if (c.VolumeProfile.Count > 0)
            {
                long maxCluster = c.VolumeProfile.Values.Max();
                if (maxCluster > 0)
                {
                    foreach (var (price, vol) in c.VolumeProfile)
                    {
                        double cy = (maxPrice - price) / priceRange * chartH;
                        double cw = (double)vol / maxCluster * candleW;
                        var cluster = new System.Windows.Shapes.Rectangle
                        {
                            Width = Math.Max(1, cw),
                            Height = Math.Max(1, chartH / priceRange * 0.5),
                            Fill = clusterBrush,
                            Opacity = 0.6
                        };
                        Canvas.SetLeft(cluster, x);
                        Canvas.SetTop(cluster, cy);
                        canvas.Children.Add(cluster);
                    }
                }
            }
        }

        // Последняя цена — горизонтальная линия
        if (visible.Count > 0)
        {
            var last = visible[^1];
            double yLast = (maxPrice - last.Close) / priceRange * chartH;
            var lastLine = new Line
            {
                X1 = 0, X2 = w, Y1 = yLast, Y2 = yLast,
                Stroke = new SolidColorBrush(Color.FromArgb(120, 255, 255, 255)),
                StrokeThickness = 1,
                StrokeDashArray = new DoubleCollection { 2, 2 }
            };
            canvas.Children.Add(lastLine);

            var lastLabel = new TextBlock
            {
                Text = last.Close.ToString("F0"),
                FontSize = 10,
                FontWeight = FontWeights.Bold,
                Foreground = new SolidColorBrush(Colors.White),
                Background = new SolidColorBrush(Color.FromArgb(180, 50, 50, 80)),
                Padding = new Thickness(4, 1, 4, 1)
            };
            Canvas.SetLeft(lastLabel, w - 55);
            Canvas.SetTop(lastLabel, yLast - 8);
            canvas.Children.Add(lastLabel);
        }
    }
}
