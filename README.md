# OpenMarketflow — HedgeFund Trading System

Мультистратегийная торговая система для MOEX FORTS (фьючерсы на $/руб, индекс Мосбиржи, Brent, акции). 

Состоит из C# веб-сервера с UI, Python скальпирующих роботов и арбитражных стратегий.

## Архитектура

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│   Web UI / API  │────▶│   C# Server (.NET 8) │────▶│  Finam Trade API│
│   (port 5050)   │◀────│   SignalR + REST     │     │  (gRPC + REST)  │
└─────────────────┘     └──────────────────────┘     └─────────────────┘
                               │
┌─────────────────┐     ┌──────┴───────────────┐     ┌─────────────────┐
│  OF Robot       │────▶│  DataProvider        │────▶│  FinamPy gRPC   │
│  (port 5080)    │     │  (port 5060)         │     │  (candles, OB,  │
└─────────────────┘     └──────────────────────┘     │  trades, fills) │
┌─────────────────┐                                   └─────────────────┐
│  Arb Robots     │───────────────────────────────────────────────────▶│
│  (5090-5093)    │                                                        
└─────────────────┘
```

## Компоненты

| Компонент | Директория | Язык | Порт | Описание |
|-----------|-----------|------|------|----------|
| **C# Server** | `src/Server/` | C# / .NET 8 | 5050 | Веб-сервер, SignalR хаб, запуск стратегий (Grid MM), хостинг UI |
| **Core** | `src/Core/` | C# | — | Брокеро-независимое ядро: модели, стратегии, индикаторы, риск-менеджер |
| **Brokers** | `src/Brokers/` | C# | — | Адаптеры: Finam gRPC, StockSharp, Альфа-Инвестиции |
| **WPF UI** | `src/UI/` | C# / WPF | — | Windows desktop клиент (SignalR) |
| **DataProvider** | `DataProvider/` | Python / FastAPI | 5060 | Кэш свечей, цитат, ордеров, филлов через FinamPy gRPC. REST API для роботов |
| **OF Robot** | `robot/` | Python | 5080 | Order Flow скальпер: CVD, VWEMA, absorption, DM wall. 1 лот |
| **Arb BR Calendar** | `robot/arb_br_calendar/` | Python | 5092 | Brent календарный спред (BRQ6/BRU6), Z-score вход |
| **Arb SBER** | `robot/arb_sber_sru6/` | Python | 5090 | Spot/futures арбитраж Сбер (SBER/SRU6) |
| **Arb LKOH** | `robot/arb_lkoh_lku6/` | Python | 5093 | Spot/futures арбитраж Лукойл (LKOH/LKU6) |
| **Robot Instance Launcher** | `robot_instance/` | Python | — | Мультиинстансы VP Scalp Grid (каждый тикер = свой порт) |
| **Backtest** | `backtest/src/` | Python | — | 80+ бэктест-скриптов: PSAR Grid, VP, OF, арбитраж, CVD |
| **Research** | `arb/` | Python | — | Исследование арбитражных стратегий |

## Требования

- **Python 3.12+** с pip
- **.NET 8 SDK** (для C# сервера)
- **Finam Trade API** — API ключ + аккаунт с доступом к FORTS
- **FinamPy** — `pip install FinamPy` (gRPC клиент)
- **Linux** (сервер) / **Windows** (WPF UI, опционально)

## Установка

```bash
# 1. Клонировать
git clone https://github.com/Nexby17/OpenMarketflow_web.git
cd OpenMarketflow_web

# 2. Python-зависимости
pip install -r DataProvider/requirements.txt
pip install FinamPy fastapi uvicorn requests

# 3. C# зависимости (для сборки сервера)
cd src && dotnet restore && cd ..

# 4. Настроить секреты
cp .env.example .env
# Отредактировать .env — вставить свои API ключи и account ID

# 5. Конфиги роботов (если нужны)
cp robot/arb_br_calendar/arb_config.json.example robot/arb_br_calendar/arb_config.json
cp robot/arb_br_calendar/arb_state.json.example robot/arb_br_calendar/arb_state.json  # создать пустой {}
```

## Запуск

### DataProvider (обязательно — нужен всем роботам)

```bash
# Ручной запуск
cd DataProvider && python3 main.py

# ИЛИ через systemd (рекомендуется)
cp scripts/dataprovider.service.example /etc/systemd/system/dataprovider.service
# Отредактировать пути в файле
systemctl daemon-reload && systemctl enable --now dataprovider
```

Проверка: `curl http://localhost:5060/health`

### C# Server (UI + стратегии)

```bash
# Сборка и запуск
cd src/Server && dotnet run

# ИЛИ деплой (как на проде)
chmod +x scripts/deploy.sh && ./scripts/deploy.sh

# ИЛИ systemd
cp scripts/hedgefund-server.service.example /etc/systemd/system/hedgefund-server.service
systemctl daemon-reload && systemctl enable --now hedgefund-server
```

Проверка: `curl http://localhost:5050/health`

### OF Robot (скальпер)

```bash
# Paper trading (по умолчанию)
cd robot && python3 main_of.py --paper --port 5080

# Реал
cd robot && python3 main_of.py --port 5080

# ИЛИ systemd
cp scripts/trading-robot.service.example /etc/systemd/system/trading-robot.service
systemctl daemon-reload && systemctl enable --now trading-robot
```

Проверка: `curl http://localhost:5080/status`

### Arb Robots

```bash
# BR Calendar (Brent spread)
cd robot/arb_br_calendar && python3 main_arb.py --no-paper --port 5092

# SBER spot/futures
cd robot/arb_sber_sru6 && python3 main_arb.py --no-paper --port 5090

# LKOH spot/futures
cd robot/arb_lkoh_lku6 && python3 main_arb.py --no-paper --port 5093
```

### Бэктесты

```bash
cd backtest/src
python3 psar_5min_grid_nofilter.py    # PSAR Grid стратегия
python3 of_bt.py                       # Order Flow бэктест
python3 arb_sber_bt.py                 # Арбитраж SBER
```

## API эндпоинты

### DataProvider (port 5060)

| Endpoint | Описание |
|----------|----------|
| `GET /health` | Здоровье + свежесть данных |
| `GET /status` | Статус подключения |
| `GET /candles/{symbol}?tf=M5&limit=100` | Свечи из кэша |
| `GET /quote/{symbol}` | Текущая цитата |
| `GET /orders?account=XXX` | Активные ордера |
| `GET /position?account=XXX&ticker=SiU6` | Позиция |
| `POST /order/place` | Разместить ордер (market/limit) |
| `POST /order/cancel` | Отменить ордер |
| `GET /pnl?account=XXX` | PnL за сегодня |
| `GET /recent-fills` | Последние филлы (10 сек) |

### OF Robot (port 5080)

| Endpoint | Описание |
|----------|----------|
| `GET /status` | Статус робота, позиция, PnL |
| `POST /start` | Запустить торговлю |
| `POST /stop` | Остановить и закрыть позиции |
| `POST /pause` | Пауза (не открывать новые) |
| `GET /config` | Текущие параметры стратегии |
| `POST /config` | Обновить параметры (hot-reload) |

### C# Server (port 5050)

| Endpoint | Описание |
|----------|----------|
| `GET /health` | Здоровье сервера |
| SignalR hub | Real-time: свечи, ордера, позиции, сделки |
| `POST /strategy/grid-mm/start` | Запустить Grid MM стратегию |
| `POST /strategy/grid-mm/stop` | Остановить Grid MM |
| `GET /strategy/grid-mm/status` | Статус Grid MM |

## Структура проекта

```
OpenMarketflow_web/
├── .env.example                  # Шаблон секретов
├── .gitignore
├── LICENSE
├── README.md
├── HedgeFund.sln                 # .NET solution
├── scripts/
│   ├── deploy.sh                 # Деплой C# сервера
│   ├── trading-robot.service.example
│   ├── dataprovider.service.example
│   └── arb-br-cal.service.example
├── src/
│   ├── Core/                     # Ядро: модели, стратегии, индикаторы
│   │   ├── Models/               # Candle, Order, Trade, Position, Signal
│   │   ├── Strategies/           # Grid MM, VStop, Fade, VP Scalp, Arb...
│   │   ├── Indicators/           # EMA, SMA, RSI, MACD, ATR, ParabolicSAR, BB
│   │   └── Averaging/            # Движок усреднения позиций
│   ├── Brokers/                  # Брокер-адаптеры + Finam gRPC proto
│   │   ├── Finam/                # Finam Trade API (gRPC + REST)
│   │   └── Finam/Protos/         # Proto-файлы для генерации gRPC клиентов
│   ├── Server/                   # ASP.NET Core сервер + SignalR
│   │   ├── Services/             # Лаунчеры стратегий, TradingService
│   │   ├── Connectors/           # Finam/Quik/Transaq адаптеры
│   │   └── wwwroot/              # Web UI (JS)
│   ├── UI/                       # WPF desktop клиент (Windows)
│   └── AlfaBridge/               # Мост к Альфа-Инвестициям (.NET 4.8)
├── DataProvider/                 # Python FastAPI — кэш данных
│   ├── main.py                   # FastAPI приложение
│   ├── provider.py               # FinamPy gRPC подключение
│   ├── cache.py                  # Кэш свечей/цитат/филлов
│   ├── config.py / config.json   # Настройки (символы, таймфреймы)
│   └── requirements.txt
├── robot/                        # Python торговые роботы
│   ├── main_of.py                # OF скальпер (entry point)
│   ├── strategy_of.py            # OF стратегия (CVD, VWEMA, absorption)
│   ├── orders_dp.py              # Ордера через DataProvider
│   ├── orders_grpc.py            # Ордера напрямую через gRPC
│   ├── orderflow_engine.py       # Движок OF анализа
│   ├── config_of.py              # Конфиг OF робота
│   ├── of_config.json            # Параметры стратегии (runtime)
│   ├── api.py                    # HTTP API для управления
│   ├── risk.py                   # Риск-менеджер
│   ├── state.py                  # Сохранение/восстановление состояния
│   ├── arb_br_calendar/          # Arb робот: Brent calendar spread
│   ├── arb_sber_sru6/            # Arb робот: SBER spot/futures
│   ├── arb_lkoh_lku6/            # Arb робот: LKOH spot/futures
│   └── tests/                    # Unit тесты
├── robot_instance/               # Мультиинстанс лаунчер
│   └── launcher.py
├── backtest/
│   ├── src/                      # Бэктест-скрипты (80+)
│   └── data/                     # Исторические данные (не в git)
├── arb/                          # Исследование арбитражных стратегий
└── docs/                         # Документация, API спецификации
```

## Стратегии

### PSAR Grid MM (C#, продакшен)
Parabolic SAR × EMA кросс → вход + сетка лимитных ордеров против позиции. ~890K₽/мес на SI фьючерсе.

### Order Flow Scalper (Python, продакшен)
CVD acceleration, VWEMA crossover, absorption, DM wall detection. Скальпинг по 1 лоту.

### Spot/Futures Arbitrage (Python, продакшен)
Z-score базиса → пара (spot + futures). RO SN, TATN, GAZP, SBER, LKOH.

### Calendar Arbitrage (Python, продакшен)
Brent календарный спред (BRQ6/BRU6). Z-score вход на аномалиях контанго/бэквордейшн.

## Брокер

Система использует [Finam Trade API](https://trade-api.finam.ru/) — gRPC для стримов данных и ордеров, REST для свечей и отчётов.

## Лицензия

См. [LICENSE](LICENSE).
