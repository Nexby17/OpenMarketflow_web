"""
MOEX Arbitrage Analysis
1. Загрузка исторических данных спот + фьючерсов через ISS API
2. Анализ базиса спот/фьючерс
3. Матрица корреляций
4. Тест на коинтеграцию
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from scipy import stats
from statsmodels.tsa.stattools import coint, adfuller
import json
import warnings
warnings.filterwarnings('ignore')

# === MOEX ISS API ===

def fetch_moex_candles(ticker, board='TQBR', engine='stock', market='shares',
                       interval=24, start='2024-04-01', end='2026-04-02'):
    """Загрузка свечей через MOEX ISS API"""
    all_data = []
    start_row = 0
    
    while True:
        url = (f"https://iss.moex.com/iss/engines/{engine}/markets/{market}/"
               f"boards/{board}/securities/{ticker}/candles.json"
               f"?from={start}&till={end}&interval={interval}&start={start_row}")
        
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Ошибка загрузки {ticker}: {e}")
            break
        
        candles = data.get('candles', {})
        columns = candles.get('columns', [])
        rows = candles.get('data', [])
        
        if not rows:
            break
            
        all_data.extend(rows)
        start_row += len(rows)
        
        if len(rows) < 500:
            break
    
    if not all_data:
        return pd.DataFrame()
    
    df = pd.DataFrame(all_data, columns=columns)
    df['begin'] = pd.to_datetime(df['begin'])
    df = df.set_index('begin')
    return df


def fetch_spot(ticker, start='2024-04-01', end='2026-04-02'):
    """Загрузка акций (TQBR)"""
    print(f"  Загрузка спот {ticker}...")
    return fetch_moex_candles(ticker, board='TQBR', engine='stock', market='shares',
                              interval=24, start=start, end=end)


def fetch_futures(ticker, start='2024-04-01', end='2026-04-02'):
    """Загрузка фьючерсов (RFUD)"""
    print(f"  Загрузка фьючерс {ticker}...")
    return fetch_moex_candles(ticker, board='RFUD', engine='futures', market='forts',
                              interval=24, start=start, end=end)


def get_futures_chain(base_ticker, start='2024-04-01', end='2026-04-02'):
    """Получить все фьючерсы для базового актива и склеить"""
    # Фьючерсы на Мосбирже: SBER-6.25, GAZP-6.25 и т.д.
    # Через ISS ищем все контракты
    url = (f"https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
           f"?q={base_ticker}")
    
    try:
        resp = requests.get(url, timeout=30)
        data = resp.json()
        securities = data.get('securities', {})
        columns = securities.get('columns', [])
        rows = securities.get('data', [])
        
        if rows:
            sec_df = pd.DataFrame(rows, columns=columns)
            # Фильтруем только фьючерсы на нужный актив
            fut_tickers = sec_df[sec_df['SECID'].str.startswith(base_ticker[:2].upper())]['SECID'].tolist()
            return fut_tickers[:10]  # Не больше 10
    except:
        pass
    
    return []


# === АНАЛИЗ ===

# Пары спот/фьючерс для анализа
# Тикеры фьючерсов на Мосбирже: SBRFxx, GAZRxx, и т.д.
SPOT_FUTURES_PAIRS = {
    'SBER': {'fut_prefix': 'SBRF', 'lot_size': 100, 'name': 'Сбер'},
    'GAZP': {'fut_prefix': 'GAZR', 'lot_size': 100, 'name': 'Газпром'},
    'LKOH': {'fut_prefix': 'LKOH', 'lot_size': 1, 'name': 'Лукойл'},
    'ROSN': {'fut_prefix': 'ROSN', 'lot_size': 100, 'name': 'Роснефть'},
    'VTBR': {'fut_prefix': 'VTBR', 'lot_size': 100000, 'name': 'ВТБ'},
    'GMKN': {'fut_prefix': 'GMKN', 'lot_size': 1, 'name': 'Норникель'},
    'NVTK': {'fut_prefix': 'NVTK', 'lot_size': 10, 'name': 'Новатэк'},
    'YDEX': {'fut_prefix': 'YDEX', 'lot_size': 1, 'name': 'Яндекс'},
    'MGNT': {'fut_prefix': 'MGNT', 'lot_size': 1, 'name': 'Магнит'},
    'ALRS': {'fut_prefix': 'ALRS', 'lot_size': 100, 'name': 'Алроса'},
}

# Пары для парного трейдинга
PAIRS_TRADING = [
    ('SBER', 'VTBR', 'Банки'),
    ('GAZP', 'NVTK', 'Газ'),
    ('LKOH', 'ROSN', 'Нефть'),
    ('LKOH', 'TATN', 'Нефть 2'),
    ('SBER', 'GAZP', 'Голубые фишки'),
    ('GMKN', 'ALRS', 'Сырьё'),
    ('ROSN', 'TATN', 'Нефть 3'),
    ('MGNT', 'FIVE', 'Ритейл'),
    ('NVTK', 'ROSN', 'Энергетика'),
]


def analyze_spot_futures():
    """Анализ базиса спот/фьючерс"""
    print("=" * 60)
    print("АНАЛИЗ БАЗИСА СПОТ/ФЬЮЧЕРС")
    print("=" * 60)
    
    results = {}
    spot_data = {}
    
    for ticker, info in SPOT_FUTURES_PAIRS.items():
        print(f"\n--- {info['name']} ({ticker}) ---")
        
        # Загружаем спот
        spot = fetch_spot(ticker)
        if spot.empty:
            print(f"  Нет данных спот для {ticker}")
            continue
        
        spot_data[ticker] = spot['close']
        
        # Ищем фьючерсы
        print(f"  Поиск фьючерсов {info['fut_prefix']}...")
        
        # Попробуем текущие контракты (ближайшие кварталы)
        months = ['3', '6', '9', '12']
        years = ['24', '25', '26']
        
        fut_prices = []
        for y in years:
            for m in months:
                fut_ticker = f"{info['fut_prefix']}-{m}.{y}"
                fut = fetch_futures(fut_ticker)
                if not fut.empty and len(fut) > 10:
                    print(f"    Найден: {fut_ticker} ({len(fut)} дней)")
                    fut_prices.append(fut[['close']].rename(columns={'close': fut_ticker}))
        
        if not fut_prices:
            # Альтернативный формат тикеров
            for y in years:
                for m in months:
                    fut_ticker = f"{ticker}-{m}.{y}"
                    fut = fetch_futures(fut_ticker)
                    if not fut.empty and len(fut) > 10:
                        print(f"    Найден: {fut_ticker} ({len(fut)} дней)")
                        fut_prices.append(fut[['close']].rename(columns={'close': fut_ticker}))
        
        if not fut_prices:
            print(f"  Фьючерсы не найдены для {ticker}")
            continue
        
        # Склеиваем фьючерсы и считаем базис
        # Берём ближайший контракт для каждой даты
        spot_close = spot['close']
        
        basis_data = []
        for fut_df in fut_prices:
            col = fut_df.columns[0]
            merged = pd.DataFrame({
                'spot': spot_close,
                'futures': fut_df[col]
            }).dropna()
            
            if len(merged) < 10:
                continue
            
            merged['basis_abs'] = merged['futures'] - merged['spot'] * info['lot_size']
            merged['basis_pct'] = (merged['futures'] / (merged['spot'] * info['lot_size']) - 1) * 100
            # Грубая аннуализация (предполагаем ~60 дней до экспирации в среднем)
            merged['basis_annual'] = merged['basis_pct'] * (365 / 60)
            
            basis_data.append({
                'contract': col,
                'days': len(merged),
                'basis_mean': merged['basis_pct'].mean(),
                'basis_std': merged['basis_pct'].std(),
                'basis_min': merged['basis_pct'].min(),
                'basis_max': merged['basis_pct'].max(),
                'annual_mean': merged['basis_annual'].mean(),
            })
        
        if basis_data:
            results[ticker] = {
                'info': info,
                'basis': basis_data,
                'spot_last': spot_close.iloc[-1] if len(spot_close) > 0 else 0,
            }
            
            print(f"\n  Результаты базиса {ticker}:")
            for b in basis_data:
                print(f"    {b['contract']}: mean={b['basis_mean']:.3f}%, "
                      f"std={b['basis_std']:.3f}%, range=[{b['basis_min']:.3f}%, {b['basis_max']:.3f}%], "
                      f"annual~{b['annual_mean']:.1f}%")
    
    return results, spot_data


def analyze_correlations(spot_data):
    """Матрица корреляций и тест на коинтеграцию"""
    print("\n" + "=" * 60)
    print("МАТРИЦА КОРРЕЛЯЦИЙ И КОИНТЕГРАЦИЯ")
    print("=" * 60)
    
    # Собираем все спот-данные в один DataFrame
    prices = pd.DataFrame(spot_data)
    prices = prices.dropna(how='all').ffill()
    
    # Добавляем TATN и FIVE если не загружены
    for extra in ['TATN', 'FIVE']:
        if extra not in prices.columns:
            print(f"\n  Загрузка {extra} для парного трейдинга...")
            data = fetch_spot(extra)
            if not data.empty:
                prices[extra] = data['close']
    
    prices = prices.dropna(how='all').ffill().dropna()
    
    print(f"\n  Загружено {len(prices.columns)} акций, {len(prices)} дней")
    
    # Корреляционная матрица
    corr_matrix = prices.pct_change().dropna().corr()
    
    print("\n  МАТРИЦА КОРРЕЛЯЦИЙ (дневные returns):")
    print(corr_matrix.round(3).to_string())
    
    # Находим пары с корреляцией > 0.9
    high_corr_pairs = []
    cols = corr_matrix.columns
    for i in range(len(cols)):
        for j in range(i+1, len(cols)):
            c = corr_matrix.iloc[i, j]
            if c >= 0.7:  # Берём от 0.7 чтобы увидеть больше кандидатов
                high_corr_pairs.append((cols[i], cols[j], c))
    
    high_corr_pairs.sort(key=lambda x: -x[2])
    
    print(f"\n  ПАРЫ С КОРРЕЛЯЦИЕЙ >= 0.7:")
    for a, b, c in high_corr_pairs:
        print(f"    {a}/{b}: {c:.4f}")
    
    # Тест на коинтеграцию
    print(f"\n  ТЕСТ НА КОИНТЕГРАЦИЮ (Engle-Granger):")
    coint_results = []
    
    for a, b, corr_val in high_corr_pairs:
        if a in prices.columns and b in prices.columns:
            pair_data = prices[[a, b]].dropna()
            if len(pair_data) < 60:
                continue
            
            try:
                score, pvalue, _ = coint(pair_data[a], pair_data[b])
                result = {
                    'pair': f"{a}/{b}",
                    'correlation': corr_val,
                    'coint_stat': score,
                    'p_value': pvalue,
                    'cointegrated': pvalue < 0.05,
                }
                coint_results.append(result)
                
                status = "✅ КОИНТЕГРИРОВАНЫ" if pvalue < 0.05 else "❌ нет коинтеграции"
                print(f"    {a}/{b}: corr={corr_val:.3f}, p-value={pvalue:.4f} {status}")
            except Exception as e:
                print(f"    {a}/{b}: ошибка теста — {e}")
    
    return corr_matrix, high_corr_pairs, coint_results


def backtest_pairs_trading(spot_data, coint_results, prices_df=None):
    """Бэктест парного трейдинга на коинтегрированных парах"""
    print("\n" + "=" * 60)
    print("БЭКТЕСТ ПАРНОГО ТРЕЙДИНГА")
    print("=" * 60)
    
    cointegrated = [r for r in coint_results if r['cointegrated']]
    
    if not cointegrated:
        print("  Нет коинтегрированных пар для бэктеста")
        # Всё равно тестируем топ-3 по корреляции
        cointegrated = sorted(coint_results, key=lambda x: -x['correlation'])[:5]
        print(f"  Тестируем топ-5 по корреляции:")
    
    prices = pd.DataFrame(spot_data).dropna(how='all').ffill().dropna()
    
    # Добавляем TATN, FIVE
    for extra in ['TATN', 'FIVE']:
        if extra not in prices.columns:
            data = fetch_spot(extra)
            if not data.empty:
                prices[extra] = data['close']
    prices = prices.ffill().dropna()
    
    bt_results = []
    
    for r in cointegrated:
        pair = r['pair']
        a, b = pair.split('/')
        
        if a not in prices.columns or b not in prices.columns:
            continue
        
        pair_data = prices[[a, b]].dropna()
        if len(pair_data) < 120:
            continue
        
        # Расчёт спреда: нормализуем цены и считаем z-score
        # Hedge ratio через OLS
        from numpy.polynomial.polynomial import polyfit
        
        train = pair_data.iloc[:len(pair_data)//2]  # Первая половина — обучение
        test = pair_data.iloc[len(pair_data)//2:]    # Вторая половина — тест
        
        # Hedge ratio
        hedge_ratio = np.polyfit(train[b], train[a], 1)[0]
        
        # Спред на тестовом периоде
        spread = test[a] - hedge_ratio * test[b]
        spread_mean = spread.rolling(20).mean()
        spread_std = spread.rolling(20).std()
        zscore = (spread - spread_mean) / spread_std
        
        # Торговые сигналы
        positions = pd.Series(0.0, index=zscore.index)
        in_position = 0  # 0 = нет, 1 = лонг спред, -1 = шорт спред
        entry_price = 0
        trades = []
        
        for i in range(20, len(zscore)):
            z = zscore.iloc[i]
            s = spread.iloc[i]
            
            if np.isnan(z):
                continue
            
            if in_position == 0:
                if z > 2.0:  # Шорт спред (a дорого vs b)
                    in_position = -1
                    entry_price = s
                elif z < -2.0:  # Лонг спред (a дёшево vs b)
                    in_position = 1
                    entry_price = s
            elif in_position == 1:
                if z > -0.5 or z > 3.0:  # Выход или стоп
                    pnl = s - entry_price
                    trades.append(pnl)
                    in_position = 0
            elif in_position == -1:
                if z < 0.5 or z < -3.0:
                    pnl = entry_price - s
                    trades.append(pnl)
                    in_position = 0
        
        if trades:
            total_pnl = sum(trades)
            win_rate = len([t for t in trades if t > 0]) / len(trades) * 100
            avg_trade = np.mean(trades)
            sharpe = np.mean(trades) / np.std(trades) * np.sqrt(len(trades)) if np.std(trades) > 0 else 0
            
            bt_results.append({
                'pair': pair,
                'correlation': r['correlation'],
                'cointegrated': r.get('cointegrated', False),
                'p_value': r['p_value'],
                'trades': len(trades),
                'win_rate': win_rate,
                'total_pnl_pts': total_pnl,
                'avg_trade': avg_trade,
                'sharpe': sharpe,
                'hedge_ratio': hedge_ratio,
            })
            
            coint_flag = "✅" if r.get('cointegrated', False) else "⚠️"
            print(f"\n  {coint_flag} {pair} (corr={r['correlation']:.3f}, p={r['p_value']:.3f}):")
            print(f"    Сделок: {len(trades)}, Win rate: {win_rate:.1f}%")
            print(f"    Total PnL (pts): {total_pnl:.2f}, Avg trade: {avg_trade:.2f}")
            print(f"    Sharpe: {sharpe:.2f}, Hedge ratio: {hedge_ratio:.4f}")
        else:
            print(f"\n  ⚠️ {pair}: нет сигналов за тестовый период")
    
    return bt_results


# === MAIN ===

if __name__ == '__main__':
    print("🚀 MOEX Arbitrage & Pairs Trading Analysis")
    print(f"   Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Период: 2024-04 — 2026-04 (2 года)")
    print()
    
    # 1. Анализ базиса спот/фьючерс
    basis_results, spot_data = analyze_spot_futures()
    
    # 2. Корреляции и коинтеграция
    corr_matrix, high_corr_pairs, coint_results = analyze_correlations(spot_data)
    
    # 3. Бэктест парного трейдинга
    bt_results = backtest_pairs_trading(spot_data, coint_results)
    
    # 4. Итоговый отчёт
    print("\n" + "=" * 60)
    print("ИТОГОВЫЙ ОТЧЁТ")
    print("=" * 60)
    
    print("\n📊 БАЗИС СПОТ/ФЬЮЧЕРС:")
    for ticker, data in basis_results.items():
        info = data['info']
        print(f"\n  {info['name']} ({ticker}):")
        for b in data['basis']:
            print(f"    {b['contract']}: basis={b['basis_mean']:.3f}% ± {b['basis_std']:.3f}%, "
                  f"annualized ~{b['annual_mean']:.1f}%")
    
    print("\n📈 ПАРНЫЙ ТРЕЙДИНГ — ЛУЧШИЕ ПАРЫ:")
    if bt_results:
        bt_sorted = sorted(bt_results, key=lambda x: -x['sharpe'])
        for r in bt_sorted:
            coint_flag = "✅" if r['cointegrated'] else "⚠️"
            print(f"  {coint_flag} {r['pair']}: Sharpe={r['sharpe']:.2f}, "
                  f"WR={r['win_rate']:.0f}%, Trades={r['trades']}, "
                  f"Corr={r['correlation']:.3f}")
    else:
        print("  Недостаточно данных для бэктеста")
    
    print("\n✅ Анализ завершён.")
