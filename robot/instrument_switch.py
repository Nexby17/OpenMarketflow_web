"""Live instrument switching for OF robots (F-017).

post_instrument_switch(): rebuild strategy/order manager/hub subscriptions
after SYMBOL/TICKER globals were updated by the /instrument endpoint.
Keeps mode (stopped/paused/running), params, account, trade history,
round trips and realized PnL. Resets only position state (new contract
always starts FLAT) and VP/CVD/VWEMA warmup state.
"""

import logging
import os
import json

log = logging.getLogger("of.instrument")


def post_instrument_switch(strategy, params, state_file, build_strategy, build_orders,
                           resubscribe, global_reset):
    """Common post-switch rebuild. Returns dict of new state snapshot."""
    # 1) wipe persisted state of the old contract (it is NOT valid for the new one)
    try:
        if os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                st = json.load(f)
        else:
            st = {}
    except Exception:
        st = {}
    st.update({"dir": 0, "totalLots": 0, "avgPrice": 0.0, "entryPrice": 0.0,
               "averageLevels": 0, "pyramidLevels": 0, "peakLots": 0,
               "lotQueue": [], "entryTime": "", "signalType": ""})
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)

    # 2) rebuild strategy, keep trade history / round trips / realized PnL
    old_hist = getattr(strategy, "_trade_history", [])
    old_rt = getattr(strategy, "_round_trips", 0)
    old_pnl = getattr(strategy, "_realized_pnl", 0.0)
    new_strategy = build_strategy(params)
    new_strategy._trade_history = old_hist
    new_strategy._round_trips = old_rt
    new_strategy._realized_pnl = old_pnl
    global_reset("strategy", new_strategy)

    # 3) rebuild order manager for the new symbol (same account/paper/rest4)
    new_orders = build_orders()
    global_reset("orders", new_orders)

    # 4) resubscribe hub to the new symbol
    resubscribe()

    log.info(f"Instrument switch complete: history kept ({len(old_hist)} trades, {old_rt} round trips)")
    return {"dir": 0, "lots": 0, "history_kept": len(old_hist)}
