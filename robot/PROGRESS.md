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

### 2026-08-18
- **paper_lab (PC-005, a25d89e):** paper-робот MXU6 полностью переведён на новый стек — данные из WS-хаба (hub_adapter), ордера/позиции/бары через REST 4.3.3 (finam_rest4 + orders_rest4). gRPC и DataProvider удалены из lab. Патчи SDK: required-поля Position, stale-JWT на /sessions, httpx-timeout (patch_sdk.py). Живой тест: VWEMA warmup 60 баров (REST), OB/цена из WS, DESYNC-монитор корректен. Soak запущен (cron ежечасно → papercuts.md).
- **paper_lab (PC-003/004, 819e96c/3570399):** hub_adapter (WS→compat мост), finam_rest4, изоляция py4. Найдены 2 бага SDK 4.3.3.
- **finam_hub.py (f87a0b1):** единый WS-хаб Finam (Этап 1 миграции). Тест 60с: 1198 сообщений, 0 ошибок.

### 2026-08-17 (merge origin/main — функционал роботов)
- **strategy_of.py:** VWEMA Avg Exit (B2) — не усреднять против VWEMA + закрытие при развороте; VAH/VAL range-режим (торговля только внутри VA).
- **main_of.py:** VA BREAKOUT STOP (выход из VA при позиции → close all); VP live-питание сделками; /reset-stats; реконструкция VP при смене VAH/VAL-параметров.
- **of_config_mx.json:** step_average 450→350; vwema_avg_exit=true.
- **main_of_mx.py:** MXU6-робот синхронизирован с прод-репо (наш MAX_AVERAGE_LEVELS=10 сохранён).

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
