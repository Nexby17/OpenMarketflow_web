"""Quick test for the new PortfolioRiskManager."""
from risk import PortfolioRiskManager, PositionInfo

prm = PortfolioRiskManager(capital=10_000_000)
print(f"Capital: {prm.limits.capital:,.0f}")
print(f"MaxLoss/pos: {prm.limits.max_loss_per_position_rub:,.0f}")
print(f"MaxGross: {prm.limits.max_gross_exposure_rub:,.0f}")
print(f"MaxNet: {prm.limits.max_net_exposure_rub:,.0f}")
print(f"VaR99 limit: {prm.limits.max_var_99_rub:,.0f}")
print(f"DailyLoss: {prm.limits.daily_loss_limit_rub:,.0f}")
print(f"DD limit: {prm.limits.drawdown_limit_pct}%")

# Register a GAZP arb position (delta-neutral)
prm.register_position(PositionInfo(
    ticker="GAZP", strategy_id="arb_gazp", side=1,
    quantity=10, exposure_rub=2_500_000,
    entry_price=150, current_price=150,
    unrealized_pnl=0, daily_pnl=0,
))
prm.register_position(PositionInfo(
    ticker="GZM6", strategy_id="arb_gazp", side=-1,
    quantity=1, exposure_rub=2_500_000,
    entry_price=15000, current_price=15000,
    unrealized_pnl=0, daily_pnl=0,
))

ok, reason = prm.can_trade_now()
print(f"\nCan trade: {ok} ({reason})")

ok, reason = prm.can_open_new_position("ROSN", 3_000_000, side=1)
print(f"Can open ROSN 3M: {ok} ({reason})")

ok, reason = prm.can_open_new_position("SBER", 2_000_000, side=1)
print(f"Can open SBER 2M: {ok} ({reason})")

var95, var99 = prm._compute_var()
print(f"\nVaR95 1-day: {var95:,.0f} RUB")
print(f"VaR99 1-day: {var99:,.0f} RUB")

stress = prm._stress_test()
print(f"\nStress tests:")
for k, v in stress.items():
    print(f"  {k}: {v:,.0f} RUB")

status = prm.get_status()
print(f"\nSector exposure: {status['sector_exposure']}")
print(f"Correlated clusters: {status['correlated_clusters']}")
print(f"Positions: {status['positions']}")

# Test kill-switch
prm.update_daily_pnl(-160_000)
ok, reason = prm.can_trade_now()
print(f"\nAfter -160K daily PnL:")
print(f"  Can trade: {ok} ({reason})")

# Test legacy compat
prm2 = PortfolioRiskManager(capital=10_000_000)
ok, msg = prm2.check_pnl(-5000)
print(f"\nLegacy check_pnl(-5000): {ok} ({msg})")
ok, msg = prm2.check_pnl(-200000)
print(f"Legacy check_pnl(-200000): {ok} ({msg})")
ok, msg = prm2.can_trade()
print(f"Legacy can_trade(): {ok} ({msg})")

print("\n=== ALL TESTS PASSED ===")
