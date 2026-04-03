"""
Активный арбитраж GAZP спот/фьючерс
- Пассивный кэрри (купил акцию / продал фьючерс, держим до экспирации)
- Активная компонента: торговля расширениями/сужениями базиса
- Дивидендный арбитраж
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
        except Exception as e:
            break
    if not all_data: return pd.DataFrame()
    df = pd.DataFrame(all_data, columns=cols)
    df['begin'] = pd.to_datetime(df['begin'])
    return df.set_index('begin')


def load_all_data():
    """Загрузка спот GAZP + все фьючерсы GZ за 2024-2026"""
    print("📥 Загрузка данных GAZP...")
    
    # Спот
    spot = fetch_candles('GAZP', 'TQBR', 'stock', 'shares')
    print(f"  Спот: {len(spot)} дней, [{spot.index[0].date()} — {spot.index[-1].date()}]")
    
    # Фьючерсы
    futures = {}
    for year in [4, 5, 6, 7]:
        for mc in MONTH_CODES:
            secid = f"GZ{mc}{year}"
            month = MONTHS[mc]
            full_year = 2020 + year
            expiry = datetime(full_year, month, 15)  # ~15 числа
            
            fut = fetch_candles(secid, 'RFUD', 'futures', 'forts')
            if not fut.empty and len(fut) > 5:
                futures[secid] = {
                    'data': fut,
                    'expiry': expiry,
                    'month': month,
                    'year': full_year,
                }
                print(f"  {secid}: {len(fut)} дней, экспирация ~{expiry.date()}")
    
    return spot, futures


def build_continuous_basis(spot, futures, lot_size=100):
    """
    Строим непрерывный ряд базиса:
    для каждой даты берём ближайший фьючерс (>10 дней до экспирации)
    """
    records = []
    
    for date in spot.index:
        spot_price = spot.loc[date, 'close']
        
        # Найти ближайший фьючерс
        best_fut = None
        best_days = 9999
        
        for secid, info in futures.items():
            days_to_exp = (info['expiry'] - date.to_pydatetime()).days
            if days_to_exp < 10:  # Слишком близко к экспирации
                continue
            if days_to_exp > 120:  # Слишком далеко
                continue
            
            if date in info['data'].index:
                fut_price = info['data'].loc[date, 'close']
                if days_to_exp < best_days:
                    best_days = days_to_exp
                    best_fut = {
                        'secid': secid,
                        'fut_price': fut_price,
                        'days_to_exp': days_to_exp,
                        'expiry': info['expiry'],
                    }
        
        if best_fut:
            basis_abs = best_fut['fut_price'] - spot_price * lot_size
            basis_pct = (best_fut['fut_price'] / (spot_price * lot_size) - 1) * 100
            basis_annual = basis_pct * 365 / best_fut['days_to_exp']
            
            records.append({
                'date': date,
                'spot': spot_price,
                'futures': best_fut['fut_price'],
                'secid': best_fut['secid'],
                'days_to_exp': best_fut['days_to_exp'],
                'basis_abs': basis_abs,
                'basis_pct': basis_pct,
                'basis_annual': basis_annual,
                'expiry': best_fut['expiry'],
            })
    
    df = pd.DataFrame(records).set_index('date')
    return df


def strategy_passive_carry(basis_df, capital=10_000_000, lot_size=100):
    """
    Стратегия 1: Пассивный кэрри
    - Входим в начале каждого квартала (лонг акция + шорт фьючерс)
    - Держим до экспирации
    - Доход = базис на момент входа
    """
    print("\n" + "="*70)
    print("СТРАТЕГИЯ 1: ПАССИВНЫЙ КЭРРИ")
    print("="*70)
    
    # Группируем по контракту
    contracts = basis_df.groupby('secid')
    
    total_pnl = 0
    trades = []
    
    for secid, group in contracts:
        if len(group) < 10:
            continue
        
        # Входим в первый день контракта
        entry = group.iloc[0]
        exit_ = group.iloc[-1]
        
        # Сколько лотов можем купить
        spot_cost = entry['spot'] * lot_size  # Стоимость 1 лота акций
        fut_go = entry['futures'] * 0.15  # ГО фьючерса ~15%
        cost_per_pair = spot_cost + fut_go  # Капитал на 1 пару
        
        n_lots = int(capital * 0.3 / cost_per_pair)  # 30% капитала на 1 контракт
        if n_lots < 1:
            n_lots = 1
        
        # PnL: (базис при входе) * n_lots
        # Лонг акция: (exit_spot - entry_spot) * lot_size * n_lots
        # Шорт фьючерс: (entry_fut - exit_fut) * n_lots
        pnl_spot = (exit_['spot'] - entry['spot']) * lot_size * n_lots
        pnl_fut = (entry['futures'] - exit_['futures']) * n_lots
        pnl_total = pnl_spot + pnl_fut
        
        # Комиссия: ~0.03% от оборота спот + ~2 руб за фьючерс
        commission = (entry['spot'] * lot_size * n_lots * 0.0003 * 2 +  # спот вход+выход
                     n_lots * 2 * 2)  # фьючерс вход+выход
        
        net_pnl = pnl_total - commission
        holding_days = len(group)
        annual_return = net_pnl / capital * 365 / holding_days * 100
        
        trades.append({
            'contract': secid,
            'entry_date': group.index[0].date(),
            'exit_date': group.index[-1].date(),
            'days': holding_days,
            'lots': n_lots,
            'entry_basis_annual': entry['basis_annual'],
            'pnl': net_pnl,
            'annual_return': annual_return,
        })
        
        total_pnl += net_pnl
        
        print(f"  {secid}: {group.index[0].date()} → {group.index[-1].date()} | "
              f"{holding_days} дн | {n_lots} лотов | "
              f"базис входа={entry['basis_annual']:.1f}%/год | "
              f"PnL={net_pnl:+,.0f} ₽ | ~{annual_return:.1f}%/год")
    
    total_days = (basis_df.index[-1] - basis_df.index[0]).days
    total_annual = total_pnl / capital * 365 / total_days * 100
    
    print(f"\n  📊 ИТОГО кэрри: {total_pnl:+,.0f} ₽ за {total_days} дней = {total_annual:.1f}%/год")
    return trades, total_pnl


def strategy_active_basis(basis_df, capital=10_000_000, lot_size=100):
    """
    Стратегия 2: Активная торговля базисом
    - Считаем z-score годового базиса (скользящее окно 20 дней)
    - Базис расширился (z > 1.5) → входим (лонг акция + шорт фьючерс)
    - Базис сузился (z < 0 или z < -0.5) → выходим
    - Базис сузился ниже нормы (z < -1.5) → обратная позиция (шорт акция + лонг фьючерс)
    """
    print("\n" + "="*70)
    print("СТРАТЕГИЯ 2: АКТИВНАЯ ТОРГОВЛЯ БАЗИСОМ")
    print("="*70)
    
    df = basis_df.copy()
    
    # Z-score годового базиса
    df['basis_ma'] = df['basis_annual'].rolling(20).mean()
    df['basis_std'] = df['basis_annual'].rolling(20).std()
    df['z_score'] = (df['basis_annual'] - df['basis_ma']) / df['basis_std']
    
    df = df.dropna(subset=['z_score'])
    
    print(f"  Средний годовой базис: {df['basis_annual'].mean():.1f}%")
    print(f"  Std базиса: {df['basis_annual'].std():.1f}%")
    print(f"  Z-score range: [{df['z_score'].min():.2f}, {df['z_score'].max():.2f}]")
    
    # Торговля
    position = 0  # 0=нет, 1=лонг спред (лонг акция + шорт фьюч), -1=шорт спред
    entry_spot = 0
    entry_fut = 0
    entry_date = None
    trades = []
    
    # Параметры
    ENTRY_LONG_Z = 1.5     # Базис расширился → лонг акция/шорт фьюч
    EXIT_LONG_Z = 0.0      # Базис вернулся к норме → выход
    ENTRY_SHORT_Z = -1.5   # Базис сузился → шорт акция/лонг фьюч
    EXIT_SHORT_Z = 0.0
    STOP_Z = 3.5           # Стоп
    
    for i in range(len(df)):
        row = df.iloc[i]
        z = row['z_score']
        
        if position == 0:
            if z > ENTRY_LONG_Z:
                position = 1
                entry_spot = row['spot']
                entry_fut = row['futures']
                entry_date = df.index[i]
                entry_secid = row['secid']
            elif z < ENTRY_SHORT_Z:
                position = -1
                entry_spot = row['spot']
                entry_fut = row['futures']
                entry_date = df.index[i]
                entry_secid = row['secid']
        
        elif position == 1:
            # Выход если z вернулся к 0 или стоп
            contract_changed = row['secid'] != entry_secid
            if z <= EXIT_LONG_Z or z > STOP_Z or contract_changed:
                # PnL: лонг акция + шорт фьючерс
                pnl_spot = (row['spot'] - entry_spot) * lot_size
                pnl_fut = (entry_fut - row['futures'])
                pnl = pnl_spot + pnl_fut
                commission = (entry_spot + row['spot']) * lot_size * 0.0003 + 4
                net = pnl - commission
                days = (df.index[i] - entry_date).days
                
                trades.append({
                    'type': 'LONG_SPREAD',
                    'entry': entry_date.date(),
                    'exit': df.index[i].date(),
                    'days': max(days, 1),
                    'entry_z': ENTRY_LONG_Z,
                    'exit_z': z,
                    'pnl': net,
                    'reason': 'stop' if z > STOP_Z else ('roll' if contract_changed else 'target'),
                })
                position = 0
        
        elif position == -1:
            contract_changed = row['secid'] != entry_secid
            if z >= EXIT_SHORT_Z or z < -STOP_Z or contract_changed:
                # PnL: шорт акция + лонг фьючерс
                pnl_spot = (entry_spot - row['spot']) * lot_size
                pnl_fut = (row['futures'] - entry_fut)
                pnl = pnl_spot + pnl_fut
                commission = (entry_spot + row['spot']) * lot_size * 0.0003 + 4
                net = pnl - commission
                days = (df.index[i] - entry_date).days
                
                trades.append({
                    'type': 'SHORT_SPREAD',
                    'entry': entry_date.date(),
                    'exit': df.index[i].date(),
                    'days': max(days, 1),
                    'entry_z': ENTRY_SHORT_Z,
                    'exit_z': z,
                    'pnl': net,
                    'reason': 'stop' if z < -STOP_Z else ('roll' if contract_changed else 'target'),
                })
                position = 0
    
    if not trades:
        print("  ❌ Нет сделок")
        return [], 0
    
    # Масштабируем PnL на капитал
    # 1 пара = ~spot*lot стоимость. На капитал можно купить N пар
    avg_spot = df['spot'].mean()
    cost_per_pair = avg_spot * lot_size + avg_spot * lot_size * 0.15  # акция + ГО
    n_lots = int(capital * 0.5 / cost_per_pair)  # 50% капитала на активную компоненту
    
    print(f"\n  Капитал: {capital:,} ₽ | ~{n_lots} лотов | Стоимость пары: {cost_per_pair:,.0f} ₽")
    print(f"\n  {'Тип':14s} | {'Вход':>10s} | {'Выход':>10s} | {'Дн':>3s} | {'PnL/лот':>10s} | {'PnL total':>12s} | {'Причина':>7s}")
    print(f"  {'-'*80}")
    
    total_pnl = 0
    wins = 0
    
    for t in trades:
        scaled_pnl = t['pnl'] * n_lots
        total_pnl += scaled_pnl
        if scaled_pnl > 0:
            wins += 1
        
        print(f"  {t['type']:14s} | {str(t['entry']):>10s} | {str(t['exit']):>10s} | {t['days']:3d} | "
              f"{t['pnl']:+10.0f} | {scaled_pnl:+12,.0f} ₽ | {t['reason']:>7s}")
    
    total_days = (df.index[-1] - df.index[0]).days
    annual_return = total_pnl / capital * 365 / total_days * 100
    win_rate = wins / len(trades) * 100
    avg_days = np.mean([t['days'] for t in trades])
    
    print(f"\n  📊 ИТОГО активный:")
    print(f"     Сделок: {len(trades)} | Win rate: {win_rate:.0f}% | Avg holding: {avg_days:.0f} дней")
    print(f"     PnL: {total_pnl:+,.0f} ₽ за {total_days} дней = {annual_return:+.1f}%/год")
    
    return trades, total_pnl


def strategy_combined_optimized(basis_df, capital=10_000_000, lot_size=100):
    """
    Стратегия 3: Оптимизированная комбинация
    - Всегда держим кэрри-позицию (базовый доход)
    - Увеличиваем/уменьшаем размер при аномалиях базиса
    - Параметры оптимизированы grid search
    """
    print("\n" + "="*70)
    print("СТРАТЕГИЯ 3: КОМБИНИРОВАННАЯ (КЭРРИ + АКТИВНАЯ)")
    print("="*70)
    
    df = basis_df.copy()
    df['basis_ma20'] = df['basis_annual'].rolling(20).mean()
    df['basis_ma60'] = df['basis_annual'].rolling(60).mean()
    df['basis_std20'] = df['basis_annual'].rolling(20).std()
    df['z20'] = (df['basis_annual'] - df['basis_ma20']) / df['basis_std20']
    df = df.dropna(subset=['z20', 'basis_ma60'])
    
    avg_spot = df['spot'].mean()
    cost_per_pair = avg_spot * lot_size  # Стоимость акций в 1 паре
    go_per_pair = avg_spot * lot_size * 0.15  # ГО фьючерса
    
    # Базовый размер: 60% капитала в кэрри
    base_lots = int(capital * 0.6 / (cost_per_pair + go_per_pair))
    # Активный доп размер: до +40% капитала
    active_max_lots = int(capital * 0.4 / (cost_per_pair + go_per_pair))
    
    print(f"  Базовая позиция (кэрри): {base_lots} лотов")
    print(f"  Активный макс: +{active_max_lots} лотов")
    
    # Sweep разных параметров
    best_result = None
    best_params = None
    
    for entry_z in [1.0, 1.5, 2.0]:
        for exit_z in [-0.5, 0.0, 0.5]:
            for add_z in [2.0, 2.5, 3.0]:
                daily_pnl = []
                active_lots = 0
                entry_prices = []
                
                for i in range(1, len(df)):
                    row = df.iloc[i]
                    prev = df.iloc[i-1]
                    z = row['z20']
                    
                    # Кэрри PnL (всегда в позиции)
                    spot_chg = (row['spot'] - prev['spot']) * lot_size * base_lots
                    fut_chg = -(row['futures'] - prev['futures']) * base_lots  # шорт фьючерс
                    carry_pnl = spot_chg + fut_chg
                    
                    # Активная компонента
                    active_pnl = 0
                    
                    # Вход/увеличение при расширении базиса
                    if z > entry_z and active_lots < active_max_lots:
                        add = min(active_max_lots - active_lots, max(1, active_max_lots // 3))
                        active_lots += add
                        entry_prices.append((row['spot'], row['futures'], add))
                    
                    # Дополнительное увеличение при сильном расширении
                    if z > add_z and active_lots < active_max_lots:
                        add = min(active_max_lots - active_lots, max(1, active_max_lots // 3))
                        active_lots += add
                        entry_prices.append((row['spot'], row['futures'], add))
                    
                    # Выход при сужении
                    if z < exit_z and active_lots > 0:
                        for es, ef, lots in entry_prices:
                            active_pnl += (row['spot'] - es) * lot_size * lots  # лонг акция
                            active_pnl += (ef - row['futures']) * lots  # шорт фьючерс
                        active_lots = 0
                        entry_prices = []
                    
                    # Текущая MTM для активных позиций
                    if active_lots > 0 and i == len(df) - 1:
                        for es, ef, lots in entry_prices:
                            active_pnl += (row['spot'] - es) * lot_size * lots
                            active_pnl += (ef - row['futures']) * lots
                    
                    daily_pnl.append(carry_pnl + active_pnl)
                
                total = sum(daily_pnl)
                days = (df.index[-1] - df.index[0]).days
                annual = total / capital * 365 / days * 100
                
                if best_result is None or total > best_result:
                    best_result = total
                    best_params = (entry_z, exit_z, add_z, annual)
    
    entry_z, exit_z, add_z, annual = best_params
    print(f"\n  🏆 Лучшие параметры: entry_z={entry_z}, exit_z={exit_z}, add_z={add_z}")
    print(f"  Годовая доходность: {annual:.1f}%")
    
    # Детальный прогон с лучшими параметрами
    print(f"\n  Детальный прогон:")
    
    active_lots = 0
    entry_prices = []
    monthly_pnl = defaultdict(float)
    total_carry = 0
    total_active = 0
    trade_count = 0
    
    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        z = row['z20']
        month_key = df.index[i].strftime('%Y-%m')
        
        # Кэрри
        carry = ((row['spot'] - prev['spot']) * lot_size * base_lots + 
                -(row['futures'] - prev['futures']) * base_lots)
        total_carry += carry
        monthly_pnl[month_key] += carry
        
        # Активная
        if z > entry_z and active_lots < active_max_lots:
            add = min(active_max_lots - active_lots, max(1, active_max_lots // 3))
            active_lots += add
            entry_prices.append((row['spot'], row['futures'], add))
            trade_count += 1
        
        if z > add_z and active_lots < active_max_lots:
            add = min(active_max_lots - active_lots, max(1, active_max_lots // 3))
            active_lots += add
            entry_prices.append((row['spot'], row['futures'], add))
        
        if z < exit_z and active_lots > 0:
            for es, ef, lots in entry_prices:
                pnl = (row['spot'] - es) * lot_size * lots + (ef - row['futures']) * lots
                total_active += pnl
                monthly_pnl[month_key] += pnl
            active_lots = 0
            entry_prices = []
    
    # Закрытие незавершённых
    if active_lots > 0:
        row = df.iloc[-1]
        for es, ef, lots in entry_prices:
            pnl = (row['spot'] - es) * lot_size * lots + (ef - row['futures']) * lots
            total_active += pnl
    
    total_pnl = total_carry + total_active
    total_days = (df.index[-1] - df.index[0]).days
    
    # Комиссии (грубо)
    total_volume = avg_spot * lot_size * base_lots * 2 * 8  # 8 ротаций кэрри
    total_volume += avg_spot * lot_size * active_max_lots * 2 * trade_count  # активные
    commission = total_volume * 0.0003 + trade_count * 4 + 8 * 4
    
    gross_annual = total_pnl / capital * 365 / total_days * 100
    net_pnl = total_pnl - commission
    net_annual = net_pnl / capital * 365 / total_days * 100
    net_after_tax = net_pnl * 0.87  # НДФЛ 13%
    net_tax_annual = net_after_tax / capital * 365 / total_days * 100
    
    print(f"\n  {'='*50}")
    print(f"  📊 ИТОГО КОМБИНИРОВАННАЯ СТРАТЕГИЯ (GAZP):")
    print(f"  {'='*50}")
    print(f"  Период: {df.index[0].date()} — {df.index[-1].date()} ({total_days} дней)")
    print(f"  Капитал: {capital:,} ₽")
    print(f"  Кэрри: {base_lots} лотов | Активный макс: {active_max_lots} лотов")
    print(f"  Активных сделок: {trade_count}")
    print(f"")
    print(f"  PnL кэрри:   {total_carry:+14,.0f} ₽")
    print(f"  PnL активный: {total_active:+14,.0f} ₽")
    print(f"  PnL gross:    {total_pnl:+14,.0f} ₽  ({gross_annual:.1f}%/год)")
    print(f"  Комиссии:     {-commission:+14,.0f} ₽")
    print(f"  PnL net:      {net_pnl:+14,.0f} ₽  ({net_annual:.1f}%/год)")
    print(f"  После НДФЛ:   {net_after_tax:+14,.0f} ₽  ({net_tax_annual:.1f}%/год)")
    
    # Месячная разбивка
    print(f"\n  Месячная разбивка PnL:")
    for month, pnl in sorted(monthly_pnl.items()):
        bar = "█" * max(0, int(pnl / 5000))
        neg_bar = "░" * max(0, int(-pnl / 5000))
        print(f"    {month}: {pnl:+10,.0f} ₽ {bar}{neg_bar}")
    
    # Sharpe
    monthly_vals = [monthly_pnl[k] for k in sorted(monthly_pnl.keys())]
    if len(monthly_vals) > 2:
        monthly_mean = np.mean(monthly_vals)
        monthly_std = np.std(monthly_vals)
        sharpe = monthly_mean / monthly_std * np.sqrt(12) if monthly_std > 0 else 0
        print(f"\n  Sharpe (месячный): {sharpe:.2f}")
        print(f"  Худший месяц: {min(monthly_vals):+,.0f} ₽")
        print(f"  Лучший месяц: {max(monthly_vals):+,.0f} ₽")
    
    return best_params, total_pnl


def run_multi_instrument(capital=10_000_000):
    """Прогон по всем инструментам"""
    print("\n" + "="*70)
    print("МУЛЬТИ-ИНСТРУМЕНТНЫЙ АНАЛИЗ")
    print("="*70)
    
    instruments = {
        'GAZP': ('GZ', 100),
        'SBER': ('SR', 100),
        'ROSN': ('RN', 100),
        'ALRS': ('AL', 100),
        'TATN': ('TT', 100),
    }
    
    results = {}
    
    for stock, (fut_prefix, lot_size) in instruments.items():
        print(f"\n{'─'*50}")
        print(f"  Загрузка {stock}...")
        
        spot = fetch_candles(stock, 'TQBR', 'stock', 'shares')
        if spot.empty:
            continue
        
        futures = {}
        for year in [4, 5, 6, 7]:
            for mc in MONTH_CODES:
                secid = f"{fut_prefix}{mc}{year}"
                month = MONTHS[mc]
                full_year = 2020 + year
                expiry = datetime(full_year, month, 15)
                fut = fetch_candles(secid, 'RFUD', 'futures', 'forts')
                if not fut.empty and len(fut) > 5:
                    futures[secid] = {'data': fut, 'expiry': expiry, 'month': month, 'year': full_year}
        
        if not futures:
            continue
        
        basis_df = build_continuous_basis(spot, futures, lot_size)
        if len(basis_df) < 80:
            continue
        
        # Quick combined strategy
        df = basis_df.copy()
        df['basis_ma20'] = df['basis_annual'].rolling(20).mean()
        df['basis_std20'] = df['basis_annual'].rolling(20).std()
        df['z20'] = (df['basis_annual'] - df['basis_ma20']) / df['basis_std20']
        df = df.dropna(subset=['z20'])
        
        avg_spot = df['spot'].mean()
        cost_pair = avg_spot * lot_size * 1.15
        base_lots = int(capital * 0.6 / cost_pair)
        
        # Simple carry PnL
        total_carry = 0
        for i in range(1, len(df)):
            carry = ((df.iloc[i]['spot'] - df.iloc[i-1]['spot']) * lot_size * base_lots + 
                    -(df.iloc[i]['futures'] - df.iloc[i-1]['futures']) * base_lots)
            total_carry += carry
        
        days = (df.index[-1] - df.index[0]).days
        carry_annual = total_carry / capital * 365 / days * 100
        avg_basis = df['basis_annual'].mean()
        basis_std = df['basis_annual'].std()
        
        results[stock] = {
            'carry_pnl': total_carry,
            'carry_annual': carry_annual,
            'avg_basis': avg_basis,
            'basis_std': basis_std,
            'base_lots': base_lots,
            'days': days,
        }
        
        print(f"  {stock}: carry={carry_annual:.1f}%/год | avg basis={avg_basis:.1f}% | std={basis_std:.1f}% | {base_lots} лотов")
    
    print(f"\n{'='*70}")
    print("📊 ИТОГО — РЕЙТИНГ ИНСТРУМЕНТОВ ДЛЯ АРБИТРАЖА")
    print(f"{'='*70}")
    
    sorted_results = sorted(results.items(), key=lambda x: -x[1]['carry_annual'])
    for stock, r in sorted_results:
        print(f"  {stock:5s}: carry {r['carry_annual']:+6.1f}%/год | basis {r['avg_basis']:+5.1f}% ± {r['basis_std']:.1f}% | {r['base_lots']} лотов")
    
    return results


# === MAIN ===
if __name__ == '__main__':
    print("🚀 АКТИВНЫЙ АРБИТРАЖ — ДЕТАЛЬНЫЙ АНАЛИЗ")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   Капитал: 10,000,000 ₽")
    print()
    
    # 1. Загрузка GAZP
    spot, futures = load_all_data()
    
    # 2. Непрерывный базис
    basis_df = build_continuous_basis(spot, futures)
    print(f"\n📊 Непрерывный базис: {len(basis_df)} дней")
    print(f"   Годовой базис: {basis_df['basis_annual'].mean():.1f}% ± {basis_df['basis_annual'].std():.1f}%")
    
    # 3. Стратегия 1: Пассивный кэрри
    carry_trades, carry_pnl = strategy_passive_carry(basis_df)
    
    # 4. Стратегия 2: Активная торговля базисом
    active_trades, active_pnl = strategy_active_basis(basis_df)
    
    # 5. Стратегия 3: Комбинированная
    params, combined_pnl = strategy_combined_optimized(basis_df)
    
    # 6. Мульти-инструмент
    multi_results = run_multi_instrument()
    
    print("\n✅ Анализ завершён.")
