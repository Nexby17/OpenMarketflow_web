# 🤖 Arbitrage Robot (PYTHON) — квазиарб спот/фьючерс и календарные спреды

*Актуально на 2026-10-01. Один код — три инстанса: GAZP/GZZ6 (:5090, боевой), SBER/SBRF (:5091), BR-календарь (:5092). ТЗ v2 (июнь 2026) удалено как устаревшее — эта страница и git-история заменяют его.*

---

## 1. Файлы и запуск

| Что | Где |
|---|---|
| Точка входа (все инстансы) | `robot/main_arb.py --port N --no-paper` |
| Стратегия (живая версия) | `robot/arb_common/strategy_arb.py` (sys.path приоритет выше копии в robot/) |
| Движок базиса/стаканов | `robot/arb_common/arb_engine.py` (BasisCalculator, OrderBookTracker) |
| Исполнение | `robot/orders_arb.py` (gRPC Finam + DP-фолбэк) |
| REST-мост | `robot/finam_rest4.py` (SDK 4.3.3, TokenManager) |
| Ресинк журнала | `robot/resync_journal.py` (dry-run / --apply) |
| Конфиг инстанса | `robot/arb_config_<pair>.json` (env ARB_CONFIG_FILE) |
| State инстанса | `robot/arb_state_<pair>.json` (env ARB_STATE_FILE, автосейв 30с) |
| Логи | `robot/logs/arb_YYYYMMDD.log` — общий на 3 инстанса (разделение — F-052 planned) |

Инстанс = пара + порт + конфиг + state (env-переменные в той же команде старта — R-013). Пара зашита в конфиг; смена пары через UI переписывает конфиг инстанса.

## 2. Механика пары

Базис = `price_B − price_A × hedge_ratio`. Для GAZP/GZZ6: hedge_ratio=100 (1 фьючерс = 100 акций), lots 10×1 (10 лотов акций = 100 акций = 1 контракт).

- **SHORT базис** (фьючерс дорог): BUY A + SELL B.
- **LONG базис** (фьючерс дёшев): SELL A + BUY B (нужен `allow_long_basis=true`, по умолчанию выключен).

Слои: каждый вход — отдельный слой ArbLayer; усреднение разрешено только при продолжении движения отклонения (`_allows_averaging`); закрытие FIFO от старейшего слоя.

## 3. Вход (не менять без решения Дмитрия — R-002)

Режим `zscore` (дефолт): ежеминутный пуш отклонения от fair value (carry − дивиденды), окно 2500 пушей (~5 торговых дней), вход в % годовых:

- SHORT: `dev_ann_pct > dev_ann_high` (0.2 для GAZP) + реализуемый спред > 0
- LONG: `dev_ann_pct < dev_ann_low` (−1.0) + спред < fair

Режим `spread_rub` (legacy): абсолютные пороги спреда в ₽.

Гейты входа: достаточность данных, санитария стакана (`_orderbook_sane`: крест/mid-vs-last/спред/свежесть — F-040), капитал, дубли-защита, daily_stop.

## 4. Исполнение

- Leg A (акция): LIMIT IOC по стороне входа (BUY→ask, SELL→bid), timeout `leg_a_timeout` (5с), недобор добирается MARKET (top-up).
- Leg B (фьючерс): MARKET сразу после фила A.
- Цены исполнения: `_resolve_fill_price` — trade sub → REST GetTrades (wavg за 6 дней) → gRPC avg_price → оценка (флаг is_real). Журнал = цены брокера (R-005, F-048).
- Срыв второй ноги при исполненной первой — известная дыра (L14, 30.09): фиксируется R-006, код-guard F-051 planned.

## 5. Выход

1. **Одиночный тейк (FIFO):** реализуемый PnL слоя ≥ `min_profit_value` (30₽ GAZP) N тиков подряд (3 × 0.5с, F-032). Реализуемый = стакан обеих ног − slippage 5bps на B − комиссии; протухший стакан (>3с) блокирует решение.
2. **Групповой тейк:** все слои > 0 и сумма ≥ порога (`profit_target_total`).
3. **Риск:** stop_loss_rub / time_stop / max_dd / daily_stop (5000₽, до 07:00 МСК, F-028).

Комиссии: акции % оборота (`commission_stock_pct` 0.02), фьючерс ₽/контракт RT (`commission_futures_rt` 0.2).

## 6. Авто-ресинк журнала (F-048)

При старте `resync_silent` тянет GetTrades за 6 дней и правит в state сделки, у которых `priceSource != broker`: матч ордеров к слоям по времени (±120с), wavg по size, фильтр чужих фьючерсов (цена 9000–11000 отсекает SI/MXU6 других роботов). Пересчитывает realized/peak/max_dd, чинит входные цены открытых слоёв. Ручной прогон: `python resync_journal.py [--apply]`; живой процесс: `POST /reload-state`.

## 7. HTTP API (:5090)

GET `/status /health /params /accounts /instruments` · POST `/start /stop /pause /params /account /reload-state /reset-stats /copy`.

`/start /stop /pause` логируются (F-050); UI-кнопки с confirm (F-050). `/stop` закрывает позиции рыночными — осознанно (R-008). Смена режима видна в логе.

## 8. State

`arb_state_<pair>.json`: layers (side, цены входа, order_id_a/b, price_real_a/b), trade_history (500 последних, с priceSource и 4 order_id), realized/peak/max_dd, next_layer_id, mode.

- Редактировать руками только при остановленном процессе (R-012); живой процесс — `POST /reload-state` (журнал/статистика, слои не трогает).
- daily_stop не переживает рестарт (F-028).
- При старте: авто-ресинк → load_state → startup-сверка с брокером.

## 9. Известные ограничения

- Общий лог трёх инстансов (F-052 planned); чужие строки фильтровать по имени логгера (`robot_arb` общий, инстанс различить по порту запросов).
- Лента сделок брокера неполная/с задержкой (R-010) — сверять по позиции.
- DP 5060 может лежать → фолбэки отмен мертвы (R-015, F-053 planned); gRPC-путь работает напрямую.
- Сорванная лимитка A может исполниться после таймаута — проверять статус ордера (R-006/R-014).
