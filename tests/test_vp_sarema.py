"""F-065 unit tests: Volume Profile SAREMA + VA-gate + config validation."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # repo root
sys.path.insert(0, os.path.join(_ROOT, "robot"))         # .../robot (sarema pkg)
sys.path.insert(0, _ROOT)

from sarema.vp_sarema import VolumeProfileSarema, gate_check, _nice_bin  # noqa: E402
from sarema.config_sarema import validate_params, DEFAULTS               # noqa: E402


class _Row:
    """Строка H1-DataFrame для feed_bars (itertuples)."""
    def __init__(self, begin, high, low, close, volume):
        self.begin = begin
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class _G:
    def __init__(self, rows):
        self._rows = rows

    def itertuples(self):
        return iter(self._rows)


def _feed(vp, base=100.0, n=60, vol=10.0):
    """Реалистичное окно: случайный-подобный дрейф вокруг base (детерминированный)."""
    rows = []
    for k in range(n):
        px = base + (1.0 if k % 2 else -1.0) * (0.5 + (k % 7) * 0.1)
        rows.append(_Row("2026-10-01 10:00:00", px * 1.01, px * 0.99, px, vol))
    vp.feed_bars(_G(rows))
    return vp.calculate()


# ---------- VolumeProfileSarema ----------

def test_vp_not_ready_without_data():
    vp = VolumeProfileSarema()
    assert not vp.ready
    assert not vp.calculate()


def test_vp_ready_and_va_contains_poc():
    vp = VolumeProfileSarema()
    assert _feed(vp, base=100.0)
    assert vp.ready
    assert vp.val < vp.poc < vp.vah
    # цены гуляют 98.4..101.6; VA 70% может быть уже полного размаха — проверяем охват центра
    assert vp.val <= 100.0 <= vp.vah


def test_vp_range_gate():
    vp = VolumeProfileSarema()
    _feed(vp, base=100.0)
    ok, reason, zone = gate_check("range", (vp.val + vp.vah) / 2, vp.vah, vp.val)
    assert ok and reason == "range_inside" and zone == ""
    ok, reason, zone = gate_check("range", vp.vah + 50.0, vp.vah, vp.val)
    assert not ok and reason == "range_outside" and zone == ""
    ok, reason, zone = gate_check("range", vp.val - 50.0, vp.vah, vp.val)
    assert not ok and reason == "range_outside"


def test_vp_fade_breakout_gates():
    vp = VolumeProfileSarema()
    _feed(vp, base=100.0)
    mid = (vp.val + vp.vah) / 2
    # внутри VA → блок, зона пустая
    ok, reason, zone = gate_check("fade", mid, vp.vah, vp.val)
    assert not ok and reason == "inside_va" and zone == ""
    # выше VAH → зона VAH (и fade, и breakout)
    ok, reason, zone = gate_check("fade", vp.vah + 1, vp.vah, vp.val)
    assert ok and zone == "VAH"
    ok, reason, zone = gate_check("breakout", vp.vah + 1, vp.vah, vp.val)
    assert ok and zone == "VAH"
    # ниже VAL → зона VAL
    ok, reason, zone = gate_check("fade", vp.val - 1, vp.vah, vp.val)
    assert ok and zone == "VAL"


def test_vp_gate_not_ready():
    ok, reason, _ = gate_check("range", 100.0, 0.0, 0.0)
    assert not ok and reason == "vp_not_ready"


def test_vp_bin_zero_means_auto():
    vp = VolumeProfileSarema(bin_size=0.0)
    assert _feed(vp, base=7000.0)   # LKOH-масштаб
    assert vp.ready
    assert vp._effective_bin > 0     # авто-бин подобран


def test_vp_nice_bin():
    assert _nice_bin(3.3) == 5.0
    assert _nice_bin(0.02) == 0.02
    assert _nice_bin(760.0) == 1000.0
    assert _nice_bin(0) == 0.0
    assert _nice_bin(2.0 / 30) == 0.1


def test_vp_state_snapshot():
    vp = VolumeProfileSarema()
    st = vp.state()
    assert st["ready"] is False and st["bars"] == 0
    _feed(vp)
    st = vp.state()
    assert st["ready"] is True and st["bars"] == 60


# ---------- config: VP-параметры ----------

def test_validate_vp_params_ok():
    clean, errors = validate_params({
        "vah_val_mode": "range", "vah_val_bin": 5.0,
        "vah_val_pct": 70, "vah_val_lookback": 200,
    })
    assert errors == []
    assert clean["vah_val_mode"] == "range"
    assert clean["vah_val_bin"] == 5.0
    assert clean["vah_val_pct"] == 70
    assert clean["vah_val_lookback"] == 200


def test_validate_vp_params_null_defaults():
    clean, errors = validate_params({
        "vah_val_bin": None, "vah_val_pct": None, "vah_val_lookback": None,
    })
    assert errors == []
    assert clean["vah_val_bin"] is None
    assert clean["vah_val_pct"] is None
    assert clean["vah_val_lookback"] is None


def test_validate_vp_zero_bin_is_auto():
    clean, errors = validate_params({"vah_val_bin": 0})
    assert errors == [] and clean["vah_val_bin"] == 0.0
    clean, errors = validate_params({"vah_val_pct": 0})
    assert errors == [] and clean["vah_val_pct"] is None   # 0 → дефолт 70


def test_validate_vp_params_bad():
    _, errors = validate_params({"vah_val_mode": "marginal"})
    assert len(errors) == 1
    _, errors = validate_params({"vah_val_pct": 30})
    assert len(errors) == 1
    _, errors = validate_params({"vah_val_lookback": 10})
    assert len(errors) == 1
    _, errors = validate_params({"vah_val_bin": -5})
    assert len(errors) == 1


def test_vp_defaults_off():
    """Дефолт = off: пока Дмитрий не включит, поведение SAREMA не меняется (R-002)."""
    assert DEFAULTS["vah_val_mode"] == "off"
    assert DEFAULTS["vah_val_bin"] is None
