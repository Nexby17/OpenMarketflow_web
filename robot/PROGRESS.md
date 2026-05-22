# Python Trading Robot — Прогресс

**Дата старта:** 22.05.2026
**Архитектурный план:** `memory/robot-architecture.md`

## Модули (8/8 + API) — все готовы

| Модуль | Файл | Описание |
|--------|------|----------|
| config | `config.py` | Symbol: SiM6@RTSX, Account: 1225953, TF: M1 |
| feed | `feed.py` | gRPC подписки (quotes, bars, orders, trades) через FinamPy |
| vp | `vp.py` | Volume Profile (POC, VAH, VAL, rolling window 33 баров) |
| state | `state.py` | JSON persistence, atomic write, corrupt recovery |
| orders | `orders.py` | Market/limit ордера через gRPC, fill tracking |
| strategy | `strategy.py` | VP Scalp Grid логика (чистые сигналы, без API) |
| risk | `risk.py` | PnL limits, lots limits, night/clearing time |
| orchestrator | `main.py` | Оркестратор: feed → VP → strategy → orders, paper mode |
| api | `api.py` | FastAPI: status, start, stop, pause, resume |

## Paper Trading — первые результаты (22.05, 10:49-12:28 UTC)

**7 завершённых сделок, все плюсовые:**

| # | Время MSK | Dir | Entry | Exit | PnL | Причина |
|---|-----------|-----|-------|------|-----|---------|
| 1 | 13:57-13:58 | LONG | 71749 | 71776 | +26 | POC hit |
| 2 | 14:04-14:18 | LONG→SHORT | 71851 | 71775 | +75 | POC hit SHORT |
| 3 | 14:18-14:18 | LONG | 71749 | 71800 | +50 | POC hit (6 сек!) |
| 4 | 14:18-14:19 | LONG | 71745 | 71775 | +29 | POC hit |
| 5 | 14:19-14:20 | SHORT | 71802 | 71775 | +26 | POC hit |
| 6 | 14:04-14:04 | LONG | ~71750 | 71775 | +28 | POC hit |
| 7 | 14:06-14:18 | SHORT | 71851 | 71775 | +75 | POC hit |

**Итого: ~309₽ paper profit за 1.5 часа**

**Наблюдения:**
- Сделки очень быстрые (6 сек - 12 мин) — цена мечется вокруг POC
- Grid levels НЕ достигались — рынок не уходил далеко от entry
- VA слишком широкая (VAL-VAH = 100-150 пт) → много мелких входов
- POC=71775 был стабилен весь день — цена возвращалась к нему

## Найденные и пофикшенные баги

### 1. client_order_id > 20 символов
**Симптом:** Finam rejects `RBT-ENTRY-LONG-1779446010933` (30 chars)
**Фикс:** 13-digit timestamp без префикса
**Файл:** `orders.py`

### 2. Entry spam при ошибке
**Симптом:** При ошибке entry робот спамил ордера каждую секунду
**Фикс:** skip_ticks=30 (success), 60 (failure)
**Файл:** `main.py`

### 3. Quote last=0
**Симптом:** Finam quote иногда не имеет `last` field → entry @ 0
**Фикс:** Fallback to mid=(bid+ask)/2; guard `q.last > 0` before entry check
**Файл:** `feed.py`, `main.py`

### 4. Entry только по bar (M1)
**Симптом:** Entry signal проверялся только при новом M1 баре (раз в минуту)
**Фикс:** Entry check и по quote — цена может прыгнуть за VA между барами
**Файл:** `main.py`

### 5. gRPC streams умирают после клиринга
**Симптом:** После клиринга (13:59-14:06 MSK) gRPC streams перестают отдавать данные. Thread жив, callback не вызывается. Робот "слепой".
**Причина:** Finam server-side streams молча закрываются. Нет keepalive/heartbeat в gRPC streams.
**Фикс:** Watchdog thread — если 60 сек нет quotes И bars → disconnect + reconnect. Обновляются `_last_quote_ts`/`_last_bar_ts` в callbacks.
**Файл:** `feed.py` (watchdog loop), `main.py` (`_on_stale_streams` reconnect)

### 6. import time забыт в feed.py
**Симптом:** NameError при watchdog startup
**Фикс:** `import time` добавлен
**Файл:** `feed.py`

## systemd сервис

**Файл:** `/etc/systemd/system/trading-robot.service`
**Команды:**
```
systemctl start trading-robot    # Запуск (paper mode по умолчанию)
systemctl stop trading-robot     # Остановка
systemctl restart trading-robot  # Перезапуск
journalctl -u trading-robot -f   # Логи в реальном времени
```
**ExecStart:** `python3 main.py --paper` (paper mode по умолчанию)
**Restart:** on-failure, 10 sec delay
**Зависимость:** After=dataprovider.service

## Архитектурные решения

- **gRPC push events** (не REST polling) — нулевая задержка
- **Чистая стратегия** (strategy.py = сигналы, main.py = execution)
- **Paper mode** (`--paper` flag) — логирует сигналы без реальных ордеров, симулирует fills
- **Atomic state** (os.replace для JSON)
- **Entry pending lock** (30 sec)
- **Broker = source of truth** (sync on start)
- **Watchdog** (60 sec stale → auto-reconnect)

## Следующие шаги

1. [ ] Накопить статистику paper trading (win rate, avg PnL, avg hold time)
2. [ ] UI интеграция (OpenMarketflow frontend — status/controls)
3. [ ] Config hot-update через API (параметры без рестарта)
4. [ ] Переход на реальную торговлю (убрать `--paper`)
5. [ ] Метрики: дневной PnL, round trips, win rate → в status API
