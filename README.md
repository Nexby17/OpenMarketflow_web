# ⚡ OpenMarketflow

Алгоритмический торговый робот для Московской биржи с поддержкой усреднения.

## Требования

- **Windows 10/11** (WPF)
- **.NET 8 SDK** — скачать: https://dotnet.microsoft.com/download/dotnet/8.0

## Быстрый старт

### Вариант 1: Через Visual Studio
1. Открыть `HedgeFund.sln`
2. ПКМ на `HedgeFund.UI` → **Set as Startup Project**
3. **F5** (или Ctrl+F5 без дебага)

### Вариант 2: Из командной строки
```bash
cd src/UI
dotnet run
```

### Вариант 3: Собрать .exe
```bash
dotnet publish src/UI/HedgeFund.UI.csproj -c Release -r win-x64 --self-contained
```
Готовый `OpenMarketflow.exe` будет в:
```
src/UI/bin/Release/net8.0-windows/win-x64/publish/
```

## Структура

```
src/
├── Core/           # Ядро: модели, индикаторы, стратегии, усреднение
├── Brokers/        # Коннекторы к брокерам (Альфа, Финам)
└── UI/             # WPF интерфейс
```

## Возможности

- 📊 Стратегии: Scalping (EMA+RSI), Breakout (BB+ATR), Spread Arbitrage
- ⚖️ Усреднение: Фикс / Мартингейл, настраиваемый лимит
- 🛡️ Стоп-лосс: вкл/выкл, в пунктах или %
- 🌙 Тёмная тема
