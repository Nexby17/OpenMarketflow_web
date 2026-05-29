"""FastAPI web API for robot control."""
import logging
import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

log = logging.getLogger("api")

app = FastAPI(title="Trading Robot API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reference to robot instance (set by main.py)
_robot = None


def set_robot(robot):
    global _robot
    _robot = robot


# === CONFIG MODELS ===

class ConfigUpdate(BaseModel):
    max_levels: Optional[int] = None
    step_base: Optional[int] = None
    spread_base: Optional[int] = None
    max_hold_minutes: Optional[int] = None
    min_profit_per_lot: Optional[int] = None
    vp_lookback: Optional[int] = None
    vp_bin_size: Optional[int] = None
    vp_va_percent: Optional[float] = None
    rv_adaptation: Optional[bool] = None


# === ENDPOINTS ===

@app.get("/status")
def status():
    if not _robot:
        return {"status": "not_initialized"}
    return _robot.get_status()


@app.get("/api/active-strategies")
def active_strategies():
    """Compatibility endpoint for OpenMarketflow frontend."""
    if not _robot:
        return {"strategies": []}

    s = _robot.strategy
    st = _robot.get_status()
    mode = st.get("mode", "stopped")
    mode_text = "Running" if mode == "running" else "Paused" if mode == "paused" else "Stopped"
    pos_dir = st.get("direction", 0)

    return {
        "strategies": [{
            "id": "python-vp-scalp-grid",
            "name": "VP Scalp Grid (Python)",
            "instrument": "SiM6",
            "mode": mode_text,
            "connected": st.get("connected", False),
            "posDir": pos_dir,
            "entryPrice": st.get("entry_price", 0),
            "openLots": st.get("total_lots", 0),
            "totalPnL": st.get("realized_pnl", 0) + st.get("pnl", 0),
            "roundTrips": st.get("round_trips", 0),
            "holdMinutes": st.get("hold_minutes", 0),
            "gridLevels": st.get("grid_levels", 0),
            "filledLevels": st.get("filled_levels", 0),
            "poc": st.get("poc", 0),
            "vah": st.get("vah", 0),
            "val": st.get("val", 0),
            "currentPrice": st.get("current_price", 0),
            "paper": st.get("paper", True),
            "realizedPnl": st.get("realized_pnl", 0),
            "unrealizedPnl": st.get("pnl", 0),
            # Config params
            "config": {
                "max_levels": s.params.max_levels,
                "step_base": s.params.step_base,
                "spread_base": s.params.spread_base,
                "max_hold_minutes": s.params.max_hold_minutes,
                "min_profit_per_lot": s.params.min_profit_per_lot,
            },
        }]
    }


@app.get("/api/robot/config")
def get_config():
    """Get current strategy parameters."""
    if not _robot:
        return {"error": "Robot not initialized"}
    p = _robot.strategy.params
    return {
        "max_levels": p.max_levels,
        "step_base": p.step_base,
        "spread_base": p.spread_base,
        "max_hold_minutes": p.max_hold_minutes,
        "min_profit_per_lot": p.min_profit_per_lot,
        "commission": p.commission,
        "vp_lookback": p.vp_lookback,
        "vp_bin_size": p.vp_bin_size,
        "vp_va_percent": p.vp_va_percent,
        "rv_adaptation": p.rv_adaptation,
    }


@app.post("/api/robot/config")
def update_config(cfg: ConfigUpdate):
    """Hot-update strategy parameters."""
    if not _robot:
        return {"error": "Robot not initialized"}

    p = _robot.strategy.params
    changes = {}
    for field, val in cfg.dict(exclude_none=True).items():
        if hasattr(p, field):
            old = getattr(p, field)
            setattr(p, field, val)
            changes[field] = {"old": old, "new": val}
            log.info(f"Config hot-update: {field} {old} → {val}")

    if changes:
        # Sync VP params if changed
        if 'vp_lookback' in changes or 'vp_bin_size' in changes or 'vp_va_percent' in changes:
            _robot.vp.lookback = p.vp_lookback
            _robot.vp.bin_size = p.vp_bin_size
            _robot.vp.va_percent = p.vp_va_percent
        _robot._save_state()
    return {"updated": changes}


@app.post("/start")
def start():
    if not _robot:
        return {"error": "Robot not initialized"}
    if _robot._mode == "running":
        return {"status": "already_running"}
    threading.Thread(target=_robot.start, daemon=True).start()
    return {"status": "starting"}


@app.post("/stop")
def stop():
    if not _robot:
        return {"error": "Robot not initialized"}
    threading.Thread(target=lambda: _robot.stop(close_position=True), daemon=True).start()
    return {"status": "stopping"}


@app.post("/pause")
def pause():
    if not _robot:
        return {"error": "Robot not initialized"}
    _robot.pause()
    return {"status": "paused"}


@app.post("/resume")
def resume():
    if not _robot:
        return {"error": "Robot not initialized"}
    _robot.resume()
    return {"status": "running"}


@app.get("/health")
def health():
    return {"status": "ok"}


# === GRID LEVELS DETAIL ===

@app.get("/api/robot/grid-levels")
def grid_levels():
    """Detailed grid levels info."""
    if not _robot:
        return {"levels": []}
    levels = []
    for g in _robot.strategy.grid_levels:
        levels.append({
            "level": g.level,
            "price": g.price,
            "side": "BUY" if g.side == 1 else "SELL",
            "status": g.status,
            "tp_price": g.tp_price,
            "tp_closed_price": g.tp_closed_price,
        })
    return {"levels": levels}
