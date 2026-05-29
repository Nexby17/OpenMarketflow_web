# VP Scalp Grid Robot

Автономный торговый робот для фьючерса SI ($/руб) на Московской бирже через Finam Trade API. Стратегия Volume Profile Scalp Grid — вход при выходе цены за Value Area, усреднение через сетку лимитных ордеров, выход по POC или PnL/lot.

## Архитектура

```
gRPC Push Events (FinamPy)
         │
    ┌────▼────┐
    │ feed.py │ ← quotes, bars, orders, trades
    └────┬────┘
         │
    ┌────▼────┐
    │  vp.py  │ → POC, VAH, VAL (Volume Profile)
    └────┬────┘
         │
    ┌────▼────────┐
    │ strategy.py │ → Entry/Grid/TP/Close сигналы
    └────┬────────┘
         │
    ┌────▼────┐     ┌──────────┐
    │ main.py │────▶│ orders.py│ → gRPC PlaceOrder/Cancel
    └────┬────┘     └──────────┘
         │
    ┌────▼────┐     ┌─────────┐
    │ risk.py │     │state.py │ → JSON persistence
    └─────────┘     └─────────┘
```

**Один процесс, gRPC push events, событийная архитектура.** Никакого REST polling.

## Стратегия: VP Scalp Grid

### Вход
- M1 бары → Volume Profile (33 бара, bin=50 пт, VA=70%)
- Цена < VAL → LONG
- Цена > VAH → SHORT

### Управление позицией
- Entry: 1 лот market
- Grid: limit ордера через step пт против позиции (усреднение)
- TP: limit ордера +spread пт от grid fill
- Max grid уровней: 100

### Выход
- **1 лот:** POC hit → close all безусловно
- **2+ лота:** PnL/lot ≥ MinProfitPerLot → close all
- **Timeout:** MaxHoldMinutes → close all

### Параметры по умолчанию
| Параметр | Значение | Описание |
|----------|----------|----------|
| max_levels | 100 | Макс grid уровней |
| step_base | 31 | Шаг grid (пт) |
| spread_base | 31 | TP spread (пт) |
| max_hold_minutes | 999 | Таймаут позиции |
| min_profit_per_lot | 29 | Мин PnL/lot для exit |
| vp_lookback | 33 | Баров для VP |
| vp_bin_size | 50 | Размер бина (пт) |
| va_percent | 0.70 | Value Area % |

## Модули

### `config.py` — Конфигурация
- Symbol, account, timeframe, warmup bars
- Env vars: `FINAM_TOKEN`, `FINAM_ACCOUNT_ID`

### `feed.py` — gRPC подписки
- Quotes (bid/ask/last) в реальном времени
- Bars (M1) — закрытые бары
- Orders — статусы ордеров
- Trades — fills
- **Watchdog:** auto-reconnect через 60 сек без данных (после клиринга)

### `vp.py` — Volume Profile калькулятор
- Rolling window баров
- Бины по цене, кумулятивный объём
- POC = max volume bin, VA = top bins covering 70% volume

### `strategy.py` — Торговая логика
- Чистые сигналы (Signal dataclass), без API вызовов
- `check_entry(price)` → ENTRY LONG/SHORT
- `on_entry_fill()` → GRID-1 + POC-TP
- `on_grid_fill()` → next GRID + TP
- `check_exit()` → CLOSE_ALL

### `orders.py` — Ордера через gRPC
- Market и limit ордера
- Cancel, cancel_all
- Fill tracking
- client_order_id ≤ 20 символов (Finam limit)

### `state.py` — State persistence
- JSON, atomic write (os.replace)
- Recovery после краша
- Tracked orders: add/update/remove/filter

### `risk.py` — Risk manager
- Max loss: -7000₽
- Max lots: 101
- Night mode: 23:50-07:00 MSK
- Clearing: 13:59-14:06 MSK

### `main.py` — Оркестратор
- Подключение, warmup VP, подписки, broker sync
- Paper mode (`--paper` flag)
- Entry pending lock (30 сек)
- Stale streams → auto-reconnect

### `api.py` — FastAPI web API
- `GET /status` — текущий статус
- `POST /start`, `/stop`, `/pause`, `/resume`
- `GET /health`

## Быстрый старт

### Требования
- Python 3.12+
- Finam Trade API токен
- `pip install finampy fastapi uvicorn`

### Paper trading (без реальных ордеров)
```bash
export FINAM_TOKEN="your-token"
export FINAM_ACCOUNT_ID="your-account-id"
python3 main.py --paper
```

### Реальная торговля
```bash
python3 main.py
```

### systemd сервис
```bash
# Установить
sudo cp trading-robot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-robot

# Запустить
sudo systemctl start trading-robot

# Логи
journalctl -u trading-robot -f
```

## Тесты

```bash
python3 test_vp.py         # Volume Profile на живых данных
python3 test_state.py      # State persistence
python3 test_strategy.py   # Торговые сигналы
python3 test_risk.py       # Risk manager
python3 test_orders.py     # gRPC ордера
python3 test_integration.py # 15 сек живого рынка
```

## Paper Trading результаты (22.05.2026)

7 сделок за 1.5 часа, все плюсовые:

| # | Dir | Entry | Exit | PnL | Hold |
|---|-----|-------|------|-----|------|
| 1 | LONG | 71749 | 71776 | +26₽ | ~1 мин |
| 2 | SHORT | 71851 | 71775 | +75₽ | ~12 мин |
| 3 | LONG | 71749 | 71800 | +50₽ | 6 сек |
| 4 | LONG | 71745 | 71775 | +29₽ | ~1 мин |
| 5 | SHORT | 71802 | 71775 | +26₽ | ~1 мин |
| 6 | LONG | ~71750 | 71775 | +28₽ | ~1 мин |
| 7 | SHORT | 71851 | 71775 | +75₽ | ~12 мин |

**Итого: ~309₽, Win Rate: 100%**

## Известные баги (исправленные)

1. **client_order_id > 20 chars** — Finam reject. Fix: 13-digit timestamp
2. **Entry spam on error** — skip_ticks=30/60
3. **Quote last=0** — fallback to mid=(bid+ask)/2
4. **Entry только по bar** — добавлена проверка по quote
5. **gRPC streams die after clearing** — watchdog auto-reconnect
6. **import time забыт** — добавлен в feed.py

## Безопасность

- **Paper mode по умолчанию** в systemd
- Stop-loss: -7000₽
- Max lots: 101
- Night mode: нет торговли 23:50-07:00 MSK
- Clearing: нет торговли 13:59-14:06 MSK
- Atomic state: corrupt recovery → defaults
- Entry pending lock: 30 сек

## Лицензия

Private project.
