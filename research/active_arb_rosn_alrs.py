"""
Активный арбитраж ROSN и ALRS — детальный бэктест
Аналогично GAZP: торговля z-score базиса
+ Grid search параметров для каждого инструмента
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


def build_continuous_basis(spot, futures, lot_size):
    records = []
    for date in spot.index:
        spot_price = spot.loc[date, 'close']
        best_fut = None
        best_days = 9999
        for secid, info in futures.items():
            days_to_exp = (info['expiry'] - date.to_pydatetime()).days
            if days_to_exp < 10 or days_to_exp > 120:
                continue
            if date in info['data'].index:
                fut_price = info['data'].loc[date, 'close']
                if days_to_exp < best_days:
                    best_days = days_to_exp
                    best_fut = {
                        'secid': secid, 'fut_price': fut_price,
                        'days_to_exp': days_to_exp, 'expiry': info['expiry'],
                    }
        if best_fut:
            basis_pct = (best_fut['fut_price'] / (spot_price * lot_size) - 1) * 100
            basis_annual = basis_pct * 365 / best_fut['days_to_exp']
            records.append({
                'date': date, 'spot': spot_price,
                'futures': best_fut['fut_price'], 'secid': best_fut['secid'],
                'days_to_exp': best_fut['days_to_exp'],
                'basis_pct': basis_pct, 'basis_annual': basis_annual,
            })
    return pd.DataFrame(records).set_index('date')


def load_instrument(stock, fut_prefix, lot_size):
    print(f"\n📥 Загрузка {stock}...")
    spot = fetch_candles(stock, 'TQBR', 'stock', 'shares')
    if spot.empty:
        print(f"  ❌ Нет спот-данных")
        return None, None
    print(f"  Спот: {len(spot)} дней")
    
    futures = {}
    for year in [4, 5, 6, 7]:
        for mc in MONTH_CODES:
            secid = f"{fut_prefix}{mc}{year}"
            month = MONTHS[mc]
            full_year = 2020 + year
            expiry = datetime(full_year, month, 15)
            fut = fetch_candles(secid, 'RFUD', 'futures', 'forts')
            if not fut.empty and len(fut) > 5:
                futures[secid] = {'data': fut, 'expiry': expiry}
                print(f"  {secid}: {len(fut)} дней")
    
    if not futures:
        return None, None
    
    basis_df = build_continuous_basis(spot, futures, lot_size)
    print(f"  Непрерывный базис: {len(basis_df)} дней")
    print(f"  Годовой базис: {basis_df['basis_annual'].mean():.1f}% ± {basis_df['basis_annual'].std():.1f}%")
    return basis_df, spot


def backtest_active(basis_df, capital, lot_size, entry_z=1.5, exit_z=0.0, stop_z=3.5, 
                    short_entry_z=-1.5, short_exit_z=0.0, window=20, alloc_pct=0.5, label=""):
    """Бэктест активной торговли базисом с заданными параметрами"""
    df = basis_df.copy()
    df['basis_ma'] = df['basis_annual'].rolling(window).mean()
    df['basis_std'] = df['basis_annual'].rolling(window).std()
    df['z'] = (df['basis_annual'] - df['basis_ma']) / df['basis_std']
    df = df.dropna(subset=['z'])
    
    if len(df) < 30:
        return None
    
    avg_spot = df['spot'].mean()
    cost_pair = avg_spot * lot_size * 1.15
    n_lots = max(1, int(capital * alloc_pct / cost_pair))
    
    position = 0
    entry_spot = entry_fut = 0
    entry_secid = None
    trades = []
    
    for i in range(len(df)):
        row = df.iloc[i]
        z = row['z']
        
        if position == 0:
            if z > entry_z:
                position = 1
                entry_spot, entry_fut = row['spot'], row['futures']
                entry_secid = row['secid']
                entry_date = df.index[i]
            elif z < short_entry_z:
                position = -1
                entry_spot, entry_fut = row['spot'], row['futures']
                entry_secid = row['secid']
                entry_date = df.index[i]
        elif position == 1:
            contract_changed = row['secid'] != entry_secid
            if z <= exit_z or z > stop_z or contract_changed:
                pnl = (row['spot'] - entry_spot) * lot_size + (entry_fut - row['futures'])
                comm = (entry_spot + row['spot']) * lot_size * 0.0003 + 4
                days = max(1, (df.index[i] - entry_date).days)
                trades.append({'type': 'L', 'pnl': pnl - comm, 'days': days,
                             'reason': 'stop' if z > stop_z else ('roll' if contract_changed else 'ok'),
                             'entry': entry_date.date(), 'exit': df.index[i].date()})
                position = 0
        elif position == -1:
            contract_changed = row['secid'] != entry_secid
            if z >= short_exit_z or z < -stop_z or contract_changed:
                pnl = (entry_spot - row['spot']) * lot_size + (row['futures'] - entry_fut)
                comm = (entry_spot + row['spot']) * lot_size * 0.0003 + 4
                days = max(1, (df.index[i] - entry_date).days)
                trades.append({'type': 'S', 'pnl': pnl - comm, 'days': days,
                             'reason': 'stop' if z < -stop_z else ('roll' if contract_changed else 'ok'),
                             'entry': entry_date.date(), 'exit': df.index[i].date()})
                position = 0
    
    if not trades:
        return None
    
    pnls = [t['pnl'] * n_lots for t in trades]
    total = sum(pnls)
    wins = len([p for p in pnls if p > 0])
    total_days = (df.index[-1] - df.index[0]).days
    annual = total / capital * 365 / total_days * 100 if total_days > 0 else 0
    wr = wins / len(trades) * 100
    sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if np.std(pnls) > 0 else 0
    avg_hold = np.mean([t['days'] for t in trades])
    
    return {
        'label': label,
        'trades': trades,
        'n_lots': n_lots,
        'total_pnl': total,
        'annual': annual,
        'win_rate': wr,
        'sharpe': sharpe,
        'n_trades': len(trades),
        'avg_hold': avg_hold,
        'wins': wins,
        'params': {'entry_z': entry_z, 'exit_z': exit_z, 'stop_z': stop_z,
                   'short_entry_z': short_entry_z, 'short_exit_z': short_exit_z, 'window': window},
    }


def grid_search(basis_df, capital, lot_size, stock_name):
    """Grid search по параметрам"""
    print(f"\n{'='*70}")
    print(f"  GRID SEARCH — {stock_name}")
    print(f"{'='*70}")
    
    results = []
    
    for window in [15, 20, 30]:
        for entry_z in [1.0, 1.5, 2.0, 2.5]:
            for exit_z in [-0.5, 0.0, 0.5]:
                for stop_z in [3.0, 3.5, 4.0]:
                    r = backtest_active(basis_df, capital, lot_size,
                                       entry_z=entry_z, exit_z=exit_z, stop_z=stop_z,
                                       short_entry_z=-entry_z, short_exit_z=-exit_z,
                                       window=window, alloc_pct=0.5)
                    if r and r['n_trades'] >= 5:
                        results.append(r)
    
    if not results:
        print("  ❌ Нет результатов")
        return None
    
    # Сортируем по Sharpe
    results.sort(key=lambda x: -x['sharpe'])
    
    print(f"\n  Протестировано комбинаций: {len(results)}")
    print(f"\n  ТОП-10 ПО SHARPE:")
    print(f"  {'#':>2s} | {'Window':>6s} | {'Entry':>5s} | {'Exit':>5s} | {'Stop':>4s} | "
          f"{'Trades':>6s} | {'WR':>5s} | {'Sharpe':>6s} | {'Annual':>7s} | {'PnL':>12s}")
    print(f"  {'-'*85}")
    
    for i, r in enumerate(results[:10]):
        p = r['params']
        print(f"  {i+1:2d} | {p['window']:6d} | {p['entry_z']:5.1f} | {p['exit_z']:5.1f} | {p['stop_z']:4.1f} | "
              f"{r['n_trades']:6d} | {r['win_rate']:4.0f}% | {r['sharpe']:+6.2f} | {r['annual']:+6.1f}% | {r['total_pnl']:+12,.0f}")
    
    # ТОП по annual return
    results_annual = sorted(results, key=lambda x: -x['annual'])
    print(f"\n  ТОП-10 ПО ГОДОВОЙ ДОХОДНОСТИ:")
    print(f"  {'#':>2s} | {'Window':>6s} | {'Entry':>5s} | {'Exit':>5s} | {'Stop':>4s} | "
          f"{'Trades':>6s} | {'WR':>5s} | {'Sharpe':>6s} | {'Annual':>7s} | {'PnL':>12s}")
    print(f"  {'-'*85}")
    
    for i, r in enumerate(results_annual[:10]):
        p = r['params']
        print(f"  {i+1:2d} | {p['window']:6d} | {p['entry_z']:5.1f} | {p['exit_z']:5.1f} | {p['stop_z']:4.1f} | "
              f"{r['n_trades']:6d} | {r['win_rate']:4.0f}% | {r['sharpe']:+6.2f} | {r['annual']:+6.1f}% | {r['total_pnl']:+12,.0f}")
    
    return results[0]  # Best by Sharpe


def detailed_report(basis_df, capital, lot_size, stock_name, best_result):
    """Детальный отчёт лучшей стратегии"""
    print(f"\n{'='*70}")
    print(f"  📊 ДЕТАЛЬНЫЙ ОТЧЁТ — {stock_name}")
    print(f"{'='*70}")
    
    p = best_result['params']
    print(f"  Параметры: window={p['window']}, entry_z={p['entry_z']}, "
          f"exit_z={p['exit_z']}, stop_z={p['stop_z']}")
    print(f"  Лотов: {best_result['n_lots']}")
    
    # Таблица сделок
    print(f"\n  {'Тип':>4s} | {'Вход':>10s} | {'Выход':>10s} | {'Дн':>3s} | {'PnL/лот':>10s} | {'PnL total':>12s} | {'Причина':>7s}")
    print(f"  {'-'*70}")
    
    monthly_pnl = defaultdict(float)
    
    for t in best_result['trades']:
        scaled = t['pnl'] * best_result['n_lots']
        month_key = str(t['exit'])[:7]
        monthly_pnl[month_key] += scaled
        print(f"  {t['type']:>4s} | {str(t['entry']):>10s} | {str(t['exit']):>10s} | {t['days']:3d} | "
              f"{t['pnl']:+10.0f} | {scaled:+12,.0f} ₽ | {t['reason']:>7s}")
    
    print(f"\n  📊 ИТОГО {stock_name}:")
    print(f"     Сделок: {best_result['n_trades']} | Win rate: {best_result['win_rate']:.0f}% | "
          f"Avg hold: {best_result['avg_hold']:.0f} дней")
    print(f"     PnL: {best_result['total_pnl']:+,.0f} ₽ | Годовая: {best_result['annual']:+.1f}% | "
          f"Sharpe: {best_result['sharpe']:.2f}")
    
    # НДФЛ
    net = best_result['total_pnl'] * 0.87
    basis_df_days = (basis_df.index[-1] - basis_df.index[0]).days
    net_annual = net / capital * 365 / basis_df_days * 100
    print(f"     После НДФЛ: {net:+,.0f} ₽ ({net_annual:.1f}%/год)")
    
    # Месячная разбивка
    print(f"\n  Месячная разбивка:")
    for month in sorted(monthly_pnl.keys()):
        pnl = monthly_pnl[month]
        bar = "█" * max(0, int(pnl / 5000))
        neg = "░" * max(0, int(-pnl / 5000))
        print(f"    {month}: {pnl:+10,.0f} ₽ {bar}{neg}")
    
    if len(monthly_pnl) > 2:
        vals = list(monthly_pnl.values())
        print(f"\n  Прибыльных месяцев: {len([v for v in vals if v > 0])}/{len(vals)}")
    
    return monthly_pnl


# === MAIN ===
if __name__ == '__main__':
    print("🚀 АКТИВНЫЙ АРБИТРАЖ — ROSN & ALRS")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Капитал: 10,000,000 ₽ на каждый инструмент\n")
    
    CAPITAL = 10_000_000
    
    instruments = {
        'ROSN': {'prefix': 'RN', 'lot_size': 100, 'name': 'Роснефть'},
        'ALRS': {'prefix': 'AL', 'lot_size': 100, 'name': 'Алроса'},
        # Бонус — SBER и TATN для сравнения
        'SBER': {'prefix': 'SR', 'lot_size': 100, 'name': 'Сбер'},
        'TATN': {'prefix': 'TT', 'lot_size': 100, 'name': 'Татнефть'},
    }
    
    all_results = {}
    
    for stock, info in instruments.items():
        basis_df, spot = load_instrument(stock, info['prefix'], info['lot_size'])
        if basis_df is None or len(basis_df) < 80:
            print(f"  ⚠️ Недостаточно данных для {stock}")
            continue
        
        # Grid search
        best = grid_search(basis_df, CAPITAL, info['lot_size'], info['name'])
        if best is None:
            continue
        
        # Детальный отчёт лучшей стратегии
        monthly = detailed_report(basis_df, CAPITAL, info['lot_size'], info['name'], best)
        
        all_results[stock] = {
            'best': best,
            'monthly': monthly,
            'basis_df': basis_df,
        }
    
    # Итоговое сравнение
    print(f"\n{'='*70}")
    print(f"📋 ИТОГОВОЕ СРАВНЕНИЕ ВСЕХ ИНСТРУМЕНТОВ")
    print(f"{'='*70}")
    print(f"  {'Инструмент':>12s} | {'Annual':>7s} | {'Sharpe':>6s} | {'WR':>5s} | {'Trades':>6s} | {'Hold':>4s} | {'PnL':>12s} | {'Window':>3s} | {'Entry':>5s} | {'Exit':>5s}")
    print(f"  {'-'*95}")
    
    for stock in sorted(all_results.keys(), key=lambda x: -all_results[x]['best']['annual']):
        r = all_results[stock]['best']
        p = r['params']
        print(f"  {stock:>12s} | {r['annual']:+6.1f}% | {r['sharpe']:+6.2f} | {r['win_rate']:4.0f}% | "
              f"{r['n_trades']:6d} | {r['avg_hold']:3.0f}d | {r['total_pnl']:+12,.0f} | "
              f"{p['window']:3d} | {p['entry_z']:5.1f} | {p['exit_z']:5.1f}")
    
    # Портфель
    print(f"\n{'='*70}")
    print(f"💰 ОПТИМАЛЬНЫЙ ПОРТФЕЛЬ (10 млн)")
    print(f"{'='*70}")
    
    total_annual = 0
    for stock, data in sorted(all_results.items(), key=lambda x: -x[1]['best']['annual']):
        r = data['best']
        # Распределяем капитал пропорционально Sharpe
        alloc = 2_500_000  # Равномерно по 2.5 млн на каждый
        expected = r['annual'] * alloc / CAPITAL
        total_annual += expected
        print(f"  {stock}: {alloc/1e6:.1f} млн → ~{expected:.1f}%")
    
    print(f"\n  Общий ожидаемый gross: ~{total_annual:.1f}%")
    print(f"  После комиссий (~1.5%): ~{total_annual - 1.5:.1f}%")
    print(f"  После НДФЛ (13%): ~{(total_annual - 1.5) * 0.87:.1f}%")
    
    print("\n✅ Анализ завершён.")
