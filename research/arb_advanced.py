"""
Продвинутый анализ арбитража:
1. Оптимизация ролла (за N дней до экспирации, плавный переход)
2. Walk-forward (IS/OOS) валидация
3. Внутридневные данные (часовки)
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
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

# Лучшие параметры из предыдущего grid search
BEST_PARAMS = {
    'ROSN': {'window': 15, 'entry_z': 1.0, 'exit_z': 0.0, 'stop_z': 3.5},
    'TATN': {'window': 15, 'entry_z': 1.0, 'exit_z': 0.5, 'stop_z': 3.5},
    'GAZP': {'window': 20, 'entry_z': 1.5, 'exit_z': 0.0, 'stop_z': 3.5},
    'SBER': {'window': 15, 'entry_z': 1.5, 'exit_z': -0.5, 'stop_z': 3.5},
    'ALRS': {'window': 15, 'entry_z': 2.5, 'exit_z': 0.5, 'stop_z': 3.0},
}

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


def load_instrument(stock, fut_prefix, lot_size, interval=24, start='2024-04-01', end='2026-04-02'):
    spot = fetch_candles(stock, 'TQBR', 'stock', 'shares', start=start, end=end, interval=interval)
    if spot.empty:
        return None, None
    
    futures = {}
    for year in [4, 5, 6, 7]:
        for mc in MONTH_CODES:
            secid = f"{fut_prefix}{mc}{year}"
            month = MONTHS[mc]
            full_year = 2020 + year
            expiry = datetime(full_year, month, 15)
            fut = fetch_candles(secid, 'RFUD', 'futures', 'forts', start=start, end=end, interval=interval)
            if not fut.empty and len(fut) > 3:
                futures[secid] = {'data': fut, 'expiry': expiry}
    
    return spot, futures


def build_basis(spot, futures, lot_size, min_days_to_exp=10, max_days_to_exp=120):
    """Строим непрерывный базис с ближайшим контрактом"""
    records = []
    for date in spot.index:
        spot_price = spot.loc[date, 'close']
        best = None
        best_days = 9999
        for secid, info in futures.items():
            dte = (info['expiry'] - date.to_pydatetime()).days
            if dte < min_days_to_exp or dte > max_days_to_exp:
                continue
            if date in info['data'].index:
                if dte < best_days:
                    best_days = dte
                    best = {'secid': secid, 'fut': info['data'].loc[date, 'close'],
                            'dte': dte, 'expiry': info['expiry']}
        if best:
            basis_pct = (best['fut'] / (spot_price * lot_size) - 1) * 100
            basis_annual = basis_pct * 365 / best['dte']
            records.append({
                'date': date, 'spot': spot_price, 'futures': best['fut'],
                'secid': best['secid'], 'dte': best['dte'],
                'basis_pct': basis_pct, 'basis_annual': basis_annual,
                'expiry': best['expiry'],
            })
    return pd.DataFrame(records).set_index('date')


def build_basis_smart_roll(spot, futures, lot_size, roll_days=15):
    """
    Улучшенный ролл: переход на следующий контракт за roll_days до экспирации
    Вместо резкого перехода — берём следующий по дальности контракт заранее
    """
    records = []
    for date in spot.index:
        spot_price = spot.loc[date, 'close']
        
        # Собираем все доступные контракты
        available = []
        for secid, info in futures.items():
            dte = (info['expiry'] - date.to_pydatetime()).days
            if dte < 3 or dte > 200:
                continue
            if date in info['data'].index:
                available.append({
                    'secid': secid, 'fut': info['data'].loc[date, 'close'],
                    'dte': dte, 'expiry': info['expiry'],
                })
        
        if not available:
            continue
        
        available.sort(key=lambda x: x['dte'])
        
        # Если ближайший контракт < roll_days до экспирации — берём следующий
        if available[0]['dte'] < roll_days and len(available) > 1:
            chosen = available[1]
        else:
            chosen = available[0]
        
        basis_pct = (chosen['fut'] / (spot_price * lot_size) - 1) * 100
        basis_annual = basis_pct * 365 / chosen['dte']
        
        records.append({
            'date': date, 'spot': spot_price, 'futures': chosen['fut'],
            'secid': chosen['secid'], 'dte': chosen['dte'],
            'basis_pct': basis_pct, 'basis_annual': basis_annual,
        })
    
    return pd.DataFrame(records).set_index('date')


def backtest(basis_df, capital, lot_size, params, alloc_pct=0.5):
    """Единый бэктестер"""
    df = basis_df.copy()
    w = params['window']
    df['ma'] = df['basis_annual'].rolling(w).mean()
    df['std'] = df['basis_annual'].rolling(w).std()
    df['z'] = (df['basis_annual'] - df['ma']) / df['std']
    df = df.dropna(subset=['z'])
    
    if len(df) < w + 10:
        return None
    
    avg_spot = df['spot'].mean()
    cost = avg_spot * lot_size * 1.15
    n_lots = max(1, int(capital * alloc_pct / cost))
    
    pos = 0; es = ef = 0; esec = None; edate = None
    trades = []
    
    ez = params['entry_z']
    xz = params['exit_z']
    sz = params['stop_z']
    
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
                pnl = (r['spot'] - es) * lot_size + (ef - r['futures'])
                comm = (es + r['spot']) * lot_size * 0.0003 + 4
                d = max(1, (df.index[i] - edate).days)
                trades.append({'pnl': pnl - comm, 'days': d,
                             'reason': 'stop' if z > sz else ('roll' if cc else 'ok')})
                pos = 0
        elif pos == -1:
            cc = r['secid'] != esec
            if z >= -xz or z < -sz or cc:
                pnl = (es - r['spot']) * lot_size + (r['futures'] - ef)
                comm = (es + r['spot']) * lot_size * 0.0003 + 4
                d = max(1, (df.index[i] - edate).days)
                trades.append({'pnl': pnl - comm, 'days': d,
                             'reason': 'stop' if z < -sz else ('roll' if cc else 'ok')})
                pos = 0
    
    if not trades:
        return None
    
    pnls = [t['pnl'] * n_lots for t in trades]
    total = sum(pnls)
    days = (df.index[-1] - df.index[0]).days
    if days <= 0: return None
    
    wins = len([p for p in pnls if p > 0])
    roll_losses = sum(t['pnl'] * n_lots for t in trades if t['reason'] == 'roll' and t['pnl'] < 0)
    
    return {
        'total_pnl': total,
        'annual': total / capital * 365 / days * 100,
        'win_rate': wins / len(trades) * 100,
        'sharpe': np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if np.std(pnls) > 0 else 0,
        'n_trades': len(trades),
        'avg_hold': np.mean([t['days'] for t in trades]),
        'roll_losses': roll_losses,
        'n_lots': n_lots,
        'trades': trades,
        'days': days,
    }


# =============================================
# ЧАСТЬ 1: ОПТИМИЗАЦИЯ РОЛЛА
# =============================================

def test_roll_optimization():
    print("=" * 70)
    print("🔄 ЧАСТЬ 1: ОПТИМИЗАЦИЯ РОЛЛА")
    print("=" * 70)
    
    CAPITAL = 10_000_000
    
    for stock, (prefix, lot_size) in INSTRUMENTS.items():
        print(f"\n{'─'*50}")
        print(f"  {stock}")
        print(f"{'─'*50}")
        
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None:
            continue
        
        params = BEST_PARAMS[stock]
        
        # Тест разных дней ролла
        results = []
        for roll_days in [5, 10, 15, 20, 25, 30]:
            basis = build_basis_smart_roll(spot, futures, lot_size, roll_days=roll_days)
            if len(basis) < 80:
                continue
            
            r = backtest(basis, CAPITAL, lot_size, params)
            if r is None:
                continue
            
            results.append({
                'roll_days': roll_days,
                **r,
            })
        
        # Также тест без оптимизации (old method)
        basis_old = build_basis(spot, futures, lot_size, min_days_to_exp=10)
        r_old = backtest(basis_old, CAPITAL, lot_size, params)
        
        if r_old:
            print(f"  Без оптимизации (old): {r_old['annual']:+.1f}%/год | "
                  f"WR={r_old['win_rate']:.0f}% | roll losses={r_old['roll_losses']:+,.0f} | "
                  f"trades={r_old['n_trades']}")
        
        print(f"\n  {'Roll days':>9s} | {'Annual':>7s} | {'WR':>5s} | {'Sharpe':>6s} | {'Roll loss':>12s} | {'Trades':>6s} | {'PnL':>12s}")
        print(f"  {'-'*75}")
        
        for r in results:
            marker = " 🏆" if r['roll_losses'] == 0 or r['annual'] == max(x['annual'] for x in results) else ""
            print(f"  {r['roll_days']:9d} | {r['annual']:+6.1f}% | {r['win_rate']:4.0f}% | "
                  f"{r['sharpe']:+6.2f} | {r['roll_losses']:+12,.0f} | {r['n_trades']:6d} | "
                  f"{r['total_pnl']:+12,.0f}{marker}")
    
    return


# =============================================
# ЧАСТЬ 2: WALK-FORWARD ВАЛИДАЦИЯ
# =============================================

def test_walk_forward():
    print(f"\n\n{'=' * 70}")
    print("📊 ЧАСТЬ 2: WALK-FORWARD ВАЛИДАЦИЯ")
    print("=" * 70)
    print("  Метод: 4 окна по ~6 месяцев. IS=train, OOS=test.")
    print("  Параметры оптимизируются на IS, применяются на OOS.\n")
    
    CAPITAL = 10_000_000
    
    # Периоды: 4 окна по ~6 месяцев
    windows = [
        ('2024-04-01', '2024-10-01', '2024-10-01', '2025-04-01'),  # H1 2024 → H2 2024
        ('2024-07-01', '2025-01-01', '2025-01-01', '2025-07-01'),  # H2 2024 → H1 2025
        ('2024-10-01', '2025-04-01', '2025-04-01', '2025-10-01'),  # H1 2025 → H2 2025
        ('2025-01-01', '2025-07-01', '2025-07-01', '2026-04-02'),  # H2 2025 → H1 2026
    ]
    
    for stock, (prefix, lot_size) in INSTRUMENTS.items():
        print(f"\n{'─'*60}")
        print(f"  {stock} — WALK-FORWARD")
        print(f"{'─'*60}")
        
        # Загружаем все данные
        spot, futures = load_instrument(stock, prefix, lot_size)
        if spot is None:
            continue
        
        wf_results = []
        
        for i, (is_start, is_end, oos_start, oos_end) in enumerate(windows):
            # IS: оптимизируем параметры
            is_start_dt = pd.Timestamp(is_start)
            is_end_dt = pd.Timestamp(is_end)
            oos_start_dt = pd.Timestamp(oos_start)
            oos_end_dt = pd.Timestamp(oos_end)
            
            # Строим базис для IS и OOS
            basis_full = build_basis_smart_roll(spot, futures, lot_size, roll_days=15)
            
            basis_is = basis_full[(basis_full.index >= is_start_dt) & (basis_full.index < is_end_dt)]
            basis_oos = basis_full[(basis_full.index >= oos_start_dt) & (basis_full.index < oos_end_dt)]
            
            if len(basis_is) < 50 or len(basis_oos) < 30:
                continue
            
            # Grid search на IS
            best_is = None
            best_sharpe = -999
            
            for w in [10, 15, 20]:
                for ez in [1.0, 1.5, 2.0]:
                    for xz in [-0.5, 0.0, 0.5]:
                        p = {'window': w, 'entry_z': ez, 'exit_z': xz, 'stop_z': 3.5}
                        r = backtest(basis_is, CAPITAL, lot_size, p)
                        if r and r['n_trades'] >= 3 and r['sharpe'] > best_sharpe:
                            best_sharpe = r['sharpe']
                            best_is = {'params': p, 'result': r}
            
            if best_is is None:
                continue
            
            # Применяем лучшие IS параметры на OOS
            r_oos = backtest(basis_oos, CAPITAL, lot_size, best_is['params'])
            
            if r_oos is None:
                continue
            
            is_r = best_is['result']
            p = best_is['params']
            
            wf_results.append({
                'window': i + 1,
                'is_period': f"{is_start[:7]}→{is_end[:7]}",
                'oos_period': f"{oos_start[:7]}→{oos_end[:7]}",
                'is_annual': is_r['annual'],
                'oos_annual': r_oos['annual'],
                'is_sharpe': is_r['sharpe'],
                'oos_sharpe': r_oos['sharpe'],
                'oos_wr': r_oos['win_rate'],
                'oos_trades': r_oos['n_trades'],
                'params': p,
            })
        
        if not wf_results:
            print(f"  ❌ Недостаточно данных")
            continue
        
        print(f"\n  {'#':>2s} | {'IS период':>17s} | {'OOS период':>17s} | {'IS ann':>7s} | {'OOS ann':>7s} | {'OOS WR':>6s} | {'OOS Sh':>6s} | {'OOS tr':>6s} | Параметры")
        print(f"  {'-'*110}")
        
        for r in wf_results:
            p = r['params']
            oos_ok = "✅" if r['oos_annual'] > 0 else "❌"
            print(f"  {r['window']:2d} | {r['is_period']:>17s} | {r['oos_period']:>17s} | "
                  f"{r['is_annual']:+6.1f}% | {r['oos_annual']:+6.1f}% | {r['oos_wr']:5.0f}% | "
                  f"{r['oos_sharpe']:+6.2f} | {r['oos_trades']:6d} | "
                  f"w={p['window']},e={p['entry_z']},x={p['exit_z']} {oos_ok}")
        
        # Средний OOS
        avg_oos = np.mean([r['oos_annual'] for r in wf_results])
        avg_oos_wr = np.mean([r['oos_wr'] for r in wf_results])
        oos_positive = len([r for r in wf_results if r['oos_annual'] > 0])
        
        print(f"\n  📊 Средний OOS: {avg_oos:+.1f}%/год | WR={avg_oos_wr:.0f}% | "
              f"Прибыльных окон: {oos_positive}/{len(wf_results)}")


# =============================================
# ЧАСТЬ 3: ВНУТРИДНЕВНЫЕ ДАННЫЕ (ЧАСОВКИ)
# =============================================

def test_intraday():
    print(f"\n\n{'=' * 70}")
    print("⏰ ЧАСТЬ 3: ВНУТРИДНЕВНЫЕ ДАННЫЕ (ЧАСОВКИ)")
    print("=" * 70)
    print("  MOEX ISS даёт часовые свечи (interval=60) бесплатно.")
    print("  Тестируем: больше сделок → больше прибыли?\n")
    
    CAPITAL = 10_000_000
    
    # ISS API отдаёт внутридневные за ~6 месяцев. Берём последние данные.
    start = '2025-10-01'
    end = '2026-04-02'
    
    for stock in ['ROSN', 'TATN', 'GAZP']:
        prefix, lot_size = INSTRUMENTS[stock]
        
        print(f"\n{'─'*60}")
        print(f"  {stock} — ЧАСОВЫЕ СВЕЧИ ({start} → {end})")
        print(f"{'─'*60}")
        
        # Загружаем часовки
        print(f"  Загрузка спот {stock} (часовки)...")
        spot_h = fetch_candles(stock, 'TQBR', 'stock', 'shares',
                               start=start, end=end, interval=60)
        
        if spot_h.empty:
            print(f"  ❌ Нет часовых данных для спота")
            continue
        
        print(f"  Спот: {len(spot_h)} свечей")
        
        # Фьючерсы (часовки)
        futures_h = {}
        for year in [5, 6, 7]:
            for mc in MONTH_CODES:
                secid = f"{prefix}{mc}{year}"
                month = MONTHS[mc]
                full_year = 2020 + year
                expiry = datetime(full_year, month, 15)
                fut = fetch_candles(secid, 'RFUD', 'futures', 'forts',
                                   start=start, end=end, interval=60)
                if not fut.empty and len(fut) > 10:
                    futures_h[secid] = {'data': fut, 'expiry': expiry}
                    print(f"  {secid}: {len(fut)} свечей")
        
        if not futures_h:
            print(f"  ❌ Нет часовых фьючерсов")
            continue
        
        # Строим часовой базис
        basis_h = build_basis_smart_roll(spot_h, futures_h, lot_size, roll_days=15)
        print(f"  Часовой базис: {len(basis_h)} свечей")
        
        if len(basis_h) < 100:
            print(f"  ⚠️ Мало данных")
            continue
        
        # Также загружаем дневные для сравнения на том же периоде
        spot_d = fetch_candles(stock, 'TQBR', 'stock', 'shares',
                               start=start, end=end, interval=24)
        futures_d = {}
        for year in [5, 6, 7]:
            for mc in MONTH_CODES:
                secid = f"{prefix}{mc}{year}"
                month = MONTHS[mc]
                full_year = 2020 + year
                expiry = datetime(full_year, month, 15)
                fut = fetch_candles(secid, 'RFUD', 'futures', 'forts',
                                   start=start, end=end, interval=24)
                if not fut.empty and len(fut) > 3:
                    futures_d[secid] = {'data': fut, 'expiry': expiry}
        
        basis_d = build_basis_smart_roll(spot_d, futures_d, lot_size, roll_days=15)
        
        # Grid search на часовках
        print(f"\n  Grid search на часовках...")
        best_h = None
        best_sharpe_h = -999
        all_h = []
        
        for w in [10, 20, 40, 60, 80]:
            for ez in [1.0, 1.5, 2.0, 2.5]:
                for xz in [-0.5, 0.0, 0.5]:
                    p = {'window': w, 'entry_z': ez, 'exit_z': xz, 'stop_z': 3.5}
                    r = backtest(basis_h, CAPITAL, lot_size, p)
                    if r and r['n_trades'] >= 5:
                        all_h.append({'params': p, **r})
                        if r['sharpe'] > best_sharpe_h:
                            best_sharpe_h = r['sharpe']
                            best_h = r
                            best_h['params'] = p
        
        # Дневные с лучшими параметрами
        params_d = BEST_PARAMS[stock]
        r_d = backtest(basis_d, CAPITAL, lot_size, params_d) if len(basis_d) > 30 else None
        
        print(f"\n  СРАВНЕНИЕ: ДНЕВНЫЕ vs ЧАСОВЫЕ ({start} → {end})")
        print(f"  {'':>12s} | {'Annual':>7s} | {'Sharpe':>6s} | {'WR':>5s} | {'Trades':>6s} | {'Hold':>5s} | {'PnL':>12s}")
        print(f"  {'-'*65}")
        
        if r_d:
            print(f"  {'Дневные':>12s} | {r_d['annual']:+6.1f}% | {r_d['sharpe']:+6.2f} | "
                  f"{r_d['win_rate']:4.0f}% | {r_d['n_trades']:6d} | {r_d['avg_hold']:4.0f}д | "
                  f"{r_d['total_pnl']:+12,.0f}")
        
        if best_h:
            p = best_h['params']
            print(f"  {'Часовые':>12s} | {best_h['annual']:+6.1f}% | {best_h['sharpe']:+6.2f} | "
                  f"{best_h['win_rate']:4.0f}% | {best_h['n_trades']:6d} | {best_h['avg_hold']:4.0f}ч | "
                  f"{best_h['total_pnl']:+12,.0f}")
            print(f"  Лучшие часовые: w={p['window']}, entry={p['entry_z']}, exit={p['exit_z']}")
        
        # Топ-5 часовых
        if all_h:
            all_h.sort(key=lambda x: -x['sharpe'])
            print(f"\n  ТОП-5 ЧАСОВЫХ ПО SHARPE:")
            print(f"  {'Window':>6s} | {'Entry':>5s} | {'Exit':>5s} | {'Annual':>7s} | {'Sharpe':>6s} | {'WR':>5s} | {'Trades':>6s}")
            print(f"  {'-'*55}")
            for r in all_h[:5]:
                p = r['params']
                print(f"  {p['window']:6d} | {p['entry_z']:5.1f} | {p['exit_z']:5.1f} | "
                      f"{r['annual']:+6.1f}% | {r['sharpe']:+6.2f} | {r['win_rate']:4.0f}% | {r['n_trades']:6d}")
            
            # Топ по annual
            all_h_annual = sorted(all_h, key=lambda x: -x['annual'])
            print(f"\n  ТОП-5 ЧАСОВЫХ ПО ANNUAL:")
            print(f"  {'Window':>6s} | {'Entry':>5s} | {'Exit':>5s} | {'Annual':>7s} | {'Sharpe':>6s} | {'WR':>5s} | {'Trades':>6s}")
            print(f"  {'-'*55}")
            for r in all_h_annual[:5]:
                p = r['params']
                print(f"  {p['window']:6d} | {p['entry_z']:5.1f} | {p['exit_z']:5.1f} | "
                      f"{r['annual']:+6.1f}% | {r['sharpe']:+6.2f} | {r['win_rate']:4.0f}% | {r['n_trades']:6d}")


# =============================================
# MAIN
# =============================================

if __name__ == '__main__':
    print("🚀 ПРОДВИНУТЫЙ АНАЛИЗ АРБИТРАЖА")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    
    test_roll_optimization()
    test_walk_forward()
    test_intraday()
    
    print(f"\n\n{'='*70}")
    print("✅ ВСЕ ТЕСТЫ ЗАВЕРШЕНЫ")
    print("="*70)
