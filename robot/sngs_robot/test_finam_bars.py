"""Finam Bars как первоисточник свечей + ослабленная гарда последнего бара (23.11).

Offline, без сети: мок fp/resp для gRPC Bars.
  1. finam_fetch_bars конвертирует бары Finam в формат движка (t,o,h,l,c,v; epoch UTC)
  2. дедуп по t + сортировка + хвост count
  3. fp=None → [] (чистый фолбэк на ISS)
  4. исключение gRPC → [] (фолбэк, не краш)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from candles_source import finam_fetch_bars

class FakeLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False

class FakeBar:
    def __init__(self, t, c):
        from decimal import Decimal
        class Q:
            def __init__(self, v): self.value = Decimal(str(v))
        class TS:
            def __init__(self, s): self.seconds = s
        self.timestamp = TS(t)
        self.open = Q(c - 1); self.high = Q(c + 2); self.low = Q(c - 2); self.close = Q(c)
        class V:
            value = 100
        self.volume = V()

class FakeResp:
    def __init__(self, bars): self.bars = bars

class FakeStub:
    def Bars(self, req): return None  # заглушка; реальный ответ даёт call_function

class FakeFP:
    def __init__(self, pages):
        import datetime as dt
        self.marketdata_stub = FakeStub()
        self._pages = list(pages); self._n = 0; self._td = dt.timedelta(days=1)
    def timeframe_to_finam_timeframe(self, tf): return 60, self._td, True
    def call_function(self, stub, req):
        r = self._pages[self._n] if self._n < len(self._pages) else FakeResp([])
        self._n += 1
        return r

NOW = int(time.time())          # бары в реальном времени (отрезание незакрытого бара)

def mk(pages_bars):
    return FakeFP([FakeResp([FakeBar(t, c) for t, c in pb]) for pb in pages_bars])

def test_1_convert_and_dedup():
    # 2 «страницы»: вторая дублирует бар t=2000
    t1, t2, t3 = NOW - 7200, NOW - 3600, NOW - 3700   # все закрыты (t+3600 < now)
    fp = mk([[(t1, 10.0), (t2, 11.0)], [(t2, 11.0), (t3, 12.0)]])
    out = finam_fetch_bars(fp, FakeLock(), "H1", 10, "MTLR@MISX")
    assert [b["t"] for b in out] == sorted({t1, t2, t3}), out
    b = min(out, key=lambda x: x["t"])
    assert b["o"] == 9.0 and b["h"] == 12.0 and b["l"] == 8.0 and b["c"] == 10.0 and b["v"] == 100
    assert all(set(b.keys()) == {"t", "o", "h", "l", "c", "v"} for b in out)
    print("OK 1: конвертация формата + дедуп + сортировка")

def test_2_tail_count():
    ts = [NOW - 3600 * k for k in range(11, 0, -1)]
    fp = mk([[(t, float(i)) for i, t in enumerate(ts, 1)]])
    out = finam_fetch_bars(fp, FakeLock(), "H1", 3, "MTLR@MISX")
    assert [b["t"] for b in out] == ts[-3:], out
    # незакрытый бар отрезается: t в будущем относительно закрытия
    fp2 = mk([[(NOW - 3600, 20.0), (NOW, 21.0)]])   # NOW: t+3600 > now → отрезан
    out2 = finam_fetch_bars(fp2, FakeLock(), "H1", 5, "MTLR@MISX")
    assert [b["t"] for b in out2] == [NOW - 3600], out2
    print("OK 2: хвост по count")

def test_3_none_fp_fallback():
    assert finam_fetch_bars(None, FakeLock(), "H1", 10, "X") == []
    print("OK 3: fp=None → [] → ISS-фолбэк")

def test_4_grace_error():
    """gRPC-ошибка → [] → ISS-фолбэк, без крэша."""
    class Boom(FakeFP):
        def call_function(self, *a): raise RuntimeError("gRPC dead")
    assert finam_fetch_bars(Boom([]), FakeLock(), "H1", 10, "X") == []
    print("OK 4: gRPC-ошибка → [] → ISS-фолбэк")

def test_5_relaxed_last_bar_guard():
    """Гарда: последний бар обрабатывается при now >= t + tf + 90 (а не 2×tf=7200)."""
    from config_mtlr import MtlrParams
    from strategy_mtlr import StrategyEngine
    TF = 3600
    now = time.time()
    p = MtlrParams(); p.paper = True; p.timeframe = "H1"
    p.use_cross = True; p.use_pullback = False; p.use_donchian = False
    p.use_rsi = False; p.use_regime = False; p.use_d1 = False

    def mk(age_last, hist=8):
        # hist старых баров (давно закрыты) + последний бар age_last секунд назад
        out = [{"t": int(now) - TF * (hist + 2) + TF * k, "o": 10, "h": 11, "l": 9, "c": 10.0}
               for k in range(hist)]
        out.append({"t": int(now) - TF - age_last, "o": 10, "h": 11, "l": 9, "c": 10.5})
        return out

    # закрыт 40с назад (< 90с буфера) → ещё ждёт (не досрочно)
    c1 = mk(40)
    eng = StrategyEngine(p); eng.recompute(c1); eng.scan()
    assert eng.last_bar_t != c1[-1]["t"], "бар 40с не должен обрабатываться досрочно"
    # закрыт 200с назад (> 90с) → обработан (при старой гарде 2×tf ждал бы ещё ~56 мин)
    c2 = mk(200)
    eng2 = StrategyEngine(p); eng2.recompute(c2); eng2.scan()
    assert eng2.last_bar_t == c2[-1]["t"], "бар 200с должен обработаться при tf+90"
    print("OK 5: гарда tf+90с — через 90с после закрытия, не досрочно")

if __name__ == "__main__":
    test_1_convert_and_dedup()
    test_2_tail_count()
    test_3_none_fp_fallback()
    test_4_grace_error()
    test_5_relaxed_last_bar_guard()
    print("\n✅ 5/5 — Finam Bars первоисточник работает, фолбэк чист, гарда ослаблена")
