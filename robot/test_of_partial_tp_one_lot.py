"""F-216: partial TP закрывает по 1 лоту даже если слой создан ручной корректировкой."""
import sys, time
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
from strategy_of import OFParams, OrderFlowStrategy, LONG, SHORT


class LE:
    def __init__(self, price, side, lots, age=600.0):
        self.price, self.side, self.lots = price, side, lots
        self.added_ts = time.monotonic() - age
        self.src = "manual"
        self.uid = "u"


def mk(queue, direction, spread=375, pct=60, min_profit=160):
    p = OFParams()
    p.partial_tp = True
    p.spread = spread
    p.min_profit_per_lot = min_profit
    p.close_half_pct = pct
    p.use_timing = False
    p.use_vah_val = False
    p.use_vwema = False
    s = OrderFlowStrategy(p)
    s._dir = direction
    s._total_lots = sum(l for _, l in queue)
    s._peak_lots = s._total_lots
    s._avg_price = sum(pr * l for pr, l in queue) / s._total_lots
    s._lot_queue = [LE(pr, direction, l) for pr, l in queue]
    s._current_price = queue[-1][0]
    return s


print("=== слой 11 лотов (ручной adjust), peak 11, Close % All 60 → порог 6 ===")
s = mk([(233200.0, 11)], LONG)
print("  до:", s._total_lots, "лот(ов), очередь:", [(e.price, e.lots) for e in s._lot_queue])

acts = s._check_partial_tp(233625.0)   # 233200 + 375 = 233575 → тейк
print("  TP #1:", acts[0]["qty"], "лот | осталось:", s._total_lots, "| очередь:", [(e.price, e.lots) for e in s._lot_queue])
assert acts[0]["qty"] == 1 and s._total_lots == 10

for i in range(2, 6):
    a = s._check_partial_tp(233625.0)
    assert a and a[0]["qty"] == 1, a
    print(f"  TP #{i}: 1 лот | осталось: {s._total_lots}")
assert s._total_lots == 6, s._total_lots

print("  остаток 6 == порог Close % All → следующий тейк закрывает ВЕСЬ остаток одним ордером")
a = s._check_partial_tp(233625.0)
print("  BATCH:", a[0]["action"], "qty=", a[0]["qty"], "reason=", a[0].get("reason"))
assert a[0]["action"] == "close_all" or a[0]["qty"] == 6, a
print("PASS: 11 → по 1 лоту до 6 → остаток пакетом")

print("\n=== пик 2 лота: порог Close % All = 2 → остаток 2 закрывается пакетом (правило) ===")
s2 = mk([(233200.0, 1), (232800.0, 1)], LONG)
a2 = s2._check_partial_tp(233575.0)
print("  TP тип:", a2[0]["action"], "| qty:", a2[0]["qty"], "| reason:", a2[0].get("reason"), "| осталось:", s2._total_lots)
assert a2[0]["action"] == "close_all" and a2[0]["qty"] == 2
print("PASS (малый пик: сразу пакет, как и требует Close % All)")

print("\n=== пик 5 лотов, остаток 5 > порога 3 → по 1 лоту ===")
s2b = mk([(233200.0, 3), (232900.0, 2)], LONG)
a2b = s2b._check_partial_tp(233575.0)
print("  TP qty:", a2b[0]["qty"], "| осталось:", s2b._total_lots, "| порог:", max(2, int(5 * 60 / 100)))
assert a2b[0]["qty"] == 1 and s2b._total_lots == 4
print("PASS")

print("\n=== SHORT, слой 5 лотов ===")
s3 = mk([(234000.0, 5)], SHORT)
a3 = s3._check_partial_tp(233625.0)   # 234000 - 375
print("  TP:", a3[0]["qty"], "лот | шаг:", a3[0]["side"], "| осталось:", s3._total_lots)
assert a3[0]["qty"] == 1 and a3[0]["side"] == "buy" and s3._total_lots == 4
print("PASS")

print("\nALL OK")
