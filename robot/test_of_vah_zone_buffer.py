"""F-226: гистерезис зоны VAH/VAL — проверка реального метода vah_val_zone()."""
import sys
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web\robot")
sys.path.insert(0, r"C:\Users\User\.openclaw-autoclaw\workspace\OpenMarketflow_web")
from strategy_of import OFParams, OrderFlowStrategy


class VP:
    def __init__(self, vah, val):
        self.vah, self.val, self.poc, self.ready = vah, val, (vah + val) / 2, True


def mk(buf=0.0, block_inside=False, vah=234500.0, val=233000.0):
    p = OFParams()
    p.use_vah_val = True
    p.vah_val_mode = "range"
    p.vah_val_block_inside = block_inside
    p.vah_val_zone_buffer_pts = buf
    p.use_timing = False
    s = OrderFlowStrategy(p)
    s.vp = VP(vah, val)
    s._vah_zone = ""
    return s


print("=== БЕЗ буфера (0): дребезг ===")
s = mk(0.0)
seq = [(234502, "VAH"), (234498, ""), (234501, "VAH"), (234499, "")]
for px, want in seq:
    got = s.vah_val_zone(px)
    print(f"  {px} -> {got or 'BOTH'}")
    assert got == want, (px, got, want)
print("  подтверждено: зона мигает на каждом тике")

print("\n=== БУФЕР 5 пт (VAH 234500) ===")
s = mk(5.0)
seq = [(234502, "VAH"), (234498, "VAH"), (234496, "VAH"), (234494, ""), (234600, "VAH")]
for px, want in seq:
    got = s.vah_val_zone(px)
    print(f"  {px} -> {got or 'BOTH'}")
    assert got == want, (px, got, want)

print("\n=== снизу (VAL 233000, буфер 5) ===")
s = mk(5.0)
seq = [(232998, "VAL"), (233002, "VAL"), (233004, "VAL"), (233006, "")]
for px, want in seq:
    got = s.vah_val_zone(px)
    print(f"  {px} -> {got or 'BOTH'}")
    assert got == want, (px, got, want)

print("\n=== смена зон через весь диапазон ===")
s = mk(5.0)
seq = [(234600, "VAH"), (233800, ""), (233100, ""), (232990, "VAL")]
for px, want in seq:
    got = s.vah_val_zone(px)
    print(f"  {px} -> {got or 'BOTH'}")
    assert got == want, (px, got, want)

print("\n=== нет VP / не готов → обе стороны ===")
s = mk(5.0)
s.vp = VP(0, 0)
s.vp.ready = False
assert s.vah_val_zone(234500.0) == ""

print("\nALL OK")
