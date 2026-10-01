# robot/ — Торговые роботы OpenMarketflow

Python-роботы: Order Flow скальперы, VP-сетка, арбитраж. Данные — через единый WS-хаб (`finam_hub.py` в корне проекта, подключается после завершения миграции у каждого робота), ордера/аккаунты — Finam REST API v1.

**Прогресс ведётся в [PROGRESS.md](PROGRESS.md) — обновлять при каждом изменении роботов.**
Эксперименты — только в [paper_lab/papercuts.md](paper_lab/papercuts.md).

## Состав

| Робот | Файл | Порт | Инструмент | Стратегия | Статус (01.10) |
|---|---|---|---|---|---|
| OF Si | `main_of.py` | 5080 | SiZ6@RTSX | Order Flow: CVD/VWEMA/absorption/DM-wall/VAH-VAL | 🟢 боевой (real) |
| OF MX | `main_of_mx.py` | 5081 | MXZ6@RTSX | + VWEMA Avg Exit (B2) | 🟢 боевой (real) |
| Арб GAZP | `main_arb.py` | 5090 | GAZP@MISX × GZZ6@RTSX | Квазиарб базиса, Z(dev) вход, слои FIFO | 🟢 боевой (real) |
| Арб SBER | `main_arb.py` | 5091 | SBER@MISX × SBRF | То же | 🟢 работает |
| Арб BR | `main_arb.py` | 5092 | BR-календарь | Календарный спред | 🟢 работает |
| VP Scalp Grid | `main.py` | 5070 | SiZ6@RTSX | VP сетка (не запускался с 08.19) | ⚪ код готов |
| Paper Lab | `paper_lab/` | 5181 | MXU6 | Лаборатория нового стека (WS+REST4) | ⚪ журнал: papercuts.md |

**Схема инстансов (с 29.09):** один `main_arb.py` + env `ARB_CONFIG_FILE`/`ARB_STATE_FILE` на инстанс — см. [docs/arbitrage_robot.md](../docs/arbitrage_robot.md). Папки arb_*/ содержат только примеры конфигов (`*.example`). Старт боевых роботов — только Дмитрием (RULES.md R-001).


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
4. Эксперименты — только в `paper_lab/` (PC-XXX в papercuts.md), перенос — через PORT.
5. Embedded python: изоляция `pip --target` (см. paper_lab/py4 + patch_sdk.py).
