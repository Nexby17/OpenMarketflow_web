# 🤖 Order Flow Robot (PYTHON) — Si (:5080) и MX (:5081) — полное описание логики

_Версия документа: 01.10.2026 (база 28.09.2026). Соответствует коду: `robot/main_of.py` + `robot/main_of_mx.py` + `robot/strategy_of.py` + `robot/orderflow_engine.py` (порт-архитектура PORT-A2/B2 + фичи server-main 28.09.2026 + F-050 логирование режимов)._ 
_Оба робота — один код: OF Si (`main_of.py`, SiZ6@RTSX) и OF MX (`main_of_mx.py`, MXZ6@RTSX) отличаются конфигом и парой мелких фич (VWEMA Avg Exit у MX)._

Робот торгует фьючерс **SI ($/руб)** — контракты чередуются (SiU6 → SiZ6 → …), активный инструмент задаётся в конфиге/через UI. Документ описывает логику независимо от конкретного контракта.

---

## 1. Общая архитектура

```
Finam WS-hub (FinamHubAdapter, PORT-A2)        REST 4.3.3 Finam (PORT-B2)
 ├─ trades       → сделки                       ├─ /orders (market) + wait_fill
 ├─ orderbook    → стакан L2                    ├─ /account/positions (синк/ребро)
 ├─ quotes       → bid/ask, fallback цены       ├─ get_bars (VWEMA warmup, VP backfill)
 └─ bars (M1/M5) → бары                         └─ get_last_quote (fallback цены)
        │                                        ▲
        ▼                                        │
  orderflow_engine.py  (метрики)          orders_rest4.OrderManagerRest4
   ├─ TradeCollector    (delta, CVD)       (batch cancel, REST fill wait,
   ├─ OrderBookTracker  (imbalance, стены)  WS ORDERS fills — F-019)
   └─ SignalEngine      (сигналы)
        │
        ▼
  strategy_of.py: OrderFlowStrategy ── actions ──┘
   (вход / усреднение / пирамидинг / partial TP / выходы)
        │
        ▼
  main_of.py: main_loop (тик 50 мс) + HTTP API :5080 (threaded) + watchdog + state
```

- **Файлы робота:** `main_of.py` (точка входа, порт 5080), `strategy_of.py` (вся торговая логика), `orderflow_engine.py` (метрики и сигналы), `orders_rest4.py` (ордера через Finam REST 4.3.3), `hub_adapter.py` (WS-данные), `config_of.py` (дефолты), `of_config.json` (актуальные параметры, пишется из UI через `/params`), `of_state.json` (persist-состояние), `instrument_switch.py` (F-017).
- **Режимы:** `stopped` / `running` / `paused`.
  - **PAUSE** (семантика по ТЗ Дмитрия, f06e2e8): ордера не наращивают позицию (entry/average/pyramid игнорируются), но выходы работают — partial TP, стопы, tp_full исполняются, позиция «ведётся».
  - **STOP** (`POST /stop`): снятие ордеров + закрытие позиции по рынку + reset.
- **Бумажный режим:** `python main_of.py --paper` — действия только в лог + симулированные филлы (состояние стратегии всё равно применяется — фикс вечного CLOSE_ALL-цикла dc12ef5).
- **Счёт:** двухаккаунтная схема `main` / `edp`, активный хранится в `of_state.json` (`activeAccount`), переключение `POST /account`.
- **API-сервер:** `ThreadingMixIn` — многопоточный (PORT-FIX c4d2111: однопоточный деградировал под поллингом UI).
- **Логи:** `DailyFileHandler` — суточная ротация файлов логов (F-015, 4978f58).

## 2. Данные и метрики

### 2.1 Потоки
| Поток | Что даёт | Использование |
|---|---|---|
| trades | обезличенные сделки (price, size, side) | delta/CVD за бар, Volume Profile, агрессия |
| orderbook | L2-стакан, инкрементальные апдейты | imbalance, стены, ликвидность возле цены |
| bars (M1/M5) | OHLCV по закрытии бара | закрытие бар-метрик, HL-окно, ATR, VWEMA |
| quotes | bid/ask | текущая цена = mid, fallback bid/ask; REST get_last_quote как второй fallback |
| WS ORDERS | исполнения СВОИХ ордеров (F-019) | реальная цена филла для учёта PnL + fast fill wait |

Кросс-символьный фильтр: события чужих символов отбрасываются (фикс 5339d2d «price contamination» после live-свитчинга).

### 2.2 Тиковые метрики (`TradeCollector`)
- На каждый закрытый бар: `buy_volume`, `sell_volume`, `delta`, `delta_ratio`, `total_volume`.
- **CVD**: `CVD += delta` на каждом баре; сброс в 07:00 МСК ежедневно.

### 2.3 Стакан (`OrderBookTracker`)
- Снапшот `bids/asks: price → size`.
- **Стена** = уровень ≥ `wall_multiplier` (3×) от средней по последним 2000 наблюдениям (≥500 сэмплов).
- **Wall consumed**: стена, съеденная до <30% пика за 5 минут → событие с направлением (bid-стена → SHORT, ask-стена → LONG); remove = фейк, трекинг снимается.
- **OB-фильтр ликвидности**: `near_buy + near_sell` в радиусе `ob_scan_radius` (50 пт) ≥ `ob_min_lots` (20 лотов), иначе вход запрещён.

### 2.4 Агрессия (aggression)
Тики по улучшающимся ценам: выше предыдущей → buy-агрессия, ниже → sell. Окно — `agg_window` баров, `agg_ratio = Σagg_buy / max(Σagg_sell, 1)`.

### 2.5 VWEMA-режим (`VWEMARegime`)
- Две EMA (fast/slow) с volume-бустом (clamp 0.3–3 на быстрой), `direction = sign((emaF − emaS)/ATR)`, флэт-порог `vwema_flat_th`.
- Обновление на закрытии бара (live — delta бара как volume).
- **Warmup при старте** (`_warmup_vwema`): `vwema_slow + 20` баров через REST `get_bars` — фильтр готов сразу.

### 2.6 Volume Profile / VAH/VAL (`VolumeProfileCalculator`)
- Профиль «цена → объём» по тиковым сделкам, бин `vah_val_bin_size` пт; POC; Value Area — жадное расширение от POC до `vah_val_pct` % объёма.
- Сброс в 00:00 МСК (сессия = сутки).
- **VP backfill при старте** (порт server-main, fix 22.09.2026): сегодняшние M1-бары с полуночи МСК добавляются как `(H+L+C)/3 × volume` через REST `get_bars`, затем `calculate()` — VAH/VAL отражают всю сессию сразу после рестарта.

## 3. Сигналы (`SignalEngine.generate_signals`)

Активный набор задаётся флагами `use_cvd_accel`, `use_cvd`, `use_dm_wall` (ob_imbalance вырезан — legacy):

| Сигнал | Флаг | Условие | Направление |
|---|---|---|---|
| **CVD Acceleration** | `use_cvd_accel=true` | `CVD_now − CVD[cvd_accel_period баров назад] > +threshold` / `< −threshold` | LONG / SHORT |
| **CVD Trend** | `use_cvd=true` | пересечение EMA_fast/EMA_slow на CVD | по пересечению |
| dm_wall_agree | `use_dm_wall` | `dm_lookback` баров дельты одного знака + съеденная стена в ту же сторону | по дельте |

**Фильтр Aggression Ratio** (`use_agg_ratio=true`): LONG требует `agg_ratio ≥ agg_ratio_threshold`; SHORT — `agg_ratio ≤ 1/max(threshold−0.3, 0.5)`.

**Правило входа:** ≥ `signal_confirm_count` сигналов, все в одну сторону (конфликт → пропуск).

**Дневной сброс CVD Trend**: при сбросе CVD в 07:00 EMA реинициализируются.

## 4. Главный цикл `process_tick` (тик 50 мс)

Порядок проверок — приоритет сверху вниз:

1. **Суточный сброс** PnL и CVD.
2. **Ночь/клиринг**: 23:50–07:00 МСК или 14:00–14:05 МСК → никаких действий (позиция живёт; автоматического session-close у нас нет — params `session_start/end` отсутствуют).
3. **Stale price**: цена не обновлялась → пропуск тика.
4. **`close_price_all`** (если задан): цена достигла ручной цели → `close_all(close_price_all)`, цель сбрасывается.
5. **В позиции → Partial TP** (приоритет №1 — «скальп по 1 лоту — ЗАКОН»).
6. **Выходы** (§7): stop_loss → vwema_avg_exit → reverse_signal → tp_full → timeout.
7. **Усреднение** (§5.2, с VA-паузой).
8. **Пирамидинг** (§5.3).
9. **FLAT → вход** (§5.1), если нет entry-lock.

В PAUSE-режиме main_loop исполняет только выходы/TP (entry/average/pyramid отбрасываются).

**Удалено (порт a6ec3b8, утверждено 28.09.2026):** жёсткий **VA BREAKOUT STOP** (закрытие позиции при выходе цены из Value Area). Позиция вне VA живёт и закрывается только обычными правилами.

Каждое действие ставит `entry_lock` (10 с) — защита от мгновенного перевхода.

## 5. Вход и наращивание позиции

### 5.1 Entry (FLAT)
Гварды по порядку:
1. Данных достаточно (`signals.has_enough_data`).
2. **Daily stop** не пробит: иначе close при позиции и блок входов до следующего дня.
3. **OB-фильтр**: ликвидность ≥ `ob_min_lots`.
4. **VAH/VAL GATE** (`use_vah_val=true`): режим range — вход строго ВНУТРИ `[VAL..VAH]`; fade/breakout — у краёв; VP не готов → блок.
5. **Генерация сигналов** (после VA-гейта): ≥ `signal_confirm_count`, все в одну сторону.
6. **direction_filter** (`both/long/short`).
7. **HL-фильтр** (`hl_filter`, порт ca58913, only_strict + verdict both):
   - LONG разрешён только если `price − winLow ≤ hl_delta` (у лоу окна из `hl_window` закрытых баров);
   - SHORT — только если `winHigh − price ≤ hl_delta` (у хая);
   - середина диапазона → блок; окно не готово (≥2 баров) → блок;
   - если окно уже `2×hl_delta` — оба направления проходят (фильтр нейтрален, вердикт «both»).
8. **VWEMA-фильтр** (`vwema_block_counter`): контртрендовый вход блокируется.

Исполнение: маркет `lots` через REST 4.3.3 + `wait_fill`; реальная цена филла обновляет entry (`update_fill_price`); лот в LIFO-очередь; `entry_lock` 10 с.

### 5.2 Усреднение (average — против позиции)
- Условие: цена ушла против позиции на ≥ `step_average` пт от экстремальной цены очереди (LONG — от минимума, SHORT — от максимума).
- **Пауза вне VA (порт a6ec3b8)**: в режиме range вне Value Area усреднение замирает (`AVG VA BLOCK`), позиция НЕ закрывается. Пропущенные уровни копятся и добираются catch-up'ом при возврате цены в VA.
- **Catch-up**: если цена перескочила несколько уровней — добавляются ВСЕ пропущенные (`missed = distance // step`), потолки: остаток до `max_average_levels`, максимум 10 уровней за тик.
- **B2 vwema_avg_exit** (если включён): против VWEMA усредняться нельзя.
- Каждый уровень: +`lots`, VWAP-пересчёт avg, лот в очередь.
- Опционально `step_atr` (выключен).

### 5.3 Пирамидинг (pyramid — по тренду)
- Только в плюсе (`unrealized > 0`) и цена прошла ≥ `step_pyramid` от последнего уровня.
- Лимит `max_pyramid_levels` (конфиг = 1, фактически выключен).

## 6. Partial TP (LIFO) — приоритетное закрытие

- Очередь лотов закрывается с конца (LIFO).
- Лот закрывается, если `(price − lot.price) × side ≥ spread` пт; несколько лотов в плюсе — закрываются все за тик (catch-up).
- **Grace 2 с**: только что добавленный лот не закрывается первые 2 секунды.
- Очередь опустела → позиция обнулена, `round_trips++`, `entry_lock` 10 с.
- **`close_half_pct`** (40%): когда `total_lots ≤ max(2, peak_lots × pct/100)` и LIFO-лот в плюсе ≥ spread → остаток закрывается ЦЕЛИКОМ (`close_half_{pct}pct`).
- PnL — по реальной цене исполнения минус комиссия `2 × commission` за лот (RT).

## 7. Выходы (порядок проверки)

| # | Выход | Условие |
|---|---|---|
| 1 | **stop_loss** | `unrealized ≤ −stop_loss_value` (rub / pct от ГО / pts) |
| 2 | **vwema_avg_exit** (B2) | позиция усреднялась и VWEMA развернулся против → close_all |
| 3 | **reverse_signal** | включённые сигналы против позиции `signal_confirm_exit` баров подряд |
| 4 | **tp_full** | `(price − avg) × dir ≥ min_profit_per_lot` пт |
| 5 | **timeout** | держим ≥ `max_hold_minutes` |
| 6 | **close_price_all** | ручная цена цели (§4 п.4) |
| 7 | **daily_stop** | дневной лимит убытка (§5.1 п.2) |
| 8 | **manual_stop** | `POST /stop` |
| 9 | **close_half_pct** | порог остатка от пика (§6) |
| — | ~~va_breakout_stop~~ | **удалён** (порт a6ec3b8): выход из VA больше не закрывает позицию |

## 8. Исполнение ордеров и учёт (main_of.py + orders_rest4.py)

- Ордера — Finam REST 4.3.3, маркет. Теги: `of_entry`, `of_average`, `of_pyramid`, `of_partial_tp`, `of_close_<reason>`, `of_stop`.
- **F-019 fast execution** (8438b60): batch cancel (`cancel_many`), REST fill wait (`wait_fill` на каждом исполнении), филлы по WS ORDERS.
- **F-043 anti-double-close guard** (7eb550d, лечение фантомной позиции 28.09): перед повторным close_all проверяется (dir, qty, окно 300 с) + позиция брокера; если брокер FLAT — робот синхронизируется в FLAT; иначе повторная отправка close БЛОКИРУЕТСЯ (требуется ручное/UI-подтверждение).
- **Отказ брокера**: close_all — пауза 3 с → запрос позиции; изменилась → считать исполненным; нет → rollback / возврат лота в очередь / повтор на следующем тике.
- **Учёт PnL**: только реальные филлы; `pnl = (exit − entry) × side × lots − 2×commission×lots`. `realizedPnL`, `dailyPnL` (сброс 00:00), журнал `tradeHistory` (последние 200, персистится).
- **Sync с брокером** (30 с): только цены (avg/current) для отображения; dir/lots/lotQueue не трогаются. Десинк-монитор: расхождение лотов → warning (сам не чинит).
- **Startup reconciliation**: брокер ≠ 0, робот FLAT → `mode=stopped` + warning; брокер 0, робот с позицией → сброс (ручное закрытие).
- **Ручная подгонка**: `POST /position/adjust` — state-only; ручные закрытия попадают в журнал (`_record_manual_trade`, 287f092). `GET /sync?lots=N` — жёсткая подгонка счётчика лотов (FIFO-удаление старейших).

## 9. State и восстановление

`of_state.json` (сохранение каждые 30 с и при каждом API-действии): позиция (dir, avg, lots, `lotQueue` с ценами и `added_ts` каждого лота, levels, peak), PnL, журнал сделок, `mode`, `activeAccount`, `symbol`, `direction_filter`.
При старте состояние загружается → позиция переживает рестарт. Восстановленные лоты без grace-периода. Путь state — от `__file__`, не от cwd (d04dc18). При shutdown mode сохраняется (c89dadb).

## 10. Watchdog (устойчивость)

- **Цена stale > 60 с** → полный reconnect WS-hub (не чаще 1 раза в 30 с).
- **Трейд-стрим мёртв > 120 с** → reconnect (цена может жить через quotes).
- **Fallback цены**: quotes-поток 0 → REST `get_last_quote`.
- **Fill-подписка**: WS ORDERS (F-019); paper-филлы симулируются `OrderManagerRest4`.

## 11. HTTP API (:5080, многопоточный)

| Endpoint | Назначение |
|---|---|
| `GET /status` | полный статус: mode, symbol, позиция, PnL, параметры, VP/VWEMA/**HL**/CVD-метрики, broker; фильтры журнала `?startDate&endDate&all=true` |
| `GET /health` | liveness |
| `GET /account` | счета и активный |
| `GET /sync?lots=N` | жёсткая синхронизация лотов с брокером |
| `POST /start` | режим running (сброс entry-lock) |
| `POST /stop` | снять ордера + закрыть позицию по рынку |
| `POST /pause` | PAUSE-семантика: без наращивания, выходы работают |
| `POST /restart[?force=1]` | **регламентный рестарт (порт 24e6cb2)**: FLAT-чек (409 при позиции без force), cooldown 90 с (таймстамп в `of_restart_ts.json`, переживает рестарт; force НЕ обход cooldown), сохранение state, отложенный detached-перезапуск: Windows — helper-процесс «старт нового → kill старого» через 3 с; Linux — `systemd-run` |
| `POST /params` | изменить параметры (persist в of_config.json; стабильный путь от `__file__`; битый JSON → 400) |
| `POST /instrument` | **F-017 live-смена инструмента** (только stopped + FLAT, иначе 409 — guard перенесён/сохранён по решению от 28.09; persist ROBOT_SYMBOL/ROBOT_TICKER в .env); после смены — пересоздание стратегии/ордерера, resubscribe hub, VWEMA warmup |
| `POST /position/adjust` | ручная корректировка позиции (state-only, с журналированием закрытий) |
| `POST /account` | переключение счёта main/edp |
| `POST /trades` | журнал сделок с фильтром дат |
| `POST /load-state` | перечитать of_state.json с диска |
| `POST /reset-stats` | обнулить PnL/журнал/roundTrips |

## 12. Выбор инструмента (SiU6 → SiZ6 и т.д.)

- **Runtime-смена**: `POST /instrument` (F-017) — без рестарта процесса; только в stopped + FLAT.
- **Guard позиции/режима**: robot running/paused или позиция открыта → 409, смена невозможна до закрытия позиции и остановки (решение Дмитрия 28.09: оставить live-свитчинг, guard сохранён).
- **Persist**: ROBOT_SYMBOL/ROBOT_TICKER пишутся в robot/.env для следующего старта процесса.
- После live-свитча: обнуление/пересоздание состояния стратегии, новый OrderManagerRest4 на новый символ, resubscribe WS-hub, VWEMA warmup, `save_state()`.
- Кросс-символьная защита: события/филлы чужих символов игнорируются (5339d2d).

## 13. Актуальные параметры (of_config.json, сентябрь 2026)

> Решение Дмитрия 28.09.2026: параметры НЕ меняем при переносе фич (значения ниже — из репозитория; боевой файл на машине робота правится через /params и может отличаться).

| Группа | Параметр | Значение |
|---|---|---|
| Инструмент | symbol | из robot/.env (ROBOT_SYMBOL, напр. SiZ6@RTSX) |
| База | timeframe | M5 |
| | lots | 1 |
| | commission | 0.90₽ (сторона), RT 1.80₽ |
| Вход/выход | signal_confirm_count / exit | 1 / 777 (реверс-выход факт. выкл) |
| | direction_filter | both |
| | stop_loss | rub / 3 000₽ (позиционный + daily-stop входов) |
| | min_profit_per_lot | 38 пт |
| | max_hold_minutes | 7777 |
| | close_half_pct | 40% от пика → добить остаток |
| | close_price_all | 0 (выкл) |
| Partial TP | spread | 33 пт |
| | partial_tp | true |
| Усреднение | step_average | 36 пт |
| | max_average_levels | 35 |
| | step_atr | false |
| Пирамидинг | step_pyramid / max_pyramid_levels | 25 пт / 1 (выключен) |
| CVD | use_cvd_accel / period / threshold | true / 10 / 777 |
| | use_cvd / EMA fast/slow | true / 7 / 21 |
| | agg_window / agg_ratio_threshold | 10 / 0.5 |
| Фильтры | use_vah_val (mode breakout) | true, бин 53 пт, VA 70% |
| | use_vwema | true, 50/100, flat 0.5, block counter |
| | vwema_avg_exit (B2) | false |
| | **hl_filter** (новое) | **false по умолчанию; параметры `hl_window` (24) / `hl_delta` (150.0); включается из UI/params** |
| | enable_ob_filter / ob_min_lots | true / 20 (радиус 50 пт) |
| | use_dm_wall | true |

## 14. Сводка поведения одной строкой

Робот входит **1 лот** по сигналу CVD-ускорения/CVD-тренда **только внутри Value Area**, с ликвидностью в стакане, у экстремума HL-окна (лонг у лоу / шорт у хая, если фильтр включён) и не против VWEMA-тренда; при движении против — усредняется сеткой (вне VA пауза с catch-up при возврате, жёсткого va-stop больше нет), каждый лот в +spread закрывает LIFO-скальпом; остаток ≤40% от пика закрывает целиком; полный выход — SL, +min_profit/лот, таймаут или принудительные close_price_all/manual_stop; всё считается по реальным филлам брокера с F-043-защитой от двойного закрытия, позиция переживает рестарты (в т.ч. регламентный POST /restart), стримы лечатся watchdog-ом, инструмент меняется live из UI.

## 15. Тесты

- `tests/test_of_port_features.py` — смок перенесённых фич: `get_hl_window`, вердикты `_hl_state`, HL-гейт входа (блок середины / проход у лоу), VA-пауза усреднения + catch-up, дефолт hl_filter=OFF. Запуск: `python tests/test_of_port_features.py` (пути к py4 проставляются автоматически).
- `tests/test_close_half_pct_check.py` — семантика close_half_pct.

## 16. История переноса (28.09.2026, план утверждён Дмитрием)

| # | Что | Откуда |
|---|---|---|
| 1 | HL-фильтр входа (params, гейт, `_hl_state`, `get_hl_window`) | ca58913 |
| 2 | Пауза усреднения вне VA + catch-up; **удаление VA BREAKOUT STOP** | a6ec3b8 |
| 3 | VP backfill при старте | server-main (fix 22.09.2026) |
| 4 | POST /restart (FLAT-чек, cooldown 90с) + Windows-механизм перезапуска | 24e6cb2 |
| 5 | Инструмент-свитчинг: оставлен наш live `/instrument`, guard 409 сохранён | решение Дмитрия |
| 6 | Параметры не менять | решение Дмитрия |
| 7 | Документ заменяет устаревший `docs/orderflow-spec.md` (v3 от 19.06.2026) | — |

Не откатывались (наши фиксы поверх): F-043 anti-double-close, F-019 fast execution, threaded API, честный broker-sync, PAUSE-семантика, ротация логов.
