# robot/ — Торговые роботы OpenMarketflow

Python-роботы: Order Flow скальперы, VP-сетка, арбитраж. Данные — через единый WS-хаб (`finam_hub.py` в корне проекта, подключается после завершения миграции у каждого робота), ордера/аккаунты — Finam REST API v1.

**Прогресс ведётся в [PROGRESS.md](PROGRESS.md) — обновлять при каждом изменении роботов.**
Эксперименты — только в [paper_lab/papercuts.md](paper_lab/papercuts.md).

## Состав

| Робот | Файл | Порт | Инструмент | Стратегия | Статус |
|---|---|---|---|---|---|
| OF Si | `main_of.py` | 5080 | SiU6@RTSX | Order Flow: CVD/VWEMA/absorption/DM-wall/VAH-VAL | Код готов; ждёт Этап 5 |
| OF MX | `main_of_mx.py` | 5081 | MXU6@RTSX | + VWEMA Avg Exit (B2), VA-breakout stop, /reset-stats | Код готов; ждёт Этап 5 |
| VP Scalp Grid | `main.py` | 5070 | SiU6@RTSX | Volume Profile сетка (вход вне VA, TP к POC) | Код готов |
| Арбитраж SBER | `arb_sber_sru6/` | 5090 | SBER/SRU6 | Z-score базиса, LIMIT+MARKET ноги | Код готов (общий `arb_common/`) |
| Арбитраж LKOH | `arb_lkoh_lku6/` | 5093 | LKOH/LKU6 | Z-score базиса | Код готов |
| Арбитраж BR | `arb_br_calendar/` | 5092 | BRQ6/BRU6 | Календарный спред | Код готов |
| **Paper Lab** | `paper_lab/` | 5181 | MXU6 | Лаборатория нового стека (WS+REST4) | 🟡 Soak PC-005 |

## API роботов (единый для OF)

```
GET  /status          — состояние, позиция, PnL, параметры, журнал сделок
POST /start|/stop|/pause|/resume
POST /params          — горячая смена параметров (JSON)
POST /reset-stats     — сброс статистики (только OF)
```

## Архитектура OF-робота

```
finam_hub (WS: quotes/orderbook/trades/bars)  ──▶ hub_adapter ──▶ _on_quote/_on_ob/_on_trades/_on_bar
                                                                      │
                                              strategy_of.py (OrderFlowStrategy)
                                              ├─ VWEMA-фильтр (avg-exit B2)
                                              ├─ Volume Profile (VAH/VAL/POC, range|fade|breakout)
                                              ├─ CVD/accel/agg-ratio сигналы
                                              └─ risk: daily-stop, max-levels
                                                                      │
                                              orders (orders_dp | orders_rest4 в lab)
```

## Конфигурация

- Параметры стратегии: `config_of*.py` (дефолты) + `of_config*.json` (hot-update через /params)
- Состояние: `of_state_*.json` — переживает рестарт (позиция/статистика)
- Ключ: `src/.env` → `FINAM_API_KEY`, `FINAM_ACCOUNT_ID`

## Правила для роботов (из AGENTS.md корня)

1. **Одна фича → сделана → протестирована → ОК → следующая.**
2. Торговую логику/параметры не менять без явного запроса Дмитрия.
3. `MAX_AVERAGE_LEVELS=10` в `config_of_mx.py` — риск-фикс, не поднимать.
4. Эксперименты — только в `paper_lab/` (PC-XXX в papercuts.md), перенос — через PORT.
5. Embedded python: изоляция `pip --target` (см. paper_lab/py4 + patch_sdk.py).
