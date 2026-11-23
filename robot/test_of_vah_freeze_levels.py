"""F-227: заморозка уровней VAH/VAL на закрытый бар."""
import sys
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
from strategy_of import OFParams, OrderFlowStrategy


class VP:
    def __init__(self, vah, val):
        self.vah, self.val, self.poc, self.ready = vah, val, (vah + val) / 2, True


class Sig:
    def __init__(self):
        self._bars_added = 100


def mk(freeze, buf=5.0):
    p = OFParams()
    p.use_vah_val = True
    p.vah_val_mode = "range"
    p.vah_val_block_inside = False
    p.vah_val_zone_buffer_pts = buf
    p.vah_val_freeze_levels_per_bar = freeze
    s = OrderFlowStrategy(p)
    s.vp = VP(234500.0, 233000.0)
    s.signals = Sig()
    s._vah_zone = ""
    return s


print("=== freeze=OFF: уровни дрейфуют внутри бара ===")
s = mk(False)
print("  vp VAH=234500, цена 234480 ->", s.vah_val_zone(234480.0) or "BOTH")
s.vp.vah = 234400.0   # профиль перестроился на тике
print("  vp VAH стал 234400, цена 234480 ->", s.vah_val_zone(234480.0) or "BOTH", "(зона сменилась внутри бара)")

print("\n=== freeze=ON: уровни держатся до нового бара ===")
s = mk(True)
z1 = s.vah_val_zone(234480.0)
print("  vp VAH=234500, цена 234480 ->", z1 or "BOTH", "| заморожено:", s._vah_levels_frozen)
s.vp.vah = 234400.0
z2 = s.vah_val_zone(234480.0)
print("  vp VAH дрейфовал до 234400, цена та же ->", z2 or "BOTH", "| заморожено:", s._vah_levels_frozen)
assert z2 == z1, (z1, z2)
assert s._vah_levels_frozen == (234500.0, 233000.0)

s.signals._bars_added = 101   # закрылся новый бар
z3 = s.vah_val_zone(234480.0)
print("  новый бар (101): пересчёт ->", z3 or "BOTH", "| заморожено:", s._vah_levels_frozen)
assert z3 == "VAH", z3
assert s._vah_levels_frozen == (234400.0, 233000.0)

print("\n=== freeze=ON + буфер: реакция на цену остаётся тиковой ===")
s = mk(True)
s.vah_val_zone(234600.0)
print("  234600 ->", s.vah_val_zone(234600.0) or "BOTH")
print("  234498 ->", s.vah_val_zone(234498.0) or "BOTH", "(гистерезис держит)")
print("  234494 ->", s.vah_val_zone(234494.0) or "BOTH")

print("\nALL OK")
