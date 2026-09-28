# 🤖 Order Flow Robot Si (PYTHON) — полное описание логики

_Версия документа: 28.09.2026. Соответствует коду: `robot/main_of.py` + `robot/strategy_of.py` + `robot/orderflow_engine.py` (git main)._

Робот торгует фьючерс **SI ($/руб)** — контракты чередуются (SiU6 → SiZ6 → …), активный инструмент задаётся в конфиге (`of_config.json`, поле `ticker`). Документ описывает логику независимо от конкретного контракта.

---

## 1. Общая архитектура

```
Finam gRPC (FinamPy)                     DataProvider REST (localhost:5060)
 ├─ SubscribeLatestTrades  → сделки       ├─ POST /order/place   (market/limit)
 ├─ SubscribeOrderBook     → стакан L2    ├─ POST /order/cancel
 ├─ SubscribeBars          → бары M1/M5   ├─ GET  /active-orders
 ├─ SubscribeQuote         → bid/ask      ├─ GET  /position      (лоты/средняя у брокера)
 └─ OrdersTrades           → свои филлы   └─ GET  /quote, /recent-fills
        │                                        ▲
        ▼                                        │ ордера только через DP
  orderflow_engine.py  (метрики)                 │
   ├─ TradeCollector    (delta, CVD)             │
   ├─ OrderBookTracker  (imbalance, стены)       │
   └─ SignalEngine      (сигналы)                │
        │                                        │
        ▼                                        │
  strategy_of.py: OrderFlowStrategy ─── actions ─┘
   (вход / усреднение / пирамидинг / partial TP / выходы)
        │
        ▼
  main_of.py: main_loop (тик 50 мс) + HTTP API :5080 + watchdog + state
```

- **Файлы робота:** `main_of.py` (точка входа, порт 5080, systemd-сервис `of-robot`), `strategy_of.py` (вся торговая логика), `orderflow_engine.py` (метрики и сигналы), `orders_dp.py` (ордера через DataProvider), `config_of.py` (дефолты), `of_config.json` (актуальные параметры, пишется из UI через `/params`), `of_state.json` (persist-состояние).
- **Режимы:** `stopped` / `running` / `paused`. Пауза = снятие ордеров, позиция живёт. Стоп = снятие ордеров + закрытие позиции по рынку.
- **Бумажный режим:** `python3 main_of.py --paper` — все действия только в лог.
- **Счёт:** двухаккаунтная схема `main=1225953` / `edp=2049688`, активный хранится в `of_state.json` (`activeAccount`), переключение `POST /account`.

## 2. Данные и метрики

### 2.1 Потоки (FinamPy)
| Поток | Что даёт | Использование |
|---|---|---|
| LatestTrades | обезличенные сделки (price, size, side, ts) | delta/CVD за бар, Volume Profile, агрессия |
| OrderBook | L2-стакан, инкрементальные апдиты (action 1=remove/2=add/3=upd) | imbalance, стены, ликвидность возле цены |
| Bars (M1) | OHLCV по закрытии бара | закрытие бар-метрик, HL-окно, ATR, VWEMA |
| Quote | bid/ask | текущая цена = mid(bid,ask), fallback bid или ask |
| OrdersTrades | исполнения СВОИХ ордеров | реальная цена филла для учёта PnL |

### 2.2 Тиковые метрики (`TradeCollector`)
- На каждый закрытый бар агрегируются сделки: `buy_volume`, `sell_volume`, `delta = buy−sell`, `delta_ratio = delta/total`, `total_volume`.
- **CVD** (кумулятивная дельта): `CVD += delta` на каждом баре; авто-сброс в 07:00 МСК ежедневно.

### 2.3 Стакан (`OrderBookTracker`)
- Держит снапшот `bids/asks: price → size`.
- **Стена** = размер уровня ≥ `wall_multiplier` (3×) от средней по последним 2000 наблюдениям (нужно ≥500 сэмплов).
- **Wall consumed**: стена, съеденная до <30% от своего пика в течение 5 минут жизни → событие с направлением (съеденная bid-стена → SHORT, ask-стена → LONG). Отмена уровня (action=remove) удаляет трекинг (фейк).
- **OB-фильтр ликвидности**: `near_buy + near_sell` в радиусе `ob_scan_radius` (50 пт) от mid должно быть ≥ `ob_min_lots` (20 лотов), иначе вход запрещён.
- `imbalance = (totalBuy − totalSell)/total` — сигнал OB Imbalance (сейчас отключён).

### 2.4 Агрессия (aggression)
Тики по улучшающимся ценам: сделка выше предыдущей цены → buy-агрессия, ниже → sell-агрессия. Накапливается внутри бара, на закрытии бара подаётся в движок (`add_bar_aggression`), окно — последние `agg_window` баров. `agg_ratio = sum(agg_buy)/max(sum(agg_sell),1)` — >1 покупатели агрессивнее, <1 продавцы.

### 2.5 VWEMA-режим (`VWEMARegime`, фильтр тренда)
- Две EMA (fast/slow) с volume-бустом: alpha быстрой EMA масштабируется отношением объёмных EMA (clamp 0.3–3), медленная — без буста. Инициализация SMA.
- `direction = sign((emaF − emaS)/ATR)`, при `|s| < vwema_flat_th` → 0 (флэт).
- Обновляется на закрытии бара (в live в качестве volume подаётся delta бара; при warmup — объём бара). ATR — Wilder-14.
- На старте — **warmup историческими барами** (`_warmup_vwema`): подгружает `vwema_slow + 20` баров, чтобы фильтр был готов сразу.

### 2.6 Volume Profile / VAH/VAL (`VolumeProfileCalculator`)
- Профиль «цена → объём» по тиковым сделкам, бин `vah_val_bin_size` пт. POC = бин с макс. объёмом. Value Area: расширение от POC в обе стороны (жадно по большему объёму), пока не наберёт `vah_val_pct` % объёма. `VAL` = нижняя граница, `VAH` = верхняя + размер бина.
- Сброс профиля в 00:00 МСК (сессия = сутки).
- **Backfill при старте** (`_backfill_vp`): сегодняшние M1-бары добавляются в профиль `(H+L+C)/3 × volume`, чтобы VAH/VAL отражали всю сессию, а не время с рестарта.

## 3. Сигналы (`SignalEngine.generate_signals`)

Активный набор задаётся флагами `use_cvd_accel`, `use_cvd`, `use_dm_wall`, `use_ob_imbalance`:

| Сигнал | Флаг | Условие | Направление |
|---|---|---|---|
| **CVD Acceleration** | `use_cvd_accel=true` | `CVD_now − CVD[cvd_accel_period баров назад] > +cvd_accel_threshold` | LONG |
| | | `… < −cvd_accel_threshold` | SHORT |
| **CVD Trend** | `use_cvd=true` | пересечение EMA_fast/EMA_slow, посчитанных на CVD (обновление раз в бар) | по пересечению (fast>slow → LONG) |
| dm_wall_agree | `use_dm_wall=false` | `dm_lookback` баров подряд дельта одного знака + съеденная стена в `wall_window` (120 c) в ту же сторону | по дельте |
| ob_imbalance | отключён | \|imbalance\| ≥ 0.5 + стена с той же стороны | по imbalance |

**Фильтр Aggression Ratio для CVD Accel** (`use_agg_ratio=true`): сигнал LONG требует `agg_ratio ≥ agg_ratio_threshold`; SHORT требует `agg_ratio ≤ 1/max(threshold−0.3, 0.5)` (при threshold 0.5 → ratio ≤ 2.0).

**Дневной сброс CVD Trend**: если CVD упал с большого значения до ~0 (сброс 07:00), EMA-и реинициализируются, направления сбрасываются.

**Правило входа по сигналам:** сработало ≥ `signal_confirm_count` сигналов, и **все в одну сторону** (конфликт → пропуск).

## 4. Главный цикл `process_tick` (вызов каждые 50 мс)

Порядок проверок — **приоритет сверху вниз**:

1. **Суточный сброс** PnL и CVD.
2. **Ночь/клиринг**: 23:50–07:00 МСК или 14:00–14:05 МСК → никаких действий.
3. **Сессия** (`session_start/end`, если заданы): вне окна → при наличии позиции `close_all(session_close)`; входов нет.
4. **Stale price**: цена не обновлялась >180 с → пропуск тика.
5. **`close_price_all`** (если задан): LONG и price ≥ цели, либо SHORT и price ≤ цели → `close_all(close_price_all)`, цель сбрасывается в 0.
6. **В позиции → Partial TP** (см. §6) — приоритет №1 («робот должен скальпить по 1 лоту — ЗАКОН»).
7. **Выходы** (см. §7): стоп-лосс → VWEMA avg-exit → обратный сигнал → полный TP → таймаут.
8. **Усреднение** (см. §5.2).
9. **Пирамидинг** (см. §5.3).
10. **FLAT → вход** (см. §5.1), если нет entry-lock.

Каждое действие ставит `entry_lock` (10 с после входа/закрытия) — защита от мгновенного перевхода.

## 5. Вход и наращивание позиции

### 5.1 Entry (FLAT)
Гварды по порядку:
1. Данных достаточно: закрытых баров ≥ `cvd_lookback+1`.
2. **Daily stop** не пробит: `dailyPnL > −stop_loss_value` (иначе входов нет до следующего дня).
3. **OB-фильтр**: `near_buy+near_sell ≥ ob_min_lots` (ликвидность для входа).
4. **VAH/VAL GATE** (`use_vah_val=true`):
   - режим **range** (текущий): цена строго ВНУТРИ `[VAL..VAH]` → гейт пройден; вне VA → вход запрещён. Если VP не готов (нет данных) → вход запрещён.
   - режим **fade**: вход только у краёв VA (price ≥ VAH или ≤ VAL); внутри VA → блок. Тип входа `VAH/VAL-Confirm` (сигнал в сторону края) или `VAH/VAL-Fade` (против края).
   - режим **breakout**: гейт как fade, интерпретация — пробой края.
5. **Генерация сигналов** (только после прохождения гейта): CVD Accel + CVD Trend (включённые). Нужно ≥ `signal_confirm_count`, все в одну сторону.
6. **direction_filter** (`both/long/short`): блок запрещённой стороны.
7. **HL-фильтр** (`hl_filter=true`, режим only_strict): по окну последних `hl_window` закрытых баров:
   - LONG разрешён только если `price − winLow ≤ hl_delta` (у лоу окна);
   - SHORT — только если `winHigh − price ≤ hl_delta` (у хая окна);
   - середина диапазона → блок; окно не готово → блок.
8. **VWEMA-фильтр** (`use_vwema=true`, `vwema_block_counter=true`): при готовом направлении тренда ≠ 0 контртрендовый вход блокируется.

Исполнение: **маркет-ордер** `lots` (1) контрактов через DP. В очередь лотов (`lotQueue`) добавляется `LotEntry(price, side, lots, ts)`. Направление, avg, entry_time фиксируются; `entry_lock` 10 с.

### 5.2 Усреднение (average — против позиции)
- Условие: цена ушла против позиции на ≥ `step_average` пт от **экстремальной цены очереди** (для LONG — от минимума цен лотов в очереди, для SHORT — от максимума). Это исключает повторное усреднение на тех же уровнях.
- **Catch-up**: если цена перескочила несколько уровней (гэп, рестарт, пауза VA) — сразу добавляются ВСЕ пропущенные уровни по текущей цене (`missed = distance // step`), с потолками: остаток до `max_average_levels`, максимум 10 уровней за тик.
- **Пауза вне VA** (режим range): вне Value Area усреднение приостановлено (позиция НЕ закрывается — жёсткий va_stop убран 07.09.2026 решением Самурая). Пропущенные уровни копятся и добираются catch-up'ом при возврате цены в VA.
- **B2 vwema_avg_exit** (если включён): против направления VWEMA усредняться нельзя.
- Каждый уровень: +`lots`, avg_price пересчитывается как VWAP очереди, в очередь добавляется лот.
- Лимит: `average_levels < max_average_levels`. Потолок позиции: `lots × (1 + max_average_levels + max_pyramid_levels)`.
- Опционально `step_atr`: шаг = `base × ATR/50`, clamp `[base/2, 3×base]` (сейчас выключен).

### 5.3 Пирамидинг (pyramid — по тренду)
- Только когда позиция **в плюсе** (`unrealized > 0`) и цена прошла ≥ `step_pyramid` пт в сторону прибыли от последнего пирамид-уровня.
- Лимит `max_pyramid_levels` (текущий конфиг = 1, т.е. фактически выключен).
- +`lots`, VWAP-пересчёт, лот в очередь.

## 6. Partial TP (LIFO) — приоритетное закрытие

- Очередь лотов закрывается **с конца** (LIFO — последним вошёл, первым вышел).
- Лот закрывается, если `(price − lot.price) × side ≥ spread` пт — **частичный тейк по 1 лоту**; если подряд несколько лотов в плюсе (catch-up, рестарт) — закрываются все сразу за один тик.
- **Grace 2 с**: только что добавленный лот нельзя закрыть первые 2 секунды.
- Когда очередь опустела — позиция обнуляется, `round_trips++`, `entry_lock` 10 с.
- **`close_half_pct`** (0 = выкл): когда `total_lots ≤ max(2, peak_lots × pct/100)` и LIFO-лот в плюсе ≥ spread → остаток закрывается ЦЕЛИКОМ (`close_all(close_half_{pct}pct)`). Т.е. поза скальпится по лоту, пока не дойдёт до порога от пика, затем добивается разом.
- PnL каждого закрытия считается по **реальной цене исполнения** (fill-поток) минус комиссия `2 × commission` (RT) за лот.

## 7. Выходы (порядок проверки)

| # | Выход | Условие |
|---|---|---|
| 1 | **stop_loss** | `unrealized ≤ −stop_loss_value` (режим `rub` — рубли по позиции; `pct` — % от ГО = margin_per_lot×lots; `pts` — пункты на лот) |
| 2 | **vwema_avg_exit** (B2, опция) | позиция усреднялась (`average_levels>0`) и VWEMA-тренд развернулся против позиции → немедленное close_all |
| 3 | **reverse_signal** | (а) CVD Trend: направление EMA против позиции ≥ `signal_confirm_exit` баров подряд; (б) CVD Accel: `signal_confirm_exit` баров подряд с ускорением против позиции; (в) любой включённый доп. сигнал против позиции. Примечание: при `signal_confirm_exit=50` (текущий конфиг) реверсивный выход практически отключён |
| 4 | **tp_full** | `(price − avg) × dir ≥ min_profit_per_lot` пт → close_all |
| 5 | **timeout** | позиция держится ≥ `max_hold_minutes` → close_all |
| 6 | **session_close** | вне окна `session_start/end` (если задано) |
| 7 | **close_price_all** | ручная цена цели (см. §4 п.5) |
| 8 | **manual_stop** | `POST /stop` |
| 9 | **close_half_pct** | порог остатка от пика (см. §6) |

`close_all`: сначала снимаются все активные ордера, затем маркет-закрытие всего объёма; после филла позиция сбрасывается, сделка пишется в журнал по реальной цене.

## 8. Исполнение ордеров и учёт (main_of.py)

- Ордера — только через DataProvider REST (`/order/place` market). Теги: `of_entry`, `of_average`, `of_pyramid`, `of_partial_tp`, `of_close_<reason>`, `of_stop`.
- **Реальные филлы**: подписка на OrdersTrades-поток. После `place_market` робот ждёт колбэк ~0.3–0.5 с (`_consume_fill_price`); цена филла обновляет entry/avg позиции (`update_fill_price`) и используется в учёте PnL. Если филла нет — используется расчётная цена стратегии (с пометкой в логе).
- **Отказ DP** (рест/ошибка): пауза 3 с → запрос позиции у брокера. Лоты изменились → считать исполненным (rollback не делаем); не изменились → rollback внутреннего состояния (вход) / вернуть лот в очередь (partial TP) / повтор на следующем тике (close_all).
- **Учёт PnL**: только по реальным сделкам; `pnl = (exit − entry) × side × lots − 2×commission×lots`. Ведутся `realizedPnL`, `dailyPnL` (сброс в 00:00), `roundTrips`, журнал `tradeHistory` (последние 200, хранится в стейте).
- **Sync с брокером** (каждые 30 с, `sync_from_broker`): синхронизирует ТОЛЬКО цены (avg/current) для отображения — **не трогает** dir/lots/lotQueue. Одновременно пассивный десинк-монитор: расхождение лотов робот↔брокер → warning `DESYNC` (сам не чинит).
- **Startup reconciliation** (при старте): брокер vs робот. Брокер ≠ 0, робот FLAT → `mode=stopped` + warning (сам не входит). Оба ненулевые и разные → `stopped` + warning. Брокер 0, робот с позицией → позиция сброшена (ручное закрытие). Совпадают → норм.
- **Ручная подгонка**: `POST /position/adjust` — state-only (ордеров НЕ шлёт): человек закрыл/открыл руками в терминале, робот усыновляет позицию (LIFO-закрытие из очереди, VWAP-пересчёт, запись PnL). `GET /sync?lots=N` — жёсткая подгонка счётчика лотов к брокеру (FIFO-удаление старейших).

## 9. State и восстановление

`of_state.json` (сохранение каждые 30 с и при каждом API-действии): позиция (dir, avg, lots, `lotQueue` с ценами каждого лота, levels, peak), PnL (realized/daily), журнал сделок, `mode`, `activeAccount`, `symbol`, `direction_filter`.
При старте состояние загружается → робот продолжает управлять позицией после рестарта. Восстановленные лоты не имеют grace-периода.

## 10. Watchdog (устойчивость)

- **Цена stale > 60 с** → полный reconnect FinamPy (закрыть канал, переподключить все подписки; не чаще 1 раза в 30 с).
- **LatestTrades мёртв > 120 с** (gRPC-стримы Finam умирают молча, цена при этом может жить через Quotes) → reconnect.
- **Поток своих сделок умер** (тред не alive) → re-subscribe OrdersTrades.
- Fallback цены: если Quote-поток дал 0 — запрос `/quote` у DP.

## 11. HTTP API (:5080)

| Endpoint | Назначение |
|---|---|
| `GET /status` | полный статус: mode, symbol/ticker/pendingTicker, позиция, PnL, параметры, VP/VWEMA/HL/CVD-метрики, brokerLots/brokerMismatch; фильтры журнала `?startDate&endDate&all=true` |
| `GET /health` | liveness |
| `GET /account` | счета и активный |
| `GET /sync?lots=N` | жёсткая синхронизация лотов с брокером |
| `POST /start` | режим running; **отказывает (409)**, если в конфиге pending-тикер ≠ активному (смена инструмента требует рестарта) |
| `POST /stop` | снять ордера + закрыть позицию по рынку |
| `POST /pause` | снять ордера, позицию держать |
| `POST /restart[?force=1]` | регламентный рестарт: FLAT-чек (409 при позиции без force), cooldown 90 с, `journalctl --vacuum-size=500M`, отложенный на 2 с `systemctl restart of-robot` через `systemd-run` |
| `POST /params` | изменить параметры (persist в of_config.json). Поле `ticker` — выбор инструмента, применяется при рестарте; при открытой позиции на другом символе смена блокируется |
| `POST /position/adjust` | ручная корректировка позиции (state-only) |
| `POST /account` | переключение счёта main/edp |
| `POST /trades` | журнал сделок с фильтром дат |
| `POST /load-state` | перечитать of_state.json с диска |
| `POST /reset-stats` | обнулить PnL/журнал/roundTrips |

## 12. Выбор инструмента (SiU6 → SiZ6 и т.д.)

`.env` (`ROBOT_SYMBOL/ROBOT_TICKER`) общий для всех OF-сервисов, поэтому активный тикер живёт в `of_config.json` (`ticker`, сейчас `SiZ6`). При старте `_resolve_instrument()`:
1. тикер из конфига валиден (буквы+цифры, 3–8 символов) → `SYMBOL = <ticker>@RTSX`;
2. **защита позиции**: если в стейте открытая позиция на другом символе — робот остаётся на старом символе (поза должна реаттачиться), смена висит pending, `POST /start` возвращает 409 до закрытия позиции и рестарта;
3. смена тикера через UI → применяется при рестарте (стримы биндятся на символ при старте процесса).

## 13. Актуальные параметры (of_config.json, сентябрь 2026)

| Группа | Параметр | Значение |
|---|---|---|
| Инструмент | ticker / счёт | SiZ6@RTSX / edp 2049688 |
| База | timeframe | **M1** |
| | lots | 1 |
| | commission | 0.90₽ (сторона), RT 1.80₽ |
| Вход/выход | signal_confirm_count | 1 |
| | signal_confirm_exit | 50 (реверс-выход фактически выключен) |
| | direction_filter | both |
| | stop_loss | rub / 20 000₽ (позиционный + daily-stop входов) |
| | min_profit_per_lot | 30 пт |
| | max_hold_minutes | 7777 |
| | close_half_pct | 40% от пика → добить остаток |
| | close_price_all | 0 (выкл) |
| Partial TP | spread | 50 пт |
| | partial_tp | true |
| Усреднение | step_average | 70 пт |
| | max_average_levels | 218 |
| | step_atr | false |
| Пирамидинг | step_pyramid / max_pyramid_levels | 100 пт / 1 (выключен) |
| CVD | use_cvd_accel / period / threshold | true / 10 баров / 777 |
| | use_cvd / EMA fast/slow | true / 7 / 17 |
| | agg_window / agg_ratio_threshold | 10 / 0.5 |
| Фильтры | use_vah_val (mode range) | true, бин 80 пт, VA 70% |
| | use_vwema | true, 50/100, flat 0.5, block counter |
| | vwema_avg_exit (B2) | false |
| | hl_filter | true, окно 12 баров (12 мин на M1), Δ 6 пт |
| | enable_ob_filter / ob_min_lots | true / 20 (радиус 50 пт) |
| | use_dm_wall / use_ob_imbalance | false / false |
| Сессия | session_start/end | пусто (вся сессия 07:00–23:50 МСК) |

## 14. Сводка поведения одной строкой

Робот входит **1 лот** по сигналу CVD-ускорения/CVD-тренда **только внутри Value Area**, с ликвидностью в стакане, у экстремума короткого HL-окна (лонг у лоу / шорт у хая) и не против VWEMA-тренда; при движении против — усредняется сеткой по 70 пт (до 218 уровней, вне VA пауза с catch-up), каждый лот в +50 пт закрывает LIFO-скальпом; остаток ≤40% от пика закрывает целиком; полный выход — SL −20 000₽, +30 пт/лот, таймаут 7777 мин или принудительные close_price_all/session_close; всё считается по реальным филлам брокера, позиция переживает рестарты, стримы лечатся watchdog-ом.
