# VP Scalp Grid Robot — Прогресс

## Текущий статус: РЕАЛЬНЫЙ РЕЖИМ (не запущен)

**Дата перехода в real:** 26.05.2026
**Текущее состояние:** Остановлен по команде пользователя
**Версия API:** 2.0 (FastAPI + config hot-update)
**Версия Robot:** e8baa8f (Fix #1-5 applied)

## Paper Trading результаты

### 22.05.2026 (день 1)
- ~15 сделок, ~+800₽ total
- Найдены баги: grid level duplication, quote spike, PnL=price at entry=0
- Все пофикшены в v2

### 25.05.2026 (день 2)
- 19 закрытых сделок + 1 ушла в ночь
- 16/19 прибыльных (84% WR)
- Общий PnL: -4730₽ (из-за 3 рестартов с багами)
- **Без багов: +1317₽, 17/17 = 100% WR**
- Средний PnL прибыльной сделки: +48₽

### Сделка #22 (ушла в ночь 25→26.05)
- SHORT @ 72603, набрано 19 grid уровней
- 17 из 19 TP заполнились корректно
- Закрылась по Timeout (999 мин) с PnL=-720₽
- Grid levels восстановились из state после ночи ✅

## Исправленные баги

### Баг 1: Рестарт закрывает позицию
- **Симптом:** `systemctl restart` → stop(close_position=True) → Close All с убытком
- **Фикс:** stop(close_position=False) по умолчанию, Ctrl+C → stop(True)
- **Файл:** main.py, строка ~105

### Баг 2: TP fill не уменьшает total_lots
- **Симптом:** 12 grid, 7 TP заполнились, но total_lots=13 → exit никогда не сработает
- **Фикс:** on_tp_fill → статус CLOSED, total_lots/open_levels считают только открытые, calc_unrealized_pnl учитывает только открытые лоты, realized_pnl копит закрытые
- **Файл:** strategy.py — GridLevel (добавлено tp_closed_price), on_tp_fill, total_lots, open_levels, realized_pnl, calc_unrealized_pnl

### Баг 3: Grid levels не сохраняются в state
- **Симптом:** После рестарта grid_levels=[] → теряет все TP/pending ордера
- **Фикс:** serialize_grid/restore_grid в state.py, _save_state/main.py
- **Файлы:** state.py (grid_levels field), strategy.py (serialize/restore), main.py (save/restore)

### Баг 4: Watchdog не ловит замороженную цену
- **Симптом:** Finam шлёт quotes с одной ценой в выходные → watchdog думает что всё ок
- **Статус:** НЕ ФИКСШЕН. Ручной рестарт 25.05 решил проблему

### Баг 5: Grid ордера отменяли предыдущий (FIXED 26.05)
- **Симптом:** `_place_grid()` cancel предыдущий grid → потеря уровня
- **Фикс:** `_grid_order_ids: dict[str, int]` — каждый уровень независим, не отменяется

### Баг 6: TP matching list (FIXED 26.05)
- **Симптом:** `_tp_order_ids: list` — ненадёжный matching при нескольких TP
- **Фикс:** `_tp_order_ids: dict[str, int]` (order_id → level)

### Баг 7: Entry pending бесконечный (FIXED 26.05)
- **Симптом:** Ордер не заполнился → `_entry_pending` висит вечно
- **Фикс:** Проверка в `_on_quote()`: если > 30 сек → cancel + reset

### Баг 8: Нет периодического broker sync (FIXED 26.05)
- **Симптом:** Позиция рассинхронизируется, sync только при start/resume
- **Фикс:** Каждые 60 сек в `_on_quote()` → `_sync_broker()`

### Баг 9: POC-TP без проверки (FIXED 26.05)
- **Симптом:** LONG entry + POC < entry → TP с убытком
- **Фикс:** Sanity check: LONG+POC>entry или SHORT+POC<entry

## Стратегия: VP Scalp Grid

**Логика:**
1. Entry LONG когда price < VAL, SHORT когда price > VAH
2. Grid: step=31 пт против позиции, max 100 уровней
3. Каждый grid level имеет TP = grid_price ± spread(31)
4. Exit: 1 лот → POC hit, 2+ лота → PnL/lot ≥ 29₽
5. Timeout: 999 мин
6. Стоп-лосс: unrealized PnL ≤ -7000₽

**Параметры:**
- max_levels: 100
- step_base: 31
- spread_base: 31
- max_hold_minutes: 99999999999999 (timeout отключен)
- min_profit_per_lot: 29
- commission: 0.90₽ RT
- vp_lookback: 33
- vp_bin_size: 50
- vp_va_percent: 0.70
- rv_adaptation: False (флаг есть, логика не подключена)

**Риск-менеджмент:**
- Max loss: -7000₽
- Max lots: 101
- Ночной режим: 23:50-07:00 MSK (no entry)
- Клиринг: 13:59-14:06 MSK (no trade)

## Модули (HedgeFund/robot/)

| Файл | Описание |
|------|----------|
| config.py | Finam credentials, символ, таймфрейм |
| feed.py | gRPC подписки (quotes, bars, orders, trades) + QuoteFilter (0.2%) + Watchdog (60s) |
| vp.py | Volume Profile (POC, VAH, VAL, lookback=33, bin=50, va=70%) |
| state.py | JSON persistence, atomic write, grid_levels serialize/restore |
| orders.py | gRPC market/limit/cancel, fill tracking, client_order_id ≤ 20 chars |
| strategy.py | VP Scalp Grid, grid_levels list, TP fill → CLOSED, realized_pnl |
| risk.py | PnL limits, lots limits, night mode, clearing |
| main.py | Orchestrator, paper/real modes, entry guard, round trip logging |
| api.py | FastAPI v2.0: status/start/stop/pause/resume/health + config hot-update + grid-levels detail + active-strategies compat |

## Инфраструктура

- **systemd:** /etc/systemd/system/trading-robot.service (enabled)
- **State:** /tmp/robot-state.json
- **Логи:** journalctl -u trading-robot
- **Env:** HedgeFund/robot/.env (FINAM_TOKEN, FINAM_ACCOUNT_ID)
- **Режим:** REAL (--paper убран из ExecStart 26.05.2026)
- **Счёт:** 1225953
- **Инструмент:** SiM6@RTSX

## UI интеграция (26.05.2026)

### Выполнено
- [x] api.py v2.0 — config hot-update, grid-levels, active-strategies compat endpoint
- [x] main.py — uvicorn API server на порту 5070 (отдельный поток)
- [x] Вкладка «Роботы» — Python Robot как строка в таблице (badge PYTHON)
- [x] Всегда виден — даже когда не запущен ("🔴 Не запущен" + кнопка ▶)
- [x] Вкладка «Стратегии» — VP Scalp Grid (Python) с badge PYTHON
- [x] Кнопки «💾 Сохранить» / «🔄 Загрузить» — hot-update параметров через API
- [x] Параметры по умолчанию: step=31, spread=31, min_profit=29, hold=99999999999999
- [x] Polling каждые 2 сек → обновление строки в таблице
- [x] Кнопки ▶⏸⏹ в таблице → robot API
- [x] **Все C# стратегии убраны из UI** (V7/V8, VP Copy, VP Simple, V8 Trail, оптимизация, создание робота)
- [x] **C# server strategies убраны из renderRobots()** — polling /api/active-strategies отключён
- [x] **Двойной клик** на Python robot → popup с полным статусом, параметрами, grid levels
- [x] **VP параметры добавлены в API**: vp_lookback, vp_bin_size, vp_va_percent
- [x] **RV Adaptation флаг** добавлен в StrategyParams + API + UI
- [x] **Анализ пути параметров**: JS→API→StrategyParams→VP sync — все 9 полей корректны
- [x] **Popup в стиле editRobot()** — card, card-header, metrics-row, metric-card, VP индикаторы live
- [x] **localStorage роботы убраны** из renderRobots() — только Python Robot
- [x] **Fallback дефолтов** — popup + Стратегии показывают дефолты если робот не запущен
- [x] **Fix #1-5**: grid multi-order, TP dict, entry timeout, broker sync, POC-TP sanity
- [x] **Проект реорганизован**: robot/tests/, backtest/src/archive/, _archive/, README.md обновлён
- [x] **Push в GitHub**: n0iz3on3/Vp_sc_grid_mm_robot (commit e8baa8f)
- [x] **C# proxy для systemctl** — `/api/robot/service/start|stop` через C# сервер (port 5050), whitelist в auth middleware
- [x] **JS robotApi() update** — start/stop идут через C# proxy когда робот мёртв, pause/resume через прямой API
- [x] **Broker position parsing fix** — `float('' or '0')` вместо `float('')` → нет crash на пустом value
- [x] **Fallback дефолтов** в pythonRobotLoadToPanel + pythonRobotLoadConfig — показывают defaults если API мёртв

### Параметры (9 полей)
| Поле | JS | API | StrategyParams | VP sync |
|------|-----|-----|----------------|--------|
| max_levels | ✅ | ✅ | ✅ grid limit | — |
| step_base | ✅ | ✅ | ✅ grid step | — |
| spread_base | ✅ | ✅ | ✅ TP spread | — |
| max_hold_minutes | ✅ | ✅ | ✅ timeout | — |
| min_profit_per_lot | ✅ | ✅ | ✅ exit cond | — |
| vp_lookback | ✅ | ✅ | ✅ store | ✅ vp.lookback |
| vp_bin_size | ✅ | ✅ | ✅ store | ✅ vp.bin_size |
| vp_va_percent | ✅ | ✅ | ✅ store | ✅ vp.va_percent |
| rv_adaptation | ✅ | ✅ | ✅ store | ⚠️ logic pending |

### Архитектура
- Robot API: port 5070 (uvicorn в main.py)
- C# сервер: port 5050 (статика + systemd proxy для start/stop)
- Frontend polling: `ROBOT_API/status` каждые 2 сек
- Config: «Стратегии» или popup → POST `ROBOT_API/api/robot/config`
- Start/Stop кнопки: через C# proxy `/api/robot/service/start|stop` (работает даже когда робот мёртв)

### Файлы
- `HedgeFund/robot/api.py` — v2.0: status, config GET/POST, grid-levels, active-strategies, health
- `HedgeFund/robot/main.py` — uvicorn launch + VP params sync
- `HedgeFund/robot/strategy.py` — StrategyParams: 9 полей (включая VP + rv_adaptation)
- `HedgeFund/src/Server/wwwroot/index.html` — только VP Scalp Grid (PYTHON)
- `HedgeFund/src/Server/wwwroot/js/app.js` — pollPythonRobot, pythonRobotEditPanel, renderRobots()

## Следующие шаги

1. **Запуск реальной торговли** — по команде пользователя
2. **Мониторинг первых сделок** — проверить что grid выставляется, TP срабатывает
3. **RV Adaptation логика** — реализовать в Python strategy (флаг на месте)
4. **Watchdog frozen price fix** — определять замороженную цену (одна цена > N сек)

## Известные баги (исправлены 26.05)

| Баг | Причина | Фикс | Статус |
|-----|---------|------|--------|
| Grid не выставляется | `filled_levels` setter crash | Убрал присвоение computed property | ✅ |
| Broker position crash | `float('')` ValueError | `float(value or '0')` | ✅ |
| Entry timeout без broker check | 30с timeout без проверки fill | Проверка брокера перед reset | ✅ |
| Кнопка ▶ не работает offline | API мёртв → fetch fail | C# proxy `/api/robot/service/` | ✅ |
| Параметры не отображаются | `const cfg` block-scoped | `let cfg` в outer scope | ✅ |

## Текущее состояние (27.05 06:51 UTC)

- Робот: **остановлен** (inactive)
- C# сервер: работает (port 5050)
- State: очищен (RT=0, PnL=0)
- Параметры: max_levels=100, step=31, spread=31, min_profit=35, hold=off
- Ожидание: команда на запуск от пользователя
