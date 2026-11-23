# AGENTS.md — OpenMarketflow

Рабочие инструкции для агентов. Читать перед каждой задачей.

**Правило №1 (от Дмитрия, 2026-08-18): одна фича за раз.**
Сделана → протестирована → ОК → следующая. Не ОК → чиним → тестируем → дальше.
Делегирование Code-агенту — только по этой же схеме.

---

## 0. Неукоснительное

1. Без лести и воды. Начинай с действия.
2. Не согласен — скажи до работы.
3. Не выдумывать: пути, API, тесты. Не знаешь — прочитай/запусти/спроси.
4. Две трактовки → спросить, не выбирать молча.
5. Трогать только то, что относится к задаче.
6. **UI-фиксы завершены только после браузерной проверки** с эмуляцией действий пользователя (CDP-тест), скриншот — доказательство.
7. **При каждом коммите кода/конфигов — обновлять feature_list.json + progress.md + robot/PROGRESS.md** (см. §7). Без документации задача НЕ считается выполненной.

---

## 1. Текущий стек (актуально на 2026-08-19)

### Сервер (C#)
- .NET 10 (target переименован с net8.0 — SDK установлен только 10-й)
- ASP.NET Minimal API, top-level Program.cs, SignalR, порт 5050
- **FinamConnector — REST-only** (gRPC из C# удалён 2026-08-17)
- Аутентификация: cookie, логин admin, rate-limit 5/мин
- ChartController: `wwwroot/js/modules/chart.js` — единая точка истины графика (v80+)

### Данные Finam — новое поколение API
- **REST SDK**: пакет `finam-trade-api==4.3.3` (Client + TokenManager, автопродление JWT)
- **WebSocket**: `wss://api.finam.ru:443/ws` — путь /ws обязателен; JWT в Authorization БЕЗ «Bearer»; подписки SUBSCRIBE/UNSUBSCRIBE c token в payload; каналы QUOTES (мульти-символьно!)/ORDER_BOOK/INSTRUMENT_TRADES/BARS/ORDERS
- **Лимиты**: 200 req/min НА МЕТОД; WS не расходует REST-квоты
- **Устаревшее**: `finam-sdk` (2.x, gRPC FinamClient) — конфликтует за имя модуля с 4.x; боевые роботы ещё на нём через finam_compat, paper_lab уже нет
- JWT: 15 мин, refresh за минуту; REST-ответы парсить с InvariantCulture (русская локаль ломает double.Parse!)

### Роботы (Python, embedded python — без venv!)
- Изоляция пакетов: `pip install --target <dir>` + sys.path-приоритет (пример: robot/paper_lab/py4)
- **finam_hub.py** (корень проекта) — единый WS-хаб: singleton, реконнект+backoff, outbox, fan-out
- **paper_lab/** — лаборатория: эксперименты PC-XXX в papercuts.md, перенос в боевых только через PORT-вердикт

---

## 2. Команды (Windows, локальная машина)

```powershell
# Сборка
cd OpenMarketflow_web; dotnet build HedgeFund.sln
# После сборки: скопировать wwwroot в bin (dotnet перезатирает)
Copy-Item src\Server\wwwroot\* src\Server\bin\Debug\net10.0\wwwroot\ -Recurse -Force
Copy-Item src\.env src\Server\bin\Debug\net10.0\.env -Force

# Запуск сервера
cd src\Server\bin\Debug\net10.0; .\HedgeFund.Server.exe
# UI: http://localhost:5050 (admin / см. src/.env APP_PASS_HASH)

# Paper-робот MXU6 (лаборатория, порт 5181)
cd robot\paper_lab; python3 main_of_mx_paper.py
# Затем: curl -X POST http://localhost:5181/start

# Браузерный тест UI (CDP, headless chrome)
# фреймворк: workspace\.openclaw\tmp\final_acceptance.py
```

---

## 3. Критичные конвенции

- **Направление сетки**: LONG → SELL-лимиты ВЫШЕ, SHORT → BUY-лимиты НИЖЕ. Не инвертировать.
- C# top-level Program.cs: никаких record/class после top-level statements.
- JS: optional chaining только на чтении; после КАЖДОЙ правки app.js → bump `?v=N` в index.html.
- Свечи: сервер отдаёт UTC-epoch, UI сдвигает +10800 (MSK). Константа MSK_OFFSET не меняется.
- Аккаунт по умолчанию: FINAM_ACCOUNT_ID из src/.env (1225953 FORTS; 2049688 EDP).
- Контракты живые (август 2026): SiU6/SiZ6, MXU6/MXZ6, GDU6, BRU6, RIU6/RIZ6. SiM6/MXM6 — истекли.

---

## 4. Запреты

- НЕ смешивать данные коннекторов (правило актуально и для WS/REST: данные робота — из хаба, ордера — из REST, без «салата»).
- НЕ менять торговую логику/метрики/параметры стратегий без явного запроса Дмитрия.
- НЕ трогать QUIK-мост (удалён) и не восстанавливать gRPC в C#.
- НЕ коммитить: .env, state-файлы с секретами, logs/, paper_lab/py4/.
- НЕ редактировать AGENTS.md/README/PROGRESS этой директории обычным run — только через Hermes-flow.

---

## 5. Приёмка работы

1. Критерии успеха сформулированы ДО кода.
2. Верификация запущена, вывод прочитан. «Похоже работает» ≠ работает.
3. UI → CDP-тест + скриншот. Python → py_compile + живой запуск. C# → dotnet build 0 ошибок + smoke API.
4. Фейл верификации → чинить причину, не тест.
5. Коммит: описательный, <72 симв., ссылка на PC-XXX при наличии.

---

## 6. Уроки проекта (проверено кровью)

- Finam REST принимает JWT в Authorization БЕЗ «Bearer» (с Bearer → 401).
- SDK 4.3.3: 3 бага — Position required-поля, stale-JWT на /sessions, нет httpx-timeout (патчи: robot/paper_lab/patch_sdk.py).
- Русская локаль Windows ломает double.Parse точкой → всегда InvariantCulture.
- LightweightCharts умирает навсегда от update() со временем < последнего (инвариант в ChartController).
- Кеш браузера: старый app.js даёт фантомные баги → Ctrl+F5 + bump версии.
- Embedded python: нет venv → pip --target; пакеты finam-sdk и finam-trade-api конфликтуют за модуль finam_trade_api.
- Finam WS handshake молчит на корне; рабочий путь — /ws.
- Расширение AutoGLM рвёт WS на длинных задачах — для UI-тестов надёжнее headless Chrome + CDP напрямую.

---

## 7. Ведение документации (обязательно, от Дмитрия)

При КАЖДОМ коммите кода/конфигов:

1. **feature_list.json** — запись с полной схемой (id/name/area/status/owner/commits/summary/logic/tests/changes); коммит ссылается на F-XXX в сообщении (`feat(F-XXX): ...` / `fix(F-XXX): ...`).
2. **progress.md** (корневой) + **robot/PROGRESS.md** — запись о работе: дата/время, F-метка, что сделано, почему, как проверено, коммит.
3. Документация — тем же коммитом или немедленным `docs:`-коммитом. Без записи задача НЕ считается выполненной.
4. Нумерация F-XXX строго последовательная; перед присвоением проверять занятость: `git log --oneline --grep="F-0"` (дубль F-032 случился 22–23.09 — урок).
5. Восстановительные записи в реестр помечать note «восстановлено при аудите <дата>».
6. Main-агент включает этот пункт в КАЖДЫЙ бриф код-агенту и проверяет выполнение до коммита.

*Аудит 23.09.2026: реестр отставал на 20 фич (F-018..F-037 существовали только в git-логе), восстановлено коммитом edb22d0; правило внесено в AGENTS.md по прямому требованию Дмитрия.*
