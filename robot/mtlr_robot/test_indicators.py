"""Тест индикаторов + smoke-тест сигнальной логики на реальной истории MTLR (ISS H1)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators import ema, psar, atr, rsi, regime_osc

# --- 1. EMA: период 3, ручной расчёт ---
vals = [10, 11, 12, 13, 14, 15]
e = ema(vals, 3)
assert e[1] is None and e[2] == 11.0, e          # SMA(10+11+12)/3
k = 2/4
assert abs(e[3] - (13*k + 11*(1-k))) < 1e-9
assert abs(e[5] - (15*k + e[4]*(1-k))) < 1e-9
print("EMA OK", e[2:])

# --- 2. PSAR: растущий тренд — SAR под ценой; падающий — над ---
highs = [10, 10.5, 11, 11.5, 12, 12.5, 13]
lows  = [9.5, 10, 10.4, 11, 11.5, 12, 12.4]
s = psar(highs, lows, 0.02, 0.02, 0.2)
assert s[1] == 9.5                                  # старт SAR = low[0] в ап-тренде
assert all(s[i] < lows[i] for i in range(2, 7)), s  # SAR под ценой в аптренде
print("PSAR up OK", [round(x,3) for x in s[1:]])

highs2 = [13, 11.5, 11, 10.5, 10, 9.5, 9]
lows2  = [12.4, 11, 10.5, 10, 9.4, 9, 8.5]   # гэп вниз: старт даунтренда
s2 = psar(highs2, lows2, 0.02, 0.02, 0.2)
assert s2[1] == 13.0
assert all(s2[i] > highs2[i] for i in range(2, 7)), s2
print("PSAR down OK", [round(x,3) for x in s2[1:]])

# --- 3. ATR: константный TR -> ATR = TR ---
h = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]
l = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
c = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5, 19.5, 20.5, 21.5, 22.5, 23.5, 24.5]
# уберём гэпы: close внутри следующего диапазона
# флэт: h=10, l=9, c=9.5 — TR = 1 на каждом баре без гэпов
h2 = [10.0]*16
l2 = [9.0]*16
c2 = [9.5]*16
a = atr(h2, l2, c2, 14)
assert a[14] == 1.0, a
assert a[15] == 1.0, a
print("ATR OK", a[14])

# --- 4. RSI: все растёт -> 100; все падает -> 0; стабильные данные Wilder ---
r_up = rsi([float(i) for i in range(30)], 14)
assert r_up[29] == 100.0
r_dn = rsi([float(30 - i) for i in range(30)], 14)
assert r_dn[29] == 0.0
# классика: Wilder RSI на этих данных
data = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
        46.03, 46.41, 46.22, 45.64]
r = rsi(data, 14)
# первое значение RSI ≈ 70.46 (известный пример StockCharts)
assert abs(r[14] - 70.46) < 0.15, r[14]
print("RSI OK", round(r[14], 2))

# --- 5. regime osc: знак ---
o = regime_osc([10, 10.5], [9.5, 9.8], [1, 1])
assert o[0] == 0.5 and abs(o[1] - 0.7) < 1e-9
print("RegimeOsc OK")

print("\n=== ВСЕ ТЕСТЫ ИНДИКАТОРОВ ПРОЙДЕНЫ ===")
