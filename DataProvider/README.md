# Finam DataProvider

Сервис-прокси для рыночных данных Finam Trade API. Подписывается на gRPC-потоки (котировки, свечи, заявки) и раздаёт данные по REST на localhost.

## Зачем

Несколько C# роботов опрашивают Finam REST API → 429 rate limits. DataProvider подписывается один раз через gRPC и кэширует данные, роботы забирают через localhost REST без лимитов.

## Запуск

```bash
export FINAM_TOKEN="your-long-token"
python3 main.py
```

Или через systemd:
```bash
cp dataprovider.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dataprovider
```

## REST API

| Endpoint | Описание |
|---|---|
| `GET /status` | Статус + подписки |
| `GET /quote/{symbol}` | Последняя котировка (bid, ask, last) |
| `GET /candles/{symbol}?tf=M5&limit=100` | Кэшированные свечи |
| `GET /orders?account=1225953` | Активные заявки |
| `GET /position?account=1225953&ticker=SiM6@RTSX` | Текущая позиция |
| `POST /subscribe/{symbol}?tf=M5` | Подписка на свечи |
| `POST /unsubscribe/{symbol}?tf=M5` | Отписка от свечей |

## Конфигурация

`config.json`:
```json
{
  "port": 5060,
  "account_id": "1225953",
  "symbols": ["SiM6@RTSX", "RIM6@RTSX", "MXM6@RTSX"],
  "timeframes": ["M1", "M5"],
  "candle_cache_size": 500
}
```

Env-переменные (переопределяют config): `DP_PORT`, `DP_ACCOUNT_ID`, `DP_SYMBOLS`, `DP_TIMEFRAMES`, `DP_CANDLE_CACHE_SIZE`.

## Формат symbol

`ticker@MIC` — например `SiM6@RTSX` (фьючерсы), `SBER@MISX` (акции).
