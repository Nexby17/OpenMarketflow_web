# Paper Lab — журнал экспериментов

**Правила:** каждый эксперимент = запись PC-XXX. Изменения только в `robot/paper_lab/`.
Перенос в боевого робота (`robot/`) — только со статусом **PORT** и отдельным коммитом `PC-XXX → robot/`.

---

## Активные эксперименты

### PC-001: VWEMA Avg Exit (B2) live-обкатка на MXU6
- **Гипотеза:** запрет усреднения против тренда VWEMA + закрытие усреднённой позиции при развороте VWEMA снижает просадки без потери прибыльных входов
- **Статус:** 🟢 RUNNING (запущен 2026-08-18 11:50 MSK, pid см. logs)
- **Конфиг:** продовые настройки MXU6 (step_average=350, vwema_avg_exit=true, vah_val_mode=fade), PAPER_MODE=True
- **Изменено здесь:** ничего (первый запуск — baseline валидация свежеподтянутого функционала + проверка live-потока через finam_compat)
- **Метрики для наблюдения:** averageLevels vs realizedPnL; количество срабатываний vwema_avg_exit; max drawdown vs baseline
- **Наблюдения:**
  - 2026-08-18 11:50: запуск успешен. Live-цена MXU6 идёт (215550→215725), VP построен: VAH=216000 VAL=215400. VWEMA direction=0 (ждёт прогрева). Примечание: Finam gRPC отдаёт RESOURCE_EXHAUSTED (Too Many Requests) на параллельные подписки — рестримы автоматические, данные не теряются. Проверено: finam_compat (finam-sdk 2.19.0) работает в бою.
- **Вердикт:** —

### PC-002: VAH/VAL mode comparison (range vs fade) на MXU6 — ЗАПЛАНИРОВАН
- Гипотеза: range-режим (торговля только внутри VA) даёт больше сделок с лучшим RR на MXU6, чем fade по краям
- План: прогнать PC-001 неделю в fade → переключить vah_val_mode=range → сравнить недели

---

## Архив

(пока пусто)

---

## Правила переноса в боевого робота

1. Эксперимент должен отработать ≥ 3 торговых дня (или ≥ 30 round trips)
2. Запись в журнале со статусом PORT + список конкретных изменений
3. Перенос = отдельный коммит `PC-XXX → robot/` с ссылкой на запись
4. После переноса — smoke боевого робота и сброс paper-статистики

---

## Исследование API (2026-08-18, по запросу Дмитрия)

### Finam Trade API 4.3.3 (последний SDK)
- Пакет `finam-trade-api==4.3.3`: чистый **REST** (httpx/aiohttp, async), клиент `Client(TokenManager(token))`, автопродление JWT
- Модули: account, orders (place/cancel/get), instruments (get_bars, get_last_quote, get_last_trades, get_order_book), quotas, assets, corporate_actions
- **Стриминга в SDK НЕТ** — вместо него отдельный WebSocket API

### WebSocket API (новое поколение, AsyncAPI 3.0)
- Эндпоинт: `wss://api.finam.ru:443/ws` (путь /ws обязателен!), JWT в `Authorization` header (без Bearer)
- Envelope: DATA (payload по подпискам) / ERROR / EVENT (HANDSHAKE_SUCCESS...)
- Подписки: QUOTES (мульти-символьно!), ORDER_BOOK, INSTRUMENT_TRADES, BARS, ORDERS (по счёту)
- Формат: `{"action":"SUBSCRIBE","type":"QUOTES","data":{"symbols":[...]},"token":JWT}`
- **Живой тест 18.08.2026 13:27 MSK: за 12 сек — 107 QUOTES + 111 ORDER_BOOK + 24 INSTRUMENT_TRADES по MXU6, 0 ошибок**

### Лимиты Finam (официально)
- REST: 200 запросов/мин **на метод** (не глобальный)
- «Увеличить лимиты API» — по заявке через форму
- WS-подписки не расходуют REST-квоты → решение конкуренции за лимиты

### Вывод для архитектуры
1. Новый SDK 4.3.3 (REST) для ордеров/истории/счетов
2. WS-хаб (один коннект) для всего realtime: котировки/стакан/сделки всем роботам
3. Старый finam-sdk 2.19 (gRPC) можно полностью убрать после миграции


### Прогресс миграции (правило Дмитрия: одна фича → тест → ОК → следующая)
- **Этап 1 — finam_hub.py: ✅ СДЕЛАНО + ПРОТЕСТИРОВАНО (2026-08-18)**
  - Один WS-коннект, JWT-автопродление, реконнект с backoff, fan-out подписчикам
  - Тест 60 сек: 521 QUOTES + 537 ORDER_BOOK + 140 TRADES, 0 ошибок, 0 реконнектов
  - Дефекты найдены и устранены в той же фиче: (1) .env поиск → src/ тоже; (2) stop() warning; (3) реконнект после stop
  - Финальный stop-тест: quotes=119, поток завершён, 0 errors — PASS
- Этап 2 — hub → paper_lab MX-робот (PC-003): следующая фича
