# OpenMarketflow — мультистратегийная торговая система MOEX

Торговая система для MOEX FORTS (фьючерсы Si/MX/BR/GD/RI, акции): C# веб-сервер с дашбордом, Python-роботы (Order Flow скальпинг, VP-сетки, арбитраж), единый WS-хаб рыночных данных Finam.

**Капитал:** 10 млн ₽ · **Целевая доходность:** +25% годовых net · **Брокер:** Finam Trade API

---

## Архитектура (актуально на 2026-08-19)

```
                    ┌────────────────────────────┐
                    │   Finam Trade API          │
                    │  REST v1 + WebSocket /ws   │
                    └──────┬──────────────┬──────┘
                           │              │
              REST (ордера,│              │ WS (котировки, стакан,
              аккаунты,    │              │ сделки, бары — push)
              история)     │              │
        ┌──────────────────┴───┐   ┌──────┴─────────────┐
        │  C# Server :5050     │   │  finam_hub.py      │
        │  ASP.NET + SignalR   │   │  (единый WS-коннект│
        │  REST-only коннектор │   │   для всех роботов)│
        │  ChartController UI  │   └──────┬─────────────┘
        └──────────┬───────────┘          │ fan-out
                   │ SignalR/REST         │
        ┌──────────┴───────────┐   ┌──────┴──────────────────────┐
        │  Web Dashboard       │   │  Python-роботы              │
        │  график+стакан+      │   │  OF Si :5080  OF MX :5081   │
        │  роботы+риск         │   │  paper_lab :5181 (новый     │
        └──────────────────────┘   │  стек, обкатка)             │
                                   │  Arb :5090-5093             │
                                   └─────────────────────────────┘
```

**Принцип лимитов:** один WS-коннект на все realtime-данные (не расходует REST-квоты 200 rpm/метод); REST — только ордера/аккаунты/история.

## Компоненты

| Компонент | Директория | Стек | Порт | Назначение |
|---|---|---|---|---|
| C# Server | `src/Server/` | .NET 10, Minimal API, SignalR | 5050 | Дашборд, REST API, Grid MM, риск (RiskGate+CircuitBreaker), REST-only Finam |
| Core | `src/Core/` | C# | — | Брокеро-независимое ядро: 17 стратегий, индикаторы, риск |
| Brokers | `src/Brokers/` | C# | — | Finam REST-коннектор (gRPC удалён) |
| **finam_hub** | `finam_hub.py` | Python, websockets | — | Единый WS-хаб Finam: JWT-автопродление, реконнект, fan-out |
| OF Robot Si | `robot/main_of.py` | Python | 5080 | Order Flow скальпер SiU6: CVD/VWEMA/absorption/VAH-VAL |
| OF Robot MX | `robot/main_of_mx.py` | Python | 5081 | Order Flow MXU6 + VWEMA Avg Exit (B2), VA-breakout stop |
| VP Scalp Grid | `robot/main.py` | Python | 5070 | VP-сетка Si (paper-capable) |
| Арбитраж | `robot/main_arb.py` + `arb_*/` | Python | 5090–5092 | SBER/LKOH spot-fut, BR календарь; общий `arb_common/` |
| **paper_lab** | `robot/paper_lab/` | Python | 5181 | Лаборатория: новый стек (WS-hub + REST 4.3.3), papercuts.md |
| DataProvider | `DataProvider/` | Python | 5060 | Легаси gRPC-кэш (выводится из эксплуатации) |

## Быстрый старт

```powershell
# Требуется: .NET 10 SDK, Python 3.13+, ключ Finam Trade API в src/.env
# FINAM_API_KEY=tapi_sk_...
# FINAM_ACCOUNT_ID=1225953

dotnet build HedgeFund.sln
Copy-Item src\Server\wwwroot\* src\Server\bin\Debug\net10.0\wwwroot\ -Recurse -Force
Copy-Item src\.env src\Server\bin\Debug\net10.0\.env -Force
cd src\Server\bin\Debug\net10.0; .\HedgeFund.Server.exe
# Дашборд: http://localhost:5050

# Paper-робот (лаборатория нового стека):
cd robot\paper_lab
python3 -m pip install --target .\py4 "finam-trade-api==4.3.3" websockets numpy requests
python3 patch_sdk.py
python3 main_of_mx_paper.py   # порт 5181, PAPER всегда on
curl -X POST http://localhost:5181/start
```

## Безопасность

- Ордера по умолчанию запрещены: paper-режим захардкожен в lab; real — только явный флаг
- RiskGate + CircuitBreaker + MaxPositionRub на сервере; daily-stop в OF-роботах
- JWT Finam живёт 15 мин, обновляется автоматически; .env не коммитится

## Статус и прогресс

Актуальное состояние: [PROGRESS.md](PROGRESS.md) · Роботы: [robot/PROGRESS.md](robot/PROGRESS.md)
Журнал экспериментов: [robot/paper_lab/papercuts.md](robot/paper_lab/papercuts.md)
