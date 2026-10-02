# PROGRESS.md — Роботы

Прогресс роботов. Обновлять при каждом изменении в robot/.
Формат: дата — робот — что изменилось — статус/доказательство.

---

## Текущее состояние (2026-08-19)

| Робот | Стек данных | Стек ордеров | Запуск | Режим |
|---|---|---|---|---|
| paper_lab MXU6 :5181 | **WS-hub** (новый) | **orders_rest4** (paper-эмуляция) | 🟢 Работает с 18.08 15:51 | paper, soak PC-005 |
| OF MX :5081 | gRPC (finam_compat) | orders_dp → DataProvider | 🔴 Остановлен | ждёт Этап 5 |
| OF Si :5080 | gRPC (finam_compat) | orders_dp | 🔴 Остановлен | ждёт Этап 5 |
| VP Scalp :5070 | gRPC | orders | 🔴 Остановлен | — |
| Arb ×3 :5090-5092 | gRPC | orders_arb | 🔴 Остановлены | — |

## Лента изменений

### 2026-09-28 — F-044: порт фич OF-Si с server-main (утверждено Дмитрием)
- strategy_of.py: HL-фильтр входа (hl_filter/hl_window/hl_delta, гейт после direction_filter, verdict «both» для узкого окна); _hl_state() в /status; AVG VA BLOCK — пауза усреднения вне VA + catch-up при возврате.
- orderflow_engine.py: get_hl_window() (high/low последних N закрытых баров).
- main_of.py: VP backfill при старте (M1-бары с полуночи МСК → Volume Profile через REST get_bars); POST /restart (FLAT-чек 409/force, cooldown 90с, Windows detached-helper «старт нового → kill старого»); **удалён VA BREAKOUT STOP** (позиция вне VA больше не закрывается насильно — пара к VA-паузе усреднения, a6ec3b8).
- Решения Дмитрия: live /instrument сохранён (guard stopped+FLAT → 409 подтверждён), параметры не менять.
- Документация: docs/ORDERFLOW_ROBOT_SI.md переписан (наша hub-архитектура + 16 разделов), устаревший orderflow-spec.md заменён.
- Тесты: tests/test_of_port_features.py 5/5 PASS; py_compile 3/3 OK. Не откатывать: F-043/F-019/threaded API/honest sync/PAUSE/ротация логов.

### 2026-08-24–25 — Управление и коррекция
- /position/adjust (71992c2 Si, 287f092 MX): ручная установка позиции (dir/лоты/avg/lot_queue) без ордеров; UI-блок в обеих edit-панелях (a67b0a0). Тесты: +3/-3/FLAT/валидация + CDP PASS.
- Кнопки (f06e2e8): ПАУЗА ведёт позицию (main_loop: process_tick в paused, entry/average/pyramid блокированы, close/partial исполняются); СТОП закрывает всё; СТАРТ будит. Живые тесты обоих роботов.
- /params: cfg-путь от __file__ (не cwd — конфиг плыл при запуске из другой папки), try/except 400.
- Автозапуск Windows (004898b): ТОЛЬКО сервер; роботы — вручную (d55c515, решение Дмитрия: paper только по явному указанию).


### 2026-08-21 — REAL-запуск MXU6 (:5081, счёт main 1225953)
- Дмитрий одобрил real-режим. Предстарт: позиции на обоих счётах 0, state почищен.
- **Баг real найден и исправлен сразу**: SDK 4.3.3 ждёт Side 'SIDE_BUY'/'SIDE_SELL', orders_rest4 слал 'buy'/'sell' → place_market падал (ордер НЕ ушёл — ошибка сохранила от случайного шорта при фантомном CLOSE_ALL). Фикс закоммичен.
- Стартовая последовательность: REST4 OK (equity 914 935 ₽), WS-hub данные, VWEMA warmup 44 бара (dir=-1), FLAT, mode=running/paper=false.
- Наблюдение: phantom-позиция в state (dir=1 lots=2 из paper) вызвала мгновенный CLOSE_ALL при старте — в real это привело бы к продаже 2 лотов без позиции; ошибка side-маппинга предотвратила. Урок: перед real всегда полный state-reset + проверка позиций брокера (добавлено в процедуру).

### 2026-08-20 — PORT на боевых роботов (Этап 5, ТЗ: paper_lab/SPEC_STAGE5_PORT.md)
- Code-агент выполнил 8 шагов (e021d0a→540c9c0): PORT-0 общие модули в robot/ (hub_adapter/finam_rest4/orders_rest4/patch_sdk/py4), A1+A2 данные→WS-hub, B1+B2a+B2 ордера→REST 4.3.3 (+wait_fill), C1+C2 cleanup (0 зависимостей gRPC/DP у обоих OF-роботов).
- **Баг-фикс (20.08, soak)**: paper-режим не применял действия к стратегии → вечный CLOSE_ALL-цикл при открытом state. Фикс: paper-ветка исполняет close_all/entry/average/pyramid/partial_tp на стратегии. Оба робота перезапущены detached, dir=0/FLAT, данные живые.
- Текущее состояние: оба OF-робота (MXU6 :5081, SiU6 :5080) работают в paper на новом стеке; soak продолжается.

### 2026-08-18
- **paper_lab (PC-005, a25d89e):** paper-робот MXU6 полностью переведён на новый стек — данные из WS-хаба (hub_adapter), ордера/позиции/бары через REST 4.3.3 (finam_rest4 + orders_rest4). gRPC и DataProvider удалены из lab. Патчи SDK: required-поля Position, stale-JWT на /sessions, httpx-timeout (patch_sdk.py). Живой тест: VWEMA warmup 60 баров (REST), OB/цена из WS, DESYNC-монитор корректен. Soak запущен (cron ежечасно → papercuts.md).
- **paper_lab (PC-003/004, 819e96c/3570399):** hub_adapter (WS→compat мост), finam_rest4, изоляция py4. Найдены 2 бага SDK 4.3.3.
- **finam_hub.py (f87a0b1):** единый WS-хаб Finam (Этап 1 миграции). Тест 60с: 1198 сообщений, 0 ошибок.

### 2026-08-17 (merge origin/main — функционал роботов)
- **strategy_of.py:** VWEMA Avg Exit (B2) — не усреднять против VWEMA + закрытие при развороте; VAH/VAL range-режим (торговля только внутри VA).
- **main_of.py:** VA BREAKOUT STOP (выход из VA при позиции → close all); VP live-питание сделками; /reset-stats; реконструкция VP при смене VAH/VAL-параметров.
- **of_config_mx.json:** параметр step_average изменён; vwema_avg_exit=true.
- **main_of_mx.py:** MXU6-робот синхронизирован с прод-репо.

### 2026-08-13
- Миграция finam_compat (shim FinamPy поверх finam-sdk 2.19) — роботы работают без старого FinamPy.

### До 2026-08 (историческое)
- PSAR Grid MM (C#): продовая статистика ~890K₽/мес на Si.
- VP Scalp Grid: paper 100% WR без багов (17/17), переход в real 26.05, остановлен по команде.
- Арбитраж: 3 пары, общий модуль arb_common/, risk-модуль daily-stop.

## Soak PC-005 (живой журнал)

Актуальные снапшоты — в [paper_lab/papercuts.md](paper_lab/papercuts.md), раздел «PC-005 soak-журнал» (пишет cron-монитор ежечасно).

## Следующие шаги

1. Soak ≥1 торговый день → PORT-вердикт PC-005
2. Этап 5 (по шагам, одна фича за раз): main_of_mx.py → hub+rest4 (paper), затем real минимальным лотом (только по явному одобрению Дмитрия) → main_of.py → арбитраж → удаление gRPC-зависимостей
3. DataProvider :5060 — вывод из эксплуатации после Этапа 5


## 2026-10-01 — Уборка robot/: −5,6 ГБ, код-зомби удалён, R-020 ротация логов

- Удалены soak-логи (5398 МБ txt от soak-прогонов августа), *.phase1.bak, corrupt-state bak.
- Из git: main_patch.py, collector.py, orders_grpc.py (никем не импортируются; gRPC запрещён), arb_lkoh_lku6/ (старая схема, не запускался).
- arb_sber_sru6/ и arb_br_calendar/ сжаты до *.example конфигов (устаревшие копии main_arb.py без F-048/F-050 удалены; живые 5091/5092 работают с корневым robot/main_arb.py).
- README.md: таблица роботов приведена к env-схеме инстансов, LKOH убран.
- Логи: R-020 (30 дней) — удалено 53 файла (208 МБ).

## 2026-09-29/30 — Арб-роботы: F-048 журнал = цены брокера, раскорреляция L14, F-050 логи режимов

- **F-048 (29.09):** журнал арба писал оценки (leg A лимитка, leg B стакан) — gRPC-стримы Finam без auth metadata (12607 ошибок/день), в proto Order нет average_price. Фикс: auth во все стримы (finam_compat.py), _resolve_fill_price (trade sub → REST GetTrades → gRPC → оценка с флагом is_real) в orders_arb.py, order_id/priceSource в журнале (strategy_arb.py), авто-ресинк при старте + POST /reload-state (main_arb.py), robot/resync_journal.py (dry-run/--apply). Реальный итог 29.09: −69.62₽ вместо +644.19₽. Тесты 25/25. Перезапуск 17:52 — API подтвердил broker-цены.
- **Раскорреляция L14 (30.09):** тейк сорвался (gRPC limit failed + DP 5060 лежит), нога A исполнена брокером (SELL 100 @97.43) но не записана → у брокера остался голый SHORT GZZ6 −1. По решению Дмитрия (вариант B): kill процесса, BUY GZZ6 1 @9998 (11:40:06), позиция 0/0, L14 закрыт в state по фактам (+1.87, manual_resync_close), realized −79.09. Старт 11:52 без env подхватил мусорный дефолтный arb_state.json (фантомный L15 от 22.09, daily_stop → EXIT ALL фантома, лимитка SELL GAZP успела уйти — отменена, ущерба нет). Повторный старт 12:00 с env — корректный state, running. Дефолтный arb_state.json очищен.
- **F-050 (30.09):** main_arb/main_of/main_of_mx — log.warning на каждый POST /start /stop /pause (переход режима + размер позиции); app.js — confirm() на кнопках Start/Pause/Stop (фоновая вкладка не может молча включить торговлю — инцидент: OF Si самонабрал 4 лота за 12:00–13:25). py_compile 3/3, node --check OK, тесты 25/25. OF Si/MX перезапущены с логированием; startup-реконсиляция поймала «broker 4 lots vs robot FLAT → NOT entering».

## 2026-09-14 — Фикс: честная сверка позиции брокера (знак шорта + ретраи)

Инцидент 11:46 МСК: MXU6 шорт 1 @232200 исполнился (order 1984994379379006531, POST /orders 200),
но DESYNC-монитор закричал "robot=1 broker=-1" — Finam отдаёт quantity СО ЗНАКОМ (шорт=-1),
а робот хранит шорт как 1 лот. Ложная раскорреляция → ручной стоп, закрытие 232325 (~-125 пт).

Фикс (orders_rest4.py + main_of.py + main_of_mx.py):
- get_broker_position: {lots: |lots|, dir: ±1, avg_price} | None; transient-ошибки (429 code=8,
  токен code=16) ретраятся 3 раза; при неуспехе None = "позиция НЕИЗВЕСТНА", не 0.
- positions_in_sync(): честное сравнение лотов И направления; broker=None — не тревога.
- avg_price: читаем average_price/avg_price/weighted_average_price (раньше avg_broker всегда 0).
- Мониторы/verify-ветки (close_all/entry/partial_tp/startup) знают про None.
- cancel(): "cannot be canceled" (ордер уже исполнен) — info, не ERROR.
- Startup reconciliation при None: пропустить проверку (раньше transient-429 остановил бы робота).

Тесты: robot/test_broker_pos_sync.py — 32/32 PASS (мок-брокер, сценарий инцидента включён).
Роботы НЕ перезапускались; фиксы подхватятся при следующем ▶ Дмитрия.


## 2026-09-15 — Старт роботов на счёте EDP (по команде Дмитрия)

- Дмитрий разрешил старт и перевёл торговлю на другой счёт (EDP). Разведка: под ключом два счёта —
  старый FORTS 1225953 (флэт, free 2 787 ₽) и UNION 2049688 (13,6 млн ₽, портфель акций/ОФЗ, фьючерсов Si/MX нет).
- В роботах уже был multi-account: ACCOUNTS {main, edp}, activeAccount в state-файле. Переключение:
  activeAccount=edp в of_state.json/of_state_mx.json до старта. .env FINAM_ACCOUNT_ID 1225953 → 2049688 (3 файла).
- Фикс 1 (Program.cs): автостартовый сервер не находил python3 (урезанный PATH) → 500 на /api/of/process/*/start.
  Добавлен ResolvePython() (PYTHON3_EXE → известные пути → PATH scan). Коммит 3e3d953.
- Фикс 2 (Program.cs): /api/positions падал на UNION (quantity "173.0" дробной строкой, int.Parse → catch → []).
  Теперь double.TryParse + round; панель показывает все 12 позиций ЕДП. Тот же коммит.
- Рестарт сервера 2 раза (сборка с роботами блокирует dll — сначала стоп процессов).
- Финальное состояние 12:05 МСК: Si PID 10588 :5080 running, MX PID 13620 :5081 running, оба FLAT,
  account=edp (2049688), dirFilter MX=short (сохранён), DESYNC-монитор молчит (фикс c1bbbd3 в работе).
- Уроки: (а) activeAccount хранится в state — переключение счёта без правки кода; (б) UNION отдаёт
  quantity дробью — любой int-парсинг по EDP сломан; (в) не билдить при живом сервере.


## 2026-09-17 — F-017: живая смена контракта (UI селект -> робот)

- Дмитрий выбрал MXZ6 в меню — робот продолжил торговать MXU6. Цепочка была разорвана в 3 местах
  (UI только localStorage; C#-эндпоинт писал в несуществующий Linux-путь; робот читает символ лишь при старте).
- Роботы: POST /instrument (Si :5080, MX :5081) — живая смена при stopped+FLAT (иначе 409):
  пересоздаются strategy/orders/hub-подписки, VWEMA прогревается барами нового контракта,
  история сделок/round trips/realized PnL сохраняются, позиция сбрасывается (новый контракт = FLAT).
  Выбор сохраняется в robot/.env — рестарты держат контракт. Общий код: instrument_switch.py.
- Сервер: /api/robot/update-instrument теперь пишет robot/.env (был /root/...), поддерживает of|of_mx.
- UI: селекты OF/MX вызывают applyInstrument(): живая смена (или env, если процесс не поднят),
  выбранное значение берётся из живого /status.symbol. app.js?v=89.
- Проверено живьём: MXU6->MXZ6->MXU6, рестарт с сохранением MXZ6, 409 на running, Si unchanged, регистр SiU6 сохранён.
- Коммит 55ff947 запушен. robot/.env в git не входит (gitignore) — это правильно.

### 12:59 — коррекция state MX (вариант б, одобрен Дмитрием)
- Удалены 16 призрачных сделок 17.09 (cross-symbol инцидент), realizedPnL 81611.20 -> 33606.68 (-48004.52),
  dailyPnL -> 0. roundTrips не тронут. История сделок 200 -> 184.
- Брокерская истина: MXZ6 32 сделки = -725 руб (спред), MXU6 2 = 0. Позиции флэт.
- Процедура: kill процесса (чтобы save_state не перезаписал) -> правка of_state_mx.json -> старт.
  MX снова stopped/MXZ6/edp, цена своя (229150).

## 2026-09-18 — Арб-роботы подключены к Finam gRPC (БЕЗ запуска, команда Дмитрия)

- Состояние было: арб-роботы (5090-5092) мёртвый код — SDK 4.3.3 не содержит FinamClient, proto grpc пак
  в py4 отсутствовал физически.
- Сделано (коммит fa1fe72):
  1. Сгенерированы python gRPC стабы из C# protos -> robot/py4 (proto namespace finam_trade_api.proto.*;
     google api/type top-level; абсолютные импорты protoc переписаны; version-check запатчен под grpcio 1.84).
  2. finam_compat: FinamClient shim = TokenManager + gRPC стабы api.finam.ru:443 c authorization metadata
     (lowercase ключ!). REST SDK клиенты через композицию. Двойная обёртка stubs устранена.
  3. config_arb: GZM6 -> GZZ6 (июньский истёк, GZZ6 ликвиден); env FINAM_ACCOUNT_ID тоже читается.
- Живые проверки: gRPC LastQuote MXZ6 (bid/ask/last OK), стрим котировок GAZP@MISX + GZM6@RTSX OK,
  main_arb импортируется и резолвит пару GAZP/GZZ6 на счёте 2049688.
- Не сделано (осознанно): роботы НЕ запущены (закон: старт только Дмитрием через UI ▶).
- Следующий шаг перед запуском: дивидендный календарь GAZP (GZZ6 до 18.12.26) в arb_config.json.

### 14:53 — F-018: арб-роботы в UI (репорт «статус не подключен»)
- UI показывал «Не подключён» т.к. опрашивал порты 5090-5092 напрямую, а процессы никто не стартовал (закон).
- RobotProcessManager: +arb_gazp(:5090 GAZP/GZZ6), arb_sber(:5091 SBER/SRZ6), arb_br(:5092 BRU6/BRV6) с env пар.
- UI: ▶ поднимает процесс через сервер, ⏹ убивает; offline = «⚪ Процесс не поднят» (не «Не подключён»). v=91.
- Endpoint /api/of/process/{arb_*}/status -> {alive:false} проверен живьём. Коммит 33e6726 запушен.

### 15:14 — Контракты арб-роботов обновлены на декабрь-26 (команда Дмитрия)
- GAZP: GZZ6 (уже был после 18.09 утреннего фикса), SBER: SRZ6 (SRU6 истёк), BR-календарь: BRV6/BRZ6 (BRQ6/BRU6 истекли).
- Ликвидность подтверждена барами REST: GZZ6/SRZ6/BRV6/BRZ6 = 33-34 бара/48ч; BRU6/BRQ6 = 0.
- Program.cs env, UI заголовки/пары/опции селектов/логи обновлены; истёкшие помечены «(истёк)», декабрьские добавлены. v=92.
- Сервер пересобран/перезапущен; MX (running, MXZ6) пережил рестарт. Коммит 0da0d33 запушен.
- Верифицировано: /api/of/process/{arb_*}/status POST → {alive:false} — UI корректно покажет «Процесс не поднят».

### 15:29-15:47 — Фикс стартовой цепочки арб-роботов (репорт: arb_sber не поднялся)
- 3 блокера: (1) finam_compat не импортировался из robot/ (нет bootstrap); (2) две версии strategy_arb
  затеняли друг друга (robot/ июльская без dev_ann vs arb_common сентябрьская с Fix#1-4) — sys.path порядок
  arb_common > robot; orders_arb берётся из robot/ явно (в arb_common копия без cancel_pending_orders);
  (3) gRPC auth: metadata ключ lowercase 'authorization', старый (fp.metadata,) подменяется шимом;
  RobotProcessManager теперь передаёт --port (arb_* иначе все стартовали бы на 5090).
- arb_sber жив на :5091 (paper): basis стримится (price_a=276.7, price_b=28633), mode=stopped, ждёт ▶.
- Коммит 324dfda запушен.

## 2026-09-21 — F-019: быстрое исполнение OF (1+3+5, по выбору Дмитрия)

- Замер 18.09: сигнал→исполнение p50=1с, p90=24с. Хвост — close_all: cancel-кластеры 30..149 ордеров
  по (REST ~200мс + sleep(0.5)) на ордер = до 31с слепого времени в main_loop.
- 1) cancel_many() без per-order sleep (rate-limit ≤180/мин); close_all/stop/pause используют;
     get_active_orders фильтрует по symbol+активным статусам (было: все ордера счёта —
     cancel цепочка задевала бы ручные лимитки Дмитрия на ЕДП).
- 3) fill из REST: sleep(0.3)+надежда → orders.wait_fill(oid, 1.2с, poll 0.06) — фактическая
     average_price от брокера; журнальные «стратегийные» филлы уходят.
- 5) WS ORDERS-канал: hub_adapter.start(on_my_trade=..., account_id=...) — свои филлы событием;
     _on_my_trade (был мёртвым кодом) принимает dict-филлы и мгновенно ставит _last_fill_price.
- Ожидаемый эффект: close_all 26с → 2-3с; журнал PnL по фактическим ценам; main_loop слепнет на 0.
- Тесты: robot/test_fast_exec.py 15/15 PASS (мок-брокер: фильтры, скорость cancel_many, wait_fill,
  парсинг ORDERS payload, чужие символы).
- Коммит 8438b60 запушен. Роботы НЕ перезапускались — подхватят при следующем ▶ Дмитрия.

### 11:19 — Фикс расчёта базиса: hedge_ratio 10 → 100
- Базис обоих арб-роботов считался завышенным ×30 (спред +25777₽ вместо +940₽ на SBER): в arb_config.json
  стоял hedge_ratio=10 (наследие старого конфига), при том что 1 фьючерс = 100 акций.
- Верификация после фикса (GAZP/GZZ6): spread=+335₽, fair=+335.2₽, dev=-0.22₽ — рынок около fair.

### 11:29 — Фикс ▶ арб-робота (репорт: «нажал старт — не запустился»)
- Причина: startViaServer (F-018) только поднимал процесс, а POST /start на робота (включение mode=running)
  потерялся при переносе кнопки на серверный process manager.
- Фикс: startViaServer после подъёма процесса ждёт 2.5с и шлёт POST /start роботу; stopViaServer сначала
  останавливает торговлю, потом убивает процесс. v=93.
- Попутно: SubscribeQuote/SubscribeLatestTrades стримы finam_compat ходили без auth-metadata
  (UNAUTHENTICATED цикл в err) — jwt добавлен.
- Верификация: arb_gazp перезапущен → robot /start → mode=running, basis стримится, UNAUTH=0. Коммит 637cf06.

## 2026-09-23 16:3x — F-038: тейк-профит по реализуемому PnL (b82ecd0)

Корень утечки арба GAZP/GZZ6: 147/149 выходов profit_target отрицательные (avg -17,13) - тейк по last-принтам, исполнение по стакану+slippage (~15,5 круг). Фикс (решение Дмитрия): решение о выходе по реализуемому PnL (bid/ask из OB-трекеров - slippage 5bps - комиссии), FIFO по слоям, подтверждение 3 тика, протухший стакан блокирует решение, входы не тронуты (basis_calc на last). Тесты 42/42. Оба инстанса (GAZP/SBER) - код общий, подхватят при следующем рестарте. Реестр фич восстановлен: F-018..F-038 (правило: реестр+дневники обновлять при КАЖДОМ коммите). Детали: progress.md корневой 23.09.

## 2026-09-25 12:1x — F-039: честные фиды для тейка по мин. профиту (BR-календарь)

**Симптом (Дмитрий):** BRV6/BRZ6 :5092 «не закрывает и не старается» при Мин. профит 25 ₽; карточка показывала Unrealized +46,5 ₽.

**Диагноз:** механизм F-032 исправен (стартовал 11:17 уже с фиксом), но голодал по данным: Finam quote-стрим в цикле UNAUTHENTICATED (reconnect #37+), ISS-fallback не подпитывал фьючерсную ногу A (ветка только для акций), маршрутизация «last < 1000 → A» на календарном спреде (обе ноги ~95–105 ₽) лила BRZ6-принты в price_a BRV6 (last 95,325 vs OB 105,64 — фантом −10 ₽). Реализуемый PnL по стакану: −0,8…−1,2 ₽/слой × 5 слоёв ≈ −6 ₽ — тейк +25 ₽ честно не срабатывал. Плюс 5 входов за 12 секунд на разогнанном dev_ann (−13% ↔ −49% на мусорных данных), остановлено капиталом (used 10 529 > 10 000).

**Фикс:** route_quote (тикер → ближайший OB mid ±2% → drop неоднозначных); ISS для фьючерсной ноги A; last-guard ±2% для обеих ног (при расхождении обновление mid); карточка Unrealized = реализуемый PnL (last-вариант в localUnrealizedPnl). Входы не тронуты.

**Тесты:** 53 PASS / 0 FAIL (11 новых F-039). Коммит: см. git log F-039.

## 2026-09-28 10:5x — F-040: санитарный контроль стакана на входе

**Симптом (Дмитрий):** «ошибка PnL в торговом журнале» GAZP/GZZ6 — realized +15 228 ₽ (в пятницу был −1 693).

**Диагноз:** 28.09 07:14:11–38 Finam отдал мусорный OB GAZP (ask=93.00/bid=105.00 при рынке 98.6–99.8; GAZP не торговался в 93/105 весь сентябрь). 12 фантомных кругов: вход paper по ob_a.best_ask=93.00 (LegResult всегда filled), через тик ob_a.best_bid=105.00 → realizable +1183.56 ≥ 25 → мгновенный тейк. +16 632 ₽ фантома в журнале, realized +15 228. Finam-стрим всё утро в UNAUTH-цикле — валидный OB не приходил.

**Фикс:** _orderbook_sane() гейт в check_entry (свежесть <5с, bid<ask, mid ±1% от last, спред ≤2%; до первого снапшота — не блокирует) + дроп crossed-снапшотов в OB-поллере. Выход уже защищён (F-032/F-039). Тесты 59/59 (6 новых). Входная dev_ann-логика не тронута.

### 28.09 — F-041: починен фильтр направления OF-Si (и хвосты MX)

**Симптом (Дмитрий):** «Робот не переключается между режимами шорт/лонг, только оба».

**Причина (3 слоя):**
1. `ofSetDirection` — обработчик селектора «Фильтр» (ОБА/LONG/SHORT) Si-панели — **отсутствовал в app.js**. ReferenceError на каждый клик; селектор визуально менялся (браузер меняет value до onchange), робот ничего не получал.
2. `toggleOfSignal` — обработчик чекбоксов CVD Trend / dm_wall / CVD Accel Si-панели — тоже отсутствовал (та же проверка выявила).
3. `ofRobotSaveFromPanel`/`ofMxRobotSaveFromPanel` не отправляли `direction_filter` при «Сохранить».

У MX-панели аналоги были (`ofMxSetDirection`/`toggleOfMxSignal`) — сломана была только Si-ветка + save-тела обоих.

**Фикс (v=113):**
- app.js: добавлены `ofSetDirection(value)` (POST `/params {direction_filter}` на 5080 + `ofRobotPoll()` + журнал) и `toggleOfSignal(param, value)` (POST `/params {[param]: value}` + журнал).
- Оба save-body дополнены `direction_filter: el('ofDirFilter'/'ofMxDirFilter')?.value || 'both'`.
- Деплой: app.js → bin/Debug/net10.0/wwwroot, index.html v=112→113 (оба).

**Проверка:**
- `node --check` обеих копий — OK.
- Сервер отдаёт v=113, в JS есть обе функции (проверено GET /js/app.js?v=113 после логина).
- E2E API-эмуляция действий панели на живом Si (5080, mode=stopped): POST direction_filter short → status=short → long → status=long → both → status=both; полный save-body с direction_filter=short → of_config.json direction_filter=short; восстановлено both.
- Сам робот не менялся: strategy_of.py block-entry по direction_filter (строка ~751) работал всегда.
- autoglm браузер-проверка не прошла (Extension did not connect in time) — финальное подтверждение за Дмитрием: Ctrl+F5, переключить фильтр, в «Лог событий» придёт «OF-Si direction filter → SHORT».

## 2026-09-28 10:5x — F-041: real-исполнение готово к бою (нормализация единиц + хедж)

**Задача Дмитрия:** проверить логику исполнения и перевести GAZP/GZZ6 в боевой режим.

**Аудит real-ветки нашёл 3 критичных бага единиц:** (1) брокер возвращает executed_quantity в акциях (100 на 10 лотов), open_layer получал это как lots_a → позиция, PnL, стопы, капитал ×10; (2) min_fill_ratio акции/лоты = всегда pass — фильтр частичных заливок мёртв; (3) выход конвертил fake-lots обратно в акции (1000 на вход 100) → голая позиция 900 акций. Плюс частичный fill ноги A не ужимал ногу B.

**Фикс:** нормализация fill к лотам сразу после заливки (кратно лоту && > запроса → делить), честный ratio, пропорциональный leg B. Тесты 59/59. 

**Боевые проверки:** аккаунт 2049688 (EDP): equity 13.48М ₽, free 11.1М ₽, позиций GAZP/GZZ6 нет; OB GAZP/GZZ6 живой (16 обновлений/60 с), price_a синхронизирован со стаканом; риск: Finam UNAUTH-цикл (reconnect каждые ~30 с) — страховка: ISS-фиды + sanity-гейты F-039/F-040.

## 2026-09-28 — F-043: анти-дабл-клоз guard (фантомная позиция Si) — 7eb550d
- **Инцидент**: DESYNC-шторм 12:02–12:58 (robot=0/broker=2, robot=-1/broker=1…). Корень: в 12:01:27 close_all дошёл до биржи и исполнился (IOC buy 2 @85748), но REST-вызов упал по таймауту -> робот решил «не ушло» -> повторный close в 12:01:51 (buy 2 @85740) -> неттинг: SHORT −2 превратился в фантомный LONG +2. Фантом ликвидирован вручную (12:58:43/12:59:01, два ордера без комментария), после чего монитор замолчал.
- **Фикс** (main_of.py + main_of_mx.py, коммит 7eb550d): guard перед ретраем close_all — если прошлый close имел неизвестный исход, сверяемся с брокером: FLAT -> синк робота во ФЛЭТ без ордера; иначе блок ретрая с явной ошибкой. Guard разряжается после подтверждения (тест поймал: без разряда блокировалось бы легитимное закрытие), истекает через 5 мин. VA-stop: wait_fill 2с + запись в tradeHistory + верификация при DP-fail (раньше — слепой reset, сделки не записывались).
- **Тест**: tmp/test_f043_guard.py — 7/7 PASS (реальный _execute_action, фейковые orders/брокер; сценарии: инцидент-ретрай при FLAT, брокер неизвестен, guard истёк).
- **Попутно 28.09**: убиты зомби-близнецы 19100 (Si, сервер-спавн 10:20) и 9684 (MX, 25.09) — спам 'WS disconnected' прекратился. F-042 (самоисцеление websockets-импорта в finam_hub) — в бэклоге. Реальные сделки счёта: роботские ордера имеют комментарии of_* / va_stop; сделки 12:58 без комментария = ручная ликвидация фантома.

## 2026-09-29 — F-045: фикс краша check_entry у арб-роботов (2828c2b)
- Дмитрий прислал скрин GAZP/GZZ6 (SHORT 10×1, +59,8 ₽): «робот должен закрывать по мин профит, почему не делает?»
- **Корень**: strategy_arb.check_entry при открытом слое и signal=None (dev_ann вне порогов — норма) падал на `signal["side"]` → TypeError в main loop каждый тик; check_exit стоит после check_entry → тейк не вычислялся никогда. В логе 1107 крашей за 10:00–10:30; EXIT-строк нет вообще за всю историю файла.
- **Фикс**: guard `if signal and self.layers and not self._allows_averaging(signal["side"])`. Тест tmp/test_f045_arb_entry.py 6/6 PASS (реальный arb_common код). Код общий для всех арб-роботов (GAZP/SBER/BR).
- **Почему панель показывала плюс, а закрытия не было**: min_profit=15 ₽ считается по реализуемому PnL (стакан − slippage 5 bps − комиссии), что давало −1,11 ₽; +59,8 ₽ на панели — mid-PnL без издержек. Плюс выходы только в сессии 10:00–18:45 (use_evening=false).
- Нумерация: F-044 занят (перенос OF-Si фич 28.09), фикс назван F-045. Действует после следующего ▶ арб-роботов.

### F-045 активация (29.09 12:11, по слову Дмитрия «Перезапусти и примени»)
- Перезапущены все три арб-робота: GAZP PID 19048→2368 (--no-paper live), SBER 3544→12560 (paper), BR 2836→3868 (paper).
- Метод: taskkill → ожидание освобождения LISTENING (netstat, TIME_WAIT не мешает бинду) → detached-спавн с per-instance env ARB_CONFIG_FILE/ARB_STATE_FILE → health-check → сверка слоёв.
- Слои восстановлены из state 1-в-1: GAZP 1×SHORT(-1,10,1), SBER 2×SHORT, BR 4×LONG; mode=running у всех.
- Проверка: в новом логе 0 «Main loop error»; realizableUnrealizedPnl считается и обновляется (−1,11→+3,89 ₽ за 20с); прогрев dev-истории после старта (39-40/50) — входы разблокируются через несколько минут, выходы работают сразу.

## 2026-09-29 13:0x — F-042: зомби-процесс MXU6 ломал сохранение фильтров

Симптом: снятие галочки VAH/VAL Filter в панели MXU6 не сохранялось. Причина: два процесса на :5081 (зомби 10588 от 25.09 с мёртвым родителем + серверный ребёнок 26836) — POST и GET попадали в разные процессы; оба писали один of_config_mx.json. Зомби убит, use_vah_val=False применён и стабилен, конфиг записан. Ops-фикс, кода не менял. Урок: при старте через UI проверять, не занят ли порт зомби.

## 2026-09-29 — F-046 JWT self-heal + F-047 панель realizable PnL (61aedbc, f3cfce9)
- Дмитрий: «Робот всё ещё не закрывает по мин профиту» (скрин SHORT 20×2 +30,9 ₽).
- **F-046**: шторм «token expired» (350 строк/мин) делал стакан слепым >3 с → realizable=null → тейк не оценивался. Корень в finam_compat.py: JWT для gRPC refreshing только в __init__ через asyncio.get_event_loop() — в не-main потоках молча умирал; get_token() фолбэкал на raw-секрет. Фикс: _refresh_jwt() (Lock + 9-мин троттлинг + asyncio.run() в любом потоке), громкие ошибки, запрет raw-секрета. Тест 7/7. Рестарт 13:11–13:13 → шторм 0.
- **F-047**: панель показывала last-PnL без издержек (+30,9 ₽), робот решал по realizable (−19…−23 ₽: стакан − slippage − комиссии). Карточка теперь показывает realizable (при протухшем стакане '~' + last). v=115.
- Второй слой GAZP вошёл 12:24 (z=2.813) после F-045 фикса — механизм входов/усреднения работает.
- Рестарты арб-роботов: GAZP 24528 (live), SBER 24432, BR 20960. Слои восстановлены 1-в-1.

## 2026-09-29 14:0x — F-043: WS-хаб потерял websockets.connect — сигналы не шли

Симптом: MXU6 «не торгует», в UI сигналы — тиры. Причина: finam_hub с 25.09 почти не коннектился (28.09: 1426× AttributeError «websockets has no attribute connect», 0 коннектов) — trade/quote/bars потоки пусты, CVD/VWEMA не считаются. Атрибут в py4-websockets 17.0.1 есть; сбой зависит от порядка инициализации в потоке хаба. Фикс: явный однократный импорт connect с fallback (websockets.connect → websockets.asyncio.client.connect) + диагностика файла модуля. После рестарта MXU6: WS connected, 70 баров M5, VWEMA ready, CVD копится, цена живая. MXU6 в running.

## 2026-09-30 - F-051: reconciliation OF (main_of/main_of_mx) — позиция из state vs брокер

- Было: любой broker lots!=0 → «robot is FLAT» → _mode=stopped, даже если стратегия восстановила позицию из state (инцидент 30.09: Si 4 лота, MX 1 лот — оба stopped после рестарта).
- Стало: 4 ветки — match → mode сохраняется («STARTUP: position OK»); broker-pos+robot-FLAT → stopped; robot-pos+broker-FLAT → stopped (desync); mismatch lots/dir → stopped.
- Проверки: py_compile 2/2 OK; decision matrix 6/6 OK; tests/test_of_port_features.py SMOKE PASS.
- Роботы не перезапускались (R-001) — фикс подхватится при следующем старте Дмитрием.


## 2026-09-30 17:23-18:35 — F-052: тейки по фантомному стакану закрыты (GAZP/SBER/BR)

Симптом: 15/18 сделок GAZP-арба reason=profit_target с отрицательным PnL (realized −103.15₽), Дмитрий запретил убыточные закрытия. Корень: ISS-поллер robot/main_arb.py при OFFER=null рисовал ask=LAST; gRPC-стакан в 429-бэкоффе — решения принимались по фантомному аску (15:31:27: realizable=+51.89, факт −13.10₽). Exit-путь не имел sanity (вход — F-040).

Фикс (bb551ae):
- main_arb.py, ISS-поллер обеих ног: только реальные строки OB, никакой подмены LAST→ask/bid.
- arb_common/strategy_arb.py, _layer_realizable_pnl: sanity last∈[bid×(1−eps), ask×(1+eps)], eps 0.5% futures / 0.2% stock (по _is_futures), интервальная форма — широкий, но реальный стакан BR проходит; рассинхрон → None → тейк не квалифицируется.
- Гейты во всех трёх main_arb (robot/, arb_sber_sru6/, arb_br_calendar/): одиночный profit_target перепроверяется перед отправкой ордеров (_exit_qualifies по живому стакану), групповой profit_target_total требует все слои >0 и сумму ≥ порога; ALARM-лог при тейке в минус.
- Риск-выходы (stop_loss/daily_stop_loss), manual_stop, cleanup — без гейта, стоп-защита прежняя.

Тесты: +4 кейса F-052 в robot/test_arb_fair.py (мусорный OB → None и блок выхода; согласованный OB → тейк после 3 тиков; акция-нога 1.2% рассинхрона → None; широкий реальный OB проходит). Итого 62/62 PASS, py_compile 5/5.

Рестарт 18:32-18:35 (явное «делай» Дмитрия, R-001): kill /F + spawn с per-instance env (ARB_CONFIG_FILE/ARB_STATE_FILE/ARB_SYMBOL_* — зеркало RobotProcessManager Program.cs). Ключевой нюанс: on_shutdown (SIGTERM//stop) ставит mode=stopped → при старте cleanup закроет слои по рынку; рестарт с позициями — только kill /F при свежем state (<40 с, periodic save 30 с). Результат: 5090 GAZP/GZZ6 real PID 23636 (2 SHORT-слоя, −103.15₽), 5091 SBER/SRZ6 paper PID 34236 (1 слой, +2283.22₽), 5092 BRV6/BRZ6 paper PID 716 (4 слоя, +50.42₽); OB живые, state загружен 1-в-1, F-048-ресинк 0 правок. Смена бронирования: R-011 (раздельные логи) → F-054; F-052 закреплён за этим фиксом.

## 2026-10-01 — F-055: Order Flow BR (Brent) — портирование + интеграция
- Перенос продакшен BR-робота из server-main в main (коммит 75e18a9, ветка feature/of-br-robot): main_of_br.py, config_of_br.py, конфиги/стейты двух инстансов (:5082 main/1225953, :5083 edp/2049688).
- strategy_of.py смержен с серверным (fmt_price, session-окно, manual_adjust, float VP bins), наш фикс daily-stop close сохранён.
- F-048: стримы BR переведены на finam_compat (auth interceptor) вместо сырого FinamPy; proto через вендоренный finam_trade_api.
- C# RobotProcessManager: spec of_br/of_br_edp + env ROBOT_STATE_SUFFIX_BR/ROBOT_ACTIVE_ACCOUNT_BR; /api/of/config/{of_br,of_br_edp} прокси.
- UI: две строки BR в таблице роботов (read-only BRV6 бэдж), панель всех параметров §11, журнал с фильтрами, коррекция позиции, сохранение в робота или json при офлайне.
- Тесты: test_of_br_params (48 по §11), test_of_br_logic (11) PASS; регрессия arb_fair 32/32, pos_sync 32/32, C# 24/24; сборка 0 ошибок; node --check OK.
- Старт роботов НЕ выполнялся (R-001 — только Дмитрий).

- 10:43 b7dd4b6: UI-дополнения — панель SYNC лотов с брокером (GET /sync?lots=N absolute), кнопка Применить для close_price_all (0=выкл), метрика OB-ликвидности, ретраи старта (4х3.5с под ~15с коннект Finam), confirm на стоп, TDZ-примечание для OF_BR_INSTANCE_ORDER. node --check OK, скопировано в bin wwwroot.

- 16:02 749b6b8: finam_compat fix — SubscribeOrderTrade bidi-стрим требует request-итератор (итератор итератора ошибок OrderTradeRequest не iterable). Обнаружено при запуске BR-edp :5083; после фикса все 5 стримов стабильны (LatestTrades/OB/Bars/Quotes/OrderTrade).

- 16:11 Рестарт C#-сервера (PID 11920 вместо 13948) с новой DLL (of_br/of_br_edp spec в RobotProcessManager). Причина: кнопка ▶ BR-edp в UI давала unknown robot — старый процесс держал DLL от 25.09. Копирование сборки в bin, DOTNET_ROOT=C:\Program Files\dotnet обязателен (D:\AutoClaw runtime 8.0 не тянет net10). Все python-роботы пережили: 5080 SI, 5081 MX, 5090/5091/5092 arb, 5083 BR-edp (мой ручной процесс 23596). Зомби: 17884 (старый main_of_mx без порта).

- 16:26 16917d2: BR contract rollover BRV6->BRX6 (BR-11.26 фронт, октябрь истёк). config_of_br defaults + UI бэджи + перезапуск edp :5083 (mode=stopped, стримы BRX6 живые: цена 99.975, VP/POC 99.9, 30 баров). tradeHistory (67 сделок, realizedPnL -129.58) восстановлена из git-стейта после перезаписи процессом. Арб BRV6/BRZ6 календарь НЕ переведён (отдельное решение).

- 16:53 DP :5060 поднят (ручной запуск, fastapi/uvicorn доустановлены в D:\AutoClaw python; bootstrap sys.path добавлен в DataProvider/main.py — isolated build игнорирует PYTHONPATH; config.json обновлён: SiZ6/MXZ6/BRX6). Причина инцидента: заявки BR-edp не доходили до брокера — DP лежал, orders_dp.py шлёт ордера только через DP. Робот дважды корректно откатил входы (16:40, 16:45) с rollback.

- 17:26 fix _effective_step: ATR-адаптация int() схлопывала дробный шаг BR (0.4 -> 0) при step_atr=True -> ZeroDivisionError в _check_averaging, тик валился и усреднение не работало вовсе. Фикс: float-математика + кламп к 1 тику (0.01); SI без изменений (50/atr50 -> 50). Живое подтверждение: AVERAGE buy 1 @100.99 исполнен на BRX6 edp, 2 лота, avg 101.05, broker sync ok.

- 17:40 e0833ae: safe perf package (по вердикту Дмитрия): orders_dp keep-alive Session; broker price-sync вынесен в фоновый поток (DP-лаги больше не стопорят 50мс тик); /status поллинг INFO->DEBUG (было ~86K строк шума в день); step_atr=False в обоих BR-конфигах — документный шаг усреднения 0.4 теперь точный (без ATR-половинки). Позиция пережила рестарт: LONG 3 @ 101.00 (усреднения 101.25 -> 100.99 -> 100.78 по шагу 0.4/0.4/0.43), брокер синхронен, тик без ошибок.

- 02.10 10:40 step_atr цикл: localStorage-буфер панели (ofBrSavedParams_edp со старым True) ре-включал адаптацию при каждом сохранении параметров → усреднение шло по 0.2 вместо 0.4 (вход 101.18 → avg на 101.39 вместо 101.58). Исправлено: step_atr=False на диск + в живой процесс, след. триггер = экстремум 101.36 + 0.4 = 101.76. Чекбокс в панели теперь отображает актуальное False — цикл разорван. Формула экстремума очереди (max для SHORT) в коде верна по §5.2.
