# DataProvider (Python FastAPI, порт 5060)

*Актуально на 2026-10-01. Статус критичности: R-015 (RULES.md) — лежащий DP убивает фолбэки ордеров арб-роботов.*

## Назначение

Кэширующий прокси Finam REST для Python-роботов: свечи/цитаты с кэшем, прокси постановки/статуса ордеров. Снижает расход REST-квоты (200 req/min на метод) и разгружает роботов от прямых вызовов.

## Файлы

- `DataProvider/launcher.py` — точка входа (порт 5060)
- `DataProvider/cache.py` — кэш свечей/цитат
- `DataProvider/config.py` / `config.json` — настройки
- `dataprovider.service` — systemd-юнит (Linux-вариант; на Windows — автозапуск не настроен)

## Основные эндпоинты (используются роботами)

- `POST /order/place` — постановка ордера (account, symbol, side, quantity, price, order_type, tag)
- `GET /order/status` — статус ордера (account, order_id)
- `POST /api/orders/cancel-all` — отмена всех ордеров счёта (fallback отмены в orders_arb)
- Кэш баров/цитат — через `get_bars`-путь SDK

## Использование роботами

- `orders_arb.py` / `orders_dp.py` — gRPC-first, DP как фолбэк постановки/отмены
- Если DP лежит: фолбэки мертвы → сорванные отмены/постановки (инцидент L14, 30.09)

## Статус и планы

- **F-053 (planned):** healthcheck DP в main-циклах роботов + алерт Дмитрию при недоступности
- Автоподнятие DP при старте роботов — на рассмотрении
