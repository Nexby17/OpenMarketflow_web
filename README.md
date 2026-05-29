# OpenMarketflow — HedgeFund Trading System

Automated trading system for MOEX futures (SI, RTS, MIX) with VP Scalp Grid strategy.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Web UI      │────▶│  Python Robot    │────▶│  Finam gRPC  │
│  (port 5050) │◀────│  API (port 5070) │◀────│  REST API    │
└──────────────┘     └──────────────────┘     └──────────────┘
```

- **Python Robot** — VP Scalp Grid strategy, gRPC feeds, FastAPI control
- **C# Server** — static frontend hosting (OpenMarketflow UI)
- **DataProvider** — candles, quotes, OI data cache (port 5060)

## Directories

| Directory | Description |
|-----------|-------------|
| `robot/` | Python trading robot (main production code) |
| `robot/tests/` | Unit tests |
| `src/` | C# server (static hosting) |
| `DataProvider/` | Market data cache service |
| `backtest/src/` | Active backtest scripts |
| `backtest/src/archive/` | Archived research scripts (209 files) |
| `backtest/data/` | Historical CSV data |
| `backtest/results/` | Backtest results & reports |
| `arb/` | Arbitrage research scripts |
| `scripts/` | Deployment & utility scripts |
| `_archive/` | Deprecated (StockSharp, old prototypes) |
| `docs/` | Architecture & API docs |

## Robot: VP Scalp Grid

**Instrument:** SiM6 (SI futures) | **TF:** M1 | **Account:** 1225953

**Logic:**
1. Entry: price < VAL → LONG, price > VAH → SHORT
2. Grid: step=31pt against position, max 100 levels
3. TP per level: grid_price ± 31pt
4. Exit: 1 lot → POC hit, 2+ lots → PnL/lot ≥ 29₽, timeout disabled

**Parameters (hot-update via API):**
- max_levels=100, step_base=31, spread_base=31
- min_profit_per_lot=29, commission=0.90₽ RT
- vp_lookback=33, vp_bin_size=50, vp_va_percent=0.70
- rv_adaptation=False

## Quick Start

```bash
# Start robot
systemctl start trading-robot

# Check status
curl localhost:5070/status

# Web UI
http://<server>:5050
```

## Git

- **Main repo:** github.com/Nexby17/OpenMarketflow.git
- **Robot only:** github.com/n0iz3on3/Vp_sc_grid_mm_robot
