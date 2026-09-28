# 🤖 Order Flow Robot BR (PYTHON) — полное описание логики

_Версия документа: 28.09.2026. Соответствует коду: `robot/main_of_br.py` + общий движок `robot/strategy_of.py` + `robot/orderflow_engine.py` (git main)._

Робот торгует фьючерс **BR (Brent)** — контракты чередуются (BRU6 → BRV6 → …), символ задаётся окружением сервиса (`ROBOT_SYMBOL_BR`, сейчас BRV6@RTSX). Логика стратегии полностью совпадает с SI-роботом (общий `strategy_of.py`); документ самодостаточен, отличия BR — в §1, §9, §13.

---

## 1. Развертывание: два инстанса

| | **of-robot-br** (main) | **of-robot-br-edp** (edp) |
|---|---|---|
| Порт | 5082 | 5083 |
| Точка входа | `main_of_br.py --port 5082` | `main_of_br.py --port 5083` |
| Счёт | main **1225953** | edp **2049688** |
| Конфиг | `of_config_br.json` | `of_config_br_edp.json` |
| Стейт | `of_state_br.json` | `of_state_br_edp.json` |
| env | — | `ROBOT_STATE_SUFFIX_BR=_edp`, `ROBOT_ACTIVE_ACCOUNT_BR=edp` |
| Маркер рестартов | `of_restart_ts_br.json` | `of_restart_ts_br_edp.json` |

Суффиксы файлов (`STATE_SUFFIX`) и активный счёт задаются переменными окружения → один и тот же код работает на двух счетах параллельно. В отличие от SI-робота, BR берёт символ **только** из `config_of_br.py`/env — переключение инструмента из UI через конфиг не поддерживается (меняется env сервиса + рестарт).

**Особенности кода BR** (отличия от main_of.py):
- HTTP-сервер — **ThreadingHTTPServer** + `timeout=60` на idle keep-alive: однопоточный сервер + уснувший браузер намертво вешали `/status` (баг 21.09 на 5083).
- Цены BR — доли (шаг 0.01): везде `fmt_price()` (до 2 знаков), параметры в конфигах дробные.
- Файлы: `config_of_br.py` (дефолты), логи `logs/of_br_YYYYMMDD.log`.
- `POST /restart` перезапускает сервис `of-robot-br` (для edp-инстанса рестарт по регламенту через консоль хоста).
- `POST /start` без проверки pendingTicker (нет UI-переключателя инструмента).

## 2. Данные и метрики (общий движок)

### 2.1 Потоки (FinamPy gRPC)
| Поток | Что даёт | Использование |
|---|---|---|
| LatestTrades | обезличенные сделки | delta/CVD за бар, Volume Profile, агрессия |
| OrderBook | L2-стакан (инкрементальные апдиты) | imbalance, стены, ликвидность возле цены |
| Bars (M5) | OHLCV по закрытии бара | бар-метрики, HL-окно, ATR, VWEMA |
| Quote | bid/ask | текущая цена (mid) для тиков |
| OrdersTrades | исполнения своих ордеров | реальная цена филла для PnL |

Ордера — через **DataProvider REST** (localhost:5060): `POST /order/place` (market), `POST /order/cancel`, `GET /active-orders`, `GET /position`, `GET /quote`.

### 2.2 Бар-метрики (`TradeCollector`)
- На закрытый бар: `buy_volume`, `sell_volume`, `delta`, `delta_ratio`, `total_volume`.
- **CVD** — кумулятивная дельта, авто-сброс 07:00 МСК.

### 2.3 Стакан (`OrderBookTracker`)
- Снапшот bids/asks; **стена** = размер ≥ 3× средней (по 2000 последним наблюдениям); отслеживается **съедание стены** (<30% пика за 5 мин) → направленное событие (bid-стена съедена → SHORT, ask → LONG); отмена уровня удаляет трекинг.
- **OB-фильтр ликвидности**: near-объём в радиусе 50 пт от mid ≥ 20 лотов — обязательное условие входа.

### 2.4 Агрессия
Тики по улучшающимся ценам: вверх → buy-агрессия, вниз → sell. Окно `agg_window` баров; `agg_ratio = buy/sell`.

### 2.5 VWEMA-режим (edp-инстанс: вкл, main: выкл)
Volume-weighted EMA fast/slow (alpha быстрой масштабируется объёмом), `direction = sign((emaF−emaS)/ATR)` с флэт-зоной. Warmup историческими барами при старте. Блокирует контртрендовые входы (`vwema_block_counter`).

### 2.6 Volume Profile / VAH/VAL
Профиль «цена→объём» по тикам, бин `vah_val_bin_size` (у BR — 0.35$, т.е. 35 центов), POC + Value Area `vah_val_pct` (95%), VAH/VAL. Сброс в 00:00 МСК.

## 3. Сигналы

| Сигнал | Флаг | Условие | Направление |
|---|---|---|---|
| **CVD Acceleration** | `use_cvd_accel=true` | `CVD_now − CVD[10 баров назад] > ±cvd_accel_threshold` | +→LONG, −→SHORT |
| **CVD Trend** | `use_cvd=true` | пересечение EMA(fast/slow) на CVD (обновление раз в бар, сброс при дневном обнулении CVD) | по пересечению |
| dm_wall_agree / ob_imbalance | false | (выключены) | — |

**Aggression Ratio-фильтр** для CVD Accel: LONG требует `agg_ratio ≥ agg_ratio_threshold` (0.35), SHORT — `agg_ratio ≤ 1/max(threshold−0.3, 0.5)` = ≤2.0.

**Вход**: ≥ `signal_confirm_count` сигналов, все в одну сторону.

## 4. Главный цикл `process_tick` (тик 50 мс)

1. Суточный сброс PnL/CVD.
2. Ночь (23:50–07:00 МСК) / клиринг (14:00–14:05 МСК) → нет действий.
3. Вне окна сессии (если задано) → `close_all(session_close)`.
4. Цена stale >180 с → пропуск тика.
5. `close_price_all` (ручная цель, если задана).
6. В позиции → **Partial TP** — приоритет №1 (скальп по лоту — ЗАКОН).
7. Выходы: SL → VWEMA avg-exit → reverse → tp_full → timeout.
8. Усреднение.
9. Пирамидинг.
10. FLAT → вход (если нет entry-lock).

## 5. Вход и наращивание

### 5.1 Entry (FLAT)
Гварды по порядку: данных достаточно → daily stop не пробит (`dailyPnL > −SL`) → OB-ликвидность ≥ 20 лотов → **VAH/VAL GATE (range)**: вход только внутри `[VAL..VAH]` (вне VA и при неготовом VP — блок) → сигналы (≥1, согласованы) → `direction_filter` → **HL-фильтр**: LONG только у лоу окна (`price − winLow ≤ hl_delta`), SHORT только у хая (`winHigh − price ≤ hl_delta`) → VWEMA-блок контртренда (edp).

Исполнение: маркет `lots` (1) через DP; лот в `lotQueue`; `entry_lock` 10 с.

### 5.2 Усреднение
- Цена ушла против на ≥ `step_average` от экстремума очереди лотов → +1 уровень.
- **Catch-up**: все пропущенные уровни сразу (до 10/тик), потолок `max_average_levels`.
- **Вне VA усреднение на паузе** (позиция не закрывается — va_stop убран 07.09.2026); при возврате в VA catch-up добирает всё.
- `vwema_avg_exit` (B2): против VWEMA не усредняться.
- avg_price — всегда VWAP очереди.

### 5.3 Пирамидинг
Только в плюсе, шаг `step_pyramid` по тренду, лимит `max_pyramid_levels` (у BR = 1, фактически выключен).

## 6. Partial TP (LIFO)
- Лот из конца очереди закрывается при `(price − lot.price) × side ≥ spread` (у BR spread в долях: main 0.15, edp 0.23 ≈ 15/23 цента).
- Grace 2 с на свежие лоты; catch-up — закрывает все прибыльные лоты разом.
- Очередь пуста → round trip, `entry_lock` 10 с.
- **close_half_pct**: при `total_lots ≤ max(2, peak×pct/100)` и прибыльном LIFO-лоте → закрыть весь остаток (main и edp: 50%).
- Учёт — по реальному филлу, комиссия 2×0.90₽ за лот RT.

## 7. Выходы (порядок)
| # | Выход | Условие |
|---|---|---|
| 1 | stop_loss | `unrealized ≤ −stop_loss_value` (rub; поддержка pct/pts) — BR: **1000₽** |
| 2 | vwema_avg_exit | усреднённая позиция против развернувшегося VWEMA |
| 3 | reverse_signal | CVD Trend против позиции N баров подряд / CVD Accel против N баров подряд (`signal_confirm_exit`=18) / доп. сигнал против |
| 4 | tp_full | `(price − avg) × dir ≥ min_profit_per_lot` |
| 5 | timeout | hold ≥ `max_hold_minutes` (999) |
| 6 | session_close | вне окна сессии |
| 7 | close_price_all | ручная цель |
| 8 | manual_stop | `POST /stop` |
| 9 | close_half_pct | порог 50% от пика |

## 8. Исполнение, учёт, state, watchdog (как у SI)

- Market-ордера через DP REST с тегами `of_*`; **реальные филлы** из OrdersTrades-потока обновляют цены входов и учёт PnL; при отсутствии филла — расчётная цена с пометкой.
- Отказ DP → проверка лотов у брокера через 3 с → исполнено/rollback (вход, TP) / retry (close_all).
- `sync_from_broker` каждые 30 с — только цены; десинк лотов → warning `DESYNC`, сам не чинит.
- **Startup reconciliation**: любое расхождение с брокером при старте → `mode=stopped` + warning (кроме «брокер FLAT, робот в позиции» → reset).
- `POST /position/adjust` — ручная state-only корректировка; `GET /sync?lots=N` — жёсткая подгонка лотов.
- Стейт `of_state_br*.json` — каждые 30 с; позиция и очередь лотов переживают рестарт.
- **Watchdog**: цена stale >60 с → reconnect FinamPy (≤1/30 с); LatestTrades мёртв >120 с → reconnect (gRPC-стримы Finam умирают молча); умерший fill-поток → re-subscribe.

## 9. HTTP API (:5082 / :5083)
`GET /status` (позиция, PnL, параметры, метрики VP/VWEMA/HL/CVD, brokerLots/brokerMismatch, фильтры журнала `?startDate&endDate&all=true`), `GET /health`, `GET /account`, `GET /sync?lots=N`, `POST /start`, `POST /stop` (снять ордера + закрыть позицию), `POST /pause`, `POST /restart` (FLAT-чек, cooldown 90 с, journal vacuum 500M, отложенный `systemctl restart of-robot-br`), `POST /params` (persist в of_config_br*.json), `POST /position/adjust`, `POST /account` (переключение main/edp), `POST /trades`, `POST /load-state`, `POST /reset-stats`.

## 10. Отличия логики от SI-робота
1. **Таймфрейм M5** (SI сейчас M1) — окно HL у main = 24 бара = 2 часа.
2. **Дробные цены/шаги** (BR торгуется в $ с шагом 0.01): spread/step/min_profit/bin_size — в долях.
3. **Два инстанса на двух счетах** из одного кода (суффиксы стейта/конфига через env).
4. ThreadingHTTPServer + keep-alive timeout (лечение зависания /status).
5. `direction_filter=short` на main (только шорт), `both` на edp.
6. Нет UI-переключения инструмента (символ из env сервиса).
7. Aggressive risk: SL 1000₽, maxAvg 5 (main) / 15 (edp) — против 20 000₽ и 218 у SI.

## 11. Актуальные параметры (сентябрь 2026)

| Параметр | **BR main (5082)** | **BR edp (5083)** |
|---|---|---|
| symbol / счёт | BRV6@RTSX / 1225953 | BRV6@RTSX / 2049688 |
| timeframe | M5 | M5 |
| direction_filter | **short** | both |
| lots | 1 | 1 |
| stop_loss (rub) | 1 000₽ | 1 000₽ |
| spread (partial TP) | 0.15 | 0.23 |
| step_average | 0.35 | 0.40 |
| max_average_levels | 5 | 15 |
| step_pyramid / max_pyramid | 25 / 1 | 25 / 1 |
| min_profit_per_lot | 0.30 | 0.17 |
| max_hold_minutes | 999 | 999 |
| close_half_pct | 50% | 50% |
| use_vah_val (range) | true, бин 0.35, VA 95% | true, бин 0.35, VA 95% |
| use_vwema | false | true (20/40, flat 1.0, block counter) |
| vwema_avg_exit (B2) | false | false |
| hl_filter | true, окно 24 бара (2 ч), Δ 120 | true, окно 12 баров (1 ч), Δ 75 |
| cvd_accel | true, 10 баров, threshold 150 | true, 10 баров, threshold 150 |
| use_cvd (EMA) | true, 5/25 | true, 5/25 |
| agg_window / ratio | 10 / 0.35 | 10 / 0.35 |
| signal_confirm_count / exit | 1 / 18 | 1 / 18 |
| ob_filter | 20 лотов / радиус 50 | 20 лотов / радиус 50 |
| session_start/end | пусто | пусто |

## 12. Сводка поведения одной строкой

BR-робот входит **1 лот** по CVD-ускорению/CVD-тренду **только внутри Value Area (95%)**, при ликвидности в стакане ≥20 лотов, с учётом HL-окна (main — только SHORT; edp — оба направления, но не против VWEMA); при движении против — усредняется малой сеткой (main до 5 уровней по 0.35$, edp до 15 по 0.40$; вне VA пауза с catch-up); каждый лот в плюс на spread (0.15$/0.23$) закрывает LIFO; остаток ≤50% от пика закрывает целиком; жёсткие выходы: SL −1000₽, min_profit 0.17–0.30$/лот, таймаут 999 мин; учёт по реальным филлам, позиция переживает рестарты, стримы лечатся watchdog-ом; инстансы полностью независимы (свои конфиг/стейт/счёт/порт).
