"""F-061: trade journal tests — open/close lifecycle, persistence, ordering."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "robot"))

from sarema.state_sarema import SaremaState  # noqa: E402


def _st(tmp_path):
    s = SaremaState(str(tmp_path / "st.json"))
    return s


def test_journal_open_and_close(tmp_path):
    s = _st(tmp_path)
    s.journal_open({"tic": "SBER", "tf": "H1", "date_open": "2026-10-07", "time_open": "10:00:01",
                    "dir": "L", "entry": 300.0, "qty": 100, "volume": 30000.0,
                    "risk_pct": 0.5, "risk_pts": 3.5, "stop": 296.5})
    assert len(s.trade_history) == 1
    assert s.trade_history[0]["status"] == "open"
    assert s.trade_history[0]["close"] is None
    closed = s.journal_close({"date_close": "2026-10-07", "time_close": "15:00:00",
                              "exit": 304.0, "commission": 90.0, "pnl": 310.0,
                              "R": 0.886, "reason": "signal"})
    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["close"]["exit"] == 304.0
    # повторное закрытие — нечего закрывать
    assert s.journal_close({"exit": 305}) is None


def test_journal_multiple_open_uses_last(tmp_path):
    """Несколько открытых (не должно случаться, 1 позиция/тикер, но защита):
    закрытие падает в последнюю открытую."""
    s = _st(tmp_path)
    s.journal_open({"tic": "SBER", "dir": "L", "entry": 100})
    s.journal_open({"tic": "SBER", "dir": "L", "entry": 101})
    s.journal_close({"exit": 102, "pnl": 100})
    assert s.trade_history[0]["status"] == "open"
    assert s.trade_history[1]["status"] == "closed"


def test_journal_persist_roundtrip(tmp_path):
    s = _st(tmp_path)
    s.journal_open({"tic": "GAZP", "dir": "S", "entry": 96.0, "qty": 100,
                    "volume": 9600.0, "risk_pct": 0.5, "risk_pts": 2.0, "stop": 98.0})
    s.journal_close({"exit": 95.0, "pnl": 50.0, "R": 0.25})
    s.save()
    s2 = SaremaState(str(tmp_path / "st.json"))
    s2.load()
    assert len(s2.trade_history) == 1
    assert s2.trade_history[0]["close"]["exit"] == 95.0
    assert s2.trade_history[0]["status"] == "closed"


def test_journal_cap_500(tmp_path):
    s = _st(tmp_path)
    for i in range(600):
        s.journal_open({"tic": "X", "n": i})
        s.journal_close({"exit": 1})
    assert len(s.trade_history) <= 500


def test_journal_ordering_open_first(tmp_path):
    """API-порядок: открытые сверху (новые первые), затем закрытые (новые первые)."""
    s = _st(tmp_path)
    s.journal_open({"tic": "A", "n": 1}); s.journal_close({"exit": 1})
    s.journal_open({"tic": "B", "n": 2}); s.journal_close({"exit": 2})
    s.journal_open({"tic": "C", "n": 3})                       # открытая
    open_recs = [r for r in s.trade_history if r["status"] == "open"]
    closed_recs = [r for r in s.trade_history if r["status"] == "closed"]
    assert open_recs[0]["tic"] == "C"
    assert closed_recs[-1]["n"] == 2 and closed_recs[0]["n"] == 1
