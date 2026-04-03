"""
MOEX Arbitrage Analysis v2
Правильные тикеры фьючерсов FORTS
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime
from statsmodels.tsa.stattools import coint
import warnings
warnings.filterwarnings('ignore')

# Маппинг: акция → (префикс фьючерса, лот фьючерса в акциях, название)
PAIRS = {
    'SBER': ('SR', 100, 'Сбер'),
    'GAZP': ('GZ', 100, 'Газпром'),
    'LKOH': ('LK', 1, 'Лукойл'),
    'ROSN': ('RN', 100, 'Роснефть'),
    'VTBR': ('VB', 100000, 'ВТБ'),
    'GMKN': ('GK', 1, 'Норникель'),
    'NVTK': ('NK', 10, 'Новатэк'),
    'YDEX': ('YD', 1, 'Яндекс'),
    'MGNT': ('MN', 1, 'Магнит'),
    'ALRS': ('AL', 100, 'Алроса'),
    'TATN': ('TT', 100, 'Татнефть'),
}

# Месяцы фьючерсов: H=март, M=июнь, U=сент, Z=дек
MONTHS = {'H': 3, 'M': 6, 'U': 9, 'Z': 12}

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
            if not rows:
                break
            cols = data['candles']['columns']
            all_data.extend(rows)
            start_row += len(rows)
            if len(rows) < 500:
                break
        except Exception as e:
            print(f"    Ошибка {secid}: {e}")
            break
    
    if not all_data:
        return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=cols)
    df['begin'] = pd.to_datetime(df['begin'])
    df = df.set_index('begin')
    return df

def fetch_spot(ticker):
    return fetch_candles(ticker, 'TQBR', 'stock', 'shares')

def fetch_futures(secid):
    return fetch_candles(secid, 'RFUD', 'futures', 'forts')

def generate_fut_tickers(prefix):
    """Генерирует все тикеры фьючерсов за 2024-2026"""
    tickers = []
    for year in [4, 5, 6]:  # 2024, 2025, 2026
        for month_code in ['H', 'M', 'U', 'Z']:
            secid = f"{prefix}{month_code}{year}"
            tickers.append(secid)
    return tickers

def analyze_basis():
    """Анализ базиса по всем парам"""
    print("=" * 70)
    print("АНАЛИЗ БАЗИСА СПОТ/ФЬЮЧЕРС (2 года)")
    print("=" * 70)
    
    all_spot = {}
    results = {}
    
    for stock, (fut_prefix, lot_size, name) in PAIRS.items():
        print(f"\n{'─'*50}")
        print(f"  {name} ({stock}) | Фьючерс: {fut_prefix}xx | Лот: {lot_size} акций")
        print(f"{'─'*50}")
        
        spot = fetch_spot(stock)
        if spot.empty:
            print(f"    ❌ Нет спот-данных")
            continue
        
        print(f"    Спот: {len(spot)} дней, last={spot['close'].iloc[-1]:.2f}")
        all_spot[stock] = spot['close']
        
        fut_tickers = generate_fut_tickers(fut_prefix)
        contracts = []
        
        for ft in fut_tickers:
            fut = fetch_futures(ft)
            if not fut.empty and len(fut) > 5:
                contracts.append((ft, fut))
                # print(f"    ✅ {ft}: {len(fut)} дней")
        
        if not contracts:
            print(f"    ❌ Фьючерсы не найдены")
            continue
        
        print(f"    Найдено контрактов: {len(contracts)}")
        
        # Анализ базиса по каждому контракту
        basis_all = []
        for ft, fut in contracts:
            merged = pd.DataFrame({
                'spot': spot['close'],
                'futures': fut['close']
            }).dropna()
            
            if len(merged) < 5:
                continue
            
            # Базис = (фьючерс - спот*лот) / (спот*лот) * 100
            merged['basis_pct'] = (merged['futures'] / (merged['spot'] * lot_size) - 1) * 100
            
            # Дней до экспирации (грубо — из тикера)
            month_code = ft[-2]
            year = int('202' + ft[-1])
            month = MONTHS.get(month_code, 6)
            # Экспирация ~15 числа месяца
            expiry = datetime(year, month, 15)
            merged['days_to_exp'] = [(expiry - d.to_pydatetime()).days for d in merged.index]
            merged = merged[merged['days_to_exp'] > 0]
            
            if len(merged) < 5:
                continue
            
            # Аннуализация
            merged['basis_annual'] = merged['basis_pct'] * 365 / merged['days_to_exp']
            
            avg_basis = merged['basis_pct'].mean()
            std_basis = merged['basis_pct'].std()
            avg_annual = merged['basis_annual'].mean()
            std_annual = merged['basis_annual'].std()
            
            basis_all.append({
                'contract': ft,
                'days': len(merged),
                'basis_pct': avg_basis,
                'basis_std': std_basis,
                'basis_min': merged['basis_pct'].min(),
                'basis_max': merged['basis_pct'].max(),
                'annual_mean': avg_annual,
                'annual_std': std_annual,
                'annual_min': merged['basis_annual'].min(),
                'annual_max': merged['basis_annual'].max(),
            })
            
            print(f"    {ft}: basis={avg_basis:+.3f}% ± {std_basis:.3f}% | "
                  f"annual={avg_annual:+.1f}% [{merged['basis_annual'].min():.1f}%..{merged['basis_annual'].max():.1f}%] | "
                  f"{len(merged)} дней")
        
        if basis_all:
            # Средний годовой базис по всем контрактам
            avg = np.mean([b['annual_mean'] for b in basis_all])
            std = np.mean([b['annual_std'] for b in basis_all])
            print(f"\n    📊 Средний годовой базис: {avg:+.1f}% ± {std:.1f}%")
            print(f"    📊 Разброс: [{min(b['annual_min'] for b in basis_all):.1f}%..{max(b['annual_max'] for b in basis_all):.1f}%]")
            
            results[stock] = {
                'name': name,
                'lot_size': lot_size,
                'contracts': basis_all,
                'avg_annual': avg,
                'avg_std': std,
            }
    
    return results, all_spot

def analyze_correlations_and_coint(all_spot):
    """Корреляции + коинтеграция"""
    print(f"\n{'='*70}")
    print("МАТРИЦА КОРРЕЛЯЦИЙ И КОИНТЕГРАЦИЯ")
    print(f"{'='*70}")
    
    prices = pd.DataFrame(all_spot).dropna(how='all').ffill().dropna()
    returns = prices.pct_change().dropna()
    
    print(f"\n  Акций: {len(prices.columns)} | Дней: {len(prices)}")
    
    corr = returns.corr()
    
    # Все пары с корреляцией
    pairs = []
    cols = corr.columns.tolist()
    for i in range(len(cols)):
        for j in range(i+1, len(cols)):
            c = corr.iloc[i, j]
            if not np.isnan(c):
                pairs.append((cols[i], cols[j], c))
    
    pairs.sort(key=lambda x: -x[2])
    
    print(f"\n  ТОП-15 ПАР ПО КОРРЕЛЯЦИИ:")
    for a, b, c in pairs[:15]:
        print(f"    {a:5s}/{b:5s}: {c:.4f}")
    
    # Тест коинтеграции для пар с корр > 0.6
    print(f"\n  КОИНТЕГРАЦИЯ (пары с корр > 0.6):")
    coint_results = []
    
    for a, b, corr_val in pairs:
        if corr_val < 0.6:
            break
        
        pair_data = prices[[a, b]].dropna()
        if len(pair_data) < 100:
            continue
        
        try:
            score, pvalue, _ = coint(pair_data[a], pair_data[b])
            is_coint = pvalue < 0.05
            status = "✅" if is_coint else "❌"
            coint_results.append({
                'a': a, 'b': b, 'corr': corr_val,
                'p_value': pvalue, 'cointegrated': is_coint
            })
            print(f"    {status} {a:5s}/{b:5s}: corr={corr_val:.3f}, p={pvalue:.4f}")
        except:
            pass
    
    return pairs, coint_results, prices

def backtest_pairs(prices, coint_results):
    """Бэктест парного трейдинга"""
    print(f"\n{'='*70}")
    print("БЭКТЕСТ ПАРНОГО ТРЕЙДИНГА")
    print(f"{'='*70}")
    
    # Тестируем все пары с корр > 0.6
    candidates = sorted(coint_results, key=lambda x: (not x['cointegrated'], -x['corr']))[:10]
    
    results = []
    
    for r in candidates:
        a, b = r['a'], r['b']
        if a not in prices.columns or b not in prices.columns:
            continue
        
        data = prices[[a, b]].dropna()
        if len(data) < 120:
            continue
        
        # Train/test split
        split = len(data) // 2
        train = data.iloc[:split]
        test = data.iloc[split:]
        
        # Hedge ratio (OLS)
        hedge = np.polyfit(train[b], train[a], 1)[0]
        
        # Spread
        spread = test[a] - hedge * test[b]
        roll_mean = spread.rolling(20).mean()
        roll_std = spread.rolling(20).std()
        zscore = (spread - roll_mean) / roll_std
        
        # Simple mean-reversion strategy
        position = 0  # +1=long spread, -1=short spread
        entry_z = 0
        entry_spread = 0
        trades = []
        
        for i in range(20, len(zscore)):
            z = zscore.iloc[i]
            s = spread.iloc[i]
            if np.isnan(z):
                continue
            
            if position == 0:
                if z > 2.0:
                    position = -1
                    entry_spread = s
                    entry_z = z
                elif z < -2.0:
                    position = 1
                    entry_spread = s
                    entry_z = z
            elif position == 1:
                if z >= -0.3 or z > 3.5:
                    pnl_pct = (s - entry_spread) / abs(entry_spread) * 100 if entry_spread != 0 else 0
                    trades.append({'pnl_pts': s - entry_spread, 'pnl_pct': pnl_pct})
                    position = 0
            elif position == -1:
                if z <= 0.3 or z < -3.5:
                    pnl_pct = (entry_spread - s) / abs(entry_spread) * 100 if entry_spread != 0 else 0
                    trades.append({'pnl_pts': entry_spread - s, 'pnl_pct': pnl_pct})
                    position = 0
        
        if len(trades) < 3:
            continue
        
        pnls = [t['pnl_pts'] for t in trades]
        win_rate = len([p for p in pnls if p > 0]) / len(pnls) * 100
        total = sum(pnls)
        sharpe = np.mean(pnls) / np.std(pnls) * np.sqrt(len(pnls)) if np.std(pnls) > 0 else 0
        
        # Rough annual return estimate
        test_days = (test.index[-1] - test.index[0]).days
        annual_return = sum([t['pnl_pct'] for t in trades]) * (365 / test_days) if test_days > 0 else 0
        
        flag = "✅" if r['cointegrated'] else "⚠️"
        print(f"\n  {flag} {a}/{b} (corr={r['corr']:.3f}, p={r['p_value']:.3f})")
        print(f"    Hedge ratio: {hedge:.4f}")
        print(f"    Сделок: {len(trades)} за {test_days} дней ({len(trades)*365/test_days:.0f}/год)")
        print(f"    Win rate: {win_rate:.0f}%")
        print(f"    Sharpe: {sharpe:.2f}")
        print(f"    ~Годовая доходность: {annual_return:.1f}%")
        
        results.append({
            'pair': f"{a}/{b}",
            'corr': r['corr'],
            'cointegrated': r['cointegrated'],
            'p_value': r['p_value'],
            'trades': len(trades),
            'win_rate': win_rate,
            'sharpe': sharpe,
            'annual_return': annual_return,
            'hedge_ratio': hedge,
        })
    
    return results

def summary(basis_results, pairs_results):
    """Итоговый отчёт"""
    print(f"\n{'='*70}")
    print("📋 ИТОГОВЫЙ ОТЧЁТ")
    print(f"{'='*70}")
    
    print(f"\n🏦 АРБИТРАЖ СПОТ/ФЬЮЧЕРС — РЕЙТИНГ ПО ГОДОВОМУ БАЗИСУ:")
    sorted_basis = sorted(basis_results.items(), key=lambda x: -x[1]['avg_annual'])
    for stock, data in sorted_basis:
        print(f"  {data['name']:12s} ({stock}): годовой базис = {data['avg_annual']:+.1f}% ± {data['avg_std']:.1f}% | "
              f"контрактов: {len(data['contracts'])}")
    
    print(f"\n📈 ПАРНЫЙ ТРЕЙДИНГ — РЕЙТИНГ ПО SHARPE:")
    if pairs_results:
        sorted_pairs = sorted(pairs_results, key=lambda x: -x['sharpe'])
        for r in sorted_pairs:
            flag = "✅" if r['cointegrated'] else "⚠️"
            print(f"  {flag} {r['pair']:12s}: Sharpe={r['sharpe']:.2f} | WR={r['win_rate']:.0f}% | "
                  f"~{r['annual_return']:.1f}%/год | {r['trades']} сделок | corr={r['corr']:.3f}")
    
    # Расчёт потенциала портфеля
    print(f"\n💰 ПОТЕНЦИАЛ ПОРТФЕЛЯ (10 млн капитал, ставка ЦБ 15%):")
    if sorted_basis:
        top3 = sorted_basis[:3]
        avg_basis = np.mean([d[1]['avg_annual'] for d in top3])
        print(f"  Топ-3 пары по базису: средний = {avg_basis:.1f}% годовых (gross)")
        print(f"  Пассивный кэрри: ~{avg_basis*0.7:.1f}% чистыми (−комиссии −НДФЛ)")
        print(f"  Активный арбитраж (ловля расширений): +3-8% сверху")
        if pairs_results:
            best_pair = max(pairs_results, key=lambda x: x['sharpe'])
            print(f"  Лучший парный трейдинг: {best_pair['pair']} ~{best_pair['annual_return']:.1f}%/год")

# === MAIN ===
if __name__ == '__main__':
    print("🚀 MOEX Arbitrage & Pairs Trading Analysis v2")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Период: 2024-04 — 2026-04\n")
    
    basis_results, all_spot = analyze_basis()
    
    corr_pairs, coint_results, prices = analyze_correlations_and_coint(all_spot)
    
    bt_results = backtest_pairs(prices, coint_results)
    
    summary(basis_results, bt_results)
    
    print("\n✅ Анализ завершён.")
