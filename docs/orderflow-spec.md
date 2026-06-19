# 📊 Order Flow Strategy — Финальная спецификация v3

_Утверждена: 19.06.2026_

## Концепция
Анализ реального потока сделок и стакана. Три сигнала на вход, усреднение + пирамидинг, четыре условия выхода + частичный TP (LIFO). Не использует производные индикаторы — только реальные сделки и лимитные заявки.

## Инструмент
- **Тикер:** SIU6 (SI фьючерс $/руб)
- **Таймфрейм:** настраиваемый (default M5)
- **Комиссия:** 0.90₽ RT

## Источники данных (Finam gRPC)
| Поток | Метод | Что даёт |
|-------|-------|----------|
| Trades | SubscribeLatestTrades | deal: price, size, side (BUY/SELL), timestamp |
| OrderBook | SubscribeOrderBook | стакан: price→buy_size, sell_size, action |
| Bars | SubscribeBars | OHLCV, ATR, уровни |
| Quotes | SubscribeQuote | Bid/Ask/Last для исполнения |

## Расчётные метрики
### Из Trades (на каждый бар):
- buy_volume, sell_volume, delta, delta_ratio
- CVD (кумулятивная, сброс 07:00 МСК)
### Из OrderBook:
- imbalance, wall detection (size > 3× avg)
### Из Bars:
- ATR(14), экстремумы за N баров

## Сигналы
A. Absorption: |delta_ratio| ≥ 0.35 + |price_change| < 0.3×ATR
B. CVD Divergence: цена = новый экстремум, CVD не подтверждает
C. OrderBook Imbalance: |imbalance| ≥ 0.50 + wall

## Параметры
Lots, MaxPyramidLevels(5), MaxAverageLevels(100), StepAverage(50), StepPyramid(35),
Spread(50), PartialTP(true), StopLossMode(rub/pct/pts), StopLossValue(7000),
MarginPerLot(7000), MinProfitPerLot(30), MaxHoldMinutes(999),
AbsorptionThreshold(0.35), CVDLookback(10), OBImbalanceThreshold(0.50),
SignalConfirmCount(1), VPFilter(false), ATRPeriod(14), Timeframe(M5), StepATR(false)

## Entry
1. Сигналов ≥ SignalConfirmCount
2. Нет позиции, Running, не ночь/клиринг
3. Market order, Lots контрактов
4. Entry lock 60 сек
5. Broker sync ПЕРЕД входом

## Усреднение (против позиции)
averageLevels < MaxAverageLevels + цена ушла ≥ StepAverage ПРОТИВ → BUY/SELL Lots

## Пирамидинг (по тренду)
pyramidLevels < MaxPyramidLevels + цена ушла ≥ StepPyramid В СТОРОНУ + PnL > 0 → BUY/SELL Lots

## Выход
- PartialTP (LIFO): PnL последнего лота ≥ Spread → закрыть 1 лот
- StopLoss: rub/pct/pts
- Обратный сигнал → CloseAll
- TP: PnL/totalLots ≥ MinProfitPerLot → CloseAll
- Timeout: HoldMinutes ≥ MaxHoldMinutes → CloseAll
- Re-entry lock: 60 сек после CloseAll

## State Persistence
dir, entryPrice, avgPrice, totalLots, averageLevels, pyramidLevels,
lastAveragePrice, lastPyramidPrice, roundTrips, realizedPnL, entryTime, signalType
lotQueue — только в RAM (не персистить)
