"""
Полная валидация арбитражной стратегии:
1. Стресс-тест (волатильные периоды)
2. Monte Carlo (1000 перестановок)
3. Sensitivity analysis (параметры ±20%)
4. Реальные комиссии и проскальзывания

Комиссии:
- Акция: 0.04% от оборота (вход + выход)
- Фьючерс: 0.004% от оборота (вход + выход)
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

MONTHS = {'H': 3, 'M': 6, 'U': 9, 'Z': 12}
MONTH_CODES = ['H', 'M', 'U', 'Z']

INSTRUMENTS = {
    'ROSN': ('RN', 100),
    'TATN': ('TT', 100),
    'GAZP': ('GZ', 100),
    'SBER': ('SR', 100),
    'ALRS': ('AL', 100),
}

BEST_PARAMS = {
    'ROSN': {'window': 15, 'entry_z': 1.0, 'exit_z': 0.0, 'stop_z': 3.5},
    'TATN': {'window': 15, 'entry_z': 1.0, 'exit_z': 0.5, 'stop_z': 3.5},
    'GAZP': {'window': 20, 'entry_z': 1.5, 'exit_z': 0.0, 'stop_z': 3.5},
    'SBER': {'window': 15, 'entry_z': 1.5, 'exit_z': -0.5, 'stop_z': 3.5},
    'ALRS': {'window': 15, 'entry_z': 2.5, 'exit_z': 0.5, 'stop_z': 3.0},
}

OPTIMAL_ROLL = {
    'ROSN': 20, 'TATN': 15, 'GAZP': 20, 'SBER': 10, 'ALRS': 5,
}

# Комиссии
STOCK_COMMISSION = 0.0004  # 0.04% от оборота (вход + выход = x2)
FUTURES_COMMISSION = 0.00004  # 0.004% от оборота (вход + выход = x2)
SLIPPAGE_PCT = 0.0001  # 0.01% проскальзывание на вход и выход


def fetch_candles(secid, board, engine, market, start='2024-04-01', end='2026-04-02', interval=24):
    all_data = []
    start_row = 0
    while True:
        url = (f"https://iss.moex.com/iss/engines/{engine}/markets/{market}/"
               f"boards/{board}/securities/{secid}/candles.json"
               f"?from={start}&till={end}&interval={interval}&start={start_row}&iss.meta=off")
        try:
            resp = requests.get(url, timeout=30)
            data = resp.json()
            rows = data['candles']['data']
            if not rows: break
            cols = data['candles']['columns']
            all_data.extend(rows)
            start_row += len(rows)
            if len(rows) < 500: break
        except: break
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=cols)
    df['begin'] = pd.to_datetime(df['begin'])
    return df.set_index('begin')


def load_instrument(stock, fut_prefix, lot_size):
    spot = fetch_candles(stock, 'TQBR', 'stock', 'shares')
    if spot.empty: return None, None
    futures = {}
    for year in [4, 5, 6, 7]:
        for mc in MONTH_CODES:
            secid = f"{fut_prefix}{mc}{year}"
            month = MONTHS[mc]
            full_year = 2020 + year
            expiry = datetime(full_year, month, 15)
            fut = fetch_candles(secid, 'RFUD', 'futures', 'forts')
            if not fut.empty and len(fut) > 3:
                futures[secid] = {'data': fut, 'expiry': expiry}
    return spot, futures


def build_basis(spot, futures, lot_size, roll_days=15):
    records = []
    for date in spot.index:
        sp = spot.loc[date, 'close']
        available = []
        for secid, info in futures.items():
            dte = (info['expiry'] - date.to_pydatetime()).days
            if dte < 3 or dte > 200: continue
            if date in info['data'].index:
                available.append({'secid': secid, 'fut': info['data'].loc[date, 'close'],
                                  'dte': dte, 'expiry': info['expiry']})
        if not available: continue
        available.sort(key=lambda x: x['dte'])
        chosen = available[1] if available[0]['dte'] < roll_days and len(available) > 1 else available[0]
        bp = (chosen['fut'] / (sp * lot_size) - 1) * 100
        ba = bp * 365 / chosen['dte']
        records.append({'date': date, 'spot': sp, 'futures': chosen['fut'],
                        'secid': chosen['secid'], 'dte': chosen['dte'],
                        'basis_pct': bp, 'basis_annual': ba})
    return pd.DataFrame(records).set_index('date')


def calc_commission(entry_spot, exit_spot, entry_fut, exit_fut, lot_size, n_lots):
    """
    Реальные комиссии:
    - Акция: 0.04% от оборота на каждую сторону (вход + выход)
    - Фьючерс: 0.004% от оборота на каждую сторону
    - Проскальзывание: 0.01% на каждую сторону
    """
    # Акция: оборот = цена * лот * n_lots, дважды (вход + выход)
    stock_volume = (entry_spot + exit_spot) * lot_size * n_lots
    stock_comm = stock_volume * STOCK_COMMISSION
    
    # Фьючерс: оборот = цена фьючерса * n_lots, дважды
    fut_volume = (abs(entry_fut) + abs(exit_fut)) * n_lots
    fut_comm = fut_volume * FUTURES_COMMISSION
    
    # Проскальзывание: на обе ноги, вход и выход
    slippage_stock = stock_volume * SLIPPAGE_PCT
    slippage_fut = fut_volume * SLIPPAGE_PCT
    
    return stock_comm + fut_comm + slippage_stock + slippage_fut


def backtest_realistic(basis_df, capital, lot_size, params, alloc_pct=0.5,
                       extra_slippage=0.0, comm_mult=1.0):
    """Бэктест с реальными комиссиями"""
    df = basis_df.copy()
    w = params['window']
    df['ma'] = df['basis_annual'].rolling(w).mean()
    df['std'] = df['basis_annual'].rolling(w).std()
    df['z'] = (df['basis_annual'] - df['ma']) / df['std']
    df = df.dropna(subset=['z'])
    
    if len(df) < w + 10: return None
    
    avg_spot = df['spot'].mean()
    cost = avg_spot * lot_size * 1.15
    n_lots = max(1, int(capital * alloc_pct / cost))
    
    pos = 0; es = ef = 0; esec = None; edate = None
    trades = []
    ez = params['entry_z']; xz = params['exit_z']; sz = params['stop_z']
    
    for i in range(len(df)):
        r = df.iloc[i]
        z = r['z']
        
        if pos == 0:
            if z > ez:
                pos = 1; es = r['spot']; ef = r['futures']
                esec = r['secid']; edate = df.index[i]
            elif z < -ez:
                pos = -1; es = r['spot']; ef = r['futures']
                esec = r['secid']; edate = df.index[i]
        elif pos == 1:
            cc = r['secid'] != esec
            if z <= xz or z > sz or cc:
                # PnL
                pnl_spot = (r['spot'] - es) * lot_size * n_lots
                pnl_fut = (ef - r['futures']) * n_lots
                gross_pnl = pnl_spot + pnl_fut
                
                # Реальные комиссии
                comm = calc_commission(es, r['spot'], ef, r['futures'], lot_size, n_lots) * comm_mult
                
                # Доп. проскальзывание
                slip = (es + r['spot']) * lot_size * n_lots * extra_slippage
                
                net_pnl = gross_pnl - comm - slip
                d = max(1, (df.index[i] - edate).days)
                
                trades.append({
                    'pnl_gross': gross_pnl,
                    'commission': comm,
                    'slippage': slip,
                    'pnl_net': net_pnl,
                    'days': d,
                    'reason': 'stop' if z > sz else ('roll' if cc else 'ok'),
                    'entry_date': edate.date(),
                    'exit_date': df.index[i].date(),
                    'entry_spot': es,
                    'exit_spot': r['spot'],
                    'entry_fut': ef,
                    'exit_fut': r['futures'],
                })
                pos = 0
        elif pos == -1:
            cc = r['secid'] != esec
            if z >= -xz or z < -sz or cc:
                pnl_spot = (es - r['spot']) * lot_size * n_lots
                pnl_fut = (r['futures'] - ef) * n_lots
                gross_pnl = pnl_spot + pnl_fut
                comm = calc_commission(es, r['spot'], ef, r['futures'], lot_size, n_lots) * comm_mult
                slip = (es + r['spot']) * lot_size * n_lots * extra_slippage
                net_pnl = gross_pnl - comm - slip
                d = max(1, (df.index[i] - edate).days)
                trades.append({
                    'pnl_gross': gross_pnl, 'commission': comm, 'slippage': slip,
                    'pnl_net': net_pnl, 'days': d,
                    'reason': 'stop' if z < -sz else ('roll' if cc else 'ok'),
                    'entry_date': edate.date(), 'exit_date': df.index[i].date(),
                    'entry_spot': es, 'exit_spot': r['spot'],
                    'entry_fut': ef, 'exit_fut': r['futures'],
                })
                pos = 0
    
    if not trades: return None
    
    pnls_net = [t['pnl_net'] for t in trades]
    pnls_gross = [t['pnl_gross'] for t in trades]
    total_comm = sum(t['commission'] for t in trades)
    total_slip = sum(t['slippage'] for t in trades)
    total_gross = sum(pnls_gross)
    total_net = sum(pnls_net)
    days = (df.index[-1] - df.index[0]).days
    
    return {
        'trades': trades,
        'n_lots': n_lots,
        'total_gross': total_gross,
        'total_commission': total_comm,
        'total_slippage': total_slip,
        'total_net': total_net,
        'annual_gross': total_gross / capital * 365 / days * 100,
        'annual_net': total_net / capital * 365 / days * 100,
        'win_rate': len([p for p in pnls_net if p > 0]) / len(pnls_net) * 100,
        'sharpe': np.mean(pnls_net) / np.std(pnls_net) * np.sqrt(len(pnls_net)) if np.std(pnls_net) > 0 else 0,
        'n_trades': len(trades),
        'avg_hold': np.mean([t['days'] for t in trades]),
        'max_dd': calc_max_drawdown(pnls_net),
        'days': days,
    }


def calc_max_drawdown(pnls):
    """Максимальная просадка по сделкам"""
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    return min(dd) if len(dd) > 0 else 0


# =============================================
# ТЕСТ 1: СТРЕСС-ТЕСТ (волатильные периоды)
# =============================================

def test_stress():
    print("=" * 70)
    print("🔥 ТЕСТ 1: СТРЕСС-ТЕСТ (ВОЛАТИЛЬНЫЕ ПЕРИОДЫ)")
    print("=" * 70)
    print("  Выделяем периоды с высокой волатильностью рынка")
    print("  и проверяем, как стратегия себя ведёт.\n")
    
    CAPITAL = 10_000_000
    
    for stock, (prefix, lot_size) in INSTRUMENTS.items():
        print(f"\n{'─'*60}")
        print(f"  {stock}")
        print(f"{'─'*60}")
        
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None: continue
        
        roll_days = OPTIMAL_ROLL[stock]
        basis = build_basis(spot, futures, lot_size, roll_days)
        if len(basis) < 80: continue
        
        params = BEST_PARAMS[stock]
        
        # Считаем волатильность спота (20-дневная)
        basis['spot_ret'] = basis['spot'].pct_change()
        basis['vol20'] = basis['spot_ret'].rolling(20).std() * np.sqrt(252) * 100
        
        # Определяем периоды: низкая/средняя/высокая волатильность
        vol_33 = basis['vol20'].quantile(0.33)
        vol_67 = basis['vol20'].quantile(0.67)
        
        regimes = {
            'Низкая vol': basis[basis['vol20'] <= vol_33],
            'Средняя vol': basis[(basis['vol20'] > vol_33) & (basis['vol20'] <= vol_67)],
            'Высокая vol': basis[basis['vol20'] > vol_67],
        }
        
        print(f"  Пороги волатильности: low<{vol_33:.1f}%, mid<{vol_67:.1f}%, high>{vol_67:.1f}%")
        print(f"\n  {'Режим':>15s} | {'Annual':>7s} | {'WR':>5s} | {'Sharpe':>6s} | {'Trades':>6s} | {'Комиссии':>10s} | {'Net PnL':>12s}")
        print(f"  {'-'*80}")
        
        # Полный период для сравнения
        r_full = backtest_realistic(basis, CAPITAL, lot_size, params)
        if r_full:
            print(f"  {'ПОЛНЫЙ ПЕРИОД':>15s} | {r_full['annual_net']:+6.1f}% | {r_full['win_rate']:4.0f}% | "
                  f"{r_full['sharpe']:+6.2f} | {r_full['n_trades']:6d} | "
                  f"{r_full['total_commission']:+10,.0f} | {r_full['total_net']:+12,.0f}")
        
        for regime_name, regime_data in regimes.items():
            if len(regime_data) < 40: continue
            r = backtest_realistic(regime_data, CAPITAL, lot_size, params)
            if r is None: continue
            print(f"  {regime_name:>15s} | {r['annual_net']:+6.1f}% | {r['win_rate']:4.0f}% | "
                  f"{r['sharpe']:+6.2f} | {r['n_trades']:6d} | "
                  f"{r['total_commission']:+10,.0f} | {r['total_net']:+12,.0f}")
        
        # Худший месяц
        if r_full:
            monthly = defaultdict(float)
            for t in r_full['trades']:
                m = str(t['exit_date'])[:7]
                monthly[m] += t['pnl_net']
            worst = min(monthly.items(), key=lambda x: x[1])
            best_ = max(monthly.items(), key=lambda x: x[1])
            print(f"\n  Худший месяц: {worst[0]} = {worst[1]:+,.0f} ₽")
            print(f"  Лучший месяц: {best_[0]} = {best_[1]:+,.0f} ₽")
            print(f"  Max drawdown: {r_full['max_dd']:+,.0f} ₽")


# =============================================
# ТЕСТ 2: MONTE CARLO (1000 перестановок)
# =============================================

def test_monte_carlo():
    print(f"\n\n{'=' * 70}")
    print("🎲 ТЕСТ 2: MONTE CARLO (1000 ПЕРЕСТАНОВОК)")
    print("=" * 70)
    print("  Перемешиваем порядок сделок 1000 раз.")
    print("  Если медиана ≈ реальному результату → не повезло, а стратегия.\n")
    
    CAPITAL = 10_000_000
    N_SIMS = 1000
    
    for stock, (prefix, lot_size) in INSTRUMENTS.items():
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None: continue
        
        roll_days = OPTIMAL_ROLL[stock]
        basis = build_basis(spot, futures, lot_size, roll_days)
        if len(basis) < 80: continue
        
        params = BEST_PARAMS[stock]
        r = backtest_realistic(basis, CAPITAL, lot_size, params)
        if r is None or r['n_trades'] < 5: continue
        
        trade_pnls = [t['pnl_net'] for t in r['trades']]
        real_total = sum(trade_pnls)
        real_annual = r['annual_net']
        
        # Monte Carlo: перемешиваем порядок сделок
        mc_totals = []
        mc_max_dd = []
        
        np.random.seed(42)
        for _ in range(N_SIMS):
            shuffled = np.random.permutation(trade_pnls)
            mc_totals.append(sum(shuffled))
            
            # Max drawdown для каждой перестановки
            eq = np.cumsum(shuffled)
            peak = np.maximum.accumulate(eq)
            dd = eq - peak
            mc_max_dd.append(min(dd))
        
        mc_totals = np.array(mc_totals)
        mc_max_dd = np.array(mc_max_dd)
        
        pct_5 = np.percentile(mc_totals, 5)
        pct_50 = np.percentile(mc_totals, 50)
        pct_95 = np.percentile(mc_totals, 95)
        
        dd_5 = np.percentile(mc_max_dd, 5)
        dd_50 = np.percentile(mc_max_dd, 50)
        dd_95 = np.percentile(mc_max_dd, 95)
        
        # Вероятность прибыли
        prob_profit = np.mean(mc_totals > 0) * 100
        
        # Вероятность > 15% годовых
        threshold = CAPITAL * 0.15 * r['days'] / 365
        prob_15 = np.mean(mc_totals > threshold) * 100
        
        print(f"\n  {stock}:")
        print(f"    Реальный PnL: {real_total:+12,.0f} ₽ ({real_annual:+.1f}%/год)")
        print(f"    MC P5:        {pct_5:+12,.0f} ₽ ({pct_5/CAPITAL*365/r['days']*100:+.1f}%/год)")
        print(f"    MC Медиана:   {pct_50:+12,.0f} ₽ ({pct_50/CAPITAL*365/r['days']*100:+.1f}%/год)")
        print(f"    MC P95:       {pct_95:+12,.0f} ₽ ({pct_95/CAPITAL*365/r['days']*100:+.1f}%/год)")
        print(f"    Prob(profit): {prob_profit:.1f}%")
        print(f"    Prob(>15%/г): {prob_15:.1f}%")
        print(f"    Max DD med:   {dd_50:+,.0f} ₽ | worst 5%: {dd_5:+,.0f} ₽")


# =============================================
# ТЕСТ 3: SENSITIVITY ANALYSIS (±20%)
# =============================================

def test_sensitivity():
    print(f"\n\n{'=' * 70}")
    print("📐 ТЕСТ 3: SENSITIVITY ANALYSIS (ПАРАМЕТРЫ ±20%)")
    print("=" * 70)
    print("  Двигаем каждый параметр на ±20%. Стратегия устойчива,")
    print("  если результат не падает катастрофически.\n")
    
    CAPITAL = 10_000_000
    
    for stock, (prefix, lot_size) in INSTRUMENTS.items():
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None: continue
        
        roll_days = OPTIMAL_ROLL[stock]
        basis = build_basis(spot, futures, lot_size, roll_days)
        if len(basis) < 80: continue
        
        base_params = BEST_PARAMS[stock].copy()
        
        # Базовый результат
        r_base = backtest_realistic(basis, CAPITAL, lot_size, base_params)
        if r_base is None: continue
        
        print(f"\n  {stock} — Базовый: {r_base['annual_net']:+.1f}%/год | Sharpe={r_base['sharpe']:.2f}")
        print(f"  {'Параметр':>12s} | {'Значение':>8s} | {'Annual':>7s} | {'Δ':>6s} | {'Sharpe':>6s} | {'WR':>5s} | {'Trades':>6s} | Стабильность")
        print(f"  {'-'*85}")
        
        # Для каждого параметра
        param_ranges = {
            'window': [int(base_params['window'] * 0.8), base_params['window'], int(base_params['window'] * 1.2)],
            'entry_z': [round(base_params['entry_z'] * 0.8, 2), base_params['entry_z'], round(base_params['entry_z'] * 1.2, 2)],
            'exit_z': [round(base_params['exit_z'] - 0.3, 2), base_params['exit_z'], round(base_params['exit_z'] + 0.3, 2)],
            'stop_z': [round(base_params['stop_z'] * 0.8, 2), base_params['stop_z'], round(base_params['stop_z'] * 1.2, 2)],
        }
        
        for param_name, values in param_ranges.items():
            for val in values:
                test_params = base_params.copy()
                test_params[param_name] = max(1, val) if param_name == 'window' else val
                
                r = backtest_realistic(basis, CAPITAL, lot_size, test_params)
                if r is None:
                    print(f"  {param_name:>12s} | {val:>8.2f} | {'N/A':>7s} |")
                    continue
                
                delta = r['annual_net'] - r_base['annual_net']
                is_base = "◄ BASE" if val == base_params[param_name] else ""
                
                # Стабильность
                if abs(delta) < 3:
                    stability = "✅ стабильно"
                elif abs(delta) < 6:
                    stability = "⚠️ умеренно"
                else:
                    stability = "❌ чувствительно"
                
                if is_base:
                    stability = "◄ BASE"
                
                print(f"  {param_name:>12s} | {val:>8.2f} | {r['annual_net']:+6.1f}% | {delta:+5.1f}% | "
                      f"{r['sharpe']:+6.2f} | {r['win_rate']:4.0f}% | {r['n_trades']:6d} | {stability}")


# =============================================
# ТЕСТ 4: РЕАЛЬНЫЕ КОМИССИИ + ПРОСКАЛЬЗЫВАНИЯ
# =============================================

def test_real_costs():
    print(f"\n\n{'=' * 70}")
    print("💸 ТЕСТ 4: РЕАЛЬНЫЕ КОМИССИИ И ПРОСКАЛЬЗЫВАНИЯ")
    print("=" * 70)
    print(f"  Акция: {STOCK_COMMISSION*100:.3f}% (0.04%) на сторону")
    print(f"  Фьючерс: {FUTURES_COMMISSION*100:.4f}% (0.004%) на сторону")
    print(f"  Базовое проскальзывание: {SLIPPAGE_PCT*100:.3f}%")
    print(f"  Тестируем разные уровни проскальзывания.\n")
    
    CAPITAL = 10_000_000
    
    # Сценарии проскальзывания
    scenarios = [
        ('Идеальный (0 slip)', 0, 1.0),
        ('Базовый (0.01% slip)', 0, 1.0),  # уже включён в calc_commission
        ('Агрессивный (0.03% slip)', 0.0002, 1.0),
        ('Худший (0.05% slip)', 0.0004, 1.0),
        ('Двойная комиссия', 0, 2.0),
    ]
    
    for stock, (prefix, lot_size) in INSTRUMENTS.items():
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None: continue
        
        roll_days = OPTIMAL_ROLL[stock]
        basis = build_basis(spot, futures, lot_size, roll_days)
        if len(basis) < 80: continue
        
        params = BEST_PARAMS[stock]
        
        print(f"\n  {stock}:")
        print(f"  {'Сценарий':>25s} | {'Gross':>7s} | {'Комиссии':>10s} | {'Slip':>10s} | {'Net':>7s} | {'After tax':>7s} | {'Net PnL':>12s}")
        print(f"  {'-'*95}")
        
        for name, extra_slip, comm_mult in scenarios:
            r = backtest_realistic(basis, CAPITAL, lot_size, params,
                                  extra_slippage=extra_slip, comm_mult=comm_mult)
            if r is None: continue
            
            after_tax = r['annual_net'] * 0.87
            
            marker = ""
            if after_tax >= 20: marker = " 🏆"
            elif after_tax >= 15: marker = " ✅"
            elif after_tax >= 10: marker = " ⚠️"
            else: marker = " ❌"
            
            print(f"  {name:>25s} | {r['annual_gross']:+6.1f}% | {r['total_commission']:+10,.0f} | "
                  f"{r['total_slippage']:+10,.0f} | {r['annual_net']:+6.1f}% | {after_tax:+6.1f}% | "
                  f"{r['total_net']:+12,.0f}{marker}")
    
    # Итоговая таблица портфеля
    print(f"\n\n{'='*70}")
    print("💰 ИТОГОВЫЙ ПОРТФЕЛЬ С РЕАЛЬНЫМИ РАСХОДАМИ")
    print(f"{'='*70}")
    print(f"  Сценарий: базовые комиссии (0.04% акция / 0.004% фьюч / 0.01% slip)")
    
    total_gross = 0
    total_comm = 0
    total_slip = 0
    total_net = 0
    
    allocations = {
        'ROSN': 3_000_000,
        'TATN': 2_500_000,
        'GAZP': 2_500_000,
        'SBER': 1_500_000,
        'ALRS': 500_000,
    }
    
    print(f"\n  {'Инструмент':>12s} | {'Капитал':>10s} | {'Gross':>7s} | {'Комиссии':>10s} | {'Net':>7s} | {'After tax':>7s} | {'PnL net':>12s}")
    print(f"  {'-'*85}")
    
    for stock, alloc in allocations.items():
        prefix, lot_size = INSTRUMENTS[stock]
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None: continue
        
        roll_days = OPTIMAL_ROLL[stock]
        basis = build_basis(spot, futures, lot_size, roll_days)
        if len(basis) < 80: continue
        
        params = BEST_PARAMS[stock]
        r = backtest_realistic(basis, alloc, lot_size, params)
        if r is None: continue
        
        after_tax = r['annual_net'] * 0.87
        total_gross += r['total_gross']
        total_comm += r['total_commission']
        total_slip += r['total_slippage']
        total_net += r['total_net']
        
        print(f"  {stock:>12s} | {alloc/1e6:>8.1f}M | {r['annual_gross']:+6.1f}% | "
              f"{r['total_commission']:+10,.0f} | {r['annual_net']:+6.1f}% | {after_tax:+6.1f}% | "
              f"{r['total_net']:+12,.0f}")
    
    total_capital = sum(allocations.values())
    total_days = 700  # approx
    port_gross = total_gross / total_capital * 365 / total_days * 100
    port_net = total_net / total_capital * 365 / total_days * 100
    port_tax = port_net * 0.87
    
    print(f"  {'-'*85}")
    print(f"  {'ИТОГО':>12s} | {total_capital/1e6:>8.1f}M | {port_gross:+6.1f}% | "
          f"{total_comm:+10,.0f} | {port_net:+6.1f}% | {port_tax:+6.1f}% | "
          f"{total_net:+12,.0f}")
    print(f"\n  Общие комиссии за 2 года: {total_comm:,.0f} ₽ ({total_comm/total_capital*100:.2f}% от капитала)")
    print(f"  Общее проскальзывание: {total_slip:,.0f} ₽")
    print(f"  НДФЛ (13%): {total_net * 0.13:,.0f} ₽")
    print(f"  Чистая прибыль: {total_net * 0.87:,.0f} ₽ ({port_tax:.1f}%/год)")


# =============================================
# MAIN
# =============================================

if __name__ == '__main__':
    print("🚀 ПОЛНАЯ ВАЛИДАЦИЯ АРБИТРАЖНОЙ СТРАТЕГИИ")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Комиссия акция: 0.04% | Фьючерс: 0.004% | Slip: 0.01%\n")
    
    test_stress()
    test_monte_carlo()
    test_sensitivity()
    test_real_costs()
    
    print(f"\n\n{'='*70}")
    print("✅ ВАЛИДАЦИЯ ЗАВЕРШЕНА")
    print("="*70)
