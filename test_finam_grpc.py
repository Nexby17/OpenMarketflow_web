#!/usr/bin/env python3
"""Тест Finam Trade API — проверяем все инструменты, MIC коды, подписки"""
import os, json, time, sys
import grpc
from google.protobuf import timestamp_pb2

# Загружаем токен
with open('/root/.env') as f:
    for line in f:
        if '=' in line:
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

TOKEN = os.environ.get('FINAM_TOKEN', '')
if not TOKEN:
    print("❌ FINAM_TOKEN не найден")
    sys.exit(1)

print(f"✅ Токен: {TOKEN[:4]}...{TOKEN[-4:]} ({len(TOKEN)} символов)")

# gRPC подключение
SERVER = 'api.finam.ru:443'
creds = grpc.ssl_channel_credentials()
channel = grpc.secure_channel(SERVER, creds)

# Импортируем proto (используем grpcio-tools напрямую нельзя, используем REST для теста)
import urllib.request, urllib.error

BASE = 'https://api.finam.ru'

def api_call(method, path, body=None):
    """REST вызов к Finam API"""
    url = f"{BASE}{path}"
    headers = {'Content-Type': 'application/json'}
    
    if method == 'POST':
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    else:
        req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {'error': e.code, 'body': body}

# 1. Получаем JWT
print("\n=== 1. AUTH ===")
auth = api_call('POST', '/v1/sessions', {'secret': TOKEN})
if 'token' not in auth:
    print(f"❌ Auth failed: {auth}")
    sys.exit(1)
JWT = auth['token']
print(f"✅ JWT получен: {JWT[:20]}...")

def auth_call(method, path, body=None):
    url = f"{BASE}{path}"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {JWT}'
    }
    if method == 'POST':
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    else:
        req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {'error': e.code, 'body': body}

# 2. Список бирж
print("\n=== 2. БИРЖИ ===")
exchanges = auth_call('GET', '/v1/exchanges')
if 'exchanges' in exchanges:
    for ex in exchanges['exchanges']:
        print(f"  {ex['mic']}: {ex.get('name', '?')}")
else:
    print(f"  ❌ {exchanges}")

# 3. Время сервера
print("\n=== 3. ВРЕМЯ СЕРВЕРА ===")
clock = auth_call('GET', '/v1/assets/clock')
print(f"  {clock}")

# 4. Аккаунты
print("\n=== 4. АККАУНТЫ ===")
details = api_call('POST', '/v1/sessions/details', {'token': JWT})
print(f"  Account IDs: {details.get('account_ids', '?')}")
account_id = details.get('account_ids', [''])[0] if details.get('account_ids') else ''
print(f"  Используем: {account_id}")

# 5. Тест разных символов и MIC
print("\n=== 5. ТЕСТ СИМВОЛОВ ===")
test_symbols = [
    # Фьючерсы — пробуем разные MIC
    ('SiM6', 'RTSX'), ('SiM6', 'MISX'), ('SiM6', 'MOEX'), ('SiM6', 'XMOS'),
    ('SiU6', 'RTSX'), ('SiU6', 'MISX'),
    ('BRM6', 'RTSX'), ('BRM6', 'MISX'),
    ('GDM6', 'RTSX'), ('GDM6', 'MISX'),
    # Акции
    ('SBER', 'MISX'), ('SBER', 'RTSX'),
    ('GAZP', 'MISX'),
    # Другие форматы фьючерсов
    ('Si-6.26', 'RTSX'), ('Si-6.26', 'MISX'),
    ('SiM2026', 'RTSX'), ('SiM2026', 'MISX'),
]

for ticker, mic in test_symbols:
    symbol = f"{ticker}@{mic}"
    result = auth_call('GET', f'/v1/assets/{symbol}?account_id={account_id}')
    if 'error' in result:
        err = json.loads(result['body']) if isinstance(result['body'], str) else result['body']
        msg = err.get('message', str(err))[:60]
        print(f"  ❌ {symbol:25s} → {msg}")
    else:
        print(f"  ✅ {symbol:25s} → board={result.get('board','?'):8s} type={result.get('type','?'):10s} name={result.get('name','?')[:30]}")

# 6. Тест котировок для рабочих символов
print("\n=== 6. КОТИРОВКИ (последняя цена) ===")
working_symbols = []
for ticker, mic in test_symbols:
    symbol = f"{ticker}@{mic}"
    result = auth_call('GET', f'/v1/assets/{symbol}?account_id={account_id}')
    if 'error' not in result:
        working_symbols.append(symbol)

for symbol in working_symbols[:6]:
    quote = auth_call('GET', f'/v1/instruments/{symbol}/quotes/latest')
    if 'quote' in quote:
        q = quote['quote']
        print(f"  {symbol:25s} → last={q.get('last',{}).get('value','?'):>10s}  bid={q.get('bid',{}).get('value','?'):>10s}  ask={q.get('ask',{}).get('value','?'):>10s}")
    else:
        print(f"  {symbol:25s} → ❌ {str(quote)[:60]}")

# 7. Тест стакана
print("\n=== 7. СТАКАН ===")
for symbol in working_symbols[:3]:
    ob = auth_call('GET', f'/v1/instruments/{symbol}/orderbook')
    if 'orderbook' in ob:
        rows = ob['orderbook'].get('rows', [])
        print(f"  {symbol:25s} → {len(rows)} уровней")
        for r in rows[:3]:
            price = r.get('price', {}).get('value', '?')
            buy = r.get('buy_size', {}).get('value', '') if 'buy_size' in r else ''
            sell = r.get('sell_size', {}).get('value', '') if 'sell_size' in r else ''
            print(f"    price={price:>10s}  bid={buy:>6s}  ask={sell:>6s}")
        if len(rows) > 3:
            print(f"    ... ещё {len(rows)-3} уровней")
    else:
        print(f"  {symbol:25s} → ❌ {str(ob)[:60]}")

# 8. Свечи
print("\n=== 8. СВЕЧИ (5 мин, последние) ===")
from datetime import datetime, timezone, timedelta
now = datetime.now(timezone.utc)
fr = (now - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%SZ')
to = now.strftime('%Y-%m-%dT%H:%M:%SZ')

for symbol in working_symbols[:3]:
    bars = auth_call('GET', f'/v1/instruments/{symbol}/bars?timeframe=TIME_FRAME_M5&interval.start_time={fr}&interval.end_time={to}')
    if 'bars' in bars:
        barlist = bars['bars']
        print(f"  {symbol:25s} → {len(barlist)} свечей")
        if barlist:
            last = barlist[-1]
            print(f"    Последняя: O={last.get('open',{}).get('value','?')} H={last.get('high',{}).get('value','?')} L={last.get('low',{}).get('value','?')} C={last.get('close',{}).get('value','?')}")
    else:
        print(f"  {symbol:25s} → ❌ {str(bars)[:60]}")

print("\n✅ Тест завершён")
