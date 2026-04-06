# Альфа-Инвестиции PRO WebSocket API v5

## Подключение
- Протокол: WebSocket
- Endpoint: `ws://127.0.0.1:3366/router/`
- Требование: терминал Альфа-Директ должен быть запущен

## Маршрутизация
Роутер оперирует каналами (Channel). Команды:
- `listen` — подписка на шину
- `unlisten` — отписка
- `broadcast` — отправка на шину
- `register` — регистрация сервиса
- `unregister` — отмена регистрации
- `request` — запрос сервису
- `response` — ответ сервиса

## Формат сообщений
```json
{
  "Command": "string",
  "Channel": "string",
  "Payload": "string? (JSON as string)",
  "Id": "string?"
}
```

## Ключевые каналы
- `#Data.Query` — подписка на данные
- `#Data.Bus.<EntityType>` — получение обновлений данных
- `#ConnectionState.Bus` — состояние терминала
- `#Order.Enter.Query` — выставление заявки
- `#Order.Cancel.Query` — снятие заявки
- `#Order.Limit.Query` — лимит-сервис
- `#Archive.Query` — архивные данные (свечи)
- `#Archive.Bus.OHLCV.<idFi>.<TF>` — реалтайм свечи

## Сущности
- `AssetInfoEntity` — инструменты (Ticker, IdObject, IdFi, IdMarketBoard)
- `FinInfoLastEntity` — цена последней сделки (Last, Bid, Ask, High, Low)
- `FinInfoOrderBookEntity` — стакан (Bid, Ask, BidQty, AskQty)
- `OrderBookEntity` — полный стакан (Lines: Price, BuyQty, SellQty)
- `OrderEntity` — заявки
- `ClientPositionEntity` — позиции
- `ClientBalanceEntity` — баланс
- `AllTradeEntity` — лента сделок
- `ClientAccountEntity` — счёт
- `ClientSubAccountEntity` — субсчёт
- `SubAccountRazdelEntity` — портфель
- `AllowedOrderParamEntity` — комбинации параметров заявок

## Выставление заявки
Канал: `#Order.Enter.Query`
```json
{
  "IdAccount": "int",
  "IdSubAccount": "int",
  "IdRazdel": "int",
  "IdPriceControlType": "3",
  "IdObject": "int (из AssetInfoEntity)",
  "LimitPrice": "double",
  "BuySell": "1=buy, -1=sell",
  "Quantity": "int (в штуках)",
  "Comment": "string",
  "IdAllowedOrderParams": "int"
}
```

## Снятие заявки
Канал: `#Order.Cancel.Query`
```json
{
  "IdAccount": "int",
  "IdSubAccount": "int",
  "IdRazdel": "int",
  "NumEDocumentBase": "int64"
}
```

## SBER пример
- IdObject: 285270
- IdFi: 144950 (MICEX, IsLiquid=true)
- IdMarketBoard: 92
- IdObjectGroup: 1 (Stocks)
