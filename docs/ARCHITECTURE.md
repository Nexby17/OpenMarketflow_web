# Архитектура OpenMarketflow

*Актуально на 2026-10-01. Владелец: Дмитрий. При структурных изменениях — обновлять этим же коммитом (R-019).*

---

## Обзор

Мультистратегийная торговая система для MOEX (FORTS: фьючерсы Si/MX/GZ/BR/GD/RI; TQBR: акции) через **Finam Trade API** (REST v1 + WebSocket /ws).

```
                        ┌─────────────────────────────┐
                        │      Finam Trade API        │
                        │  REST v1 + WebSocket /ws    │
                        │  (JWT 15 мин, 200 rpm/метод)│
                        └──────┬──────────────┬───────┘
                               │              │
                 REST (ордера, │              │ WS (quotes, orderbook,
                 аккаунты,     │              │ trades, bars — push)
                 бары истории) │              │
        ┌──────────────────────┴───┐   ┌──────┴──────────────────────┐
        │  C# Server :5050         │   │  finam_hub.py (корень)      │
        │  ASP.NET Minimal API     │   │  единый WS-коннект для всех │
        │  SignalR + ChartController│  │  роботов; singleton, recon- │
        │  RobotProcessManager     │   │  нект+backoff, fan-out      │
        └──────────┬───────────────┘   └──────┬──────────────────────┘
                   │ SignalR / REST proxy     │ hub_adapter.py
        ┌──────────┴───────────┐   ┌──────────┴────────────────────┐
        │  Web Dashboard       │   │  Python-роботы (robot/)       │
        │  :5050 (cookie auth) │   │  OF Si :5080   OF MX :5081    │
        │  график, стакан,     │   │  Arb GAZP :5090  SBER :5091   │
        │  панели роботов      │   │  BR :5092   (DP :5060*)       │
        └──────────────────────┘   └───────────────────────────────┘

*DP 5060 (DataProvider, Python FastAPI) — кэш свечей/цитат; статус см. RULES.md R-015.
```

**Разделение ответственности (R-013 конвенция):** рыночные данные робота — из WS-хаба (`hub_adapter.py`), ордера/аккаунты — из REST (`finam_rest4.py`, `orders_rest4.py`). Не смешивать.

## Счёт и капитал

- Боевой счёт: **2049688** (Finam EDP), env `FINAM_ACCOUNT` / `FINAM_ACCOUNT_ID` из `src/.env`.
- Капитал: 10 млн ₽; целевая доходность +25% годовых net.
- Инструменты (контракты октябрь 2026): SiZ6, MXZ6, GZZ6, BR*, GD*, RI*; акции GAZP/SBER/LKOH/ROSN/TATN.

## Компоненты

### C# Server (`src/Server`, порт 5050)

ASP.NET Minimal API (top-level Program.cs), порт 5050, cookie-аутентификация (логин из `src/.env APP_LOGIN`, hash `APP_PASS_HASH`; дефолт admin/admin если env нет).

- **ChartController + wwwroot** (`js/app.js` v115+) — веб-дашборд: график (LightweightCharts, UTC+10800 MSK), стакан, панели роботов (arb/of/of_mx), аккаунты, журнал сделок.
- **RobotProcessManager** — старт/стоп процессов Python-роботов из UI (`/api/of/process/{name}/start|stop`); запуск боевых — только Дмитрием (R-001).
- **FinamConnector — REST-only**: gRPC из C# удалён 2026-08-17 (см. README «Актуально»).
- **AlfaBridge** (`src/AlfaBridge`) — мост Альфа-Директ PRO API (WS 127.0.0.1:3366): **legacy, в проде не используется**.

### Legacy-слои (в solution, но не в прод-контуре)

- `src/UI` — WPF-клиент OpenMarketflow (в .sln, собирается; прод-интерфейс = веб-дашборд :5050)
- `src/Brokers/Transaq` — TransaqConnector.cs (запрещён к восстановлению; C# коннектор REST-only)
- `src/Strategies` — ранние C#-версии стратегий; рабочие стратегии живут в Python-роботах и Core

### WS-хаб (`finam_hub.py`, корень)

Один WebSocket на всех роботов: подписки QUOTES (мульти-символьно) / ORDER_BOOK / INSTRUMENT_TRADES / BARS / ORDERS; реконнект с backoff; self-heal импорта websockets (F-042). JWT — в payload подписки и Authorization без «Bearer».

### DataProvider (`DataProvider/`, порт 5060)

Python FastAPI: кэш свечей/цитат через Finam REST, прокси ордеров (`/order/place`, `/order/status`). Критическая зависимость части фолбэков — R-015 (healthcheck planned F-053).

### Python-роботы (`robot/`)

| Робот | Файл | Порт | Инструмент | Стратегия |
|---|---|---|---|---|
| OF Si | `main_of.py` | 5080 | SiZ6@RTSX | Order Flow: CVD/VWEMA/absorption/DM-wall/VAH-VAL, усреднение+пирамидинг, partial TP LIFO |
| OF MX | `main_of_mx.py` | 5081 | MXZ6@RTSX | То же + VWEMA Avg Exit |
| Арб GAZP | `main_arb.py` | 5090 | GAZP@MISX × GZZ6@RTSX | Квазиарб: спот-фьючерс базис, Z(dev) вход, слои FIFO |
| Арб SBER | `main_arb.py` | 5091 | SBER@MISX × SBRF | То же |
| Арб BR | `main_arb.py` | 5092 | BR-календарь | Календарный спред Brent |

Общее ядро арбов: `robot/arb_common/` (strategy_arb, arb_engine, orders_arb — приоритет sys.path выше копий в robot/).

**Документация роботов:** [orderflow_robot_si.md](orderflow_robot_si.md) (логика OF полностью), [arbitrage_robot.md](arbitrage_robot.md) (арб-семейство), [dataprocessor.md → DataProvider](dataprovider.md).

## Потоки данных (жизненный цикл цены и ордера)

```
Finam WS ──► finam_hub ──► hub_adapter ──► strategy (сигналы, 50мс тик)
                                              │ action (entry/close/average/...)
                                              ▼
                              orders_rest4.place_market/limit ──► Finam REST
                                              │ order_id
                                              ▼
                              wait_fill: avg_price → GetTrades wavg → None
                                              │ fill (реальная цена, F-048)
                                              ▼
                              strategy.update_fill_price → state (30с автосейв)
```

Журнал сделок = цены брокера (R-005, F-048); каждая запись несёт order_id всех ног и priceSource (broker/mixed/estimate).

## Реестры и правила

- **Состояние фич:** `feature_list.json` (F-XXX, что изменено)
- **Дневник:** `progress.md` (корень) + `robot/PROGRESS.md` (когда/почему/как проверено)
- **Операционные правила:** [RULES.md](../RULES.md) (R-XXX; старт роботов R-001, торговая логика R-002, журнал R-005, сверки R-017...)

## Запуск (Windows, локальная машина)

```powershell
# Сервер (сборка + деплой wwwroot)
cd OpenMarketflow_web; dotnet build HedgeFund.sln
Copy-Item src\Server\wwwroot\* src\Server\bin\Debug\net10.0\wwwroot\ -Recurse -Force
Copy-Item src\.env src\Server\bin\Debug\net10.0\.env -Force
cd src\Server\bin\Debug\net10.0; .\HedgeFund.Server.exe

# Роботы: старт боевых — ТОЛЬКО Дмитрием (R-001).
# Типовой старт арба (env в ТОЙ ЖЕ команде — R-013):
cd robot
$env:ARB_CONFIG_FILE='arb_config_gazp.json'; $env:ARB_STATE_FILE='arb_state_gazp.json'
Start-Process python3 main_arb.py --port 5090 --no-paper
# Затем POST /start на :5090 (UI кнопка с confirm, F-050)
```

Порты: 5050 server, 5060 DP, 5080 OF Si, 5081 OF MX, 5090–5092 арбы. UI: `http://localhost:5050`.

## Ключевые инварианты

1. Входная математика арба — last-цены, тейк — реализуемый PnL из стакана (решение Дмитрия, F-038).
2. Журнал и PnL — только реальные цены брокера (F-048, F-013).
3. Анти-дабл-клоз: close_all не исполняется дважды (F-043).
4. Старт робота = сверка позиции с брокером; рассинхрон → блок входов (startup-reconciliation).
5. Любая смена режима логируется (F-050); UI меняет режимы через confirm.
6. UTC-эпоха от сервера, UI сдвигает +10800 MSK; константа MSK_OFFSET не меняется.
