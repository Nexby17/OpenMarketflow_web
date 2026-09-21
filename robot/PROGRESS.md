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
