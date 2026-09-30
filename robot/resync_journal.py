"""F-048 (2026-09-29): Resync arb trade journal with REAL broker fills.

Проблема: робот писал в журнал оценки (лимитная цена ноги A / оценка стакана ноги B),
потому что gRPC-стрим сделок падал без auth (12607 ошибок за 29.09), а в proto Order
нет поля average_price. Журнал расходился с брокером до ~126 пунктов по ноге B.

Скрипт тянет фактические исполнения через REST GetTrades (finam_rest4), сопоставляет
ордера слоям по времени входа/выхода, пересчитывает PnL и переписывает trade_history
в arb_state_*.json. Идемпотентен: сопоставление по entryTime/exitTime, цены каждый
раз берутся от брокера.

Порядок применения (журнал в работающем процессе не перечитывается):
  1) python resync_journal.py                # dry-run: таблица было/стало
  2) python resync_journal.py --apply        # пишет исправленный state
  3) POST http://localhost:5090/reload-state # живой процесс перечитает журнал
     (endpoint есть только в новом коде — сначала перезапустить робота)

Порядок слоёв/цен НЕ трогает открытые позиции, кроме входных цен (они тоже
заменяются на фактические цены брокера — по ним считается нереализованный PnL).
"""
import sys, os, json, argparse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
for _p in (os.path.join(HERE, "py4"), PROJ, HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

MSK = timezone(timedelta(hours=3))
MATCH_WINDOW_SEC = 120.0  # допуске ордера к слою по времени входа/выхода


def load_api_key() -> str:
    for base in (HERE, PROJ, os.path.join(PROJ, "src")):
        fp = os.path.join(base, ".env")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("FINAM_API_KEY="):
                            return line.split("=", 1)[1].strip()
            except OSError:
                pass
    return os.environ.get("FINAM_API_KEY", "")


def parse_msk(s: str) -> float:
    """'2026-09-29 12:24:25' (MSK, наивный) → unix ts."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).timestamp()


def parse_iso_utc(s: str) -> float:
    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).timestamp()


def agg_by_order(trades: list) -> dict:
    """Сделки → по order_id: wavg-цена, суммарный size, сторона, время первой сделки."""
    out = {}
    for t in trades:
        oid = str(t.get("order_id", ""))
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or 0)
        if not oid or price <= 0 or size <= 0:
            continue
        a = out.get(oid)
        if a is None:
            out[oid] = {
                "price": price, "size": size, "side": t.get("side", ""),
                "ts": parse_iso_utc(t.get("timestamp", "")),
                "symbol": (t.get("symbol") or "").split("@")[0].upper(),
            }
        else:
            total = a["size"] + size
            a["price"] = (a["price"] * a["size"] + price * size) / total
            a["size"] = total
    return out


def commission(entry_a: float, exit_a: float, lots_a: int, lots_b: int,
               pct: float, fut_rt: float, mult_a: float) -> float:
    """Та же формула, что strategy._calc_commission: stock % оборота + fut ₽/контракт RT."""
    stock_turnover = (abs(entry_a) + abs(exit_a)) * lots_a * mult_a
    return stock_turnover * pct / 100.0 + fut_rt * lots_b


def layer_pnl(side: str, ea: float, eb: float, xa: float, xb: float,
              lots_a: int, lots_b: int, mult_a: float, mult_b: float,
              pct: float, fut_rt: float) -> tuple:
    """SHORT: buy A + sell B → pnl = (xa-ea)*A + (eb-xb)*B. LONG наоборот."""
    if side == "SHORT":
        pnl_a = (xa - ea) * lots_a * mult_a
        pnl_b = (eb - xb) * lots_b * mult_b
    else:
        pnl_a = (ea - xa) * lots_a * mult_a
        pnl_b = (xb - eb) * lots_b * mult_b
    gross = pnl_a + pnl_b
    comm = commission(ea, xa, lots_a, lots_b, pct, fut_rt, mult_a)
    return gross, comm, gross - comm


def main() -> int:
    ap = argparse.ArgumentParser(description="Resync arb journal with broker fills")
    ap.add_argument("--state", default=os.path.join(HERE, "arb_state_gazp.json"))
    ap.add_argument("--account", default=os.environ.get("FINAM_ACCOUNT", "2049688"))
    ap.add_argument("--days", type=int, default=6)
    ap.add_argument("--apply", action="store_true", help="записать исправленный state")
    args = ap.parse_args()

    key = load_api_key()
    if not key:
        print("ERROR: FINAM_API_KEY не найден (src/.env)")
        return 1
    os.environ["FINAM_API_KEY"] = key

    import finam_rest4 as r4

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    print(f"REST GetTrades acc={args.account} {start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC")
    trades = r4.get_trades(args.account, start, end, limit=1000)
    print(f"получено сделок: {len(trades)}")
    if not trades:
        print("ERROR: брокер не вернул сделок — ресинк невозможен")
        return 1

    orders = agg_by_order(trades)
    ga = sorted([o for o in orders.values() if o["symbol"] == "GAZP"], key=lambda o: o["ts"])
    fu = sorted([o for o in orders.values() if o["symbol"] != "GAZP"
                 and 9000.0 < o["price"] < 11000.0], key=lambda o: o["ts"])
    # фильтр цены фьючерса: GZZ6 торгуется ~9700–10200; отсекает чужие ордера
    # других роботов на том же счёте (SI ~80000, MXU6 ~228000 и т.п.)
    print(f"ордеров всего: {len(orders)} | GAZP: {len(ga)} | фьючерсы: {len(fu)}")

    with open(args.state, encoding="utf-8") as f:
        state = json.load(f)

    used: set[str] = set()  # order_id уже сопоставленные

    def find(pool, side_want: str, near_ts: float, exclude_side_ts: float = None):
        """Ближайший неиспользованный ордер нужной стороны в окне MATCH_WINDOW_SEC."""
        best = None
        for o in pool:
            if o["side"] != side_want or id(o) in used:
                continue
            dt = abs(o["ts"] - near_ts)
            if dt <= MATCH_WINDOW_SEC and (best is None or dt < best[0]):
                best = (dt, o)
        if best is None:
            return None
        used.add(id(best[1]))
        return best[1]

    history = state.get("trade_history", [])
    changed = 0
    rows = []
    for rec in history:
        lid = rec.get("layerId")
        side = rec.get("side", "SHORT")
        ea_side = "SIDE_BUY" if side == "SHORT" else "SIDE_SELL"
        eb_side = "SIDE_SELL" if side == "SHORT" else "SIDE_BUY"
        t_in = parse_msk(rec["entryTime"])
        t_out = parse_msk(rec["exitTime"]) if rec.get("exitTime") else None

        oa_in = find(ga, ea_side, t_in)
        ob_in = find(fu, eb_side, t_in)
        oa_out = find(ga, eb_side, t_out) if t_out else None
        ob_out = find(fu, ea_side, t_out) if t_out else None

        if not (oa_in and ob_in):
            rows.append((lid, "SKIP: нет ордеров входа у брокера", "", ""))
            continue

        ea, eb = oa_in["price"], ob_in["price"]
        if oa_out and ob_out:
            xa, xb = oa_out["price"], ob_out["price"]
            gross, comm, net = layer_pnl(side, ea, eb, xa, xb, rec["lotsA"], rec["lotsB"],
                                         10.0, 1.0, 0.02, 0.2)
            old_net = rec.get("pnl", 0.0)
            rows.append((lid, f"{old_net:+8.2f} → {net:+8.2f}",
                         f"A {rec['entryPriceA']:.2f}/{rec['exitPriceA']:.2f} → {ea:.2f}/{xa:.2f}",
                         f"B {rec['entryPriceB']:.1f}/{rec['exitPriceB']:.1f} → {eb:.1f}/{xb:.1f}"))
            rec["entryPriceA"], rec["entryPriceB"] = round(ea, 4), round(eb, 4)
            rec["exitPriceA"], rec["exitPriceB"] = round(xa, 2), round(xb, 2)
            rec["entryBasis"] = round(eb - ea * 100.0, 2)
            rec["exitBasis"] = round(xb - xa * 100.0, 2)
            rec["commission"] = round(comm, 2)
            rec["pnl"] = round(net, 2)
            rec["priceSource"] = "broker"
            rec["entryOrderIdA"] = str([k for k, v in orders.items() if v is oa_in][0])
            rec["entryOrderIdB"] = str([k for k, v in orders.items() if v is ob_in][0])
            rec["exitOrderIdA"] = str([k for k, v in orders.items() if v is oa_out][0])
            rec["exitOrderIdB"] = str([k for k, v in orders.items() if v is ob_out][0])
            changed += 1
        else:
            # закрытия у брокера нет (экстренный выход?) — правим только вход
            rec["entryPriceA"], rec["entryPriceB"] = round(ea, 4), round(eb, 4)
            rec["entryBasis"] = round(eb - ea * 100.0, 2)
            rec["priceSource"] = "mixed"
            rows.append((lid, "вход исправлен, выход НЕ найден у брокера", f"A → {ea:.2f}", f"B → {eb:.1f}"))

    # Открытые слои: входные цены → фактические (влияет на нереализованный PnL)
    for ld in state.get("layers", []):
        t_in = float(ld.get("entry_time", 0))
        oa_in = find(ga, "SIDE_BUY" if ld["side"] == -1 else "SIDE_SELL", t_in)
        ob_in = find(fu, "SIDE_SELL" if ld["side"] == -1 else "SIDE_BUY", t_in)
        if oa_in and ob_in:
            old_a, old_b = ld["entry_price_a"], ld["entry_price_b"]
            ld["entry_price_a"], ld["entry_price_b"] = round(oa_in["price"], 4), round(ob_in["price"], 4)
            ld["entry_basis"] = ld["entry_price_b"] - ld["entry_price_a"] * 100.0
            ld["order_id_a"] = str([k for k, v in orders.items() if v is oa_in][0])
            ld["order_id_b"] = str([k for k, v in orders.items() if v is ob_in][0])
            ld["price_real_a"] = ld["price_real_b"] = True
            rows.append((f"L{ld.get('layer_id')} open", "вход",
                         f"A {old_a:.2f} → {ld['entry_price_a']:.2f}",
                         f"B {old_b:.1f} → {ld['entry_price_b']:.1f}"))

    # Пересчёт кривой realized/peak/dd в хронологии выходов
    closed = [r for r in history if r.get("exitTime")]
    closed.sort(key=lambda r: r["exitTime"])
    realized = peak = 0.0
    max_dd = 0.0
    for r in closed:
        realized += r.get("pnl", 0.0)
        peak = max(peak, realized)
        max_dd = max(max_dd, peak - realized)
    state["realized_pnl"] = realized
    state["peak_pnl"] = peak
    state["max_dd"] = max_dd

    print()
    print(f"{'слой':>12} | {'PnL было→стало':>22} | leg A | leg B")
    print("-" * 100)
    for r in rows:
        print(f"{str(r[0]):>12} | {r[1]:>22} | {r[2]:<26} | {r[3]}")
    print("-" * 100)
    print(f"исправлено закрытых сделок: {changed}/{len(history)}")
    print(f"realized_pnl: {realized:+.2f} | peak: {peak:.2f} | max_dd: {max_dd:.2f}")

    if args.apply:
        with open(args.state, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        print(f"ЗАПИСАНО: {args.state}")
        print("Далее: POST http://localhost:5090/reload-state (новый код) — живой процесс перечитает журнал.")
    else:
        print("dry-run: ничего не записано. Для записи: --apply")
    return 0


def resync_silent(state_path: str, account: str, days: int = 6) -> tuple:
    """F-048: программный ресинк (вызывается из main_arb при старте).
    Returns (changed_count, realized_pnl) or (0, None) on failure."""
    import logging
    _log = logging.getLogger("resync")
    try:
        key = load_api_key()
        if not key:
            return 0, None
        os.environ["FINAM_API_KEY"] = key
        import finam_rest4 as r4mod
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        trades = r4mod.get_trades(account, start, end, limit=1000)
        if not trades:
            return 0, None
        # повторяем логику main() без print-ов
        orders = agg_by_order(trades)
        ga = sorted([o for o in orders.values() if o["symbol"] == "GAZP"], key=lambda o: o["ts"])
        fu = sorted([o for o in orders.values() if o["symbol"] != "GAZP"
                     and 9000.0 < o["price"] < 11000.0], key=lambda o: o["ts"])
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        used: set = set()

        def find(pool, side_want, near_ts):
            best = None
            for o in pool:
                if o["side"] != side_want or id(o) in used:
                    continue
                dt = abs(o["ts"] - near_ts)
                if dt <= MATCH_WINDOW_SEC and (best is None or dt < best[0]):
                    best = (dt, o)
            if best is None:
                return None
            used.add(id(best[1]))
            return best[1]

        def oid_of(orders_map, agg):
            for k, v in orders_map.items():
                if v is agg:
                    return str(k)
            return ""

        history = state.get("trade_history", [])
        changed = 0
        for rec in history:
            if rec.get("priceSource") == "broker":
                continue  # уже ресинкнут ранее — не дёргаем
            side = rec.get("side", "SHORT")
            ea_side = "SIDE_BUY" if side == "SHORT" else "SIDE_SELL"
            eb_side = "SIDE_SELL" if side == "SHORT" else "SIDE_BUY"
            t_in = parse_msk(rec["entryTime"])
            t_out = parse_msk(rec["exitTime"]) if rec.get("exitTime") else None
            oa_in = find(ga, ea_side, t_in)
            ob_in = find(fu, eb_side, t_in)
            oa_out = find(ga, eb_side, t_out) if t_out else None
            ob_out = find(fu, ea_side, t_out) if t_out else None
            if not (oa_in and ob_in and oa_out and ob_out):
                continue
            ea, eb = oa_in["price"], ob_in["price"]
            xa, xb = oa_out["price"], ob_out["price"]
            _g, comm, net = layer_pnl(side, ea, eb, xa, xb, rec["lotsA"], rec["lotsB"],
                                      10.0, 1.0, 0.02, 0.2)
            rec["entryPriceA"], rec["entryPriceB"] = round(ea, 4), round(eb, 4)
            rec["exitPriceA"], rec["exitPriceB"] = round(xa, 2), round(xb, 2)
            rec["entryBasis"] = round(eb - ea * 100.0, 2)
            rec["exitBasis"] = round(xb - xa * 100.0, 2)
            rec["commission"] = round(comm, 2)
            rec["pnl"] = round(net, 2)
            rec["priceSource"] = "broker"
            rec["entryOrderIdA"] = oid_of(orders, oa_in)
            rec["entryOrderIdB"] = oid_of(orders, ob_in)
            rec["exitOrderIdA"] = oid_of(orders, oa_out)
            rec["exitOrderIdB"] = oid_of(orders, ob_out)
            changed += 1

        # Открытые слои: входные цены → факт брокера (корректный нереализованный PnL)
        for ld in state.get("layers", []):
            try:
                t_in = float(ld.get("entry_time", 0))
                side_int = int(ld.get("side", -1))
                oa_in = find(ga, "SIDE_BUY" if side_int == -1 else "SIDE_SELL", t_in)
                ob_in = find(fu, "SIDE_SELL" if side_int == -1 else "SIDE_BUY", t_in)
                if oa_in and ob_in:
                    ld["entry_price_a"] = round(oa_in["price"], 4)
                    ld["entry_price_b"] = round(ob_in["price"], 4)
                    ld["entry_basis"] = ld["entry_price_b"] - ld["entry_price_a"] * 100.0
                    ld["order_id_a"] = oid_of(orders, oa_in)
                    ld["order_id_b"] = oid_of(orders, ob_in)
                    ld["price_real_a"] = ld["price_real_b"] = True
            except Exception:
                continue
        if not (changed or any(l.get("price_real_a") or l.get("price_real_b") for l in state.get("layers", []))):
            return 0, None
        closed = [r for r in history if r.get("exitTime")]
        closed.sort(key=lambda r: r["exitTime"])
        realized = peak = 0.0
        max_dd = 0.0
        for r in closed:
            realized += r.get("pnl", 0.0)
            peak = max(peak, realized)
            max_dd = max(max_dd, peak - realized)
        state["realized_pnl"] = realized
        state["peak_pnl"] = peak
        state["max_dd"] = max_dd
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        _log.info(f"F-048 resync_silent: {changed} trades corrected, realized_pnl={realized:.2f}")
        return changed, realized
    except Exception as e:
        _log.warning(f"resync_silent failed (non-fatal): {e}")
        return 0, None


if __name__ == "__main__":
    sys.exit(main())
