# F-061 SAREMA — Робот SAR×EMA на акциях MOEX

Одноинстансовый робот: **1 экземпляр = 1 акция**. Требуется торговать 100 акций —
поднимается 100 экземпляров с индивидуальными env (паттерн arb-роботов проекта).

## Состав (этап 1)
- `sarema_engine.py` — сигнальный движок: точный порт бэктеста
  (`SAR/акции/gazp_sarema.py` + `extract_trades`): PSAR(0.02/0.2/0.2), EMA12,
  ATR EWM α=1/14 с дневным сбросом, std-полярность, вход по open следующего бара,
  стоп 3×ATR, трейлинг по PSAR, гэп-выход по open. **Без** портфельной логики.
- `config_sarema.py` — per-instance конфиг и лимиты Дмитрия (этап 2):
  qty = min(risk_sizing, max_qty, max_value_rub/price); null = без лимита, 0 = запрет.
- `tests/` — replay-тест 1-в-1 против эталонного CSV + unit-тесты индикаторов.

## Запуск replay
```
python -m pytest tests/ -v
```
Эталон: эталонный CSV независимого бэктеста 05.10.2026 (5898 сделок, 10 тикеров).

## Этапы (ТЗ F-061)
1. Движок + replay ← текущий
2. Сервис: feed/orders/state/risk-лимиты/API/системд-юниты
3. UI-строка + панель лимитов (webdesign)
4. Реал — только по явной команде Дмитрия (R-001). Paper отложен по решению Дмитрия.

## Env экземпляра
SAREMA_TICKER=SBER, SAREMA_PORT=5084, SAREMA_CONFIG_FILE, SAREMA_STATE_FILE,
SAREMA_ACCOUNT_ID, FINAM_API_KEY (общий).
