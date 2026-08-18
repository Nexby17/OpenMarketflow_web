// === Auth Check ===
(function() {
    // Check if authenticated
    fetch('/api/me', { credentials: 'include' })
        .then(r => { if (r.status === 401) window.location.href = '/login.html'; });
    
    // Intercept all fetch calls — redirect to login on 401
    const origFetch = window.fetch;
    window.fetch = function(...args) {
        return origFetch.apply(this, args).then(res => {
            if (res.status === 401) {
                window.location.href = '/login.html';
                return new Promise(() => {});
            }
            return res;
        });
    };
})();

// === OpenMarketflow Web UI ===

let connection = null;
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let sarSeries = null;
let emaSeries = null;
let stdSeries = null;
let positionSeries = null;
let buyOrdersSeries = null;
let sellOrdersSeries = null;
let isConnected = false;
window._signalRConnected = false;  // SignalR push connection state
// MAX_LOG defined in helpers.js

// === Tabs ===
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.tab).classList.add('active');
        if (tab.dataset.tab === 'orderbook') { switchOrderBookInstrument(); }
    });
});

// === SignalR ===
function initSignalR() {
    connection = new signalR.HubConnectionBuilder()
        .withUrl('/trading')
        .withAutomaticReconnect([0, 2000, 5000, 10000, 30000])
        .build();

    connection.on('OnStatusUpdate', status => {
        updateStatus(status);
    });

    connection.on('OnLogMessage', (time, level, msg) => {
        addLog(time, level, msg);
    });

    connection.on('OnTradeExecuted', trade => {
        addLog(new Date().toLocaleTimeString(), 'TRADE', 
            `${trade.direction} ${trade.volume}x ${trade.ticker} @ ${trade.price?.toFixed(2)} | ${trade.strategy}`);
    });

    connection.on('OnSignalGenerated', signal => {
        addLog(new Date().toLocaleTimeString(), 'INFO', 
            `Сигнал: ${signal.direction} ${signal.ticker} @ ${signal.price?.toFixed(2)} — ${signal.reason}`);
    });

    connection.on('OnPositionUpdated', pos => {
        updatePosition(pos);
    });

    connection.on('OnEquityUpdate', equity => {
        if (el('monEquity')) el('monEquity').textContent = fmt(equity);
    });

    connection.on('OnOrderBookUpdate', snapshot => {
        // SignalR PUSH - real-time orderbook updates
        if (snapshot) {
            window._lastObData = snapshot;
            renderOrderBook(snapshot);
        }
    });

    connection.on('OnQuoteUpdate', quote => {
        // SignalR PUSH - real-time quote updates
        if (quote) {
            updateQuote(quote);
            if (quote.last && window.ChartController && window.ChartController.isReady()) {
                window.ChartController.applyTick(quote.ticker || el('obInstrument')?.value, quote.last);
            }
        }
    });

    connection.on('OnError', msg => {
        addLog(new Date().toLocaleTimeString(), 'ERROR', msg);
    });

    connection.onreconnecting(() => {
        window._signalRConnected = false;
        el('statusIndicator').className = 'status-dot yellow';
        el('statusText').textContent = 'Reconnecting...';
        addLog(nowTime(), 'WARN', 'SignalR reconnecting - REST fallback active');
    });

    connection.onreconnected(() => {
        window._signalRConnected = true;
        el('statusIndicator').className = 'status-dot green';
        el('statusText').textContent = 'Connected';
        addLog(nowTime(), 'INFO', 'SignalR restored - push active');
        fetchStatus();
        var ticker = el('obInstrument')?.value;
        if (ticker) { connection.invoke('SubscribeQuotes', ticker).catch(()=>{}); connection.invoke('SubscribeOrderBook', ticker).catch(()=>{}); }
    });

    connection.onclose(() => {
        window._signalRConnected = false;
        el('statusIndicator').className = 'status-dot red';
        el('statusText').textContent = 'Disconnected';
        addLog(nowTime(), 'ERROR', 'SignalR disconnected - REST polling fallback');
    });

    connection.start()
        .then(() => {
            window._signalRConnected = true;
            el('statusIndicator').className = 'status-dot green';
            el('statusText').textContent = 'Connected to server';
            addLog(nowTime(), 'INFO', 'SignalR connected - push active');
            fetchStatus();
            var ticker = el('obInstrument')?.value;
            if (ticker) { connection.invoke('SubscribeQuotes', ticker).catch(()=>{}); connection.invoke('SubscribeOrderBook', ticker).catch(()=>{}); }
        })
        .catch(err => {
            el('statusIndicator').className = 'status-dot red';
            el('statusText').textContent = 'Ошибка подключения';
            addLog(nowTime(), 'ERROR', `SignalR: ${err.message}`);
        });
}

// === Connector Switch ===
let _activeConnector = 'Finam';

async function switchConnector(name) {
    _activeConnector = name;
    el('brokerStatus').textContent = '⚪ ' + name + ' выбран';
}

async function loadConnectorStatus() {
    try {
        const resp = await fetch('/api/connectors');
        const data = await resp.json();
        _activeConnector = data.active || 'Finam';
        const sel = el('brokerSelect');
        if (sel) sel.value = _activeConnector;
    } catch (e) { console.error('[CONNECTOR]', e); }
}

// === Actions ===
async function connectBroker() {
    const connector = _activeConnector;
    const token = localStorage.getItem('finamToken') || '';
    if (!token) {
        addLog(nowTime(), 'ERROR', '⚠ Токен Финам не указан. Укажите во вкладке Настройки.');
        return;
    }
    addLog(nowTime(), 'INFO', `🔌 Подключение к ${connector}...`);
    el('brokerStatus').textContent = '⏳ Подключаюсь...';
    try {
        const resp = await fetch('/api/connectors/switch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ connector, token })
        });
        const data = await resp.json();
        if (resp.ok) {
            isConnected = true;
            el('brokerStatus').textContent = `✅ ${connector} подключён`;
            addLog(nowTime(), 'INFO', `✅ ${connector} подключён`);
            fetchStatus();
        } else {
            el('brokerStatus').textContent = '❌ Ошибка';
            addLog(nowTime(), 'ERROR', `❌ ${data.error || 'Не удалось подключиться'}`);
        }
    } catch (e) {
        el('brokerStatus').textContent = '❌ Ошибка';
        addLog(nowTime(), 'ERROR', `❌ ${e.message}`);
    }
}

async function connectTransaq() {
    const login = el('txLogin')?.value || '';
    const password = el('txPassword')?.value || '';
    const host = el('txHost')?.value || 'tr.finam.ru';
    const port = parseInt(el('txPort')?.value) || 39000;
    
    if (!login || !password) {
        addLog(nowTime(), 'ERROR', '⚠ Укажите логин и пароль Transaq');
        return;
    }
    
    // Save to localStorage
    localStorage.setItem('txLogin', login);
    localStorage.setItem('txHost', host);
    localStorage.setItem('txPort', port);
    
    el('txStatus').textContent = '⏳ Подключаюсь...';
    addLog(nowTime(), 'INFO', `📡 Подключение к Transaq ${host}:${port}...`);
    
    try {
        const resp = await fetch('/transaq/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login, password, host, port })
        });
        const data = await resp.json();
        if (resp.ok) {
            el('txStatus').textContent = '🟢 Подключён';
            addLog(nowTime(), 'INFO', `✅ Transaq подключён: ${data.broker}`);
        } else {
            el('txStatus').textContent = '🔴 Ошибка';
            addLog(nowTime(), 'ERROR', `❌ Transaq: ${data.error}`);
        }
    } catch (e) {
        el('txStatus').textContent = '🔴 Ошибка';
        addLog(nowTime(), 'ERROR', `❌ Transaq: ${e.message}`);
    }
}

async function disconnectTransaq() {
    try {
        await fetch('/transaq/disconnect', { method: 'POST' });
        el('txStatus').textContent = '⚪ Не подключён';
        addLog(nowTime(), 'INFO', '📡 Transaq отключён');
    } catch (e) {
        addLog(nowTime(), 'ERROR', `❌ ${e.message}`);
    }
}

async function checkHealth() {
    // QUIK Bridge блок удалён — health check только фоновый
    try { await fetch('/transaq/health'); } catch (e) {}
}

async function emergencyStop() {
    if (!connection) return;
    try {
        await connection.invoke('EmergencyStop');
        addLog(nowTime(), 'ERROR', '🔴 ЭКСТРЕННАЯ ОСТАНОВКА');
    } catch (e) {
        addLog(nowTime(), 'ERROR', `❌ ${e.message}`);
    }
}

async function fetchStatus() {
    try {
        const resp = await fetch('/status');
        const status = await resp.json();
        updateStatus(status);
        loadPositions();
        updateChartLines();
        loadOrders();
        loadTrades();
        loadQuotes();
        loadAccounts();
    } catch (e) { }
}

function loadPositions() {
    fetch('/api/positions').then(r => r.json()).then(data => {
        const tbody = el('positionsTable');
        if (!tbody) return;
        if (!data || data.error || !data.length) { tbody.innerHTML = '<tr><td colspan="9" style="color:var(--text-muted)">Нет позиций</td></tr>'; return; }
        tbody.innerHTML = data.map(p => `<tr>
            <td>1225953</td>
            <td><b>${p.ticker}</b></td>
            <td class="${p.dir === 'Buy' ? 'green' : 'red'}">${p.dir === 'Buy' ? 'Лонг' : 'Шорт'}</td>
            <td>${p.qty || 0}</td>
            <td>${p.avgPrice?.toFixed(2) || '—'}</td>
            <td>${p.qty || 0}</td>
        </tr>`).join('');
    }).catch(e => console.error("[ERROR]", e));
}

function updateChartLines() {
    // Chart overlays via ChartController (single source of truth)
    const CC = window.ChartController;
    if (!CC || !CC.isReady()) return;

    // Position line
    fetch('/api/positions').then(r => r.json()).then(positions => {
        window._lastPositions = positions;
        if (!positions || positions.length === 0) {
            CC.setPositionLine(null);
            return;
        }
        if (positions.length === 1) {
            const p = positions[0];
            const price = p.entries?.[0]?.price || p.avgPrice || 0;
            const isLong = p.dir === 'Buy';
            CC.setPositionLine({ price, isLong });
        } else {
            CC.setPositionLine(null);
        }
    }).catch(e => console.error("[ERROR] updateChartLines positions:", e));

    // Order lines
    fetch('/api/orders').then(r => r.json()).then(data => {
        window._lastOrders = (data.orders || data || []);
        const orders = data.orders || data || [];
        const activeOrders = orders
            .filter(o => o.status === 'ORDER_STATUS_NEW')
            .map(o => ({
                price: parseFloat(o.order?.limit_price?.value || 0),
                isBuy: o.order?.side === 'BUY' || o.order?.side === 'SIDE_BUY',
            }))
            .filter(o => o.price > 0);
        CC.refreshOrderLines(activeOrders);
    }).catch(e => console.error('[ERROR] updateChartLines orders:', e));

}

function loadOrders() {
    fetch('/api/orders').then(r => r.json()).then(data => {
        const tbody = el('ordersTable');
        if (!tbody) return;
        // Finam REST format: { orders: [{ order: { symbol, side, quantity, limit_price }, status, ... }] }
        const orders = data.orders || data;
        if (!orders || (Array.isArray(orders) && orders.length === 0) || (orders.length === 0)) {
            tbody.innerHTML = '<tr><td colspan="7" style="color:var(--text-muted)">Нет заявок</td></tr>';
            return;
        }
        tbody.innerHTML = orders.map(o => {
            const ord = o.order || o;
            const ticker = (ord.symbol || o.ticker || '').split('@')[0];
            const side = ord.side || o.dir || '';
            const qty = ord.quantity?.value || o.qty || 0;
            const price = ord.limit_price?.value || o.price || '';
            const status = o.status || ord.status || '';
            const filled = o.executed_quantity?.value || o.filled || 0;
            const oid = o.order_id || o.id || '';
            return `<tr>
                <td>${oid.slice(-6)}</td>
                <td><b>${ticker}</b></td>
                <td class="${side.includes('BUY') || side === 'Buy' ? 'green' : 'red'}">${side.includes('BUY') || side === 'Buy' ? 'Покупка' : 'Продажа'}</td>
                <td>${parseFloat(qty)}</td>
                <td>${price ? parseFloat(price).toFixed(0) : 'MKT'}</td>
                <td>${parseFloat(filled)}/${parseFloat(qty)}</td>
                <td>${status.replace('ORDER_STATUS_','')}</td>
            </tr>`;
        }).join('');
    }).catch(e => console.error("[ERROR]", e));
}

function loadTrades() {
    fetch('/api/trades').then(r => r.json()).then(data => {
        const tbody = el('tradesTable');
        if (!tbody) return;
        const trades = data.trades || data;
        if (!trades || (Array.isArray(trades) && trades.length === 0)) {
            tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-muted)">Нет сделок за сегодня</td></tr>';
            return;
        }
        tbody.innerHTML = trades.map(t => {
            const ticker = (t.symbol || '').split('@')[0];
            const side = t.side || '';
            const price = t.price?.value || t.price || 0;
            const size = t.size?.value || t.size || 0;
            const ts = t.timestamp || t.time || '';
            const time = ts ? new Date(ts).toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
            const comment = t.comment || '';
            return `<tr>
                <td>${time}</td>
                <td><b>${ticker}</b></td>
                <td class="${side.includes('BUY') ? 'green' : 'red'}">${side.includes('BUY') ? 'Покупка' : 'Продажа'}</td>
                <td>${parseFloat(size)}</td>
                <td>${parseFloat(price).toFixed(0)}</td>
                <td>${comment}</td>
            </tr>`;
        }).join('');
    }).catch(e => console.error("[ERROR]", e));
}

function loadQuotes() {
    const tbody = el('quotesTable');
    if (!tbody) return;
    const tickers = ['SiU6','SiZ6','MXU6','GDU6','BRU6','RIU6'];
    if (!tbody.children.length || tbody.dataset.tickerv !== tickers.join(',')) {
        tbody.innerHTML = tickers.map(t => `<tr id="q_${t}"><td><b>${t}</b></td><td class="q_last" style="color:var(--text-muted)">—</td><td class="q_bid" style="color:var(--text-muted)">—</td><td class="q_ask" style="color:var(--text-muted)">—</td><td class="q_vol" style="color:var(--text-muted)">—</td></tr>`).join('');
        tbody.dataset.tickerv = tickers.join(',');
    }
    fetch('/api/quotes').then(r => r.json()).then(data => {
        const items = Array.isArray(data.data) ? data.data : [];
        items.forEach(q => {
            const row = el(`q_${q.ticker}`);
            if (!row) return;
            const cells = row.children;
            if (q.last) cells[1].textContent = q.last.toFixed(0);
            if (q.bid) cells[2].textContent = q.bid.toFixed(0);
            if (q.ask) cells[3].textContent = q.ask.toFixed(0);
            cells[4].textContent = q.volume ? (q.volume >= 1e6 ? (q.volume/1e6).toFixed(1)+'M' : q.volume >= 1e3 ? (q.volume/1e3).toFixed(0)+'K' : q.volume.toFixed(0)) : '—';
        });
    }).catch(() => {});
}

async function loadAccounts() {
    try {
        const resp = await fetch('/api/accounts');
        const data = await resp.json();
        const csharpOk = data?.accounts?.length > 0 && data.accounts[0].balance != null;
        if (csharpOk) {
            _renderAccountsTable(data.accounts);
        } else {
            // Fallback: get accounts with real balances from arb robot (via FinamPy gRPC)
            try {
                const arbResp = await arbPy.fetch('/accounts');
                if (arbResp?.accounts?.length) {
                    _renderAccountsTable(arbResp.accounts);
                }
            } catch(e) {}
        }
        const sel = el('robotAccount');
        if (sel) {
            const src = csharpOk ? data : await (async()=>{try{return await arbPy.fetch('/accounts')}catch(e){return {accounts:[]}}})();
            sel.innerHTML = (src.accounts || []).map(a => `<option value="${a.id}">${a.id} — ${a.name}</option>`).join('');
        }
    } catch (e) {}
}

function _renderAccountsTable(accounts) {
    const tbody = el('accountsTable');
    if (!tbody) return;
    const fmt2 = v => v != null ? v.toLocaleString('ru-RU',{maximumFractionDigits:2}) : '—';
    tbody.innerHTML = accounts.map(a => {
        const balance = a.balance || 0;
        const free = a.free || 0;
        const margin = a.margin || (balance && free ? balance - free : 0);
        const go = a.go || margin;
        const pnlToday = a.pnlToday || 0;
        const pnlTotal = a.pnlTotal || 0;
        return `<tr>
            <td><b>${a.id}</b> ${a.name || ''}</td>
            <td class="accent">${fmt2(balance)} ₽</td>
            <td>${fmt2(free)} ₽</td>
            <td>${fmt2(margin)} ₽</td>
            <td>${fmt2(go)} ₽</td>
            <td class="${pnlToday >= 0 ? 'green' : 'red'}">${pnlToday >= 0 ? '+' : ''}${fmt2(pnlToday)} ₽</td>
            <td class="${pnlTotal >= 0 ? 'green' : 'red'}">${pnlTotal >= 0 ? '+' : ''}${fmt2(pnlTotal)} ₽</td>
        </tr>`;
    }).join('');
}

function updateStatus(s) {
    if (!s) return;
    const v = (a, b) => s[a] ?? s[b] ?? 0;
    
    // Баланс (теперь в таблице счетов через loadAccounts)
    if (el('balance')) el('balance').textContent = fmt(v('balance','Balance'));
    if (el('monBalance')) el('monBalance').textContent = fmt(v('balance','Balance'));
    if (el('monEquity')) el('monEquity').textContent = fmt(v('equity','Equity'));
    
    const pnlToday = v('todayPnL','TodayPnL');
    const pnlTotal = v('totalPnL','TotalPnL');
    if (el('monPnlToday')) {
        el('monPnlToday').textContent = fmtPnl(pnlToday);
        el('monPnlToday').className = 'metric-value ' + (pnlToday > 0 ? 'green' : pnlToday < 0 ? 'red' : '');
    }
    if (el('monPnlTotal')) {
        el('monPnlTotal').textContent = fmtPnl(pnlTotal);
        el('monPnlTotal').className = 'metric-value ' + (pnlTotal > 0 ? 'green' : pnlTotal < 0 ? 'red' : '');
    }
    if (el('monTrades')) el('monTrades').textContent = v('todayTrades','TodayTrades');
    if (el('monPositions')) el('monPositions').textContent = v('openPositions','OpenPositions');
    
    const brokerStatus = s.brokerStatus || s.BrokerStatus || '';
    const connected = s.isConnectedToBroker ?? s.IsConnectedToBroker ?? false;
    
    // Обновление данных роботов в реал-тайм
    robots.forEach(r => {
        if (r.status === 'running' || r.status === 'paused') {
            r.pnlToday = pnlToday;
            r.pnlTotal = pnlTotal;
            r.exchangeStatus = connected ? 'Биржа OK' : 'Нет связи';
            if (s.mode === 'Stopped') r.status = 'stopped';
            else if (s.mode === 'Paused') r.status = 'paused';
            else if (s.mode === 'Running') r.status = 'running';
        }
    });
    if (typeof saveRobots === 'function') { saveRobots(); renderRobots(); }
    if (connected) {
        isConnected = true;
        if (el('brokerStatus')) el('brokerStatus').textContent = '✅ Подключён';
        if (el('startBotBtn')) el('startBotBtn').disabled = false;
        if (el('statusIndicator')) el('statusIndicator').className = 'status-dot green';
        if (el('statusText')) el('statusText').textContent = `Подключён | ${brokerStatus}`;
    }
}

// === Orderbook ===
function renderOrderBook(snapshot) {
    if (!snapshot) return;
    const ladder = el('orderbookLadder');
    const entries = snapshot.entries || snapshot.Entries || [];
    if (entries.length === 0) return;

    const lastPrice = snapshot.lastPrice || snapshot.LastPrice || 0;
    const bestBid = snapshot.bestBid || snapshot.BestBid || 0;
    const bestAsk = snapshot.bestAsk || snapshot.BestAsk || 0;
    if (lastPrice > 0) el('obLast').textContent = lastPrice.toFixed(2);
    if (bestBid > 0) el('obBid').textContent = bestBid.toFixed(2);
    if (bestAsk > 0) el('obAsk').textContent = bestAsk.toFixed(2);
    if (bestBid > 0 && bestAsk > 0) el('obSpread').textContent = (bestAsk - bestBid).toFixed(2);

    const maxVol = Math.max(...entries.map(e => Math.max(e.bidVolume || e.BidVolume || 0, e.askVolume || e.AskVolume || 0)), 1);

    // Get position and orders for overlays
    let posDir = 0, posPrice = 0, posLots = 0;
    try {
        const posData = window._lastPositions;
        if (posData && posData.length > 0) {
            const p = posData[0];
            posDir = (p.dir === 'Buy' || p.direction === 'Long') ? 1 : -1;
            posPrice = p.avgPrice || p.entryPrice || 0;
            posLots = p.volume || p.qty || 0;
        }
    } catch(e) {}
    
    // Get active order prices
    let myBuyPrices = [], mySellPrices = [];
    try {
        const ordData = window._lastOrders;
        (ordData || []).filter(o => o.status === 'ORDER_STATUS_NEW').forEach(o => {
            const price = parseFloat(o.order?.limit_price?.value || 0);
            const isBuy = o.order?.side === 'BUY' || o.order?.side === 'SIDE_BUY';
            if (price > 0) { if (isBuy) myBuyPrices.push(price); else mySellPrices.push(price); }
        });
    } catch(e) {}

    let html = '';
    const sorted = [...entries].sort((a, b) => (b.price || b.Price) - (a.price || a.Price));
    
    const spreadIdx = sorted.findIndex(e => (e.bidVolume || e.BidVolume || 0) > 0);
    const start = Math.max(0, spreadIdx - 25);
    const end = Math.min(sorted.length, spreadIdx + 25);
    const visible = sorted.slice(start, end);

    for (const e of visible) {
        const price = e.price || e.Price || 0;
        const bid = e.bidVolume || e.BidVolume || 0;
        const ask = e.askVolume || e.AskVolume || 0;
        const bidW = (bid / maxVol * 100).toFixed(0);
        const askW = (ask / maxVol * 100).toFixed(0);
        const isLast = Math.abs(price - lastPrice) < 1;
        const isBestBid = Math.abs(price - bestBid) < 1 && bid > 0;
        const isBestAsk = Math.abs(price - bestAsk) < 1 && ask > 0;
        
        // Check if this is position entry price
        const isPos = posPrice > 0 && Math.abs(price - posPrice) < 1;
        // Check if my order is at this price
        const isMyBuy = myBuyPrices.some(p => Math.abs(p - price) < 1);
        const isMySell = mySellPrices.some(p => Math.abs(p - price) < 1);
        
        let rowCls = 'ob-row';
        if (isLast) rowCls += ' ob-spread-row';
        if (bid > 0) rowCls += ' ob-has-bid';
        if (ask > 0) rowCls += ' ob-has-ask';
        if (isBestBid && bid > 0) rowCls += ' ob-best-bid';
        if (isBestAsk && ask > 0) rowCls += ' ob-best-ask';
        if (isMyBuy || isMySell) rowCls += ' ob-my-order';
        
        let priceExtra = '';
        if (isPos) {
            rowCls += ' ob-position-row';
            const pnl = posDir * (lastPrice - posPrice) * posLots;
            const pnlSign = pnl >= 0 ? '+' : '';
            priceExtra = ` <span class="ob-position-pnl" style="color:${pnl >= 0 ? '#2E7D32' : '#C62828'}">${pnlSign}${pnl.toFixed(0)}₽</span>`;
        }
        
        const posIcon = isPos ? (posDir > 0 ? '▲' : '▼') : '';

        html += `<div class="${rowCls}">
            <div class="ob-bid"><div class="ob-bar-bid" style="width:${bidW}%"></div>${bid || ''}</div>
            <div class="ob-price">${posIcon}${priceExtra ? posIcon + ' ' : ''}${price.toFixed(2)}${priceExtra}</div>
            <div class="ob-ask"><div class="ob-bar-ask" style="width:${askW}%"></div>${ask || ''}</div>
        </div>`;
    }
    ladder.innerHTML = html;
}

function updateQuote(q) {
    if (!q) return;
    const last = q.last || q.Last || 0;
    const bid = q.bid || q.Bid || 0;
    const ask = q.ask || q.Ask || 0;
    if (last > 0) el('obLast').textContent = last.toFixed(2);
    if (bid > 0) el('obBid').textContent = bid.toFixed(2);
    if (ask > 0) el('obAsk').textContent = ask.toFixed(2);
    if (bid > 0 && ask > 0) el('obSpread').textContent = (ask - bid).toFixed(2);
}


function updatePosition(pos) {
    // Обновляем роботов
    robots.forEach(r => {
        if (r.status === 'running' || r.status === 'paused') {
            r.position = (pos.direction || pos.Direction) === 'Long' ? 'Лонг' : (pos.direction || pos.Direction) === 'Short' ? 'Шорт' : '—';
            r.lotsOpen = pos.volume || pos.Volume || 0;
            r.go = (r.lotsOpen || 0) * 12000;
        }
    });
    if (typeof saveRobots === 'function') { saveRobots(); renderRobots(); }
    const tbody = el('positionsTable');
    if (!tbody) return;
    
    const ticker = pos.ticker || pos.Ticker || '';
    const dir = pos.direction || pos.Direction || '';
    const qty = pos.volume || pos.Volume || 0;
    const entryPrice = pos.entryPrice || pos.EntryPrice || 0;
    const pnl = pos.pnL || pos.PnL || 0;
    const cls = pnl >= 0 ? 'green' : 'red';
    const dirClass = dir === 'Long' || dir === 'Buy' ? 'green' : 'red';
    const dirText = dir === 'Long' || dir === 'Buy' ? 'Лонг' : dir === 'Short' || dir === 'Sell' ? 'Шорт' : '—';
    
    if (!ticker || qty === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="color:var(--text-muted)">Нет позиций</td></tr>';
        return;
    }
    
    tbody.innerHTML = `<tr>
        <td>1225953</td>
        <td><b>${ticker}</b></td>
        <td class="${dirClass}">${dirText}</td>
        <td>${qty}</td>
        <td>${entryPrice.toFixed(2)}</td>
        <td>${qty}</td>
        <td>${entryPrice.toFixed(2)}</td>
        <td class="${cls}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} ₽</td>
        <td class="${cls}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} ₽</td>
    </tr>`;
}

// === Chart ===
// Chart creation/series ownership moved to modules/chart.js (ChartController).
// app.js keeps read-only legacy refs for rendering queries only.
function initChart() {
    if (chart) return;
    const container = el('chartContainer');
    if (!container || container.offsetWidth === 0) {
        setTimeout(initChart, 100);
        return;
    }
    ChartController.init('chartContainer', { onDbClick: (x, y) => showIndicatorMenu(x, y) }).then(refs => {
        // Legacy read-only refs (DO NOT write to these series from app.js)
        chart = refs.chart;
        candleSeries = refs.candleSeries;
        volumeSeries = refs.volumeSeries;
        sarSeries = refs.sarSeries;
        emaSeries = refs.emaSeries;
        stdSeries = refs.stdSeries;
        positionSeries = refs.positionSeries;
    }).catch(e => console.error('[ChartController] init failed:', e));
}

function loadCandles(ticker, tf) {
    ChartController.loadHistory(ticker, tf).then(r => {
        if (r.aborted) return; // stale - newer request superseded this one
        if (r.error) { addLog(nowTime(), 'ERROR', r.error); return; }
        if (r.empty) {
            addLog(nowTime(), 'INFO', `Нет данных: тикер ${ticker} (${parseInt(tf) || 5}м)`);
            return;
        }
        const cc = el('obCandleCount');
        if (cc) cc.textContent = `${r.count} свечей`;
        addLog(nowTime(), 'INFO', `З? ${ticker}: ${r.count} свечей (${parseInt(tf) || 5}м)`);
        // Overlays: position/order lines + trade markers (context-guarded inside CC)
        updateChartLines();
        loadTradeMarkers(ticker);
    });
}

// === Trade markers on main chart (via ChartController) ===
async function loadTradeMarkers(ticker) {
    if (!window.ChartController || !window.ChartController.isReady()) return;

    try {
        // Only today's trades for live refresh
        const today = new Date().toISOString().slice(0, 10);
        const resp = await fetch(`/api/trades?date=${today}`);
        const raw = await resp.json();
        let trades = raw.trades || (Array.isArray(raw) ? raw : []);

        // Filter by ticker
        const tTicker = (ticker || '').replace('@RTSX', '');
        trades = trades.filter(t => {
            const sym = (t.symbol || '').replace('@RTSX', '');
            return sym.includes(tTicker) || tTicker.includes(sym);
        });

        if (!trades.length) {
            window.ChartController.setMarkers(ticker, []);
            return;
        }

        // Build markers
        const markers = trades.map(t => {
            const isBuy = (t.side || '').includes('BUY');
            const price = t.price?.value ? parseFloat(t.price.value) : parseFloat(t.price || 0);
            const time = t.timestamp || t.time || '';
            const ts = new Date(time).getTime() / 1000;
            const chartTime = ts + 10800;
            return {
                time: chartTime,
                position: isBuy ? 'belowBar' : 'aboveBar',
                color: isBuy ? '#22C55E' : '#EF4444',
                shape: isBuy ? 'arrowUp' : 'arrowDown',
                text: isBuy ? 'B' : 'S',
                price: price,
                size: 1
            };
        }).filter(m => m.time > 0 && !isNaN(m.time));

        window.ChartController.setMarkers(ticker, markers);
    } catch (e) {
        // silently fail
    }
}

function calcSAR(bars, start, step, max) {
    if (bars.length < 2) return [];
    let af = start, isLong = bars[1].c > bars[0].c;
    let sar = isLong ? bars[0].l : bars[0].h;
    let ep = isLong ? bars[0].h : bars[0].l;
    const result = [];
    for (let i = 1; i < bars.length; i++) {
        const prevSar = sar;
        sar = prevSar + af * (ep - prevSar);
        if (isLong) {
            sar = Math.min(sar, bars[i-1].l, i >= 2 ? bars[i-2].l : bars[i-1].l);
            if (bars[i].l < sar) {
                isLong = false; sar = ep; ep = bars[i].l; af = start;
            } else {
                if (bars[i].h > ep) { ep = bars[i].h; af = Math.min(af + step, max); }
            }
        } else {
            sar = Math.max(sar, bars[i-1].h, i >= 2 ? bars[i-2].h : bars[i-1].h);
            if (bars[i].h > sar) {
                isLong = true; sar = ep; ep = bars[i].h; af = start;
            } else {
                if (bars[i].l < ep) { ep = bars[i].l; af = Math.min(af + step, max); }
            }
        }
        result.push({ time: bars[i].t, value: sar });
    }
    return result;
}

function calcEMA(bars, period) {
    if (bars.length < period) return [];
    const k = 2 / (period + 1);
    let ema = 0;
    for (let i = 0; i < period; i++) ema += bars[i].c;
    ema /= period;
    const result = [{ time: bars[period-1].t, value: ema }];
    for (let i = period; i < bars.length; i++) {
        ema = bars[i].c * k + ema * (1 - k);
        result.push({ time: bars[i].t, value: ema });
    }
    return result;
}

function calcSTD(bars, period, mult, sarData) {
    if (bars.length < period) return [];
    const result = [];
    for (let i = period - 1; i < bars.length; i++) {
        let sum = 0, sumSq = 0;
        for (let j = i - period + 1; j <= i; j++) { sum += bars[j].c; sumSq += bars[j].c * bars[j].c; }
        const std = Math.sqrt(sumSq / period - Math.pow(sum / period, 2));
        const sar = sarData.find(s => s.time === bars[i].t);
        if (sar && std > 0) {
            result.push({ time: bars[i].t, value: sar.value + std * mult });
            result.push({ time: bars[i].t, value: sar.value - std * mult });
        }
    }
    return result;
}

// === Меню настроек индикаторов ===
function showIndicatorMenu(x, y) {
    const existing = el('indicatorMenu');
    if (existing) { existing.remove(); return; }
    
    const div = document.createElement('div');
    div.id = 'indicatorMenu';
    div.style.cssText = `position:fixed;top:${Math.min(y, window.innerHeight - 200)}px;left:${Math.min(x, window.innerWidth - 320)}px;z-index:1000;background:#1A1A2E;border:1px solid #3B82F6;border-radius:8px;padding:16px;width:300px;box-shadow:0 4px 16px rgba(0,0,0,.5)`;
    
    // Текущие параметры
    const sarInputs = document.querySelectorAll('#chartContainer');
    div.innerHTML = `
        <div style="display:flex;justify-content:space-between;margin-bottom:12px">
            <span style="color:#F59E0B;font-weight:bold">⚙️ Индикаторы</span>
            <button onclick="el('indicatorMenu').remove()" style="background:none;border:none;color:#9CA3AF;cursor:pointer;font-size:18px">✕</button>
        </div>
        <div style="margin-bottom:8px">
            <div style="color:#FFD700;margin-bottom:4px;font-size:12px">● SAR (Parabolic)</div>
            <div style="display:flex;gap:8px">
                <div><label style="color:#9CA3AF;font-size:11px">Start</label><input id="indSarStart" class="input" type="number" step="0.001" value="${window._sarParams?.start || 0.009}" style="width:60px"></div>
                <div><label style="color:#9CA3AF;font-size:11px">Step</label><input id="indSarStep" class="input" type="number" step="0.001" value="${window._sarParams?.step || 0.01}" style="width:60px"></div>
                <div><label style="color:#9CA3AF;font-size:11px">Max</label><input id="indSarMax" class="input" type="number" step="0.01" value="${window._sarParams?.max || 0.2}" style="width:60px"></div>
            </div>
        </div>
        <div style="margin-bottom:12px">
            <div style="color:#00BFFF;margin-bottom:4px;font-size:12px">● EMA (Exponential MA)</div>
            <div><label style="color:#9CA3AF;font-size:11px">Period</label><input id="indEmaPeriod" class="input" type="number" value="${window._emaPeriod || 30}" style="width:80px"></div>
                <div><label style="color:#9CA3AF;font-size:11px">STD</label><label style="color:#FF6B6B;font-size:11px;margin-left:4px">Period</label><input id="indStdPeriod" class="input" type="number" value="${parseInt(localStorage.getItem('v7_stdPeriod'))||14}" style="width:50px"><label style="color:#FF6B6B;font-size:11px;margin-left:4px">Mult</label><input id="indStdMult" class="input" type="number" step="0.1" value="${parseFloat(localStorage.getItem('v7_stdMult'))||1.5}" style="width:50px"><input id="indStdVisible" type="checkbox" style="width:16px;height:16px;vertical-align:middle" title="Показать STD"></div>
        </div>
        <div style="display:flex;gap:8px">
            <button class="btn btn-primary btn-sm" onclick="applyIndicatorParams()" style="flex:1">✅ Применить</button>
            <button class="btn btn-secondary btn-sm" onclick="el('indicatorMenu').remove()" style="flex:1">Отмена</button>
        </div>
    `;
    document.body.appendChild(div);
}

function applyIndicatorParams() {
    const start = parseFloat(el('indSarStart')?.value) || 0.009;
    const step = parseFloat(el('indSarStep')?.value) || 0.01;
    const max = parseFloat(el('indSarMax')?.value) || 0.2;
    const emaPeriod = parseInt(el('indEmaPeriod')?.value) || 30;
    
    window._sarParams = { start, step, max };
    window._emaPeriod = emaPeriod;
    
    // STD параметры
    const stdPeriod = parseInt(el('indStdPeriod')?.value) || 14;
    const stdMult = parseFloat(el('indStdMult')?.value) || 1.5;
    localStorage.setItem('v7_stdPeriod', stdPeriod);
    localStorage.setItem('v7_stdMult', stdMult);
    if (stdSeries && el('indStdVisible')?.checked) { stdSeries.applyOptions({ visible: true }); } else if (stdSeries) { stdSeries.applyOptions({ visible: false }); }
    
    // Обновляем в стратегии
    fetch('/strategy/grid-mm/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sarStart: start, sarStep: step, sarMax: max, emaPeriod })
    }).then(() => {
        addLog(nowTime(), 'INFO', `⚙️ Параметры стратегии: SAR(${start}/${step}/${max}) EMA(${emaPeriod})`);
    }).catch(e => addLog(nowTime(), 'ERROR', `Ошибка: ${e.message}`));
    
    // Пересчитываем на текущих данных графика
    // Пересчитать индикаторы поверх текущих свечей (через ChartController)
    if (window.ChartController && window.ChartController.isReady()) {
        window.ChartController.recalcIndicators(
            { start, step, max },
            emaPeriod,
            { period: stdPeriod, mult: stdMult }
        );
    }
    el('indicatorMenu')?.remove();
}

// Инициализация параметров
window._sarParams = { start: 0.009, step: 0.01, max: 0.2 };
window._emaPeriod = 30;

function switchOrderBookInstrument() {
    const ticker = el('obInstrument')?.value;
    const tf = el('obTimeframe')?.value;
    // Новый инструмент — сброс живой свечи
    loadQuote(ticker);
    loadCandles(ticker, tf);
    loadOrderBook(ticker);
    // Реал-тайм: котировки + стакан + живой график
    if (window._quoteInterval) clearInterval(window._quoteInterval);
    window._quoteInterval = setInterval(() => {
        const t = el('obInstrument')?.value;
        loadQuote(t);
        updateLivePrice(t);
        // Fast refresh: positions + orders + trades
        Promise.all([
            fetch('/api/positions').then(r=>r.json()).catch(()=>[]),
            fetch('/api/orders').then(r=>r.json()).catch(()=>({orders:[]})),
            fetch(`/api/trades?date=${new Date().toISOString().slice(0,10)}`).then(r=>r.json()).catch(()=>({trades:[]}))
        ]).then(([pos, ord, tradesResp]) => {
            const posChanged = JSON.stringify(window._lastPositions) !== JSON.stringify(pos);
            const ordChanged = JSON.stringify(window._lastOrders) !== JSON.stringify(ord.orders || ord || []);
            window._lastPositions = pos;
            window._lastOrders = ord.orders || ord || [];
            
            // Trade markers — incremental (via ChartController)
            const newTrades = tradesResp.trades || [];
            const newCount = newTrades.length;
            if (newCount !== window._lastTradeCount && window.ChartController && window.ChartController.isReady()) {
                window._lastTradeCount = newCount;
                const tTicker = (t || '').replace('@RTSX', '');
                const markers = newTrades.filter(tr => {
                    const sym = (tr.symbol || '').replace('@RTSX', '');
                    return sym.includes(tTicker) || tTicker.includes(sym);
                }).map(tr => {
                    const isBuy = (tr.side || '').includes('BUY');
                    const price = tr.price?.value ? parseFloat(tr.price.value) : parseFloat(tr.price || 0);
                    const ts = new Date(tr.timestamp || tr.time || '').getTime() / 1000;
                    return { time: ts + 10800, position: isBuy ? 'belowBar' : 'aboveBar', color: isBuy ? '#22C55E' : '#EF4444', shape: isBuy ? 'arrowUp' : 'arrowDown', text: isBuy ? 'B' : 'S', price: price, size: 1 };
                }).filter(m => m.time > 0 && !isNaN(m.time)).sort((a, b) => a.time - b.time);
                window.ChartController.setMarkers(t, markers);
            }
            
            // Re-render orderbook only if changed
            if ((posChanged || ordChanged) && window._lastObData) {
                _renderOrderBookCached();
            }
            // Update chart order lines (via ChartController)
            if (ordChanged && window.ChartController && window.ChartController.isReady()) {
                const activeOrders = window._lastOrders
                    .filter(o => o.status === 'ORDER_STATUS_NEW')
                    .map(o => ({
                        price: parseFloat(o.order?.limit_price?.value || 0),
                        isBuy: o.order?.side === 'BUY' || o.order?.side === 'SIDE_BUY',
                    }))
                    .filter(o => o.price > 0);
                window.ChartController.refreshOrderLines(activeOrders);
            }
        });
    }, 1000);  // Fallback polling (1s) — SignalR push handles real-time when connected
    // Orderbook + candles — 2s
    if (window._slowInterval) clearInterval(window._slowInterval);
    window._slowInterval = setInterval(() => {
        const t = el('obInstrument')?.value;
        loadOrderBook(t);
        updateLiveCandle(t);
        if (t && t.startsWith('Si')) loadStrategyIndicators();
    }, 3000);  // Candle/indicator refresh (3s) — orderbook via SignalR push
    
    // Первая загрузка индикаторов
    loadStrategyIndicators();
    
    // Инициализация графика (один раз)
    if (!chart) {
        initChart();
        if (!chart) setTimeout(() => { initChart(); loadCandles(ticker, tf); }, 300);
    } else {
        loadCandles(ticker, tf);
    }
}

function loadQuote(ticker) {
    if (!ticker) return;
    fetch(`/api/quote?ticker=${ticker}`)
        .then(r => r.json())
        .then(q => {
            if (q.last > 0) el('obLast').textContent = Math.round(q.last);
            if (q.bid > 0) el('obBid').textContent = Math.round(q.bid);
            if (q.ask > 0) el('obAsk').textContent = Math.round(q.ask);
            if (q.spread > 0) el('obSpread').textContent = q.spread.toFixed(0);
        })
        .catch(e => console.error("[ERROR]", e));
}

function loadOrderBook(ticker) {
    if (!ticker) return;
    
    // Загружаем позицию и стакан параллельно
    Promise.all([
        fetch('/api/positions').then(r => r.json()).catch(() => []),
        fetch('/api/orders').then(r => r.json()).catch(() => ({orders:[]})),
        fetch(`/api/orderbook?ticker=${ticker}`).then(r => r.json()),
        fetch(`/api/trades?date=${new Date().toISOString().slice(0,10)}`).then(r => r.json()).catch(() => ({trades:[]}))
    ]).then(([positions, ordersData, data, tradesResp]) => {
        window._recentTrades = (tradesResp.trades || tradesResp || []).filter(t => (t.symbol || t.ticker || '').includes(ticker));
        // Cache orderbook data for fast re-render
        window._lastObData = data;
        // Кэшируем для renderOrderBook
        window._lastPositions = positions;
        window._lastOrders = ordersData.orders || ordersData || [];
        
        // Парсим позицию
        let posDir = 0, posPrice = 0, posLots = 0;
        if (positions && positions.length > 0) {
            const p = positions[0];
            posDir = (p.dir === 'Buy' || p.direction === 'Long') ? 1 : -1;
            posPrice = p.avgPrice || p.entryPrice || 0;
            posLots = p.volume || p.qty || 0;
        }
        
        if (!data.rows || data.rows.length === 0) return;
        const rows = data.rows;
        const maxVol = Math.max(...rows.map(r => Math.max(r.bid || 0, r.ask || 0)), 1);
        window._lastObData.maxVol = maxVol;
            
            // Сортируем по убыванию цены
            rows.sort((a, b) => b.price - a.price);
            
            // Находим границу bid/ask и ограничиваем ±25
            const spreadIdx = rows.findIndex(r => (r.bid || 0) > 0);
            const si = spreadIdx >= 0 ? spreadIdx : Math.floor(rows.length / 2);
            const start = Math.max(0, si - 25);
            const end = Math.min(rows.length, si + 25);
            const visible = rows.slice(start, end);
            
            let html = '';
            const bestBid = Math.max(...visible.filter(r => r.bid > 0).map(r => r.price), 0);
            const bestAsk = Math.min(...visible.filter(r => r.ask > 0).map(r => r.price), 0);
            
            // Свои заявки
            const myOrders = (window._lastOrders || []).filter(o => o.status === 'ORDER_STATUS_NEW');
            const myBuyPrices = myOrders.filter(o => o.order?.side === 'BUY' || o.order?.side === 'SIDE_BUY').map(o => parseFloat(o.order?.limit_price?.value || 0)).filter(p => p > 0);
            const mySellPrices = myOrders.filter(o => o.order?.side === 'SELL' || o.order?.side === 'SIDE_SELL').map(o => parseFloat(o.order?.limit_price?.value || 0)).filter(p => p > 0);
            
            // Добавляем строки своих заявок если их цены не в стакане
            const obPrices = new Set(visible.map(r => Math.round(r.price)));
            for (const p of myBuyPrices) {
                const rp = Math.round(p);
                if (!obPrices.has(rp)) {
                    visible.push({price: p, bid: 0, ask: 0, _myBuy: true});
                    obPrices.add(rp);
                }
            }
            for (const p of mySellPrices) {
                const rp = Math.round(p);
                if (!obPrices.has(rp)) {
                    visible.push({price: p, bid: 0, ask: 0, _mySell: true});
                    obPrices.add(rp);
                }
            }
            visible.sort((a, b) => b.price - a.price);
            
            for (const r of visible) {
                const bidW = ((r.bid || 0) / maxVol * 100).toFixed(0);
                const askW = ((r.ask || 0) / maxVol * 100).toFixed(0);
                const isMyBuy = r._myBuy || myBuyPrices.some(p => Math.abs(p - r.price) < 1);
                const isMySell = r._mySell || mySellPrices.some(p => Math.abs(p - r.price) < 1);
                let cls = 'ob-row';
                if (r.bid > 0) cls += ' ob-has-bid';
                if (r.ask > 0) cls += ' ob-has-ask';
                if (Math.abs(r.price - bestBid) < 1 && r.bid > 0) cls += ' ob-best-bid';
                if (Math.abs(r.price - bestAsk) < 1 && r.ask > 0) cls += ' ob-best-ask';
                if (isMyBuy) cls += ' ob-my-buy';
                if (isMySell) cls += ' ob-my-sell';
                
                // Позиция
                const isPos = posPrice > 0 && Math.abs(r.price - posPrice) < 1;
                if (isPos) cls += ' ob-position-row';
                const posIcon = isPos ? (posDir > 0 ? '▲' : '▼') : '';
                let posHtml = '';
                if (isPos) {
                    const lastPx = parseFloat(el('obLast')?.textContent || 0);
                    const pnl = posDir * (lastPx - posPrice) * posLots;
                    const dirLabel = posDir > 0 ? '▲L' : '▼S';
                    const pnlColor = pnl >= 0 ? '#69f0ae' : '#ff5252';
                    const pnlSign = pnl >= 0 ? '+' : '';
                    posHtml = `<span class="ob-position-pnl"> ${dirLabel}${posLots} ${pnlSign}${pnl.toFixed(0)}₽</span>`;
                }
                
                html += `<div class="${cls}">
                    <div class="ob-bid"><div class="ob-bar-bid" style="width:${bidW}%"></div>${r.bid || ''}${isMyBuy ? ' ◄' : ''}</div>
                    <div class="ob-price">${Math.round(r.price)}${posHtml}</div>
                    <div class="ob-ask"><div class="ob-bar-ask" style="width:${askW}%"></div>${r.ask || ''}${isMySell ? '► ' : ''}</div>
                </div>`;
            }
            el('orderbookLadder').innerHTML = html;
            _renderTradeTape(visible);
        })
        .catch(e => console.error("[ERROR]", e));
}

// Fast re-render orderbook from cached data (no fetch)
function _renderOrderBookCached() {
    const data = window._lastObData;
    if (!data || !data.rows || !data.rows.length) return;
    const rows = data.rows;
    const maxVol = data.maxVol || 1;
    rows.sort((a, b) => b.price - a.price);
    const spreadIdx = rows.findIndex(r => (r.bid || 0) > 0);
    const si = spreadIdx >= 0 ? spreadIdx : Math.floor(rows.length / 2);
    const start = Math.max(0, si - 25);
    const end = Math.min(rows.length, si + 25);
    const visible = rows.slice(start, end);
    const bestBid = Math.max(...visible.filter(r => r.bid > 0).map(r => r.price), 0);
    const bestAsk = Math.min(...visible.filter(r => r.ask > 0).map(r => r.price), 0);
    
    let posDir=0, posPrice=0, posLots=0;
    try { const p=(window._lastPositions||[])[0]; if(p){posDir=(p.dir==='Buy'||p.direction==='Long')?1:-1;posPrice=p.entries?.[0]?.price||p.avgPrice||p.entryPrice||0;posLots=p.volume||p.qty||0;} } catch(e){}
    const myOrders=(window._lastOrders||[]).filter(o=>o.status==='ORDER_STATUS_NEW');
    const myBuyPrices=myOrders.filter(o=>o.order?.side==='BUY'||o.order?.side==='SIDE_BUY').map(o=>parseFloat(o.order?.limit_price?.value||0)).filter(p=>p>0);
    const mySellPrices=myOrders.filter(o=>o.order?.side==='SELL'||o.order?.side==='SIDE_SELL').map(o=>parseFloat(o.order?.limit_price?.value||0)).filter(p=>p>0);
    const obPrices=new Set(visible.map(r=>Math.round(r.price)));
    for(const p of myBuyPrices){const rp=Math.round(p);if(!obPrices.has(rp)){visible.push({price:p,bid:0,ask:0,_myBuy:true});obPrices.add(rp);}}
    for(const p of mySellPrices){const rp=Math.round(p);if(!obPrices.has(rp)){visible.push({price:p,bid:0,ask:0,_mySell:true});obPrices.add(rp);}}
    visible.sort((a,b)=>b.price-a.price);
    
    let html='';
    for(const r of visible){
        const bidW=((r.bid||0)/maxVol*100).toFixed(0);
        const askW=((r.ask||0)/maxVol*100).toFixed(0);
        const isMyBuy=r._myBuy||myBuyPrices.some(p=>Math.abs(p-r.price)<1);
        const isMySell=r._mySell||mySellPrices.some(p=>Math.abs(p-r.price)<1);
        let cls='ob-row';
        if(r.bid>0)cls+=' ob-has-bid';
        if(r.ask>0)cls+=' ob-has-ask';
        if(Math.abs(r.price-bestBid)<1&&r.bid>0)cls+=' ob-best-bid';
        if(Math.abs(r.price-bestAsk)<1&&r.ask>0)cls+=' ob-best-ask';
        if(isMyBuy)cls+=' ob-my-buy';
        if(isMySell)cls+=' ob-my-sell';
        const isPos=posPrice>0&&Math.abs(r.price-posPrice)<1;
        if(isPos)cls+=' ob-position-row';
        const posIcon=isPos?(posDir>0?'▲':'▼'):'';
        let posHtml='';
        if(isPos){const lastPx=parseFloat(el('obLast')?.textContent||0);const pnl=posDir*(lastPx-posPrice)*posLots;const dirLabel=posDir>0?'▲L':'▼S';const pnlSign=pnl>=0?'+':'';posHtml=`<span class="ob-position-pnl"> ${dirLabel}${posLots} ${pnlSign}${pnl.toFixed(0)}₽</span>`;}
        html+=`<div class="${cls}"><div class="ob-bid"><div class="ob-bar-bid" style="width:${bidW}%"></div>${r.bid||''}${isMyBuy?' ◄':''}</div><div class="ob-price">${Math.round(r.price)}${posHtml}</div><div class="ob-ask"><div class="ob-bar-ask" style="width:${askW}%"></div>${r.ask||''}${isMySell?'► ':''}</div></div>`;
    }
    el('orderbookLadder').innerHTML=html;
    _renderTradeTape(visible);
}

function _renderTradeTape(visible) {
    const tape = el('tradeTape');
    if (!tape || !visible || !visible.length) { if (tape) tape.innerHTML = ''; return; }
    
    const minPrice = visible[visible.length-1].price;
    const maxPrice = visible[0].price;
    const priceRange = maxPrice - minPrice || 1;
    const tapeH = tape.offsetHeight || 400;
    
    // PnL линия: от entry до current price
    let posDir = 0, posPrice = 0;
    try {
        const p = (window._lastPositions || [])[0];
        if (p) {
            posDir = (p.dir === 'Buy' || p.direction === 'Long') ? 1 : -1;
            posPrice = p.avgPrice || p.entryPrice || 0;
        }
    } catch(e) {}
    
    if (posPrice === 0) { tape.innerHTML = ''; return; }
    
    const currentPrice = parseFloat(el('obLast')?.textContent || 0);
    if (currentPrice === 0) { tape.innerHTML = ''; return; }
    
    // Позиция на шкале
    const entryPct = ((maxPrice - posPrice) / priceRange) * 100;
    const currentPct = ((maxPrice - currentPrice) / priceRange) * 100;
    const isProfit = posDir * (currentPrice - posPrice) >= 0;
    
    const topPct = Math.min(entryPct, currentPct);
    const heightPct = Math.abs(currentPct - entryPct);
    
    let html = '';
    
    // Entry horizontal line (QScalp style)
    if (posPrice >= minPrice && posPrice <= maxPrice) {
        html += `<div class="pnl-entry" style="top:${entryPct}%"></div>`;
        html += `<div class="pnl-entry-label" style="top:${entryPct}%">${posDir > 0 ? '▲' : '▼'} ${Math.round(posPrice)}</div>`;
    }
    
    // Vertical line from entry to current price
    if (heightPct > 0.1) {
        const pts = Math.round(Math.abs(currentPrice - posPrice));
        html += `<div class="pnl-line ${isProfit ? 'pnl-line-profit' : 'pnl-line-loss'}" style="top:${topPct}%;height:${Math.max(heightPct, 0.5)}%"></div>`;
        // Points label in the middle of the line
        const midPct = topPct + heightPct / 2;
        const ptsColor = isProfit ? '#4CAF50' : '#E91E63';
        const ptsSign = isProfit ? '+' : '-';
        html += `<div class="pnl-pts-label" style="top:${midPct}%;color:${ptsColor}">${ptsSign}${pts}</div>`;
    }
    tape.innerHTML = html;
}

function switchTimeframe() {
    const ticker = el('obInstrument')?.value;
    const tf = el('obTimeframe')?.value;
    // Смена TF = новая история; состояние живёт в ChartController
    loadCandles(ticker, tf);
}

// === Живой график: обновляем последнюю свечу и добавляем новые ===
// Live price/candle updates: state and writes live in ChartController.

// Quote polling (fallback ~1s): DOM labels + CC.applyTick
function updateLivePrice(ticker) {
    if (!ticker) return;
    fetch(`/api/quote?ticker=${ticker}`)
        .then(r => r.json())
        .then(q => {
            if (!q || q.error || !q.last) return;
            const CC = window.ChartController;
            if (CC && CC.isReady()) {
                CC.applyTick(ticker, q.last);
            }
        })
        .catch(e => console.error("[ERROR]", e));
    updateChartLines();
}

// Candle polling (fallback ~3s): fetch last bar + CC.applyCandle
function updateLiveCandle(ticker) {
    if (!ticker) return;
    const tf = parseInt(el('obTimeframe')?.value) || 5;
    fetch(`/api/candles?ticker=${ticker}&tf=${tf}&days=1`)
        .then(r => r.json())
        .then(data => {
            if (!data || data.error || !data.length) return;
            const CC = window.ChartController;
            if (CC && CC.isReady()) {
                CC.applyCandle(ticker, tf, data[data.length - 1]);
            }
        })
        .catch(e => console.error("[ERROR]", e));
}

// === Strategy Indicators on Chart ===
function loadStrategyIndicators() {
    const ticker = el('obInstrument')?.value || '';
    if (!ticker.startsWith('Si')) return;
    
    // Try v7 first
    fetch('/strategy/grid-mm-v7/chart-data')
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                // Fallback to v6
                loadV6Indicators();
                return;
            }
            if (data.current) {
                updateIndicatorMetrics({
                    sar: data.current.sar,
                    ema: data.current.ema,
                    std: data.current.std,
                    posDir: data.current.posDir,
                    entryPrice: data.current.entryPrice,
                    lots: data.current.lots,
                    totalPnl: data.current.totalPnl,
                    totalTrades: data.current.roundTrips
                });
            }
        })
        .catch(() => loadV6Indicators());
}

function loadV6Indicators() {
    const ticker = el('obInstrument')?.value || '';
    // Only for SI futures — skip for stocks and other instruments
    if (!ticker.startsWith('Si')) return;
    
    fetch('/strategy/grid-mm/chart-data')
        .then(r => r.json())
        .then(data => {
            if (data.error) return;
            
            if (data.indicators && data.indicators.length > 0) {
                const CC = window.ChartController;
                if (CC && CC.isReady()) {
                    CC.setStrategySeries({
                        sar: data.indicators
                            .filter(p => p.sar > 0 && !isNaN(p.sar))
                            .map(p => ({ time: toChartTime(p.time), value: p.sar })),
                        ema: data.indicators
                            .filter(p => p.ema > 0 && !isNaN(p.ema))
                            .map(p => ({ time: toChartTime(p.time), value: p.ema })),
                    });
                }
            }
            
            if (data.trades && data.trades.length > 0) {
                const CC = window.ChartController;
                if (CC && CC.isReady()) {
                    const markers = data.trades.map(t => ({
                        time: toChartTime(t.time),
                        position: t.dir === 1 ? 'belowBar' : 'aboveBar',
                        color: t.dir === 1 ? '#22C55E' : '#EF4444',
                        shape: t.dir === 1 ? 'arrowUp' : 'arrowDown',
                        text: `${t.dir === 1 ? 'B' : 'S'} ${t.lots}x@${t.price.toFixed(0)}`,
                    }));
                    CC.setMarkers(null, markers); // null ticker: не проверять контекст (стратегийные маркеры)
                }
            }
            
            if (data.current) updateIndicatorMetrics(data.current);
        })
        .catch(e => console.error("[ERROR]", e));
}

function toChartTime(isoStr) {
    if (!isoStr) return 0;
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return 0;
    return Math.floor(d.getTime() / 1000) + 10800;
}

function updateIndicatorMetrics(c) {
    // Добавляем метрики RV/HV на страницу стакана если их ещё нет
    let metricsRow = el('strategyMetrics');
    if (!metricsRow) {
        const parent = document.querySelector('#orderbook .metrics-row');
        if (parent) {
            metricsRow = document.createElement('div');
            metricsRow.className = 'metrics-row';
            metricsRow.id = 'strategyMetrics';
            metricsRow.innerHTML = `
                <div class="metric-card"><div class="metric-label">SAR</div><div id="mSar" class="metric-value" style="color:#FFD700">—</div></div>
                <div class="metric-card"><div class="metric-label">EMA</div><div id="mEma" class="metric-value" style="color:#00BFFF">—</div></div>
                <div class="metric-card"><div class="metric-label">STD(14)</div><div id="mStd" class="metric-value" style="color:#FF69B4">—</div></div>
                <div class="metric-card"><div class="metric-label">RV/HV</div><div id="mRatio" class="metric-value">—</div></div>
                <div class="metric-card"><div class="metric-label">Regime</div><div id="mRegime" class="metric-value">—</div></div>
                <div class="metric-card"><div class="metric-label">Позиция</div><div id="mPos" class="metric-value">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL</div><div id="mPnl" class="metric-value">—</div></div>
                <div class="metric-card"><div class="metric-label">Сделок</div><div id="mTrades" class="metric-value">—</div></div>
            `;
            parent.parentNode.insertBefore(metricsRow, parent.nextSibling);
        }
    }
    if (el('mSar')) el('mSar').textContent = c.sar > 0 ? c.sar.toFixed(2) : '—';
    if (el('mEma')) el('mEma').textContent = c.ema > 0 ? c.ema.toFixed(2) : '—';
    if (el('mStd')) el('mStd').textContent = c.std > 0 ? c.std.toFixed(1) : '—';
    if (el('mRatio')) {
        el('mRatio').textContent = c.ratio > 0 ? c.ratio.toFixed(2) : '—';
        el('mRatio').style.color = c.regime === 'LOW' ? 'var(--green)' : 'var(--red)';
    }
    if (el('mRegime')) {
        el('mRegime').textContent = c.regime || '—';
        el('mRegime').style.color = c.regime === 'LOW' ? 'var(--green)' : 'var(--red)';
    }
    if (el('mPos')) {
        const posText = c.posDir === 0 ? 'Flat' : c.posDir === 1 ? 'LONG' : 'SHORT';
        el('mPos').textContent = posText + (c.posDir !== 0 ? ` ×${c.lots}` : '');
        el('mPos').style.color = c.posDir === 0 ? '' : c.posDir === 1 ? 'var(--green)' : 'var(--red)';
    }
    if (el('mPnl')) {
        el('mPnl').textContent = c.totalPnl > 0 ? '+' + c.totalPnl.toFixed(0) : c.totalPnl.toFixed(0);
        el('mPnl').style.color = c.totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
    }
    if (el('mTrades')) el('mTrades').textContent = c.totalTrades;
}

// === Settings ===
function saveSettings() {
    const finam = el('finamToken')?.value;
    if (finam) localStorage.setItem('finamToken', finam);
    el('settingsStatus').textContent = '✅ Сохранено';
    updateTokenStatus();
}

function loadSettings() {
    const finam = localStorage.getItem('finamToken');
    if (finam && el('finamToken')) el('finamToken').value = finam;
    updateTokenStatus();
}

function updateTokenStatus() {
    const finam = localStorage.getItem('finamToken');
    if (el('finamTokenStatus')) el('finamTokenStatus').textContent = finam ? `✅ ${finam.slice(0,4)}...${finam.slice(-4)} (${finam.length} симв.)` : '❌ Не задан';
}

// === Backtest ===

function toggleParams() {
    const panel = el('strategyParams');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    if (panel.style.display === 'block') loadStrategyConfig();
}

function loadStrategyConfig() {
    fetch('/strategy/grid-mm/config')
        .then(r => r.json())
        .then(c => {
            if (c.error) return;
            el('cfgSarStart').value = c.sarStart;
            el('cfgSarStep').value = c.sarStep;
            el('cfgSarMax').value = c.sarMax;
            el('cfgEmaPeriod').value = c.emaPeriod;
            el('cfgGridStep').value = c.gridStep;
            el('cfgGridSpread').value = c.gridSpread;
            el('cfgMaxGridLevels').value = c.maxGridLevels;
            el('cfgMinProfitPerLot').value = c.minProfitPerLot;
            el('cfgClosePct').value = c.closePct;
            el('cfgCommission').value = c.commission;
            el('cfgMaxLots').value = c.maxLots;
            el('cfgLotStepProfit').value = c.lotStepProfit;
        }).catch(e => console.error("[ERROR]", e));
}

// Unified config reader from DOM elements
function readStratCfg() {
    return {
        sarStart: parseFloat(el('cfgSarStart')?.value),
        sarStep: parseFloat(el('cfgSarStep')?.value),
        sarMax: parseFloat(el('cfgSarMax')?.value),
        emaPeriod: parseInt(el('cfgEmaPeriod')?.value),
        gridStep: parseFloat(el('cfgGridStep')?.value),
        gridSpread: parseFloat(el('cfgGridSpread')?.value),
        maxGridLevels: parseInt(el('cfgMaxGridLevels')?.value),
        minProfitPerLot: parseFloat(el('cfgMinProfitPerLot')?.value),
        closePct: parseFloat(el('cfgClosePct')?.value),
        commission: parseFloat(el('cfgCommission')?.value),
        maxLots: parseInt(el('cfgMaxLots')?.value),
        lotStepProfit: parseFloat(el('cfgLotStepProfit')?.value),
        forceEntryOnStart: el('cfgForceEntry')?.checked,
        stdPeriod: parseInt(el('cfgStdPeriod')?.value) || 14,
        stdMult: parseFloat(el('cfgStdMult')?.value) || 1.5,
        gridHold: el('cfgGridHold')?.checked || false,
    };
}

// Автосохранение параметров при изменении (debounce 800ms)
let _cfgSaveTimer = null;
function autoSaveConfig() {
    if (_cfgSaveTimer) clearTimeout(_cfgSaveTimer);
    _cfgSaveTimer = setTimeout(() => {
        saveStrategyConfig();
    }, 800);
}
// Навешиваем на все cfg-поля
setTimeout(() => {
    document.querySelectorAll('#paramsBody input').forEach(inp => {
        inp.addEventListener('input', autoSaveConfig);
        inp.addEventListener('change', autoSaveConfig);
    });
    // Python Robot config fields
    document.querySelectorAll('[id^="cfgVp"]').forEach(inp => {
        inp.addEventListener('input', () => { if(_cfgSaveTimer) clearTimeout(_cfgSaveTimer); _cfgSaveTimer = setTimeout(pythonRobotSaveConfig, 800); });
        inp.addEventListener('change', () => { if(_cfgSaveTimer) clearTimeout(_cfgSaveTimer); _cfgSaveTimer = setTimeout(pythonRobotSaveConfig, 800); });
    });
}, 500);

function saveStrategyConfig() {
    const cfg = getStratCfgForStart();
    const ep = getConfigEndpoint();
    fetch(ep, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg)
    })
    .then(r => r.json())
    .then(res => {
        if (res.error) { addLog(nowTime(), 'ERROR', 'Config: ' + res.error); return; }
        const saved = el('cfgSaved');
        if (saved) { saved.style.display = 'inline'; setTimeout(() => saved.style.display = 'none', 2000); }
    })
    .catch(e => addLog(nowTime(), 'ERROR', 'Config: ' + e.message));
}

function getStratCfgForStart(strategyId) {
    const isVpGrid = (strategyId || '').includes('vp-scalp-grid');
    const isVpSimple = (strategyId || '').includes('vp-simple');
    const isV8 = (strategyId || '').includes('v8-trail');
    if (isVpGrid || isVpSimple || isV8) {
        return {
            maxLevels: parseInt(el('editMaxGrid')?.value) || 100,
            stepBase: parseInt(el('editGridStep')?.value) || 32,
            spreadBase: parseInt(el('editGridSpread')?.value) || 32,
            maxHoldMinutes: parseInt(el('editHoldMinutes')?.value) || 60,
            vpLookback: parseInt(el('editVpLookback')?.value) || 60,
            vpBinSize: parseInt(el('editVpBinSize')?.value) || 50,
            vaPercent: parseFloat(el('editVaPercent')?.value) || 0.7,
            minProfitPerLot: parseInt(el('editMinProfit')?.value) || 28,
            rvAdaptation: el('editRvAdapt')?.checked || false,
        };
    }
    return readStratCfg();
}

function getConfigEndpoint() {
    // Find active strategy from edit panel context
    const vpGrid = el('editVpLookback');
    if (vpGrid) {
        // Check which strategy panel is open by looking at edit fields
        const vpBin = el('editVpBinSize');
        const v8Hold = el('cfgV8MaxHold');
        const vpSimpleHold = el('cfgVpSimpleMaxHold');
        if (vpSimpleHold) return '/strategy/vp-simple/config';
        if (v8Hold) return '/strategy/v8-trail/config';
        if (vpBin) return '/strategy/vp-scalp-grid/config';
    }
    return '/strategy/grid-mm/config';
}

function getStratCfg() {
    return readStratCfg();
}

function stratTest() {
    // Открываем панель параметров
    const panel = el('strategyParams');
    panel.style.display = 'block';
    loadStrategyConfig();
}

function startTest() {
    // Переносим параметры на вкладку тестирования
    const ticker = el('stratInstrument')?.value;
    const cfg = getStratCfg();
    if (el('testTicker')) el('testTicker').value = ticker;
    if (el('tstSarStart')) el('tstSarStart').value = cfg.sarStart;
    if (el('tstSarStep')) el('tstSarStep').value = cfg.sarStep;
    if (el('tstSarMax')) el('tstSarMax').value = cfg.sarMax;
    if (el('tstEma')) el('tstEma').value = cfg.emaPeriod;
    if (el('tstGridStep')) el('tstGridStep').value = cfg.gridStep;
    if (el('tstGridSpread')) el('tstGridSpread').value = cfg.gridSpread;
    if (el('tstMaxGrid')) el('tstMaxGrid').value = cfg.maxGridLevels;
    if (el('tstClosePct')) el('tstClosePct').value = cfg.closePct;
    if (el('tstComm')) el('tstComm').value = cfg.commission;
    if (el('tstLots')) el('tstLots').value = cfg.maxLots;
    // Переключаем на вкладку тестирования и запускаем
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="testing"]').classList.add('active');
    el('testing')?.classList.add('active');
    addLog(nowTime(), 'INFO', `🧪 Запуск теста: ${ticker}`);
    setTimeout(runBacktest, 300);
}

function setTestPeriod(days) {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - days);
    const fmt = d => d.toISOString().slice(0, 10);
    if (el('testFrom')) el('testFrom').value = fmt(from);
    if (el('testTo')) el('testTo').value = fmt(to);
    if (el('testCandles')) el('testCandles').value = '';
}

let btEquityChart = null, btCandleChart = null;

async function runBacktest() {
    const ticker = el('testTicker')?.value || 'SI';
    const tf = parseInt(el('testTf')?.value) || 5;
    const status = el('testStatus');
    status.textContent = '⏳ Запуск бэктеста...';

    const params = {
        mode: 'backtest',
        ticker, tf,
        from: el('testFrom')?.value || undefined,
        to: el('testTo')?.value || undefined,
        sarStart: parseFloat(el('tstSarStart')?.value),
        sarStep: parseFloat(el('tstSarStep')?.value),
        sarMax: parseFloat(el('tstSarMax')?.value),
        emaPeriod: parseInt(el('tstEma')?.value),
        gridStep: parseFloat(el('tstGridStep')?.value),
        gridSpread: parseFloat(el('tstGridSpread')?.value),
        maxGrid: parseInt(el('tstMaxGrid')?.value),
        commission: parseFloat(el('tstComm')?.value),
        minProfit: parseFloat(el('tstMinProfit')?.value || 35),
        closePct: parseFloat(el('tstClosePct')?.value),
        lotStepProfit: parseFloat(el('tstLotStep')?.value || 1000),
        maxLots: parseInt(el('tstLots')?.value),
    };

    try {
        const resp = await fetch('/api/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params),
            signal: AbortSignal.timeout(120000)
        });
        const r = await resp.json();
        if (r.error) { status.textContent = '❌ ' + r.error; return; }

        // Render results
        el('testResults').style.display = 'block';
        const fmt = v => (v||0).toLocaleString('ru-RU', {maximumFractionDigits:0});
        const cls = v => v >= 0 ? 'metric-value green' : 'metric-value red';
        const setVal = (id, v, suffix='') => { const e = el(id); if(e) { e.textContent = (v>=0?'+':'') + fmt(v) + suffix; e.className = cls(v); } };
        const setTxt = (id, v) => { const e = el(id); if(e) e.textContent = v; };

        setVal('btPnl', r.pnl, ' ₽');
        setVal('btPpd', r.ppd, ' ₽/д');
        setTxt('btTrades', r.sessions);
        setTxt('btRtpd', r.rtpd);
        setTxt('btWR', r.winRate + '%');
        setTxt('btPF', r.profitFactor);
        setTxt('btSharpe', '—');
        setVal('btAvgTrade', r.pnl / (r.sessions||1), ' ₽');
        setVal('btAvgWin', r.avgWin, ' ₽');
        setVal('btAvgLoss', r.avgLoss, ' ₽');
        setVal('btMaxDD', -r.maxDD, ' ₽'); if(el('btMaxDD')) el('btMaxDD').className = 'metric-value red';
        setTxt('btMaxDDpct', '—');
        setTxt('btMaxDDdur', '—');
        setTxt('btMaxPos', r.maxPosition);
        setVal('btMaxGO', r.maxGO, ' ₽');
        setTxt('btSessions', r.sessions);
        setTxt('btSessionsWR', r.winRate + '%');
        setTxt('btMedSession', '—');
        setVal('btMaxWin', r.maxWin, ' ₽');
        setVal('btMaxLoss', r.maxLoss, ' ₽');
        setTxt('btMaxWinsStreak', r.maxWinStreak);
        setTxt('btMaxLossStreak', r.maxLossStreak);
        setVal('btCommission', -r.totalCommission, ' ₽'); if(el('btCommission')) el('btCommission').className = 'metric-value red';

        // Equity chart
        const ecEl = el('testEquityChart');
        if (ecEl && r.equityCurve?.length > 0) {
            if (btEquityChart) btEquityChart.remove();
            const ew = ecEl.parentElement?.offsetWidth || ecEl.offsetWidth || 800;
            btEquityChart = LightweightCharts.createChart(ecEl, {
                width: ew, height: 300,
                layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
                grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
                timeScale: { timeVisible: true },
            });
            const line = btEquityChart.addLineSeries({ color: '#22C55E', lineWidth: 2 });
            const eqData = r.equityCurve.map(p => ({ time: p.time + 10800, value: p.value }));
            line.setData(eqData);
        }

        // Candle chart with SAR + EMA + markers
        const ccEl = el('testCandleChart');
        if (ccEl && r.candles?.length > 0) {
            if (btCandleChart) btCandleChart.remove();
            const cw = ccEl.parentElement?.offsetWidth || ccEl.offsetWidth || 800;
            btCandleChart = LightweightCharts.createChart(ccEl, {
                width: cw, height: 400,
                layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
                grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
                timeScale: { timeVisible: true, secondsVisible: false },
                localization: { timeFormatter: t => { const d=new Date(t*1000); return d.getUTCDate()+'.'+(d.getUTCMonth()+1)+' '+d.getUTCHours()+':'+String(d.getUTCMinutes()).padStart(2,'0'); } },
            });
            const cs = btCandleChart.addCandlestickSeries({ upColor: '#22C55E', downColor: '#EF4444', borderUpColor: '#22C55E', borderDownColor: '#EF4444', wickUpColor: '#22C55E', wickDownColor: '#EF4444' });
            cs.setData(r.candles.map(c => ({ time: c.t + 10800, open: c.o, high: c.h, low: c.l, close: c.c })));
            if (r.sar?.length) { const s = btCandleChart.addLineSeries({ color: '#F59E0B', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }); s.setData(r.sar.map(d => ({ time: d.time + 10800, value: d.value }))); }
            if (r.ema?.length) { const e = btCandleChart.addLineSeries({ color: '#3B82F6', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }); e.setData(r.ema); }
            // Trade markers on candles
            if (r.markers?.length) {
                const markers = r.markers.map(m => ({
                    time: m.time,
                    position: m.dir === 1 ? 'belowBar' : m.dir === -1 ? 'aboveBar' : m.dir === 2 ? 'belowBar' : 'aboveBar',
                    color: (m.dir === 1 || m.dir === 2) ? '#22C55E' : '#EF4444',
                    shape: m.dir === 1 ? 'arrowUp' : m.dir === -1 ? 'arrowDown' : 'circle',
                    text: m.dir === 1 ? 'L' : m.dir === -1 ? 'S' : 'X'
                })).sort((a, b) => a.time - b.time);
                cs.setMarkers(markers);
            }
        }

        // Trades table — show session details
        const tbody = el('testTradesTable');
        if (tbody) {
            const sessions = r.sessionDetails || [];
            if (!sessions.length) { tbody.innerHTML = '<tr><td colspan="8">Нет данных</td></tr>'; }
            else {
                tbody.innerHTML = sessions.slice(0, 100).map((s, i) => {
                    const cls = s.pnl >= 0 ? 'green' : 'red';
                    return `<tr><td>${i+1}</td><td class="${cls}">${s.pnl >= 0 ? '+' : ''}${Math.round(s.pnl).toLocaleString('ru-RU')} ₽</td><td>${s.peakLots}</td><td>${s.pnl >= 0 ? '✅' : '❌'}</td></tr>`;
                }).join('');
                // Fix thead
                const thead = tbody.closest('table')?.querySelector('thead tr');
                if (thead) thead.innerHTML = '<th>#</th><th>PnL сессии</th><th>Peak Lots</th><th>WR</th>';
            }
        }

        status.textContent = `✅ ${r.tickers?.join(', ')} | ${r.totalDays} дней | ${r.sessions} сессий | PnL ${fmt(r.pnl)}₽`;
    } catch (e) {
        status.textContent = '❌ ' + e.message;
    }
}

function stratOptimize() {
    // Открываем панель оптимизации
    el('optimizePanel').style.display = 'block';
}

async function startOptimize() {
    const ticker = el('stratInstrument')?.value || 'SI';
    const tf = parseInt(el('testTf')?.value) || 5;
    addLog(nowTime(), 'INFO', `⚡ Оптимизация ${ticker}...`);

    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="testing"]').classList.add('active');
    el('testing')?.classList.add('active');
    el('testResults').style.display = 'block';
    el('testStatus').textContent = '⏳ Запуск оптимизации...';

    function getVal(id) { return parseFloat(el(id)?.value) || 0; }
    function getInt(id) { return parseInt(el(id)?.value) || 0; }

    const params = {
        mode: 'optimize',
        ticker, tf,
        from: el('testFrom')?.value || undefined,
        to: el('testTo')?.value || undefined,
        sarMax: getVal('cfgSarMax'),
        maxGrid: getInt('cfgMaxGridLevels'),
        commission: getVal('cfgCommission'),
        minProfit: getVal('cfgMinProfitPerLot'),
        lotStepProfit: getVal('cfgLotStepProfit'),
        maxLots: getInt('cfgMaxLots'),
        // Ranges
        sarStartFrom: getVal('optSarStartFrom'), sarStartTo: getVal('optSarStartTo'), sarStartStep: getVal('optSarStartStep'),
        sarStepFrom: getVal('optSarStepFrom'), sarStepTo: getVal('optSarStepTo'), sarStepStep: getVal('optSarStepStep'),
        emaPeriodFrom: getInt('optEmaFrom'), emaPeriodTo: getInt('optEmaTo'), emaPeriodStep: getInt('optEmaStep'),
        gridStepFrom: getInt('optGridStepFrom'), gridStepTo: getInt('optGridStepTo'), gridStepStep: getInt('optGridStepStep'),
        gridSpreadFrom: getInt('optGridSpreadFrom'), gridSpreadTo: getInt('optGridSpreadTo'), gridSpreadStep: getInt('optGridSpreadStep'),
        closePctFrom: getVal('optClosePctFrom'), closePctTo: getVal('optClosePctTo'), closePctStep: getVal('optClosePctStep'),
    };

    try {
        const resp = await fetch('/api/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params)
        });
        const data = await resp.json();
        if (data.error) { el('testStatus').textContent = '❌ ' + data.error; return; }

        const top = data.results || [];
        const tbody = el('testTradesTable');
        if (tbody) {
            const thead = tbody.closest('table')?.querySelector('thead tr');
            if (thead) thead.innerHTML = '<th>#</th><th>SAR</th><th>EMA</th><th>Grid S/S</th><th>Close%</th><th>PnL</th><th>Сессий</th><th>WR</th><th>Max DD</th><th>Max Pos</th>';
            tbody.innerHTML = top.map((r, i) => `<tr>
                <td>${i+1}</td>
                <td>${r.sarStart}/${r.sarStep}/${r.sarMax}</td>
                <td>${r.emaPeriod}</td>
                <td>${r.gridStep}/${r.gridSpread}</td>
                <td>${r.closePct}</td>
                <td class="${r.pnl >= 0 ? 'green' : 'red'}">${r.pnl >= 0 ? '+' : ''}${Math.round(r.pnl).toLocaleString('ru-RU')} ₽</td>
                <td>${r.sessions}</td>
                <td>${r.winRate}%</td>
                <td>${Math.round(r.maxDD).toLocaleString('ru-RU')}</td>
                <td>${r.maxPos}</td>
            </tr>`).join('');
        }

        // Clear metrics
        ['btPnl','btPpd','btTrades','btWR','btPF','btMaxDD'].forEach(id => { if(el(id)) el(id).textContent = '—'; });

        el('testStatus').textContent = `✅ ${data.totalCombinations} комбинаций. Лучший: ${Math.round(top[0]?.pnl||0).toLocaleString('ru-RU')}₽`;
        addLog(nowTime(), 'INFO', `⚡ Оптимизация: ${data.totalCombinations} комб. Лучший PnL=${Math.round(top[0]?.pnl||0).toLocaleString('ru-RU')}₽`);
    } catch (e) {
        el('testStatus').textContent = '❌ ' + e.message;
    }
}

// === Роботы (мониторинг) ===
let robots = JSON.parse(localStorage.getItem('hf_robots') || '[]');

// Пресеты параметров по стратегии + инструменту
const STRAT_PRESETS = {
    'v7-SiU6': {sarStart:0.009,sarStep:0.01,sarMax:0.2,ema:30,gridStep:35,gridSpread:35,maxGrid:70,stdPeriod:14,stdMult:1.5,gridHold:true,closePct:0.15},
    'v8-RIM6': {sarStart:0.009,sarStep:0.02,sarMax:0.2,ema:20,gridStep:1672,gridSpread:1672,maxGrid:70,stdPeriod:20,stdMult:2.5,gridHold:true,closePct:0.15},
    'v8-GDM6': {sarStart:0.009,sarStep:0.02,sarMax:0.2,ema:20,gridStep:41,gridSpread:41,maxGrid:70,stdPeriod:20,stdMult:2.5,gridHold:true,closePct:0.15},
    'v8-SPM6': {sarStart:0.009,sarStep:0.02,sarMax:0.2,ema:20,gridStep:460,gridSpread:460,maxGrid:70,stdPeriod:20,stdMult:2.5,gridHold:true,closePct:0.15},
};

function onStratVersionChange() {
    const ver = el('stratVersion')?.value || 'v7';
    // VP SG has different params
    const gridParamsEl = document.querySelectorAll('#cfgSarStart,#cfgSarStep,#cfgSarMax,#cfgEmaPeriod,#cfgStdPeriod,#cfgStdMult,#cfgGridHold,#cfgClosePct');
    if (ver === 'vpsg') {
        // Show VP Scalp Grid params
        if (el('cfgGridStep')) el('cfgGridStep').value = 15;
        if (el('cfgGridSpread')) el('cfgGridSpread').value = 50;
        if (el('cfgMaxGridLevels')) el('cfgMaxGridLevels').value = 100;
        addLog(nowTime(), 'INFO', '📋 VP Scalp Grid: step=15 spread=50 levels=100');
        return;
    }
    onStratInstrChange();
}

function onStratInstrChange() {
    const ver = el('stratVersion')?.value || 'v7';
    const instr = el('stratInstrument')?.value || 'SiU6';
    const key = ver + '-' + instr;
    const p = STRAT_PRESETS[key];
    if (p) {
        if (el('cfgSarStart')) el('cfgSarStart').value = p.sarStart;
        if (el('cfgSarStep')) el('cfgSarStep').value = p.sarStep;
        if (el('cfgSarMax')) el('cfgSarMax').value = p.sarMax;
        if (el('cfgEmaPeriod')) el('cfgEmaPeriod').value = p.ema;
        if (el('cfgGridStep')) el('cfgGridStep').value = p.gridStep;
        if (el('cfgGridSpread')) el('cfgGridSpread').value = p.gridSpread;
        if (el('cfgMaxGridLevels')) el('cfgMaxGridLevels').value = p.maxGrid;
        if (el('cfgStdPeriod')) el('cfgStdPeriod').value = p.stdPeriod;
        if (el('cfgStdMult')) el('cfgStdMult').value = p.stdMult;
        if (el('cfgGridHold')) el('cfgGridHold').checked = p.gridHold;
        addLog(nowTime(), 'INFO', `📋 Пресет загружен: ${key} EMA=${p.ema} GS=${p.gridStep} STD×${p.stdMult}`);
    }
}

function vpScalpGridCreateRobot() {
    const robot = {
        id: Date.now(),
        ticker: el('cfgVpTicker')?.value || 'SiU6',
        account: '',
        accountName: '',
        strategy: 'VP Scalp Grid',
        gridStep: el('cfgVpStepBase')?.value || '15',
        gridSpread: el('cfgVpSpreadBase')?.value || '50',
        maxGrid: el('cfgVpMaxLevels')?.value || '100',
        holdMinutes: el('cfgVpMaxHold')?.value || '60',
        status: 'stopped',
        position: '—',
        pnlToday: 0, pnlTotal: 0,
        lotsOpen: 0, go: 0,
        exchangeStatus: '—'
    };
    robots.push(robot);
    saveRobots();
    renderRobots();
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', '🤖 VP Scalp Grid робот создан (levels=' + robot.maxGrid + ' step=' + robot.gridStep + ' spread=' + robot.gridSpread + ')');
}

function vpCopyCreateRobot() {
    const robot = {
        id: Date.now(),
        ticker: el('cfgVpTicker')?.value || 'SiU6',
        account: '',
        accountName: '',
        strategy: 'VP Scalp Grid Copy',
        gridStep: el('cfgVpCopyStepBase')?.value || '15',
        gridSpread: el('cfgVpCopySpreadBase')?.value || '50',
        maxGrid: el('cfgVpCopyMaxLevels')?.value || '100',
        holdMinutes: el('cfgVpCopyMaxHold')?.value || '60',
        status: 'stopped',
        position: '—',
        pnlToday: 0, pnlTotal: 0,
        lotsOpen: 0, go: 0,
        exchangeStatus: '—'
    };
    robots.push(robot);
    saveRobots();
    renderRobots();
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', '🤖 VP Scalp Grid Copy робот создан (levels=' + robot.maxGrid + ' step=' + robot.gridStep + ' spread=' + robot.gridSpread + ')');
}

function vpSimpleCreateRobot() {
    const slPct = el('cfgVpSimpleSlPct')?.value || '0.20';
    const maxHold = el('cfgVpSimpleMaxHold')?.value || '60';
    const lookback = el('cfgVpSimpleLookback')?.value || '40';
    const bins = el('cfgVpSimpleBins')?.value || '30';
    const ticker = el('cfgVpSimpleTicker')?.value || 'MXM6';
    const finamSym = ticker + '@RTSX';
    const robot = {
        id: Date.now(),
        ticker: ticker,
        account: '',
        accountName: '',
        strategy: 'VP Scalp Simple',
        slPct: slPct,
        holdMinutes: maxHold,
        vpLookback: lookback,
        vpBins: bins,
        finamSymbol: finamSym,
        status: 'stopped',
        position: '—',
        pnlToday: 0, pnlTotal: 0,
        lotsOpen: 0, go: 0,
        exchangeStatus: '—'
    };
    robots.push(robot);
    saveRobots();
    renderRobots();
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', `⚡ VP Scalp Simple робот создан (${ticker} SL=${slPct}% hold=${maxHold}мин)`);
}

function v8TrailCreateRobot() {
    const slPct = el('cfgV8SlPct')?.value || '0.10';
    const ema = el('cfgV8Ema')?.value || '20';
    const sarStart = el('cfgV8SarStart')?.value || '0.02';
    const sarStep = el('cfgV8SarStep')?.value || '0.02';
    const sarMax = el('cfgV8SarMax')?.value || '0.2';
    const maxHold = el('cfgV8MaxHold')?.value || '240';
    const ticker = el('cfgV8Ticker')?.value || 'RTSM6';
    const finamSym = ticker + '@RTSX';
    const stepPrice = ticker.includes('RTS') ? 14.86 : 1;
    const robot = {
        id: Date.now(),
        ticker: ticker,
        account: '', accountName: '',
        strategy: 'V8 Trail',
        slPct: slPct,
        ema: ema,
        sarStart: sarStart,
        sarStep: sarStep,
        sarMax: sarMax,
        holdMinutes: maxHold,
        stepPrice: stepPrice,
        finamSymbol: finamSym,
        status: 'stopped',
        position: '—',
        pnlToday: 0, pnlTotal: 0,
        lotsOpen: 0, go: 0,
        exchangeStatus: '—'
    };
    robots.push(robot);
    saveRobots();
    renderRobots();
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', `🎯 V8 Trail робот создан (${ticker} SL=${slPct}% EMA=${ema})`);
}

function vpSimpleTest() {
    const ticker = el('cfgVpSimpleTicker')?.value || 'MXM6';
    const slPct = el('cfgVpSimpleSlPct')?.value || '0.20';
    // Switch to testing tab
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="testing"]').classList.add('active');
    el('testing')?.classList.add('active');
    // Set instrument
    const instrSel = el('testInstrument');
    if (instrSel) {
        for (let opt of instrSel.options) { if (opt.value.includes(ticker)) { instrSel.value = opt.value; break; } }
    }
    addLog(nowTime(), 'INFO', `🧪 VP Scalp Simple: откройте тестирование для ${ticker} (SL=${slPct}%)`);
}

function stratCreateRobot() {
    const cfg = getStratCfg();
    if (el('robotTicker')) el('robotTicker').value = el('stratInstrument')?.value;
    if (el('robotSarStart')) el('robotSarStart').value = cfg.sarStart;
    if (el('robotSarStep')) el('robotSarStep').value = cfg.sarStep;
    if (el('robotSarMax')) el('robotSarMax').value = cfg.sarMax;
    if (el('robotEma')) el('robotEma').value = cfg.emaPeriod;
    if (el('robotGridStep')) el('robotGridStep').value = cfg.gridStep;
    if (el('robotGridSpread')) el('robotGridSpread').value = cfg.gridSpread;
    if (el('robotMaxGrid')) el('robotMaxGrid').value = cfg.maxGridLevels;
    if (el('robotClosePct')) el('robotClosePct').value = cfg.closePct;
    if (el('robotMinProfit')) el('robotMinProfit').value = cfg.minProfit || 28;
    if (el('robotComm')) el('robotComm').value = cfg.commission;
    if (el('robotLots')) el('robotLots').value = cfg.maxLots;
    if (el('robotStdPeriod')) el('robotStdPeriod').value = cfg.stdPeriod || 14;
    if (el('robotStdMult')) el('robotStdMult').value = cfg.stdMult || 1.5;
    if (el('robotGridHold')) el('robotGridHold').checked = cfg.gridHold === true;
    if (el('robotForceEntry')) el('robotForceEntry').checked = cfg.forceEntry === 'true' || cfg.forceEntryOnStart === true;
    el('robotPanel').style.display = 'block';
}

function confirmCreateRobot() {
    const robot = {
        id: Date.now(),
        ticker: el('robotTicker')?.value || 'SiU6',
        account: el('robotAccount')?.value || '',
        accountName: el('robotAccount')?.selectedOptions?.[0]?.text || '',
        strategy: el('stratVersion')?.value === 'v8' ? 'SAR×EMA Grid MM v8 (Trend)' : 'SAR×EMA Grid MM v7',
        sarStart: el('robotSarStart')?.value,
        sarStep: el('robotSarStep')?.value,
        sarMax: el('robotSarMax')?.value,
        ema: el('robotEma')?.value,
        gridStep: el('robotGridStep')?.value,
        gridSpread: el('robotGridSpread')?.value,
        maxGrid: el('robotMaxGrid')?.value,
        closePct: el('robotClosePct')?.value,
        minProfit: el('robotMinProfit')?.value,
        forceEntry: el('robotForceEntry')?.checked ? 'true' : 'false',
        commission: el('robotComm')?.value,
        lots: el('robotLots')?.value,
        stdPeriod: el('robotStdPeriod')?.value,
        stdMult: el('robotStdMult')?.value,
        gridHold: el('robotGridHold')?.checked || false,
        status: 'stopped', // stopped, running, paused
        position: '—',
        pnlToday: 0,
        pnlTotal: 0,
        lotsOpen: 0,
        go: 0,
        exchangeStatus: '—'
    };
    robots.push(robot);
    saveRobots();
    el('robotPanel').style.display = 'none';
    renderRobots();
    // Switch to monitoring tab
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', `🤖 Робот создан: ${robot.ticker} ${robot.strategy}`);
}

function saveRobots() { localStorage.setItem('hf_robots', JSON.stringify(robots)); }

// Cached Finam accounts for dropdowns
let _finamAccounts = null;
async function loadFinamAccounts() {
    if (_finamAccounts) return _finamAccounts;
    try {
        const resp = await fetch('/api/accounts');
        const data = await resp.json();
        _finamAccounts = data.accounts || [];
    } catch (e) { _finamAccounts = []; }
    return _finamAccounts;
}
function accountDropdownHtml(currentVal, onChangeStr) {
    const accs = _finamAccounts || [];
    const opts = accs.map(a => `<option value="${a.id}" ${a.id===(currentVal||'')?'selected':''}>${a.id} — ${a.name}</option>`).join('');
    return `<select class="input" style="width:140px;font-size:11px" onchange="${onChangeStr}">${opts}</select>`;
}
function onLocalStorageRobotAccountChange(idx, val) {
    if (robots[idx]) {
        robots[idx].account = val;
        robots[idx].accountName = val;
        saveRobots();
        renderRobots();
    }
}
let _mainRobotAccount = '';
function onMainRobotAccountChange(val) {
    _mainRobotAccount = val;
}
let _mainRobotInstrument = localStorage.getItem('mainRobotInstrument') || 'SiU6';

// === Order Flow robot instrument/account ===
function onOfRobotInstrumentChange(val) {
    localStorage.setItem('ofRobotInstrument', val);
    addLog(nowTime(), 'INFO', `📊 OF контракта изменена на ${val} (применится при следующем старте)`);
}
function onOfRobotAccountChange(val) {
    localStorage.setItem('ofRobotAccount', val);
    addLog(nowTime(), 'INFO', `📊 OF счёт изменён на ${val}`);
}
function onMainRobotInstrumentChange(val) {
    _mainRobotInstrument = val;
    localStorage.setItem('mainRobotInstrument', val);
    // Update .env via C# proxy
    fetch('/api/robot/update-instrument', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({instrument: val})
    }).then(r => r.json()).then(d => {
        addLog(nowTime(), 'INFO', `📋 Контракт изменён на ${val} (применится при следующем старте)`);
    }).catch(e => {
        addLog(nowTime(), 'ERROR', `Не удалось сменить контракт: ${e.message}`);
    });
}

async function renderRobots() {
    const tbody = el('robotsTable');
    const noMsg = el('noRobots');
    if (!tbody) return;

    // Poll instance statuses in background via C# proxy
    robots.forEach((r, idx) => {
        if ((r.isInstance || r.port) && r.port) {
            fetch('/api/instance/status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ port: r.port })
            }).then(resp => resp.json()).then(data => {
                if (!data.error) {
                    r.status = data.mode || 'running';
                    r.position = data.direction > 0 ? 'Лонг' : data.direction < 0 ? 'Шорт' : '—';
                    r.pnlTotal = data.realized_pnl || 0;
                    r.lotsOpen = data.total_lots || 0;
                } else {
                    r.status = 'stopped';
                }
            }).catch(() => { r.status = 'stopped'; });
        }
    });

    // C# server strategies removed — Python is primary
    let serverStrategies = [];
    try { /* no-op, kept for compatibility */ } catch(e) {}

    // localStorage robots
    let localRows = [];
    try {
        robots.forEach((r, idx) => {
            const ticker = r.ticker || 'SiU6';
            const strat = r.strategy || 'VP Scalp Grid';
            const step = r.isInstance ? (r.step_base || '?') : (r.gridStep || '?');
            const spread = r.isInstance ? (r.spread_base || '?') : (r.gridSpread || '?');
            const statusCls = r.status === 'running' ? 'green' : 'red';
            const statusText = r.status === 'running' ? '🟢 Работает' : '🔴 Остановлен';
            localRows.push(`<tr ondblclick="openRobotEditPanel(${idx})" style="cursor:pointer" title="Двойной клик — настройки робота">
                <td><strong>${ticker}</strong></td>
                <td><strong>${strat}</strong> <span class="badge" style="background:#2196F3">PYTHON</span></td>
                <td>${accountDropdownHtml(r.account, 'onLocalStorageRobotAccountChange(' + idx + ', this.value)')}</td>
                <td>${r.position || '—'}</td>
                <td>—</td>
                <td>${r.pnlTotal >= 0 ? '+' : ''}${(r.pnlTotal||0).toFixed(0)} ₽</td>
                <td>${r.lotsOpen || 0}</td>
                <td>step=${step} spread=${spread}</td>
                <td>
                    <button class="btn btn-success btn-sm" onclick="startLocalStorageRobot(${idx})">▶</button>
                    <button class="btn btn-warning btn-sm" onclick="pauseLocalStorageRobot(${idx})">⏸</button>
                    <button class="btn btn-danger btn-sm" onclick="stopLocalStorageRobot(${idx})">⏹</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteRobot(${idx})">🗑</button>
                </td>
                <td class="${statusCls}">${statusText}</td>
            </tr>`);
        });
    } catch(e) { console.error('renderRobots local error:', e); }

    // Python Robot row — always visible
    let pythonRobotRow = '';
    if (pythonRobot && pythonRobot.status !== 'not_initialized') {
        const s = pythonRobot;
        const mode = s.mode || 'stopped';
        const modeText = mode === 'running' ? '🟢 Работает' : mode === 'paused' ? '🟡 Пауза' : '🔴 Остановлен';
        const modeCls = mode === 'running' ? 'green' : mode === 'paused' ? 'yellow' : 'red';
        const dirText = s.direction > 0 ? 'Лонг' : s.direction < 0 ? 'Шорт' : 'Флэт';
        const dirCls = s.direction > 0 ? 'green' : s.direction < 0 ? 'red' : '';
        const pnlCls = v => v >= 0 ? 'green' : 'red';
        const totalPnl = (s.realized_pnl || 0) + (s.pnl || 0);
        const paper = s.paper ? ' <span class="badge" style="background:#ff9800">PAPER</span>' : '';
        pythonRobotRow = `<tr ondblclick="pythonRobotEditPanel()" style="cursor:pointer" title="Двойной клик — настройки робота">
            <td><select class="input" style="width:80px;font-size:11px" onchange="onMainRobotInstrumentChange(this.value)">
              <option value="SiU6" ${(_mainRobotInstrument||'SiU6')==='SiU6'?'selected':''}>SiU6</option>
              <option value="SiZ6" ${_mainRobotInstrument==='SiZ6'?'selected':''}>SiZ6</option>
              <option value="SiH7" ${_mainRobotInstrument==='SiH7'?'selected':''}>SiH7</option>
              <option value="SiM7" ${_mainRobotInstrument==='SiM7'?'selected':''}>SiM7</option>
            </select></td>
            <td><strong>VP Scalp Grid</strong> <span class="badge" style="background:#2196F3">PYTHON</span>${paper}</td>
            <td>${accountDropdownHtml(_mainRobotAccount, 'onMainRobotAccountChange(this.value)')}</td>
            <td class="${dirCls}">${dirText}${s.entry_price > 0 ? ' @ ' + s.entry_price.toFixed(0) : ''}</td>
            <td>—</td>
            <td class="${pnlCls(totalPnl)}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)} ₽</td>
            <td>${s.total_lots}</td>
            <td>—</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="robotApi('start')" ${mode==='running'?'disabled':''}>▶</button>
                <button class="btn btn-warning btn-sm" onclick="robotApi('pause')" ${mode!=='running'?'disabled':''}>⏸</button>
                <button class="btn btn-danger btn-sm" onclick="robotApi('stop')" ${mode==='stopped'?'disabled':''}>⏹</button>
                <button class="btn btn-success btn-sm" onclick="robotApi('resume')" ${mode!=='paused'?'disabled':''}>▶</button>
            </td>
            <td class="${modeCls}">${modeText} ${s.connected ? '🔗' : '⚠️'}</td>
        </tr>`;
    } else {
        pythonRobotRow = `<tr ondblclick="pythonRobotEditPanel()" style="cursor:pointer" title="Двойной клик — настройки робота">
            <td><select class="input" style="width:80px;font-size:11px" onchange="onMainRobotInstrumentChange(this.value)">
              <option value="SiU6" ${(_mainRobotInstrument||'SiU6')==='SiU6'?'selected':''}>SiU6</option>
              <option value="SiZ6" ${_mainRobotInstrument==='SiZ6'?'selected':''}>SiZ6</option>
              <option value="SiH7" ${_mainRobotInstrument==='SiH7'?'selected':''}>SiH7</option>
              <option value="SiM7" ${_mainRobotInstrument==='SiM7'?'selected':''}>SiM7</option>
            </select></td>
            <td><strong>VP Scalp Grid</strong> <span class="badge" style="background:#2196F3">PYTHON</span></td>
            <td>${accountDropdownHtml(_mainRobotAccount, 'onMainRobotAccountChange(this.value)')}</td>
            <td>—</td>
            <td>—</td>
            <td>—</td>
            <td>0</td>
            <td>—</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="robotApi('start')">▶</button>
            </td>
            <td class="red">🔴 Не запущен</td>
        </tr>`;
    }

    // Order Flow Robot row
    let ofRobotRow = '';
    let _ofInstrument = localStorage.getItem('ofRobotInstrument') || 'SiU6';
    let _ofAccount = localStorage.getItem('ofRobotAccount') || '1225953';
    if (ofRobot) {
        const s = ofRobot;
        const mode = s.mode || 'stopped';
        const modeText = mode === 'running' ? '🟢 Работает' : mode === 'paused' ? '🟡 Пауза' : '🔴 Остановлен';
        const modeCls = mode === 'running' ? 'green' : mode === 'paused' ? 'yellow' : 'red';
        const dirText = s.direction === 'LONG' ? 'Лонг' : s.direction === 'SHORT' ? 'Шорт' : 'Флэт';
        const dirCls = s.direction === 'LONG' ? 'green' : s.direction === 'SHORT' ? 'red' : '';
        const pnlCls = v => v >= 0 ? 'green' : 'red';
        const pnl = s.realizedPnL || 0;
        const paper = s.paper ? ' <span class="badge" style="background:#ff9800">PAPER</span>' : '';
        ofRobotRow = `<tr ondblclick="ofRobotEditPanel()" style="cursor:pointer" title="Двойной клик — настройки робота">
            <td><select class="input" style="width:80px;font-size:11px" onchange="onOfRobotInstrumentChange(this.value)">
              <option value="SiU6" ${_ofInstrument==='SiU6'?'selected':''}>SiU6</option>
              <option value="SiZ6" ${_ofInstrument==='SiZ6'?'selected':''}>SiZ6</option>
              <option value="SiH7" ${_ofInstrument==='SiH7'?'selected':''}>SiH7</option>
              <option value="SiM7" ${_ofInstrument==='SiM7'?'selected':''}>SiM7</option>
            </select></td>
            <td><strong>Order Flow</strong> <span class="badge" style="background:#9C27B0">PYTHON</span>${paper}</td>
            <td>${accountDropdownHtml(_ofAccount, 'onOfRobotAccountChange(this.value)')}</td>
            <td class="${dirCls}">${dirText}${s.avgPrice > 0 ? ' @ ' + s.avgPrice.toFixed(0) : ''}</td>
            <td>—</td>
            <td class="${pnlCls(pnl)}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)} ₽</td>
            <td>${s.totalLots || 0}</td>
            <td>—</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="ofRobotApi('start')" ${mode==='running'?'disabled':''}>▶</button>
                <button class="btn btn-warning btn-sm" onclick="ofRobotApi('pause')" ${mode!=='running'?'disabled':''}>⏸</button>
                <button class="btn btn-danger btn-sm" onclick="ofRobotApi('stop')" ${mode==='stopped'?'disabled':''}>⏹</button>
            </td>
            <td class="${modeCls}">${modeText}</td>
        </tr>`;
    } else {
        ofRobotRow = `<tr ondblclick="ofRobotEditPanel()" style="cursor:pointer" title="Двойной клик — настройки робота">
            <td><select class="input" style="width:80px;font-size:11px" onchange="onOfRobotInstrumentChange(this.value)">
              <option value="SiU6" ${_ofInstrument==='SiU6'?'selected':''}>SiU6</option>
              <option value="SiZ6" ${_ofInstrument==='SiZ6'?'selected':''}>SiZ6</option>
              <option value="SiH7" ${_ofInstrument==='SiH7'?'selected':''}>SiH7</option>
              <option value="SiM7" ${_ofInstrument==='SiM7'?'selected':''}>SiM7</option>
            </select></td>
            <td><strong>Order Flow</strong> <span class="badge" style="background:#9C27B0">PYTHON</span></td>
            <td>${accountDropdownHtml(_ofAccount, 'onOfRobotAccountChange(this.value)')}</td>
            <td>—</td>
            <td>—</td>
            <td>—</td>
            <td>0</td>
            <td>—</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="ofRobotApi('start')">▶</button>
            </td>
            <td class="red">🔴 Не запущен</td>
        </tr>`;
    }

    // Order Flow MX Robot row
    let ofMxRobotRow = '';
    let _ofMxInstrument = localStorage.getItem('ofMxRobotInstrument') || 'MXU6';
    let _ofMxAccount = localStorage.getItem('ofMxRobotAccount') || '1225953';
    if (ofMxRobot) {
        const s = ofMxRobot;
        const mode = s.mode || 'stopped';
        const modeText = mode === 'running' ? '🟢 Работает' : mode === 'paused' ? '🟡 Пауза' : '🔴 Остановлен';
        const modeCls = mode === 'running' ? 'green' : mode === 'paused' ? 'yellow' : 'red';
        const dirText = s.direction === 'LONG' ? 'Лонг' : s.direction === 'SHORT' ? 'Шорт' : 'Флат';
        const dirCls = s.direction === 'LONG' ? 'green' : s.direction === 'SHORT' ? 'red' : '';
        const pnlCls = v => v >= 0 ? 'green' : 'red';
        const pnl = s.realizedPnL || 0;
        const paper = s.paper ? ' <span class="badge" style="background:#ff9800">PAPER</span>' : '';
        ofMxRobotRow = `<tr ondblclick="ofMxRobotEditPanel()" style="cursor:pointer" title="Двойной клик — настройки робота">
            <td><select class="input" style="width:80px;font-size:11px" onchange="onOfMxRobotInstrumentChange(this.value)">
              <option value="MXM6" ${_ofMxInstrument==='MXM6'?'selected':''}>MXM6</option>
              <option value="MXU6" ${_ofMxInstrument==='MXU6'?'selected':''}>MXU6</option>
              <option value="MXZ6" ${_ofMxInstrument==='MXZ6'?'selected':''}>MXZ6</option>
              <option value="MXH7" ${_ofMxInstrument==='MXH7'?'selected':''}>MXH7</option>
              <option value="MXM7" ${_ofMxInstrument==='MXM7'?'selected':''}>MXM7</option>
            </select></td>
            <td><strong>Order Flow</strong> <span class="badge" style="background:#007ACC">MX</span>${paper}</td>
            <td>${accountDropdownHtml(_ofMxAccount, 'onOfMxRobotAccountChange(this.value)')}</td>
            <td class="${dirCls}">${dirText}${s.avgPrice > 0 ? ' @ ' + s.avgPrice.toFixed(0) : ''}</td>
            <td>—</td>
            <td class="${pnlCls(pnl)}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)} ₽</td>
            <td>${s.totalLots || 0}</td>
            <td>—</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="ofMxRobotApi('start')" ${mode==='running'?'disabled':''}>▶</button>
                <button class="btn btn-warning btn-sm" onclick="ofMxRobotApi('pause')" ${mode!=='running'?'disabled':''}>⏸</button>
                <button class="btn btn-danger btn-sm" onclick="ofMxRobotApi('stop')" ${mode==='stopped'?'disabled':''}>⏹</button>
            </td>
            <td class="${modeCls}">${modeText}</td>
        </tr>`;
    } else {
        ofMxRobotRow = `<tr ondblclick="ofMxRobotEditPanel()" style="cursor:pointer" title="Двойной клик — настройки робота">
            <td><select class="input" style="width:80px;font-size:11px" onchange="onOfMxRobotInstrumentChange(this.value)">
              <option value="MXM6" ${_ofMxInstrument==='MXM6'?'selected':''}>MXM6</option>
              <option value="MXU6" ${_ofMxInstrument==='MXU6'?'selected':''}>MXU6</option>
              <option value="MXZ6" ${_ofMxInstrument==='MXZ6'?'selected':''}>MXZ6</option>
              <option value="MXH7" ${_ofMxInstrument==='MXH7'?'selected':''}>MXH7</option>
              <option value="MXM7" ${_ofMxInstrument==='MXM7'?'selected':''}>MXM7</option>
            </select></td>
            <td><strong>Order Flow</strong> <span class="badge" style="background:#007ACC">MX</span></td>
            <td>${accountDropdownHtml(_ofMxAccount, 'onOfMxRobotAccountChange(this.value)')}</td>
            <td>—</td>
            <td>—</td>
            <td>—</td>
            <td>0</td>
            <td>—</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="ofMxRobotApi('start')">▶</button>
            </td>
            <td class="red">🔴 Не запущен</td>
        </tr>`;
    }

    const allRows = [...serverStrategies, ...localRows, pythonRobotRow, ofRobotRow, ofMxRobotRow];
    if (!allRows.filter(r=>r).length) { tbody.innerHTML = ''; if (noMsg) noMsg.style.display = 'block'; return; }
    if (noMsg) noMsg.style.display = 'none';
    tbody.innerHTML = allRows.join('');
}

async function robotStart(i) {
    const r = robots[i];
    if (!r) return;
    let apiBase = getRobotApiBase(r);
    let body = {};
    
    if (r.strategy && r.strategy.includes('V8 Trail')) {
        body = {
            ticker: r.ticker || 'RTSM6',
            finamSymbol: r.finamSymbol || 'RTSM6@RTSX',
            stepPrice: parseFloat(r.stepPrice) || 14.86,
            slPct: parseFloat(r.slPct) || 0.10,
            emaPeriod: parseInt(r.ema) || 20,
            sarStart: parseFloat(r.sarStart) || 0.02,
            sarStep: parseFloat(r.sarStep) || 0.02,
            sarMax: parseFloat(r.sarMax) || 0.2,
            maxHoldMinutes: parseInt(r.holdMinutes) || 240
        };
    } else if (r.strategy && r.strategy.includes('VP Scalp Simple')) {
        body = {
            ticker: r.ticker || 'MXM6',
            slPct: parseFloat(r.slPct) || 0.20,
            maxHoldMinutes: parseInt(r.holdMinutes) || 60,
            vpLookback: parseInt(r.vpLookback) || 40,
            vpBins: parseInt(r.vpBins) || 30
        };
    } else if (r.strategy && r.strategy.includes('VP Scalp Grid Copy')) {
        body = {
            maxLevels: parseInt(r.maxGrid) || 100,
            stepBase: parseInt(r.gridStep) || 15,
            spreadBase: parseInt(r.gridSpread) || 50,
            maxHoldMinutes: parseInt(r.holdMinutes) || 60,
            rvAdaptation: r.rvAdaptation === true
        };
    } else if (r.strategy && r.strategy.includes('VP Scalp')) {
        body = {
            maxLevels: parseInt(r.maxGrid) || 100,
            stepBase: parseInt(r.gridStep) || 15,
            spreadBase: parseInt(r.gridSpread) || 50,
            maxHoldMinutes: parseInt(r.holdMinutes) || 60,
            vpLookback: parseInt(r.vpLookback) || 60,
            vpBinSize: parseInt(r.vpBinSize) || 50,
            vaPercent: parseFloat(r.vaPercent) || 0.7,
            minProfitPerLot: parseInt(r.minProfit) || 28,
            rvAdaptation: r.rvAdaptation === true
        };
    } else {
        const isV8 = r.strategy && r.strategy.includes('v8');
        apiBase = isV8 ? '/strategy/grid-mm-v8' : '/strategy/grid-mm-v7';
        body = {
            instrument: r.ticker || 'SiU6',
            sarStart: parseFloat(r.sarStart) || 0.009,
            sarStep: parseFloat(r.sarStep) || 0.01,
            sarMax: parseFloat(r.sarMax) || 0.2,
            emaPeriod: parseInt(r.ema) || 30,
            gridStep: parseFloat(r.gridStep) || 35,
            gridSpread: parseFloat(r.gridSpread) || 35,
            maxGridLevels: parseInt(r.maxGrid) || 70,
            closePct: parseFloat(r.closePct) || 0.30,
            minProfitPerLot: parseFloat(r.minProfit) || 35,
            maxLots: parseInt(r.lots) || 1,
            stdPeriod: parseInt(r.stdPeriod) || 14,
            stdMult: parseFloat(r.stdMult) || 1.5,
            gridHold: r.gridHold === true
        };
    }
    addLog(nowTime(), 'INFO', `▶ Запуск робота ${r.ticker} (${r.strategy})...`);
    try {
        const resp = await fetch(apiBase + '/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await resp.json();
        if (data.error) { addLog(nowTime(), 'ERROR', data.error); return; }
        r.status = 'running';
        r.exchangeStatus = 'Подключен';
        saveRobots(); renderRobots();
        addLog(nowTime(), 'INFO', `🤖 Робот ${r.strategy} ${r.ticker} запущен`);
    } catch (e) { addLog(nowTime(), 'ERROR', e.message); }
}

async function startLocalStorageRobot(idx) {
    const r = robots[idx];
    if (!r) return;

    const ticker = r.ticker || 'SiU6';

    // Already an instance with port — start via launcher
    if ((r.isInstance || r.port) && r.port) {
        try {
            const resp = await fetch('/api/instance/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ id: r.id, port: r.port })
            });
            const data = await resp.json();
            if (data.error) {
                addLog(nowTime(), 'ERROR', '❌ Ошибка запуска: ' + data.error);
            } else {
                r.status = 'starting';
                saveRobots();
                renderRobots();
                addLog(nowTime(), 'INFO', '▶ ' + ticker + ' запускается (port=' + r.port + ')');
                setTimeout(() => pollInstanceStatus(idx), 3000);
            }
        } catch(e) {
            addLog(nowTime(), 'ERROR', '❌ Ошибка: ' + e.message);
        }
        return;
    }

    // No instance yet — auto-create (always, for any ticker including SiU6)
    const usedPorts = robots.map(rb => rb.port || 0).filter(p => p > 0);
    let port = 5071;
    while (usedPorts.includes(port)) port++;
    const id = ticker + '_' + port;

    const params = {
        ticker: ticker,
        port: port,
        max_levels: parseInt(r.maxGrid || r.max_levels) || 100,
        step_base: parseInt(r.gridStep || r.step_base) || 31,
        spread_base: parseInt(r.gridSpread || r.spread_base) || 31,
        min_profit_per_lot: parseInt(r.minProfit || r.min_profit) || 35,
        vp_lookback: parseInt(r.vpLookback || r.vp_lookback) || 33,
        vp_bin_size: parseInt(r.vpBinSize || r.vp_bin_size) || 50,
        vp_va_percent: parseFloat(r.vpVaPercent || r.vp_va_percent) || 0.70,
        max_hold_minutes: parseInt(r.holdMinutes || r.hold_minutes) || 99999999999999,
        id: id
    };
    try {
        await fetch('/api/instance/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(params)
        });
    } catch(e) {}

    // Upgrade robot to instance
    r.isInstance = true;
    r.port = port;
    r.id = id;
    r.max_levels = params.max_levels;
    r.step_base = params.step_base;
    r.spread_base = params.spread_base;
    r.vp_lookback = params.vp_lookback;
    r.vp_bin_size = params.vp_bin_size;
    r.vp_va_percent = params.vp_va_percent;
    r.min_profit = params.min_profit_per_lot;
    saveRobots();

    // Now start via instance API
    return startLocalStorageRobot(idx);
}

async function pollInstanceStatus(idx) {
    const r = robots[idx];
    if (!r || !r.port) return;
    try {
        const resp = await fetch('/api/instance/status', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ port: r.port })
        });
        const data = await resp.json();
        if (!data.error) {
            r.status = 'running';
            r.position = data.direction > 0 ? 'Лонг' : data.direction < 0 ? 'Шорт' : '—';
            r.pnlTotal = data.realized_pnl || 0;
            r.lotsOpen = data.total_lots || 0;
        } else {
            r.status = 'stopped';
        }
    } catch(e) {
        r.status = 'stopped';
    }
    saveRobots();
    renderRobots();
}

async function stopLocalStorageRobot(idx) {
    const r = robots[idx];
    if (!r) return;
    const port = r.port || 5071;
    const id = r.id || (r.ticker + '_' + port);
    try {
        await fetch('/api/instance/stop', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ id: id, port: port })
        });
        r.status = 'stopped';
        saveRobots();
        renderRobots();
        addLog(nowTime(), 'INFO', '⏹ ' + (r.ticker || '?') + ' остановлен');
    } catch(e) {
        addLog(nowTime(), 'ERROR', '❌ Ошибка остановки: ' + e.message);
    }
}

async function pauseLocalStorageRobot(idx) {
    const r = robots[idx];
    if (!r) return;
    const port = r.port || 5071;
    try {
        await fetch('/api/instance/status', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ port: port, action: 'pause' })
        });
        r.status = 'paused';
        saveRobots();
        renderRobots();
        addLog(nowTime(), 'INFO', '⏸ ' + (r.ticker || '?') + ' на паузе');
    } catch(e) {
        addLog(nowTime(), 'ERROR', '❌ Ошибка паузы: ' + e.message);
    }
}

function openRobotEditPanel(idx) {
    // If no idx, open for live Python robot
    if (idx === undefined || idx === null) { pythonRobotEditPanel(); return; }
    const r = robots[idx];
    if (!r) { pythonRobotEditPanel(); return; }
    
    // Normalize params — support both old and new format
    const p = {
        maxLevels: r.isInstance ? (r.max_levels||100) : (r.maxGrid||100),
        stepBase: r.isInstance ? (r.step_base||31) : (r.gridStep||31),
        spreadBase: r.isInstance ? (r.spread_base||31) : (r.gridSpread||31),
        holdMinutes: r.isInstance ? (r.hold_minutes||99999999999999) : (r.holdMinutes||99999999999999),
        lookback: r.isInstance ? (r.vp_lookback||33) : (r.vpLookback||33),
        binSize: r.isInstance ? (r.vp_bin_size||50) : (r.vpBinSize||50),
        vaPercent: r.isInstance ? (r.vp_va_percent||0.70) : (r.vpVaPercent||0.70),
        minProfit: r.isInstance ? (r.min_profit||35) : (r.minProfit||35),
        rvAdapt: r.rvAdaptation||false
    };
    const robotPort = r.isInstance ? r.port : 5070;

    const existing = el('robotEditPanel');
    if (existing) { existing.remove(); return; }

    const div = document.createElement('div');
    div.id = 'robotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            🐍 ${r.ticker} VP Scalp Grid (PYTHON)
            <button class="btn btn-primary btn-sm" onclick="saveRobotFromPanel(${idx})">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="if(window._pyVpTimer){clearInterval(window._pyVpTimer);window._pyVpTimer=null;}el('robotEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:12px">
            <!-- VP индикаторы -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px">
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#60A5FA">VAH</div><div id="pyVAH" style="font-size:18px;font-weight:bold;color:#60A5FA">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#F59E0B">POC</div><div id="pyPOC" style="font-size:18px;font-weight:bold;color:#F59E0B">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#34D399">VAL</div><div id="pyVAL" style="font-size:18px;font-weight:bold;color:#34D399">—</div></div>
                <div class="metric-card"><div class="metric-label">Цена</div><div id="pyPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Позиция</div><div id="pyDir" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Лоты</div><div id="pyLots" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Grid</div><div id="pyGrid" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Ср. цена</div><div id="pyAvgPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL/лот</div><div id="pyPnlPerLot" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL нереал.</div><div id="pyPnlUnreal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Hold</div><div id="pyHold" style="font-size:18px;font-weight:bold">0 мин</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Max Levels</div><input id="editPyMaxLevels" class="input" type="number" value="${p.maxLevels}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Step Base (пт)</div><input id="editPyStepBase" class="input" type="number" value="${p.stepBase}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Spread Base (пт)</div><input id="editPySpreadBase" class="input" type="number" value="${p.spreadBase}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editPyMaxHold" class="input" type="number" value="${p.holdMinutes}" style="width:100px"></div>
                <div class="metric-card"><div class="metric-label">VP Lookback</div><input id="editPyLookback" class="input" type="number" value="${p.lookback}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editPyBinSize" class="input" type="number" value="${p.binSize}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VA %</div><input id="editPyVaPercent" class="input" type="number" step="0.05" value="${p.vaPercent}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">PnL/лот (пт)</div><input id="editPyMinProfit" class="input" type="number" value="${p.minProfit}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">RV Adaptation</div><br><input id="editPyRvAdapt" type="checkbox" style="width:20px;height:20px;vertical-align:middle" ${p.rvAdapt?'checked':''}></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>📋 Торговый журнал</strong>
            </div>
            <div id="pyJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="pyJournalFilter" class="input" style="width:120px" onchange="pyRenderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="pyJournalDateFrom" class="input" type="date" style="width:130px" onchange="pyLoadJournal()">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="pyJournalDateTo" class="input" type="date" style="width:130px" onchange="pyLoadJournal()">
                <button class="btn btn-secondary btn-sm" onclick="pyLoadJournal()">🔄</button>
            </div>
            <div style="max-height:350px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:8px;text-align:left">📅 Дата</th>
                            <th style="padding:8px;text-align:left">⏰ Вход</th>
                            <th style="padding:8px;text-align:left">⏰ Выход</th>
                            <th style="padding:8px;text-align:center">↔️</th>
                            <th style="padding:8px;text-align:right">💰 Вход</th>
                            <th style="padding:8px;text-align:right">💰 Выход</th>
                            <th style="padding:8px;text-align:right">Лоты</th>
                            <th style="padding:8px;text-align:right">PnL</th>
                            <th style="padding:8px;text-align:right">Кумул.</th>
                        </tr>
                    </thead>
                    <tbody id="pyJournalBody" style="background:var(--bg)"><tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Нет данных</td></tr></tbody>
                </table>
            </div>
            <div id="pyJournalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px"></div>
        </div>
    `;
    document.body.appendChild(div);

    // Store port for VP updates
    window._editRobotPort = robotPort;

    // Live VP update
    pythonRobotUpdateVp();
    if (window._pyVpTimer) clearInterval(window._pyVpTimer);
    window._pyVpTimer = setInterval(() => pythonRobotUpdateVp(), 2000);
    // Load trade journal filtered by robot ticker
    _pyJournalTicker = r.ticker || 'SiU6';
    setTimeout(() => pyLoadJournal(), 300);
}

async function saveRobotFromPanel(idx) {
    if (idx === undefined || idx < 0 || idx >= robots.length) return;
    const r = robots[idx];
    // Save to robot object (support both formats)
    const vals = {
        maxLevels: el('editPyMaxLevels')?.value,
        stepBase: el('editPyStepBase')?.value,
        spreadBase: el('editPySpreadBase')?.value,
        holdMinutes: el('editPyMaxHold')?.value,
        lookback: el('editPyLookback')?.value,
        binSize: el('editPyBinSize')?.value,
        vaPercent: el('editPyVaPercent')?.value,
        minProfit: el('editPyMinProfit')?.value,
        rvAdapt: el('editPyRvAdapt')?.checked || false
    };
    if (r.isInstance || r.port) {
        r.max_levels = parseInt(vals.maxLevels) || r.max_levels;
        r.step_base = parseInt(vals.stepBase) || r.step_base;
        r.spread_base = parseInt(vals.spreadBase) || r.spread_base;
        r.hold_minutes = parseInt(vals.holdMinutes) || r.hold_minutes;
        r.vp_lookback = parseInt(vals.lookback) || r.vp_lookback;
        r.vp_bin_size = parseInt(vals.binSize) || r.vp_bin_size;
        r.vp_va_percent = parseFloat(vals.vaPercent) || r.vp_va_percent;
        r.min_profit = parseInt(vals.minProfit) || r.min_profit;
    } else {
        r.maxGrid = vals.maxLevels || r.maxGrid;
        r.gridStep = vals.stepBase || r.gridStep;
        r.gridSpread = vals.spreadBase || r.gridSpread;
        r.holdMinutes = vals.holdMinutes || r.holdMinutes;
        r.vpLookback = vals.lookback || r.vpLookback;
        r.vpBinSize = vals.binSize || r.vpBinSize;
        r.vpVaPercent = vals.vaPercent || r.vpVaPercent;
        r.minProfit = vals.minProfit || r.minProfit;
        r.rvAdaptation = vals.rvAdapt;
    }
    saveRobots();

    const cfg = {
        max_levels: parseInt(vals.maxLevels),
        step_base: parseInt(vals.stepBase),
        spread_base: parseInt(vals.spreadBase),
        min_profit_per_lot: parseInt(vals.minProfit),
        vp_lookback: parseInt(vals.lookback),
        vp_bin_size: parseInt(vals.binSize),
        vp_va_percent: parseFloat(vals.vaPercent),
        rv_adaptation: vals.rvAdapt,
        max_hold_minutes: parseInt(vals.holdMinutes)
    };

    // For instances — send config + VP hot-update to instance API via proxy
    if (r.port && r.port !== 5070) {
        // Update instance config file
        try {
            await fetch('/api/instance/create', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ ...cfg, ticker: r.ticker, port: r.port, id: r.id })
            });
        } catch(e) {}
        // VP hot-update via proxy
        try {
            const resp = await fetch('/api/instance/status', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ port: r.port, action: 'update_config', config: cfg })
            });
            addLog(nowTime(), 'INFO', '💾 Config saved (instance): VP LB=' + cfg.vp_lookback);
        } catch(e) {
            addLog(nowTime(), 'WARN', 'Config saved locally, instance offline');
        }
        return;
    }

    // Main robot or legacy
    let saved = false;
    try {
        const resp = await fetch(ROBOT_API + '/api/robot/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(cfg)
        });
        const data = await resp.json();
        addLog(nowTime(), 'INFO', '💾 Config saved (live): ' + JSON.stringify(data));
        saved = true;
    } catch(e) {
        try {
            await fetch('/api/robot/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(cfg)
            });
            addLog(nowTime(), 'INFO', '💾 Config saved (proxy)');
            saved = true;
        } catch(e2) {
            addLog(nowTime(), 'ERROR', '💾 Save failed');
        }
    }
    addLog(nowTime(), 'INFO', '💾 Параметры робота ' + r.ticker + ' сохранены');
}

function deleteRobot(idx) {
    if (idx >= 0 && idx < robots.length) {
        const r = robots[idx];
        if (r.isInstance || r.port) {
            // Stop + delete instance on server
            fetch('/api/instance/stop', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ id: r.id })
            }).catch(() => {});
            fetch('/api/instance/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ id: r.id })
            }).catch(() => {});
        }
        robots.splice(idx, 1);
        saveRobots();
        renderRobots();
        addLog(nowTime(), 'INFO', '🗑 Робот ' + (r.ticker||'') + ' удалён');
    }
}

function getRobotApiBase(r) {
    if (r.strategy && r.strategy.includes('V8 Trail')) return '/strategy/v8-trail';
    if (r.strategy && r.strategy.includes('VP Scalp Simple')) return '/strategy/vp-simple';
    if (r.strategy && r.strategy.includes('VP Scalp Grid Copy')) return '/strategy/vp-copy';
    if (r.strategy && r.strategy.includes('VP Scalp')) return '/strategy/vp-scalp-grid';
    if (r.strategy && r.strategy.includes('v8')) return '/strategy/grid-mm-v8';
    return '/strategy/grid-mm-v7';
}

async function robotPause(i) {
    const r = robots[i];
    if (!r) return;
    const apiBase = getRobotApiBase(r);
    try {
        await fetch(apiBase + '/pause', { method: 'POST' });
        r.status = 'paused';
        saveRobots(); renderRobots();
        addLog(nowTime(), 'INFO', `⏸ Робот ${r.ticker} на паузе`);
    } catch (e) { addLog(nowTime(), 'ERROR', e.message); }
}

async function robotStop(i) {
    const r = robots[i];
    if (!r) return;
    r._stopping = true; renderRobots();
    addLog(nowTime(), 'INFO', `⏹ Остановка робота ${r.ticker}, закрытие позиций...`);
    try {
        // For Python robots: call via C# proxy to correct port
        let resp;
        if (r.port || r.isInstance) {
            resp = await fetch('/api/instance/stop', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ id: r.id, port: r.port })
            });
        } else if (r.strategy && r.strategy.includes('PYTHON')) {
            // Main Python robot via C# proxy
            resp = await fetch('/api/robot/stop', { method: 'POST' });
        } else {
            const apiBase = getRobotApiBase(r);
            resp = await fetch(apiBase + '/stop', { method: 'POST' });
        }
        const data = await resp.json();
        r.status = 'stopped';
        r._stopping = false;
        r.position = '—'; r.lotsOpen = 0; r.go = 0;
        saveRobots(); renderRobots();
        addLog(nowTime(), 'INFO', `⏹ Робот ${r.ticker} остановлен`);
    } catch (e) { r._stopping = false; renderRobots(); addLog(nowTime(), 'ERROR', e.message); }
}

function robotRemove(i) {
    const r = robots[i];
    if (r && r.status === 'running') { addLog(nowTime(), 'WARN', `Сначала остановите робота ${r.ticker}`); return; }
    robots.splice(i, 1);
    saveRobots(); renderRobots();
    addLog(nowTime(), 'INFO', `🗑 Робот удалён`);
}

// Обновление данных роботов из стратегии
async function updateRobotData() {
    if (!robots.length) return;
    for (const r of robots) {
        if (r.status !== 'running' && r.status !== 'paused') continue;
        try {
            const apiBase = getRobotApiBase(r);
            const resp = await fetch(apiBase + '/status');
            const data = await resp.json();
            if (data.status === 'not_running' || data.status === 'not_initialized') continue;
            // Use brokerPnL if available, otherwise realizedPnL
            r.pnlToday = data.brokerPnL ?? data.realizedPnL ?? data.pnl ?? (data.detail?.realizedPnL ?? 0);
            r.pnlTotal = data.brokerPnL ?? data.realizedPnL ?? data.totalPnl ?? data.pnl ?? (data.detail?.realizedPnL ?? 0);
            r.position = data.dirStr || (data.direction === 1 || data.detail?.posDir === 1 ? 'Лонг' : data.direction === -1 || data.detail?.posDir === -1 ? 'Шорт' : '—');
            r.lotsOpen = data.totalLots || data.openLots || (data.detail?.posDir !== 0 ? 1 : 0);
            r.exchangeStatus = data.connected ? 'Биржа OK' : 'Нет связи';
            if (data.status === 'stopped') r.status = 'stopped';
            if (data.status === 'paused') r.status = 'paused';
        } catch (e) {}
    }
    saveRobots(); renderRobots();
}

// Начальный рендер
loadFinamAccounts().then(() => renderRobots());
loadAccounts();
// Автообновление активных стратегий каждые 5 сек
setInterval(renderRobots, 30000);
// Обновление данных роботов при необходимости (fallback)
setInterval(updateRobotData, 30000);

function editRobot(i) {
    const r = robots[i];
    if (!r) return;
    const panel = el('robotEditPanel');
    if (panel) { 
        panel.remove(); 
    }

    // Check if VP Scalp Grid Copy
    const isVpCopy = r.strategy && r.strategy.includes('VP Scalp Grid Copy');
    const isVpSimple = r.strategy && r.strategy.includes('VP Scalp Simple');
    const isV8Trail = r.strategy && r.strategy.includes('V8 Trail');
    const isVpScalp = r.strategy && r.strategy.includes('VP Scalp Grid') && !isVpCopy && !isVpSimple;

    let paramsHtml = '';
    if (isV8Trail) {
        paramsHtml = `
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">SL %</div><input id="editSlPct" class="input" type="number" step="0.01" value="${r.slPct||0.10}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">EMA</div><input id="editEma" class="input" type="number" value="${r.ema||20}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">SAR Start</div><input id="editSarStart" class="input" type="number" step="0.005" value="${r.sarStart||0.02}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">SAR Step</div><input id="editSarStep" class="input" type="number" step="0.005" value="${r.sarStep||0.02}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">SAR Max</div><input id="editSarMax" class="input" type="number" step="0.05" value="${r.sarMax||0.2}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editHoldMinutes" class="input" type="number" value="${r.holdMinutes||240}" style="width:70px"></div>
            </div>`;
    } else if (isVpSimple) {
        paramsHtml = `
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">SL %</div><input id="editSlPct" class="input" type="number" step="0.05" value="${r.slPct||0.20}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editHoldMinutes" class="input" type="number" value="${r.holdMinutes||60}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Lookback</div><input id="editVpLookback" class="input" type="number" value="${r.vpLookback||40}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Bins</div><input id="editVpBins" class="input" type="number" value="${r.vpBins||30}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VA %</div><input id="editVaPercent" class="input" type="number" step="0.05" value="${r.vaPercent||0.70}" style="width:70px"></div>
            </div>
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:8px">
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#60A5FA">VAH</div><div id="vpVAH" style="font-size:18px;font-weight:bold;color:#60A5FA">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#F59E0B">POC</div><div id="vpPOC" style="font-size:18px;font-weight:bold;color:#F59E0B">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#34D399">VAL</div><div id="vpVAL" style="font-size:18px;font-weight:bold;color:#34D399">—</div></div>
            </div>`;
    } else if (isVpCopy) {
        paramsHtml = `
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Max Levels</div><input id="editMaxGrid" class="input" type="number" value="${r.maxGrid||100}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Step Base (пт)</div><input id="editGridStep" class="input" type="number" value="${r.gridStep||15}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Spread Base (пт)</div><input id="editGridSpread" class="input" type="number" value="${r.gridSpread||15}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editHoldMinutes" class="input" type="number" value="${r.holdMinutes||60}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Lookback</div><input id="editVpLookback" class="input" type="number" value="${r.vpLookback||60}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editVpBinSize" class="input" type="number" value="${r.vpBinSize||50}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VA %</div><input id="editVaPercent" class="input" type="number" step="0.05" value="${r.vaPercent||0.70}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Min Profit/лот</div><input id="editMinProfit" class="input" type="number" value="${r.minProfit||28}" style="width:70px"></div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px"><div class="metric-label">RV Adaptation</div><input id="editRvAdapt" type="checkbox" ${r.rvAdaptation!==false?'checked':''} style="width:20px;height:20px"></div>
                <div class="metric-card"><div class="metric-label">RV Info</div><span id="rvInfo" style="font-size:12px;color:#9CA3AF">—</span></div>
            </div>`;
    } else if (isVpScalp) {
        paramsHtml = `
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Max Levels</div><input id="editMaxGrid" class="input" type="number" value="${r.maxGrid||100}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Step Base (пт)</div><input id="editGridStep" class="input" type="number" value="${r.gridStep||15}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Spread Base (пт)</div><input id="editGridSpread" class="input" type="number" value="${r.gridSpread||50}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editHoldMinutes" class="input" type="number" value="${r.holdMinutes||60}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Lookback</div><input id="editVpLookback" class="input" type="number" value="${r.vpLookback||60}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editVpBinSize" class="input" type="number" value="${r.vpBinSize||50}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VA %</div><input id="editVaPercent" class="input" type="number" step="0.05" value="${r.vaPercent||0.70}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">PnL/лот (пт)</div><input id="editMinProfit" class="input" type="number" value="${r.minProfit||28}" style="width:70px"></div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px"><div class="metric-label">RV Adaptation</div><input id="editRvAdapt" type="checkbox" ${r.rvAdaptation!==false?'checked':''} style="width:20px;height:20px"></div>
            </div>
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:8px">
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#60A5FA">VAH</div><div id="vpVAH" style="font-size:18px;font-weight:bold;color:#60A5FA">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#F59E0B">POC</div><div id="vpPOC" style="font-size:18px;font-weight:bold;color:#F59E0B">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#34D399">VAL</div><div id="vpVAL" style="font-size:18px;font-weight:bold;color:#34D399">—</div></div>
            </div>`;
    } else {
        paramsHtml = `
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Счёт</div><select id="editAccount" class="input" style="width:180px"><option value="${r.account}">${r.accountName || r.account}</option></select></div>
                <div class="metric-card"><div class="metric-label">SAR Start</div><input id="editSarStart" class="input" type="number" step="0.001" value="${r.sarStart}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">SAR Step</div><input id="editSarStep" class="input" type="number" step="0.001" value="${r.sarStep}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">SAR Max</div><input id="editSarMax" class="input" type="number" step="0.01" value="${r.sarMax}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">EMA</div><input id="editEma" class="input" type="number" value="${r.ema}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Grid Step</div><input id="editGridStep" class="input" type="number" value="${r.gridStep}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Grid Spread</div><input id="editGridSpread" class="input" type="number" value="${r.gridSpread}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Max Grid</div><input id="editMaxGrid" class="input" type="number" value="${r.maxGrid}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Close %</div><input id="editClosePct" class="input" type="number" step="0.01" value="${r.closePct}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Лоты</div><input id="editLots" class="input" type="number" value="${r.lots}" style="width:60px"></div>
            </div>`;
    }

    // Create floating edit panel
    const div = document.createElement('div');
    div.id = 'robotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            🤖 Робот: ${r.ticker} (${r.strategy})
            <button class="btn btn-primary btn-sm" onclick="saveRobotEdit(${i})">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="if(_journalRefreshTimer){clearInterval(_journalRefreshTimer);_journalRefreshTimer=null;}if(window._vpIndTimer){clearInterval(window._vpIndTimer);window._vpIndTimer=null;}el('robotEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:12px">
            <!-- Параметры робота -->
            ${paramsHtml}
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Сводка -->
            <div id="journalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <!-- Фильтры -->
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="journalFilter" class="input" style="width:120px" onchange="renderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="journalDateFrom" class="input" type="date" style="width:130px" onchange="if(_journalRobot)loadTradeJournal(_journalRobot)">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="journalDateTo" class="input" type="date" style="width:130px" onchange="if(_journalRobot)loadTradeJournal(_journalRobot)">
                <button class="btn btn-secondary btn-sm" onclick="loadTradeJournal(robots[${i}])">🔄</button>
            </div>
            <!-- Таблица сделок -->
            <div style="max-height:400px;overflow-y:auto;border:1px solid #2D2D44;border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:8px;text-align:left">📅 Дата</th>
                            <th style="padding:8px;text-align:left">⏰ Вход</th>
                            <th style="padding:8px;text-align:left">⏰ Выход</th>
                            <th style="padding:8px;text-align:center">↔️ Напр.</th>
                            <th style="padding:8px;text-align:right">💰 Вход</th>
                            <th style="padding:8px;text-align:right">💰 Выход</th>
                            <th style="padding:8px;text-align:right">📊 Лоты</th>
                            <th style="padding:8px;text-align:right">💵 PnL</th>
                            <th style="padding:8px;text-align:right">📈 Накопл.</th>
                        </tr>
                    </thead>
                    <tbody id="journalBody" style="background:var(--bg)"></tbody>
                </table>
            </div>
            <div id="journalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px">Загрузка данных...</div>
        </div>`;
    document.body.appendChild(div);
    // Set date range: from=30 days ago, to=today
    const today = new Date();
    const todayStr = today.toISOString().slice(0, 10);
    const from = new Date(today); from.setDate(from.getDate() - 30);
    const fromStr = from.toISOString().slice(0, 10);
    if (el('journalDateFrom')) el('journalDateFrom').value = fromStr;
    if (el('journalDateTo')) el('journalDateTo').value = todayStr;
    loadAccountsInto('editAccount', r.account);
    loadTradeJournal(r);
    // Start VP indicators realtime update
    if (isVpScalp || isVpSimple) {
        updateVpIndicators(r);
        if (window._vpIndTimer) clearInterval(window._vpIndTimer);
        window._vpIndTimer = setInterval(() => updateVpIndicators(r), 2000);
    }
}

async function updateVpIndicators(robot) {
    if (!el('vpVAH')) return;
    try {
        const apiBase = getRobotApiBase(robot);
        const resp = await fetch(apiBase + '/status');
        if (!resp.ok) return;
        const d = await resp.json();
        if (d.vah != null) el('vpVAH').textContent = Math.round(d.vah);
        if (d.poc != null) el('vpPOC').textContent = Math.round(d.poc);
        if (d.val != null) el('vpVAL').textContent = Math.round(d.val);
    } catch {}
}

async function loadAccountsInto(selectId, currentVal) {
    try {
        const resp = await fetch('/api/accounts');
        const data = await resp.json();
        const sel = el(selectId);
        if (!sel) return;
        sel.innerHTML = (data.accounts || []).map(a => `<option value="${a.id}" ${a.id === currentVal ? 'selected' : ''}>${a.id} — ${a.name} (${(a.balance||0).toLocaleString('ru-RU')}₽)</option>`).join('');
    } catch (e) {}
}

let _journalTrades = [];
let _journalPositions = [];
let _journalRobot = null;
let _journalRefreshTimer = null;

async function loadTradeJournal(robot) {
    const info = el('journalInfo');
    const body = el('journalBody');
    if (!body) return;
    
    _journalRobot = robot;
    
    // Stop previous refresh timer
    if (_journalRefreshTimer) { clearInterval(_journalRefreshTimer); _journalRefreshTimer = null; }
    
    if (info) info.textContent = 'Загрузка сделок...';
    body.innerHTML = '';
    
    const dateFrom = el('journalDateFrom')?.value;
    const dateTo = el('journalDateTo')?.value;
    let dateParam = '';
    if (dateFrom) dateParam += `&dateFrom=${dateFrom}`;
    if (dateTo) dateParam += `&dateTo=${dateTo}`;
    const queryStr = dateParam ? '?' + dateParam.substring(1) : '';

    try {
        const resp = await fetch('/api/trades' + queryStr);
        const raw = await resp.json();
        let trades = Array.isArray(raw) ? raw : (raw.trades || []);
        
        // Filter by robot ticker
        const ticker = robot.ticker || 'SiU6';
        trades = trades.filter(t => {
            const tTicker = t.symbol || t.ticker || t.Ticker || '';
            return tTicker.includes(ticker) || tTicker.includes(ticker.replace('@RTSX', ''));
        });
        
        // Normalize trade fields (Finam format: price.value, size.value, SIDE_BUY/SIDE_SELL)
        _journalTrades = trades.map(t => ({
            time: t.timestamp || t.time || t.trade_date || t.datetime || '',
            ticker: t.symbol || t.ticker || t.Ticker || ticker,
            dir: (t.side || t.direction || t.dir || '').replace('SIDE_', '').toUpperCase(),
            price: parseFloat((t.price && t.price.value) ? t.price.value : (t.price || t.Price || 0)),
            lots: parseInt((t.size && t.size.value) ? t.size.value : (t.quantity || t.lots || t.Lots || t.qty || 0)),
            comment: t.comment || t.Comment || t.order_comment || ''
        }));
        
        _journalTrades.sort((a, b) => a.time.localeCompare(b.time));
        _journalPositions = groupIntoPositions(_journalTrades);
        renderJournal();
        
        // Auto-refresh every 10 seconds if viewing today
        const isToday = !dateVal || dateVal === new Date().toISOString().slice(0, 10);
        if (isToday && robot.status === 'running') {
            _journalRefreshTimer = setInterval(() => loadTradeJournal(robot), 10000);
        }
        
    } catch (e) {
        if (info) info.textContent = 'Ошибка загрузки: ' + e.message;
        loadJournalFromStrategy(robot);
    }
}

function groupIntoPositions(trades) {
    const positions = [];
    let current = null;
    let netPos = 0; // positive = long, negative = short
    let cumPnl = 0;
    
    trades.forEach(t => {
        const isBuy = t.dir === 'BUY' || t.dir === '1' || t.dir === 'B';
        const lots = isBuy ? t.lots : -t.lots;
        
        if (!current) {
            // New position
            current = {
                entryTime: t.time,
                exitTime: t.time,
                direction: isBuy ? 'LONG' : 'SHORT',
                entryPrice: t.price,
                exitPrice: t.price,
                totalLots: t.lots,
                maxLots: t.lots,
                trades: [t],
                realizedPnL: 0,
                commission: Math.round(t.lots * 0.90), // 0.90₽ RT
                cumPnl: 0,
                comments: t.comment ? [t.comment] : []
            };
            netPos = lots;
        } else {
            // Adding to or closing position
            const prevNet = netPos;
            netPos += lots;
            
            // Calculate partial PnL if closing
            if ((prevNet > 0 && !isBuy) || (prevNet < 0 && isBuy)) {
                const closingLots = Math.min(Math.abs(lots), Math.abs(prevNet));
                const pnlPerLot = prevNet > 0 ? (t.price - current.exitPrice) : (current.exitPrice - t.price);
                current.realizedPnL += pnlPerLot * closingLots;
            }
            
            current.exitTime = t.time;
            current.exitPrice = t.price;
            current.maxLots = Math.max(current.maxLots, Math.abs(netPos));
            current.trades.push(t);
            current.commission += Math.round(t.lots * 0.90);
            if (t.comment && !current.comments.includes(t.comment)) current.comments.push(t.comment);
            
            // Position closed?
            if (netPos === 0) {
                current.totalLots = current.maxLots;
                current.netPnL = current.realizedPnL - current.commission;
                cumPnl += current.netPnL;
                current.cumPnl = cumPnl;
                positions.push(current);
                current = null;
                netPos = 0;
            }
        }
    });
    
    // Open position (not closed yet)
    if (current) {
        current.totalLots = current.maxLots;
        current.netPnL = current.realizedPnL - current.commission;
        current.isOpen = true;
        cumPnl += current.netPnL;
        current.cumPnl = cumPnl;
        positions.push(current);
    }
    
    return positions;
}

async function loadJournalFromStrategy(robot) {
    const info = el('journalInfo');
    try {
        const isV8 = robot.strategy && robot.strategy.includes('v8');
        const apiBase = isV8 ? '/strategy/grid-mm-v8' : '/strategy/grid-mm-v7';
        const resp = await fetch(apiBase + '/status');
        const data = await resp.json();
        
        const summary = el('journalSummary');
        if (summary && data.detail) {
            const d = data.detail;
            summary.innerHTML = `
                <div class="metric-card"><div class="metric-label">Round Trips</div><div style="font-size:1.2em;font-weight:700;color:var(--accent)">${d.roundTrips || 0}</div></div>
                <div class="metric-card"><div class="metric-label">PnL</div><div style="font-size:1.2em;font-weight:700;color:${(d.totalPnL||0) >= 0 ? 'var(--green)' : 'var(--red)'}">${(d.totalPnL||0) >= 0 ? '+' : ''}${(d.totalPnL||0).toFixed(0)} ₽</div></div>
                <div class="metric-card"><div class="metric-label">Позиция</div><div style="font-size:1.2em;font-weight:700">${d.position || '—'}</div></div>
                <div class="metric-card"><div class="metric-label">Grid уровни</div><div style="font-size:1.2em;font-weight:700">${d.filledLevels || 0} / ${d.maxGridLevels || '?'}</div></div>
            `;
        }
        if (info) info.textContent = 'Данные из стратегии (сделки брокера недоступны)';
    } catch (e2) {
        if (info) info.textContent = 'Нет данных';
    }
}

function renderJournal() {
    const body = el('journalBody');
    const info = el('journalInfo');
    const summary = el('journalSummary');
    if (!body) return;
    
    const filter = el('journalFilter')?.value || 'all';
    let positions = _journalPositions;
    if (filter === 'LONG') positions = positions.filter(p => p.direction === 'LONG');
    else if (filter === 'SHORT') positions = positions.filter(p => p.direction === 'SHORT');
    else if (filter === 'win') positions = positions.filter(p => p.netPnL > 0);
    else if (filter === 'loss') positions = positions.filter(p => p.netPnL < 0);
    
    // === Summary stats (QScalp style) ===
    const wins = positions.filter(p => p.netPnL > 0);
    const losses = positions.filter(p => p.netPnL < 0);
    const winRate = positions.length ? (wins.length / positions.length * 100).toFixed(1) : '—';
    const totalPnl = positions.reduce((s, p) => s + p.netPnL, 0);
    const avgWin = wins.length ? wins.reduce((s, p) => s + p.netPnL, 0) / wins.length : 0;
    const avgLoss = losses.length ? losses.reduce((s, p) => s + p.netPnL, 0) / losses.length : 0;
    const grossProfit = wins.reduce((s, p) => s + p.netPnL, 0);
    const grossLoss = Math.abs(losses.reduce((s, p) => s + p.netPnL, 0));
    const pf = grossLoss > 0 ? (grossProfit / grossLoss).toFixed(2) : grossProfit > 0 ? '∞' : '—';
    const totalComm = positions.reduce((s, p) => s + p.commission, 0);
    
    // Max drawdown (from cumPnl peak)
    let maxDD = 0, peak = 0;
    positions.forEach(p => {
        if (p.cumPnl > peak) peak = p.cumPnl;
        const dd = peak - p.cumPnl;
        if (dd > maxDD) maxDD = dd;
    });
    
    if (summary) {
        summary.innerHTML = `
            <div class="metric-card"><div class="metric-label">Всего позиций</div><div style="font-size:1.2em;font-weight:700;color:var(--accent)">${positions.length}</div></div>
            <div class="metric-card"><div class="metric-label">Win / Loss</div><div style="font-size:1.2em;font-weight:700"><span style="color:var(--green)">${wins.length}</span> / <span style="color:var(--red)">${losses.length}</span></div></div>
            <div class="metric-card"><div class="metric-label">Win Rate</div><div style="font-size:1.2em;font-weight:700;color:${parseFloat(winRate) >= 50 ? 'var(--green)' : 'var(--red)'}">${winRate}%</div></div>
            <div class="metric-card"><div class="metric-label">PnL (нетто)</div><div style="font-size:1.2em;font-weight:700;color:${totalPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)} ₽</div></div>
            <div class="metric-card"><div class="metric-label">Profit Factor</div><div style="font-size:1.2em;font-weight:700">${pf}</div></div>
            <div class="metric-card"><div class="metric-label">Avg Win</div><div style="font-size:1.2em;font-weight:700;color:var(--green)">${avgWin >= 0 ? '+' : ''}${avgWin.toFixed(0)} ₽</div></div>
            <div class="metric-card"><div class="metric-label">Avg Loss</div><div style="font-size:1.2em;font-weight:700;color:var(--red)">${avgLoss.toFixed(0)} ₽</div></div>
            <div class="metric-card"><div class="metric-label">Комиссия</div><div style="font-size:1.2em;font-weight:700;color:#9CA3AF">${totalComm.toLocaleString('ru-RU')} ₽</div></div>
            <div class="metric-card"><div class="metric-label">Max DD</div><div style="font-size:1.2em;font-weight:700;color:var(--red)">${maxDD > 0 ? '-' : ''}${maxDD.toFixed(0)} ₽</div></div>
        `;
    }
    
    if (!positions.length) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Нет закрытых позиций</td></tr>';
        if (info) info.textContent = 'Нет позиций за выбранный период';
        return;
    }
    
    // Render positions (newest first)
    const sorted = [...positions].reverse();
    body.innerHTML = sorted.map((p, idx) => {
        const pnlCls = p.netPnL >= 0 ? 'var(--green)' : 'var(--red)';
        const dirCls = p.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
        const dirIcon = p.direction === 'LONG' ? '🟢' : '🔴';
        const entryT = p.entryTime ? p.entryTime.replace(/\.\d+Z$/, '').replace('T', ' ').slice(11, 19) : '—';
        const exitT = p.exitTime ? p.exitTime.replace(/\.\d+Z$/, '').replace('T', ' ').slice(11, 19) : '—';
        const dateStr = p.entryTime ? p.entryTime.slice(0, 10) : '';
        const pnlSign = p.netPnL >= 0 ? '+' : '';
        const cumSign = p.cumPnl >= 0 ? '+' : '';
        const bg = p.isOpen ? 'rgba(255,215,0,0.05)' : (idx % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)');
        const openBadge = p.isOpen ? ' <span style="color:#FFD700;font-size:10px">⚡ОТКРЫТА</span>' : '';
        const comments = p.comments.filter(c => c).join('; ');
        
        return `<tr style="border-bottom:1px solid #2D2D44;background:${bg}">
            <td style="padding:6px 8px;font-size:11px;color:#9CA3AF">${dateStr}</td>
            <td style="padding:6px 8px;font-family:monospace;font-size:12px">${entryT}</td>
            <td style="padding:6px 8px;font-family:monospace;font-size:12px">${exitT}${openBadge}</td>
            <td style="padding:6px 8px;text-align:center;color:${dirCls};font-weight:700;font-size:12px">${dirIcon} ${p.direction}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:600">${p.entryPrice.toFixed(2)}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:600">${p.exitPrice.toFixed(2)}</td>
            <td style="padding:6px 8px;text-align:right">${p.totalLots}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:700;color:${pnlCls}">${pnlSign}${p.netPnL.toFixed(0)} ₽</td>
            <td style="padding:6px 8px;text-align:right;font-size:11px;color:${p.cumPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${cumSign}${p.cumPnl.toFixed(0)}</td>
        </tr>
        ${comments ? `<tr style="background:${bg}"><td colspan="9" style="padding:2px 8px 6px;font-size:10px;color:#9CA3AF;padding-left:42px">📝 ${comments}</td></tr>` : ''}`;
    }).join('');
    
    if (info) info.textContent = `${positions.length} позиций · ${wins.length}W / ${losses.length}L · PnL ${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)}₽${_journalRefreshTimer ? ' · 🔄 Live (10с)' : ''}`;
}


async function saveRobotEdit(i) {
    const r = robots[i];
    if (!r) return;

    const isVpCopy = r.strategy && r.strategy.includes('VP Scalp Grid Copy');
    const isVpSimple = r.strategy && r.strategy.includes('VP Scalp Simple');
    const isV8Trail = r.strategy && r.strategy.includes('V8 Trail');
    const isVpScalp = r.strategy && r.strategy.includes('VP Scalp Grid') && !isVpCopy && !isVpSimple;

    if (isV8Trail) {
        r.slPct = el('editSlPct')?.value || r.slPct;
        r.ema = el('editEma')?.value || r.ema;
        r.sarStart = el('editSarStart')?.value || r.sarStart;
        r.sarStep = el('editSarStep')?.value || r.sarStep;
        r.sarMax = el('editSarMax')?.value || r.sarMax;
        r.holdMinutes = el('editHoldMinutes')?.value || r.holdMinutes;
        try {
            await fetch('/strategy/v8-trail/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    slPct: parseFloat(r.slPct),
                    emaPeriod: parseInt(r.ema),
                    sarStart: parseFloat(r.sarStart),
                    sarStep: parseFloat(r.sarStep),
                    sarMax: parseFloat(r.sarMax),
                    maxHoldMinutes: parseInt(r.holdMinutes)
                })
            });
            addLog(nowTime(), 'INFO', `📤 V8 Trail конфиг: SL=${r.slPct}% EMA=${r.ema}`);
        } catch(e) { addLog(nowTime(), 'ERROR', 'Config send failed: ' + e.message); }
    } else if (isVpSimple) {
        r.slPct = el('editSlPct')?.value || r.slPct;
        r.holdMinutes = el('editHoldMinutes')?.value || r.holdMinutes;
        r.vpLookback = el('editVpLookback')?.value || r.vpLookback;
        r.vpBins = el('editVpBins')?.value || r.vpBins;
        r.vaPercent = el('editVaPercent')?.value || r.vaPercent;
        try {
            await fetch('/strategy/vp-simple/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    slPct: parseFloat(r.slPct),
                    maxHoldMinutes: parseInt(r.holdMinutes),
                    vpLookback: parseInt(r.vpLookback),
                    vpBins: parseInt(r.vpBins),
                    vaPercent: parseFloat(r.vaPercent)
                })
            });
            addLog(nowTime(), 'INFO', `📤 VP Simple конфиг: SL=${r.slPct}% hold=${r.holdMinutes}мин LB=${r.vpLookback} Bin=${r.vpBins} VA=${r.vaPercent}`);
        } catch(e) { addLog(nowTime(), 'ERROR', 'Config send failed: ' + e.message); }
    } else if (isVpCopy) {
        r.maxGrid = el('editMaxGrid')?.value || r.maxGrid;
        r.gridStep = el('editGridStep')?.value || r.gridStep;
        r.gridSpread = el('editGridSpread')?.value || r.gridSpread;
        r.holdMinutes = el('editHoldMinutes')?.value || r.holdMinutes;
        r.vpLookback = el('editVpLookback')?.value || r.vpLookback;
        r.vpBinSize = el('editVpBinSize')?.value || r.vpBinSize;
        r.vaPercent = el('editVaPercent')?.value || r.vaPercent;
        r.minProfit = el('editMinProfit')?.value || r.minProfit;
        r.rvAdaptation = el('editRvAdapt')?.checked ?? r.rvAdaptation;
        // Send config to running strategy
        try {
            await fetch('/strategy/vp-copy/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    maxLevels: parseInt(r.maxGrid),
                    stepBase: parseInt(r.gridStep),
                    spreadBase: parseInt(r.gridSpread),
                    maxHoldMinutes: parseInt(r.holdMinutes),
                    vpLookback: parseInt(r.vpLookback),
                    vpBinSize: parseInt(r.vpBinSize),
                    vaPercent: parseFloat(r.vaPercent),
                    rvAdaptation: r.rvAdaptation,
                    minProfitPerLot: parseFloat(r.minProfit)
                })
            });
            addLog(nowTime(), 'INFO', `📤 Конфиг отправлен на сервер: step=${r.gridStep} spread=${r.gridSpread} RV=${r.rvAdaptation}`);
        } catch(e) { addLog(nowTime(), 'ERROR', 'Config send failed: ' + e.message); }
    } else if (isVpScalp) {
        r.maxGrid = el('editMaxGrid')?.value || r.maxGrid;
        r.gridStep = el('editGridStep')?.value || r.gridStep;
        r.gridSpread = el('editGridSpread')?.value || r.gridSpread;
        r.holdMinutes = el('editHoldMinutes')?.value || r.holdMinutes;
        r.vpLookback = el('editVpLookback')?.value || r.vpLookback;
        r.vpBinSize = el('editVpBinSize')?.value || r.vpBinSize;
        r.vaPercent = el('editVaPercent')?.value || r.vaPercent;
        r.minProfit = el('editMinProfit')?.value || r.minProfit;
        r.rvAdaptation = el('editRvAdapt')?.checked ?? r.rvAdaptation;
        // Send config to running strategy
        try {
            await fetch('/strategy/vp-scalp-grid/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    maxLevels: parseInt(r.maxGrid),
                    stepBase: parseInt(r.gridStep),
                    spreadBase: parseInt(r.gridSpread),
                    maxHoldMinutes: parseInt(r.holdMinutes),
                    vpLookback: parseInt(r.vpLookback),
                    vpBinSize: parseInt(r.vpBinSize),
                    vaPercent: parseFloat(r.vaPercent),
                    minProfitPerLot: parseInt(r.minProfit),
                    rvAdaptation: r.rvAdaptation
                })
            });
            addLog(nowTime(), 'INFO', `📤 VP Scalp Grid конфиг: step=${r.gridStep} spread=${r.gridSpread} LB=${r.vpLookback} minProfit=${r.minProfit}`);
        } catch(e) { addLog(nowTime(), 'ERROR', 'Config send failed: ' + e.message); }
    } else {
        r.account = el('editAccount')?.value || r.account;
        r.accountName = el('editAccount')?.selectedOptions?.[0]?.text || r.accountName;
        r.sarStart = el('editSarStart')?.value || r.sarStart;
        r.sarStep = el('editSarStep')?.value || r.sarStep;
        r.sarMax = el('editSarMax')?.value || r.sarMax;
        r.ema = el('editEma')?.value || r.ema;
        r.gridStep = el('editGridStep')?.value || r.gridStep;
        r.gridSpread = el('editGridSpread')?.value || r.gridSpread;
        r.maxGrid = el('editMaxGrid')?.value || r.maxGrid;
        r.closePct = el('editClosePct')?.value || r.closePct;
        r.lots = el('editLots')?.value || r.lots;
    }

    saveRobots();
    if (_journalRefreshTimer) { clearInterval(_journalRefreshTimer); _journalRefreshTimer = null; }
    renderRobots();
    el('robotEditPanel')?.remove();
    addLog(nowTime(), 'INFO', `💾 Параметры робота ${r.ticker} (${r.strategy}) сохранены`);
}

// === Log ===
function addLog(time, level, msg) {
    const container = el('logContainer');
    const cls = level === 'ERROR' ? 'log-error' : level === 'TRADE' ? 'log-trade' : 'log-info';
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.innerHTML = `<span class="log-time">[${time}]</span> <span class="${cls}">${msg}</span>`;
    container.appendChild(div);
    while (container.children.length > MAX_LOG) container.removeChild(container.firstChild);
    container.scrollTop = container.scrollHeight;
}

// === Helpers ===
function el(id) { return document.getElementById(id); }
function nowTime() { return new Date().toLocaleTimeString(); }
function fmt(n) { return n ? Math.round(n).toLocaleString('ru-RU') : '—'; }
function fmtPnl(n) {
    if (!n) return '0';
    const s = n > 0 ? '+' : '';
    return s + Math.round(n).toLocaleString('ru-RU');
}

// === Arbitrage Python Robot — factory ===
function createArbInstance(prefix, port) {
    const baseUrl = 'http://' + (window.location.hostname || 'localhost') + ':' + port;
    let data = null;

    async function fetch2(path, method='GET', body=null) {
        const opts = {method, headers:{'Content-Type':'application/json'}};
        if (body) opts.body = JSON.stringify(body);
        try {
            const r = await fetch(`${baseUrl}${path}`, opts);
            return r.ok ? await r.json() : null;
        } catch { return null; }
    }

    async function refresh() {
        const d = await fetch2('/status');
        if (!d) {
            const s = el(prefix+'Status');
            if (s) { s.textContent = '❌ Не подключён'; s.style.color = 'var(--red)'; }
            return;
        }
        data = d;
        const p = d.params || {};
        const pos = d.position;
        const basis = d.basis || {};

        // Header status
        const s = el(prefix+'Status');
        if (s) {
            const modeMap = {running:'▶️ Торгует', paused:'⏸ Пауза', stopped:'⏹ Остановлен'};
            s.textContent = modeMap[d.mode] || d.mode;
            s.style.color = d.mode==='running' ? 'var(--green)' : d.mode==='paused' ? 'var(--yellow)' : 'var(--red)';
        }
        if (el(prefix+'Paper')) el(prefix+'Paper').textContent = d.paper ? '📝 Да' : '💰 Реал';

        // Metrics
        const pair = `${d.tickerA||p.ticker_a||'—'} / ${d.tickerB||p.ticker_b||'—'}`;
        if (el(prefix+'Pair')) el(prefix+'Pair').textContent = pair;
        if (el(prefix+'Basis')) el(prefix+'Basis').textContent = basis.spread_rub != null ? `${basis.spread_rub>=0?'+':''}${basis.spread_rub.toFixed(2)} ₽` : '—';
        if (el(prefix+'Z')) {
            const z = basis.zscore || 0;
            el(prefix+'Z').textContent = z.toFixed(3);
            el(prefix+'Z').style.color = Math.abs(z) >= (p.entry_z||1.5) ? 'var(--accent)' : z > 0 ? 'var(--green)' : 'var(--red)';
        }
        if (el(prefix+'Pos')) {
            if (pos) {
                const dir = pos.side === 1 ? 'LONG' : 'SHORT';
                el(prefix+'Pos').textContent = `${dir} ${pos.lotsA}×${pos.lotsB}`;
                el(prefix+'Pos').style.color = pos.side===1 ? 'var(--green)' : 'var(--red)';
            } else { el(prefix+'Pos').textContent = 'Флэт'; el(prefix+'Pos').style.color = ''; }
        }
        if (el(prefix+'Unreal')) {
            const u = d.unrealizedPnl || 0;
            el(prefix+'Unreal').textContent = (u>=0?'+':'') + u.toFixed(1) + ' ₽';
            el(prefix+'Unreal').style.color = u > 0 ? 'var(--green)' : u < 0 ? 'var(--red)' : '';
        }
        if (el(prefix+'Realized')) {
            const r = d.realizedPnl || 0;
            el(prefix+'Realized').textContent = (r>=0?'+':'') + r.toLocaleString('ru-RU') + ' ₽';
            el(prefix+'Realized').style.color = r > 0 ? 'var(--green)' : r < 0 ? 'var(--red)' : '';
        }
        if (el(prefix+'Trades')) el(prefix+'Trades').textContent = d.trades || 0;

        // Table row
        if (el(prefix+'RbPair')) el(prefix+'RbPair').textContent = pair;
        if (el(prefix+'RbMode')) el(prefix+'RbMode').textContent = d.mode || '—';
        if (el(prefix+'RbPos')) el(prefix+'RbPos').textContent = pos ? `${pos.side===1?'LONG':'SHORT'} ${pos.lotsA}×${pos.lotsB}` : '—';
        if (el(prefix+'RbPnl')) el(prefix+'RbPnl').textContent = (d.totalPnl||0).toFixed(0) + ' ₽';
        if (el(prefix+'RbZ')) el(prefix+'RbZ').textContent = (basis.zscore||0).toFixed(2);
        if (el(prefix+'RbBasis')) el(prefix+'RbBasis').textContent = (basis.basis||0).toFixed(2);
        if (el(prefix+'RbStatus')) el(prefix+'RbStatus').textContent = d.mode==='running' ? '🟢' : d.mode==='paused' ? '🟡' : '⚪';
    }

    async function start() { await fetch2('/start','POST'); refresh(); }
    async function pause() { await fetch2('/pause','POST'); refresh(); }
    async function stop()  { await fetch2('/stop','POST'); refresh(); }

    return { refresh, start, pause, stop, fetch: fetch2, getData: () => data };
}

// === Create arb robot instances ===
const arbPy = createArbInstance('arbPy', 5090);
const sber = createArbInstance('sber', 5091);
const brCal = createArbInstance('brCal', 5092);

// Expose as global functions for onclick handlers
async function arbPyStart() { arbPy.start(); arbPyLog('▶️ Запуск GAZP/GZU6...'); }
async function arbPyPause() { arbPy.pause(); arbPyLog('⏸ Пауза GAZP/GZU6...'); }
async function arbPyStop()  { arbPy.stop();  arbPyLog('⏹ Стоп GAZP/GZU6...'); }
async function sberStart()  { sber.start(); arbPyLog('▶️ Запуск SBER/SRU6...'); }
async function sberPause()  { sber.pause(); arbPyLog('⏸ Пауза SBER/SRU6...'); }
async function sberStop()   { if (!confirm('Остановить SBER/SRU6?')) return; sber.stop(); arbPyLog('⏹ Стоп SBER/SRU6...'); }
async function brCalStart() { brCal.start(); arbPyLog('▶️ Запуск BR Calendar...'); }
async function brCalPause() { brCal.pause(); arbPyLog('⏸ Пауза BR Calendar...'); }
async function brCalStop()  { if (!confirm('Остановить BR Calendar?')) return; brCal.stop(); arbPyLog('⏹ Стоп BR Calendar...'); }

// Back-compat: arbPyRefresh now calls both
async function arbPyRefresh() {
    await arbPy.refresh();
    await sber.refresh();
    await brCal.refresh();
    arbPyData = arbPy.getData();
}

// Back-compat: arbPyFetch delegates to GAZP instance
async function arbPyFetch(path, method='GET', body=null) {
    return arbPy.fetch(path, method, body);
}

function arbPyLog(msg, level='INFO') {
    const c = el('arbPyLogContainer');
    if (!c) return;
    const t = new Date().toLocaleTimeString('ru-RU');
    const color = level === 'ERROR' ? '#F44336' : level === 'WARN' ? '#FF9800' : '#4CAF50';
    c.innerHTML = `<div style="padding:2px 0;color:${color}">[${t}] ${level}: ${msg}</div>` + c.innerHTML;
    while (c.children.length > 50) c.removeChild(c.lastChild);
}
function arbPyEditPanel() {
    const existing = el('arbPyEditPanel');
    if (existing) { existing.remove(); _arbJournalInstance = null; return; }
    // Set journal instance to GAZP (null=arbPy) only if not already set by sber/brCal caller
    // Always reset for GAZP panel — sber/brCal panels set their own instance AFTER this call
    _arbJournalInstance = null;

    const p = (arbPyData && arbPyData.params) || {};
    const div = document.createElement('div');
    div.id = 'arbPyEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:750px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            🔄 Арбитражный робот (PYTHON)
            <span style="flex:1"></span>
            <button class="btn btn-primary btn-sm" onclick="arbPyShowCopyDialog()">📋 Копировать</button>
            <button class="btn btn-primary btn-sm" onclick="arbPySaveFromPanel()">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="el('arbPyEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:16px;display:flex;flex-direction:column;gap:16px">
            <!-- Счёт + Капитал -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card" style="min-width:180px"><div class="metric-label">📋 Счёт</div>
                    <select id="arbAccount" class="input" style="width:200px">
                        <option value="2049688" selected>2049688 (EDP)</option>
                    </select>
                    <input type="hidden" id="arbAccountSaved" value="2049688">
                </div>
                <div class="metric-card" style="min-width:140px"><div class="metric-label">Капитал (₽)</div><input id="arbCapital" class="input" type="number" value="${p.capital||1000000}" style="width:120px"></div>
            </div>

            <hr style="border-color:var(--border-color)">

            <!-- Инструменты -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card" style="min-width:160px"><div class="metric-label">📈 Инструмент A</div>
                    <select id="arbSymA" class="input" style="width:140px" onchange="arbUpdateCommissionLabels()">
                        <option value="GAZP" ${(p.ticker_a||'GAZP')==='GAZP'?'selected':''}>GAZP (Газпром)</option>
                        <option value="SBER" ${p.ticker_a==='SBER'?'selected':''}>SBER (Сбербанк)</option>
                        <option value="LKOH" ${p.ticker_a==='LKOH'?'selected':''}>LKOH (Лукойл)</option>
                        <option value="ROSN" ${p.ticker_a==='ROSN'?'selected':''}>ROSN (Роснефть)</option>
                        <option value="TATN" ${p.ticker_a==='TATN'?'selected':''}>TATN (Татнефть)</option>
                        <option value="GMKN" ${p.ticker_a==='GMKN'?'selected':''}>GMKN (Норникель)</option>
                        <option value="ALRS" ${p.ticker_a==='ALRS'?'selected':''}>ALRS (Алроса)</option>
                        <option value="VTBR" ${p.ticker_a==='VTBR'?'selected':''}>VTBR (ВТБ)</option>
                        <option value="MTSS" ${p.ticker_a==='MTSS'?'selected':''}>MTSS (МТС)</option>
                        <option value="NVTK" ${p.ticker_a==='NVTK'?'selected':''}>NVTK (Новатэк)</option>
                        <option value="BRQ6" ${p.ticker_a==='BRQ6'?'selected':''}>BRQ6 (Brent Aug)</option>
                        <option value="BRU6" ${p.ticker_a==='BRU6'?'selected':''}>BRU6 (Brent Sep)</option>
                        <option value="BRZ6" ${p.ticker_a==='BRZ6'?'selected':''}>BRZ6 (Brent Dec)</option>
                        <option value="BRK6" ${p.ticker_a==='BRK6'?'selected':''}>BRK6 (Brent Jun)</option>
                    </select>
                </div>
                <div class="metric-card" style="min-width:160px"><div class="metric-label">📉 Инструмент B</div>
                    <select id="arbSymB" class="input" style="width:140px">
                        <option value="GZM6" ${(p.ticker_b||'GZM6')==='GZM6'?'selected':''}>GZM6 (Газпром фьюч)</option>
                        <option value="GZU6" ${p.ticker_b==='GZU6'?'selected':''}>GZU6 (Газпром фьюч)</option>
                        <option value="SRM6" ${p.ticker_b==='SRM6'?'selected':''}>SRM6 (Сбер фьюч)</option>
                        <option value="SRU6" ${p.ticker_b==='SRU6'?'selected':''}>SRU6 (Сбер фьюч)</option>
                        <option value="LKM6" ${p.ticker_b==='LKM6'?'selected':''}>LKM6 (Лукойл фьюч)</option>
                        <option value="RNM6" ${p.ticker_b==='RNM6'?'selected':''}>RNM6 (Роснефть фьюч)</option>
                        <option value="TTM6" ${p.ticker_b==='TTM6'?'selected':''}>TTM6 (Татнефть фьюч)</option>
                        <option value="MXM6" ${p.ticker_b==='MXM6'?'selected':''}>MXM6 (Мосбиржа фьюч)</option>
                        <option value="SiU6" ${p.ticker_b==='SiU6'?'selected':''}>SiU6 (Доллар/руб)</option>
                        <option value="RIM6" ${p.ticker_b==='RIM6'?'selected':''}>RIM6 (RTS фьюч)</option>
                        <option value="RIU6" ${p.ticker_b==='RIU6'?'selected':''}>RIU6 (RTS фьюч)</option>
                        <option value="GDM6" ${p.ticker_b==='GDM6'?'selected':''}>GDM6 (Золото фьюч)</option>
                        <option value="BRQ6" ${p.ticker_b==='BRQ6'?'selected':''}>BRQ6 (Brent Aug фьюч)</option>
                        <option value="BRU6" ${p.ticker_b==='BRU6'?'selected':''}>BRU6 (Brent Sep фьюч)</option>
                        <option value="BRZ6" ${p.ticker_b==='BRZ6'?'selected':''}>BRZ6 (Brent Dec фьюч)</option>
                        <option value="BRK6" ${p.ticker_b==='BRK6'?'selected':''}>BRK6 (Brent Jun фьюч)</option>
                    </select>
                </div>
                <div class="metric-card"><div class="metric-label">Лоты A</div><input id="arbLotsA" class="input" type="number" value="${p.lots_a||10}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Лоты B</div><input id="arbLotsB" class="input" type="number" value="${p.lots_b||1}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Hedge Ratio</div><input id="arbHedgeRatio" class="input" type="number" step="0.1" value="${p.hedge_ratio||10}" style="width:70px"></div>
            </div>

            <!-- Комиссия -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card" style="min-width:280px;border:1px solid var(--border-color)">
                    <div class="metric-label">💰 Комиссия</div>
                    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:4px 0">
                        <input type="checkbox" id="arbUseCommission" ${p.use_commission!==false?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-size:13px">Включить комиссию в расчёт PnL</span>
                    </label>
                </div>
                <div class="metric-card"><div class="metric-label" id="arbCommALabel">${/\d/.test(p.ticker_a||'') ? 'Фьючерс A (₽ за контракт RT)' : 'Акция A (% за сторону)'}</div><input id="arbCommStock" class="input" type="number" step="0.01" value="${/\d/.test(p.ticker_a||'') ? (p.commission_futures_rt||15) : (p.commission_stock_pct||0.04)}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Фьючерс B (₽ за контракт RT)</div><input id="arbCommFut" class="input" type="number" step="0.1" value="${p.commission_futures_rt||0.9}" style="width:80px"></div>
            </div>
            <div id="arbCommBothFut" style="font-size:12px;color:var(--yellow);padding:4px 0;display:${/\d/.test(p.ticker_a||'')?'block':'none'}">⚠️ Обе ноги — фьючерсы. Комиссия B (₽ RT) применяется к обеим ногам.</div>
            </div>

            <hr style="border-color:var(--border-color)">

            <!-- Вход -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card"><div class="metric-label">Entry Z (SHORT)</div><input id="arbEntryZ" class="input" type="number" step="0.1" value="${p.entry_z||1.5}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Entry Z (LONG)</div><input id="arbEntryZLong" class="input" type="number" step="0.1" value="${p.entry_z_long||1.5}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Lookback</div><input id="arbLookback" class="input" type="number" value="${p.lookback||50}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Leg A Timeout (сек)</div><input id="arbLegTimeout" class="input" type="number" value="${p.leg_a_timeout||5}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Min Fill Ratio</div><input id="arbMinFill" class="input" type="number" step="0.05" value="${p.min_fill_ratio||0.5}" style="width:70px"></div>
            </div>

            <!-- Fair value -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card"><div class="metric-label">Ставка (годовые %)</div><input id="arbRate" class="input" type="number" step="0.1" value="${(p.risk_free_rate||0.16)*100}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Экспирация</div><input id="arbExpiration" class="input" type="date" value="${p.expiration_date||'2026-09-18'}" style="width:130px"></div>
                <div class="metric-card"><div class="metric-label">Размер контракта</div><input id="arbContractSize" class="input" type="number" value="${p.contract_size||100}" style="width:70px"></div>
            </div>

            <hr style="border-color:var(--border-color)">

            <!-- Мин. профит (dropdown + значение) -->
            <div class="metrics-row" style="flex-wrap:wrap;align-items:flex-end">
                <div class="metric-card"><div class="metric-label">Мин. профит — тип</div>
                    <select id="arbMinProfitType" class="input" style="width:110px">
                        <option value="rub" ${(p.min_profit_type||'rub')==='rub'?'selected':''}>Рубли (₽)</option>
                        <option value="pts" ${p.min_profit_type==='pts'?'selected':''}>Пункты</option>
                        <option value="pct" ${p.min_profit_type==='pct'?'selected':''}>Проценты (%)</option>
                    </select>
                </div>
                <div class="metric-card"><div class="metric-label">Значение</div><input id="arbMinProfitVal" class="input" type="number" step="0.1" value="${p.min_profit_value||20}" style="width:90px"></div>
            </div>

            <hr style="border-color:var(--border-color)">

            <!-- Риск (dropdown + значение) -->
            <div class="metrics-row" style="flex-wrap:wrap;align-items:flex-end">
                <div class="metric-card"><div class="metric-label">Риск — тип</div>
                    <select id="arbRiskType" class="input" style="width:150px">
                        <option value="stop_loss_rub" ${(p.risk_type||'stop_loss_rub')==='stop_loss_rub'?'selected':''}>Стоп-лосс (₽)</option>
                        <option value="time_stop_min" ${p.risk_type==='time_stop_min'?'selected':''}>Тайм-стоп (мин)</option>
                        <option value="max_dd_pct" ${p.risk_type==='max_dd_pct'?'selected':''}>Max DD (%)</option>
                        <option value="kill_switch" ${p.risk_type==='kill_switch'?'selected':''}>Kill Switch</option>
                    </select>
                </div>
                <div class="metric-card"><div class="metric-label">Значение</div><input id="arbRiskVal" class="input" type="number" value="${p.risk_value||5000}" style="width:90px"></div>
            </div>

            <!-- Направление -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card" style="min-width:300px">
                    <div class="metric-label">Направление</div>
                    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:4px 0">
                        <input type="checkbox" id="arbAllowLong" ${p.allow_long_basis?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span>Разрешить лонг (шорт акции + лонг фьючерса)</span>
                    </label>
                </div>
            </div>

            <hr style="border-color:var(--border-color)">

            <!-- Раздвижка -->
            <hr style="border-color:var(--border-color);margin:12px 0">
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card" style="background:#1a2332;border:1px solid var(--accent)"><div class="metric-label" style="color:var(--accent)">Раздвижка (₽)</div><div id="arbSpreadRub" style="font-size:22px;font-weight:bold;color:var(--accent)">—</div></div>
                <div class="metric-card"><div class="metric-label">Fair Spread (₽)</div><div id="arbFairSpread" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Deviation (₽)</div><div id="arbDeviation" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Раздвижка (%)</div><div id="arbSpreadPct" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">GAZP × 100</div><div id="arbSpotValue" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label" id="arbFutLabel">GZM6 (фьюч)</div><div id="arbFutValue" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Basis (Z-score base)</div><div id="arbBasisVal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Z-score</div><div id="arbZVal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Basis Mean</div><div id="arbMeanVal" style="font-size:14px">—</div></div>
                <div class="metric-card"><div class="metric-label">Basis Std</div><div id="arbStdVal" style="font-size:14px">—</div></div>
                <div class="metric-card"><div class="metric-label">Days to Exp</div><div id="arbDaysExp" style="font-size:18px;font-weight:bold">—</div></div>
                <!-- Variant 1: absolute thresholds toggle + inputs + reference -->
                <div class="metric-card" style="min-width:320px;border:1px solid #e90"><div class="metric-label" style="color:#e90">📊 Калькулятор спреда</div>
                    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:4px 0">
                        <input type="checkbox" id="arbEntryModeRub" ${p.entry_mode==='spread_rub'?'checked':''} style="width:18px;height:18px;cursor:pointer" onchange="arbEntryModeToggle()">
                        <span style="font-size:13px">Торговать по абсолютным порогам (₽)</span>
                    </label>
                    <div id="arbRubInputs" style="display:none;margin-top:6px">
                        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
                            <div style="flex:1;min-width:100px"><label style="font-size:11px;color:#9CA3AF">Short (₽ &gt;)</label><input id="arbSpreadRubHigh" class="input" type="number" step="1" style="width:80px;font-size:14px;padding:4px" value="${p.spread_rub_high||400}"></div>
                            <div style="flex:1;min-width:100px"><label style="font-size:11px;color:#9CA3AF">Long (₽ &lt;)</label><input id="arbSpreadRubLow" class="input" type="number" step="1" style="width:80px;font-size:14px;padding:4px" value="${p.spread_rub_low||200}"></div>
                        </div>
                    </div>
                    <div style="margin-top:8px;padding-top:8px;border-top:1px solid #333">
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:12px">
                            <div style="color:#9CA3AF">Текущий спред:</div><div id="arbCalcSpread" style="font-weight:bold;text-align:right">—</div>
                            <div style="color:#9CA3AF">Fair value (cost of carry):</div><div id="arbCalcFair" style="font-weight:bold;text-align:right">—</div>
                            <div style="color:#9CA3AF">Deviation:</div><div id="arbCalcDev" style="font-weight:bold;text-align:right">—</div>
                            <div style="color:#9CA3AF">Z-score:</div><div id="arbCalcZ" style="font-weight:bold;text-align:right">—</div>
                        </div>
                        <div style="margin-top:8px;padding-top:6px;border-top:1px dashed #444">
                            <div style="font-size:11px;color:#9CA3AF;margin-bottom:4px">Исторический диапазон (lookback):</div>
                            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 12px;font-size:12px">
                                <div style="color:#9CA3AF">Max:</div><div id="arbCalcMax" style="font-weight:bold;text-align:center;color:var(--green)">—</div>
                                <div style="color:#9CA3AF">Min:</div><div id="arbCalcMin" style="font-weight:bold;text-align:center;color:var(--red)">—</div>
                                <div style="color:#9CA3AF">Mean:</div><div id="arbCalcMean" style="font-weight:bold;text-align:center">—</div>
                            </div>
                            <div style="margin-top:4px;font-size:11px;color:#9CA3AF">Suggested: High <span id="arbCalcSH" style="color:var(--green);font-weight:bold">—</span> | Low <span id="arbCalcSL" style="color:var(--red);font-weight:bold">—</span></div>
                        </div>
                    </div>
                </div>
            </div>

            <hr style="border-color:var(--border-color);margin:12px 0">

            <!-- Стакан L2 инфо -->
            <div class="metrics-row" style="flex-wrap:wrap">
                <div class="metric-card"><div class="metric-label">L2 A: Bid/Ask</div><div id="arbL2A" style="font-size:14px">—</div></div>
                <div class="metric-card"><div class="metric-label">L2 B: Bid/Ask</div><div id="arbL2B" style="font-size:14px">—</div></div>
                <div class="metric-card"><div class="metric-label">Data Points</div><div id="arbDataPts" style="font-size:14px">—</div></div>
            </div>

            <hr style="border-color:var(--border-color);margin:12px 0">

            <!-- Торговый журнал -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>📋 Торговый журнал</strong>
            </div>
            <div id="arbJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="arbJournalFilter" class="input" style="width:120px" onchange="arbRenderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="arbJournalDateFrom" class="input" type="date" style="width:130px" onchange="arbRenderJournal()">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="arbJournalDateTo" class="input" type="date" style="width:130px" onchange="arbRenderJournal()">
                <button class="btn btn-secondary btn-sm" onclick="arbJournalSetYesterday()">Вчера</button>
                <button class="btn btn-secondary btn-sm" onclick="arbLoadJournal()">🔄</button>
            </div>
            <div style="max-height:350px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:6px;text-align:left">📅 Дата</th>
                            <th style="padding:6px;text-align:left">⏰ Вход</th>
                            <th style="padding:6px;text-align:left">⏰ Выход</th>
                            <th style="padding:6px;text-align:center">↔️</th>
                            <th style="padding:6px;text-align:right">Вход A/B</th>
                            <th style="padding:6px;text-align:right">Выход A/B</th>
                            <th style="padding:6px;text-align:right">Basis вх.</th>
                            <th style="padding:6px;text-align:right">Basis вых.</th>
                            <th style="padding:6px;text-align:right">Z</th>
                            <th style="padding:6px;text-align:right">Лоты</th>
                            <th style="padding:6px;text-align:right">Холд</th>
                            <th style="padding:6px;text-align:right">Комиссия</th>
                            <th style="padding:6px;text-align:right">PnL</th>
                            <th style="padding:6px;text-align:right">Кумул.</th>
                        </tr>
                    </thead>
                    <tbody id="arbJournalBody" style="background:var(--bg)"><tr><td colspan="14" style="text-align:center;padding:24px;color:#9CA3AF">Нет данных</td></tr></tbody>
                </table>
            </div>
            <div id="arbJournalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px"></div>
        </div>
    `;
    document.body.appendChild(div);
    arbPyUpdateL2();
    arbPyLoadAccounts();
    // Init Variant 1 toggle state
    arbEntryModeToggle();
    setTimeout(() => arbLoadJournal(), 300);
}

function sberEditPanel() {
    const existing = el('sberEditPanel');
    if (existing) { existing.remove(); _arbJournalInstance = null; return; }
    _arbJournalInstance = 'sber';
    // Temporarily swap arbPyData so arbPyEditPanel renders with SBER params
    const savedArbPyData = arbPyData;
    arbPyData = sber.getData();
    // Save current instance before calling arbPyEditPanel (which would reset it)
    const savedInstance = _arbJournalInstance;
    // Call the same render logic
    arbPyEditPanel();
    // Restore journal instance (arbPyEditPanel may have reset it)
    _arbJournalInstance = savedInstance;
    // Rename the panel
    const panel = el('arbPyEditPanel');
    if (panel) {
        panel.id = 'sberEditPanel';
        // Update header to show SBER
        const header = panel.querySelector('.card-header');
        if (header) header.innerHTML = header.innerHTML.replace('Арбитражный робот (PYTHON)', 'Арбитражный робот — SBER/SRU6');
        // Override save button to save to SBER
        const saveBtn = panel.querySelector('button[onclick*="arbPySaveFromPanel"]');
        if (saveBtn) saveBtn.setAttribute('onclick', 'sberSaveFromPanel()');
        // Override copy button
        const copyBtn = panel.querySelector('button[onclick*="arbPyShowCopyDialog"]');
        if (copyBtn) copyBtn.remove();
        // Override close button
        const closeBtn = panel.querySelector('button[onclick*="arbPyEditPanel"]');
        if (closeBtn) closeBtn.setAttribute('onclick', 'el(\'sberEditPanel\')?.remove(); _arbJournalInstance=null;');
    }
}

async function sberSaveFromPanel() {
    const body = {
        ticker_a: el('arbSymA').value,
        symbol_a: el('arbSymA').value + '@RTSX',
        ticker_b: el('arbSymB').value,
        symbol_b: el('arbSymB').value + '@RTSX',
        lots_a: parseInt(el('arbLotsA').value),
        lots_b: parseInt(el('arbLotsB').value),
        hedge_ratio: parseFloat(el('arbHedgeRatio').value),
        capital: parseFloat(el('arbCapital')?.value || 100000),
        entry_z: parseFloat(el('arbEntryZ').value),
        entry_z_long: parseFloat(el('arbEntryZLong').value),
        entry_mode: el('arbEntryModeRub')?.checked ? 'spread_rub' : 'zscore',
        spread_rub_high: parseFloat(el('arbSpreadRubHigh')?.value || 400),
        spread_rub_low: parseFloat(el('arbSpreadRubLow')?.value || 200),
        risk_free_rate: parseFloat(el('arbRate')?.value || 16) / 100,
        expiration_date: el('arbExpiration')?.value || '2026-09-18',
        contract_size: parseInt(el('arbContractSize')?.value || 100),
        lookback: parseInt(el('arbLookback').value),
        leg_a_timeout: parseInt(el('arbLegTimeout').value),
        min_fill_ratio: parseFloat(el('arbMinFill').value),
        min_profit_type: el('arbMinProfitType').value,
        min_profit_value: parseFloat(el('arbMinProfitVal').value),
        risk_type: el('arbRiskType').value,
        risk_value: parseFloat(el('arbRiskVal').value),
        allow_long_basis: el('arbAllowLong')?.checked || false,
        use_commission: el('arbUseCommission')?.checked ?? true,
        commission_stock_pct: parseFloat(el('arbCommStock')?.value || 0.035),
        commission_futures_rt: parseFloat(el('arbCommFut')?.value || 0.9),
        slippage_bps: parseFloat(el('arbSlippage')?.value || 5),
    };
    // If leg A is futures, use its commission value too
    const symA = el('arbSymA')?.value || '';
    if (/\d/.test(symA)) {
        body.commission_futures_rt = parseFloat(el('arbCommStock')?.value || body.commission_futures_rt);
    }
    const r = await sber.fetch('/params', 'POST', body);
    if (r && r.ok) {
        arbPyLog('✅ SBER/SRU6 параметры сохранены');
        sber.refresh();
        el('sberEditPanel')?.remove();
    } else {
        arbPyLog('Ошибка сохранения SBER/SRU6', 'ERROR');
    }
}

async function brCalEditPanel() {
    const existing = el('brCalEditPanel');
    if (existing) { existing.remove(); _arbJournalInstance = null; return; }
    _arbJournalInstance = 'brCal';
    const savedArbPyData = arbPyData;
    await brCal.refresh();
    arbPyData = brCal.getData();
    const savedInstance = _arbJournalInstance;
    arbPyEditPanel();
    setTimeout(arbUpdateCommissionLabels, 50);
    _arbJournalInstance = savedInstance;
    const panel = el('arbPyEditPanel');
    if (panel) {
        panel.id = 'brCalEditPanel';
        const header = panel.querySelector('.card-header');
        if (header) header.innerHTML = header.innerHTML.replace('Арбитражный робот (PYTHON)', 'Арбитражный робот — BR Calendar');
        const saveBtn = panel.querySelector('button[onclick*="arbPySaveFromPanel"]');
        if (saveBtn) saveBtn.setAttribute('onclick', 'brCalSaveFromPanel()');
        const copyBtn = panel.querySelector('button[onclick*="arbPyShowCopyDialog"]');
        if (copyBtn) copyBtn.remove();
        const closeBtn = panel.querySelector('button[onclick*="arbPyEditPanel"]');
        if (closeBtn) closeBtn.setAttribute('onclick', 'el(\'brCalEditPanel\')?.remove(); _arbJournalInstance=null;');
    }
}

async function brCalSaveFromPanel() {
    const body = {
        ticker_a: el('arbSymA').value,
        symbol_a: el('arbSymA').value + '@RTSX',
        ticker_b: el('arbSymB').value,
        symbol_b: el('arbSymB').value + '@RTSX',
        lots_a: parseInt(el('arbLotsA').value),
        lots_b: parseInt(el('arbLotsB').value),
        hedge_ratio: parseFloat(el('arbHedgeRatio').value),
        capital: parseFloat(el('arbCapital')?.value || 100000),
        entry_z: parseFloat(el('arbEntryZ').value),
        entry_z_long: parseFloat(el('arbEntryZLong').value),
        entry_mode: el('arbEntryModeRub')?.checked ? 'spread_rub' : 'zscore',
        spread_rub_high: parseFloat(el('arbSpreadRubHigh')?.value || 5),
        spread_rub_low: parseFloat(el('arbSpreadRubLow')?.value || -5),
        risk_free_rate: parseFloat(el('arbRate')?.value || 14.25) / 100,
        expiration_date: el('arbExpiration')?.value || '2026-09-18',
        contract_size: parseInt(el('arbContractSize')?.value || 1),
        lookback: parseInt(el('arbLookback').value),
        leg_a_timeout: parseInt(el('arbLegTimeout').value),
        min_fill_ratio: parseFloat(el('arbMinFill').value),
        min_profit_type: el('arbMinProfitType').value,
        min_profit_value: parseFloat(el('arbMinProfitVal').value),
        risk_type: el('arbRiskType').value,
        risk_value: parseFloat(el('arbRiskVal').value),
        allow_long_basis: el('arbAllowLong')?.checked || false,
        use_commission: el('arbUseCommission')?.checked ?? true,
        commission_stock_pct: parseFloat(el('arbCommStock')?.value || 0.035),
        commission_futures_rt: parseFloat(el('arbCommFut')?.value || 7.5),
        slippage_bps: parseFloat(el('arbSlippage')?.value || 10),
    };
    // If leg A is futures, use its value as commission_futures_rt too (both legs same fee)
    const symA = el('arbSymA')?.value || '';
    if (/\d/.test(symA)) {
        body.commission_futures_rt = parseFloat(el('arbCommStock')?.value || 15);
    }
    const r = await brCal.fetch('/params', 'POST', body);
    if (r && r.ok) {
        arbPyLog('✅ BR Calendar параметры сохранены');
        brCal.refresh();
        el('brCalEditPanel')?.remove();
    } else {
        arbPyLog('Ошибка сохранения BR Calendar', 'ERROR');
    }
}

function arbEntryModeToggle() {
    const checked = el('arbEntryModeRub')?.checked;
    const inputs = el('arbRubInputs');
    if (inputs) inputs.style.display = checked ? 'block' : 'none';
    // Grey out Z-score inputs when Variant 1 active
    const zShort = el('arbEntryZ');
    const zLong = el('arbEntryZLong');
    if (zShort) { zShort.disabled = checked; zShort.style.opacity = checked ? 0.4 : 1; }
    if (zLong) { zLong.disabled = checked; zLong.style.opacity = checked ? 0.4 : 1; }
}

function arbUpdateCommissionLabels() {
    // Update commission labels based on selected instruments
    const symA = el('arbSymA')?.value || '';
    const isFutA = /\d/.test(symA);
    const labelA = el('arbCommALabel');
    const warnBoth = el('arbCommBothFut');
    if (labelA) {
        labelA.textContent = isFutA ? 'Фьючерс A (₽ за контракт RT)' : 'Акция A (% за сторону)';
    }
    if (warnBoth) {
        warnBoth.style.display = isFutA ? 'block' : 'none';
    }
}

function arbPyUpdateL2() {
    if (!el('arbL2A')) return;
    if (!arbPyData) return;
    const obA = arbPyData.obA || {};
    const obB = arbPyData.obB || {};
    const basis = arbPyData.basis || {};
    if (el('arbL2A')) el('arbL2A').textContent = `${obA.bestBid||'—'} / ${obA.bestAsk||'—'} (${obA.totalVol||0})`;
    if (el('arbL2B')) el('arbL2B').textContent = `${obB.bestBid||'—'} / ${obB.bestAsk||'—'} (${obB.totalVol||0})`;
    if (el('arbDataPts')) el('arbDataPts').textContent = basis.data_points || 0;
    // Spread metrics
    const pa = basis.price_a || 0;
    const pb = basis.price_b || 0;
    const spread = basis.spread_rub || 0;
    const spreadPct = basis.spread_pct || 0;
    if (el('arbSpreadRub')) {
        el('arbSpreadRub').textContent = (spread >= 0 ? '+' : '') + spread.toFixed(2) + ' ₽';
        el('arbSpreadRub').style.color = spread > 0 ? 'var(--green)' : spread < 0 ? 'var(--red)' : 'var(--accent)';
    }
    if (el('arbSpreadPct')) el('arbSpreadPct').textContent = (spreadPct >= 0 ? '+' : '') + spreadPct.toFixed(3) + '%';
    if (el('arbFairSpread')) el('arbFairSpread').textContent = basis.fair_spread != null ? basis.fair_spread.toFixed(1) + ' ₽' : '—';
    if (el('arbDeviation')) {
        const dev = basis.deviation || 0;
        el('arbDeviation').textContent = (dev >= 0 ? '+' : '') + dev.toFixed(1) + ' ₽';
        el('arbDeviation').style.color = dev > 0 ? 'var(--green)' : dev < 0 ? 'var(--red)' : '';
    }
    if (el('arbSpotValue')) {
        el('arbSpotValue').textContent = pa > 0 ? (pa * 100).toFixed(0) + ' ₽' : '—';
        const spotLabel = el('arbSpotValue').parentElement.querySelector('.metric-label');
        if (spotLabel) spotLabel.textContent = (arbPyData?.tickerA || 'Spot') + (parseInt(arbPyData?.params?.contract_size)>1 ? ' × ' + arbPyData?.params?.contract_size : '');
    }
    if (el('arbFutValue')) el('arbFutValue').textContent = pb > 0 ? pb.toFixed(0) + ' ₽' : '—';
    const futLabel = el('arbFutLabel');
    if (futLabel) futLabel.textContent = (arbPyData?.tickerB || 'Фьюч') + ' (фьюч)';
    if (el('arbBasisVal')) el('arbBasisVal').textContent = basis.basis != null ? basis.basis.toFixed(1) : '—';
    if (el('arbZVal')) {
        const z = basis.zscore || 0;
        el('arbZVal').textContent = z.toFixed(3);
        el('arbZVal').style.color = Math.abs(z) >= 1.5 ? 'var(--accent)' : '';
    }
    if (el('arbMeanVal')) el('arbMeanVal').textContent = (basis.basis_mean || 0).toFixed(0);
    if (el('arbStdVal')) el('arbStdVal').textContent = (basis.basis_std || 0).toFixed(0);
    if (el('arbDaysExp')) el('arbDaysExp').textContent = basis.days_to_exp || '—';
    // Spread calculator (always visible)
    if (el('arbCalcSpread')) {
        el('arbCalcSpread').textContent = (spread >= 0 ? '+' : '') + spread.toFixed(2) + ' ₽';
        el('arbCalcSpread').style.color = spread > 0 ? 'var(--green)' : spread < 0 ? 'var(--red)' : '';
    }
    if (el('arbCalcFair')) el('arbCalcFair').textContent = (basis.fair_spread != null ? basis.fair_spread.toFixed(1) + ' ₽' : '—');
    if (el('arbCalcDev')) {
        const dev = basis.deviation || 0;
        el('arbCalcDev').textContent = (dev >= 0 ? '+' : '') + dev.toFixed(1) + ' ₽';
        el('arbCalcDev').style.color = dev > 0 ? 'var(--green)' : dev < 0 ? 'var(--red)' : '';
    }
    if (el('arbCalcZ')) {
        const z = basis.zscore || 0;
        el('arbCalcZ').textContent = z.toFixed(3);
        el('arbCalcZ').style.color = Math.abs(z) >= 1.5 ? 'var(--accent)' : '';
    }
    const ext = basis.dev_extremes || {};
    if (el('arbCalcMax')) el('arbCalcMax').textContent = '+' + (ext.dev_max || 0).toFixed(1) + '₽';
    if (el('arbCalcMin')) el('arbCalcMin').textContent = (ext.dev_min || 0).toFixed(1) + '₽';
    if (el('arbCalcMean')) el('arbCalcMean').textContent = (((ext.avg_max || 0) + (ext.avg_min || 0)) / 2).toFixed(1) + '₽';
    if (el('arbCalcSH')) el('arbCalcSH').textContent = (basis.suggested_high || 0).toFixed(0) + '₽';
    if (el('arbCalcSL')) el('arbCalcSL').textContent = (basis.suggested_low || 0).toFixed(0) + '₽';
}

async function arbPySaveFromPanel() {
    const chosenAccount = el('arbAccount')?.value;
    const body = {
        ticker_a: el('arbSymA').value,
        symbol_a: el('arbSymA').value + '@RTSX',
        ticker_b: el('arbSymB').value,
        symbol_b: el('arbSymB').value + '@RTSX',
        lots_a: parseInt(el('arbLotsA').value),
        lots_b: parseInt(el('arbLotsB').value),
        hedge_ratio: parseFloat(el('arbHedgeRatio').value),
        capital: parseFloat(el('arbCapital').value),
        entry_z: parseFloat(el('arbEntryZ').value),
        entry_z_long: parseFloat(el('arbEntryZLong').value),
        entry_mode: el('arbEntryModeRub')?.checked ? 'spread_rub' : 'zscore',
        spread_rub_high: parseFloat(el('arbSpreadRubHigh')?.value || 400),
        spread_rub_low: parseFloat(el('arbSpreadRubLow')?.value || 200),
        risk_free_rate: parseFloat(el('arbRate').value) / 100,
        expiration_date: el('arbExpiration').value,
        contract_size: parseInt(el('arbContractSize').value),
        lookback: parseInt(el('arbLookback').value),
        leg_a_timeout: parseInt(el('arbLegTimeout').value),
        min_fill_ratio: parseFloat(el('arbMinFill').value),
        min_profit_type: el('arbMinProfitType').value,
        min_profit_value: parseFloat(el('arbMinProfitVal').value),
        risk_type: el('arbRiskType').value,
        risk_value: parseFloat(el('arbRiskVal').value),
        allow_long_basis: el('arbAllowLong')?.checked || false,
        use_commission: el('arbUseCommission')?.checked ?? true,
        commission_stock_pct: parseFloat(el('arbCommStock')?.value || 0.04),
        commission_futures_rt: parseFloat(el('arbCommFut')?.value || 0.9),
    };
    // If leg A is futures, use its commission value too
    const symA = el('arbSymA')?.value || '';
    if (/\d/.test(symA)) {
        body.commission_futures_rt = parseFloat(el('arbCommStock')?.value || body.commission_futures_rt);
    }
    // Save account to server and refresh data
    if (chosenAccount) {
        await arbPyFetch('/account', 'POST', {account: chosenAccount});
        if (arbPyData) arbPyData.account = chosenAccount;
    }
    const r = await arbPyFetch('/params', 'POST', body);
    if (r && r.ok) {
        arbPyLog('✅ Параметры сохранены');
        arbPyRefresh();
    } else {
        arbPyLog('Ошибка сохранения параметров', 'ERROR');
    }
}

function arbPyShowCopyDialog() {
    const existing = el('arbCopyDialog');
    if (existing) { existing.remove(); return; }

    const p = (arbPyData && arbPyData.params) || {};
    const div = document.createElement('div');
    div.id = 'arbCopyDialog';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1100;width:450px;box-shadow:0 8px 32px rgba(0,0,0,.6)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            📋 Копировать робота
            <span style="flex:1"></span>
            <button class="btn btn-secondary btn-sm" onclick="el('arbCopyDialog')?.remove()">✕</button>
        </div>
        <div style="padding:16px;display:flex;flex-direction:column;gap:12px">
            <p style="color:#9CA3AF;font-size:13px">Создаст копию робота с новыми инструментами. Конфиг сохранится в отдельную папку.</p>
            <div class="metric-card"><div class="metric-label">Новый инструмент A (спот)</div>
                <select id="copySymA" class="input" style="width:180px">
                    <option value="GAZP">GAZP (Газпром)</option>
                    <option value="SBER">SBER (Сбербанк)</option>
                    <option value="LKOH">LKOH (Лукойл)</option>
                    <option value="ROSN">ROSN (Роснефть)</option>
                    <option value="TATN">TATN (Татнефть)</option>
                    <option value="GMKN">GMKN (Норникель)</option>
                    <option value="ALRS">ALRS (Алроса)</option>
                    <option value="VTBR">VTBR (ВТБ)</option>
                    <option value="MTSS">MTSS (МТС)</option>
                    <option value="NVTK">NVTK (Новатэк)</option>
                </select>
            </div>
            <div class="metric-card"><div class="metric-label">Новый инструмент B (фьючерс)</div>
                <select id="copySymB" class="input" style="width:180px">
                    <option value="GZM6">GZM6</option>
                    <option value="GZU6">GZU6</option>
                    <option value="SRM6">SRM6</option>
                    <option value="SRU6">SRU6</option>
                    <option value="LKM6">LKM6</option>
                    <option value="RNM6">RNM6</option>
                    <option value="TTM6">TTM6</option>
                    <option value="MXM6">MXM6</option>
                    <option value="SiU6">SiU6 (USD/RUB)</option><option value="SiZ6">SiZ6</option><option value="MXU6">MXU6</option><option value="GDU6">GDU6</option><option value="BRU6">BRU6</option><option value="SiU6">SiU6</option>
                    <option value="RIM6">RIM6</option>
                    <option value="GDM6">GDM6</option>
                    <option value="BRK6">BRK6</option>
                </select>
            </div>
            <button class="btn btn-primary" onclick="arbPyExecuteCopy()">📋 Создать копию</button>
            <div id="copyResult" style="font-size:13px;color:#9CA3AF"></div>
        </div>
    `;
    document.body.appendChild(div);
}

async function arbPyExecuteCopy() {
    const tickerA = el('copySymA').value;
    const tickerB = el('copySymB').value;
    const r = el('copyResult');
    r.textContent = '⏳ Копирование...';
    const resp = await arbPyFetch('/copy', 'POST', {ticker_a: tickerA, ticker_b: tickerB});
    if (resp && resp.ok) {
        r.innerHTML = `✅ Создан: <b>${resp.dir}</b><br>Папка: <code>${resp.path}</code><br>Для запуска: <code>cd ${resp.path} && python3 main_arb.py --port 5091</code>`;
        arbPyLog(`📋 Робот скопирован: ${tickerA}/${tickerB} → ${resp.dir}`);
    } else {
        r.textContent = '❌ Ошибка копирования';
        arbPyLog('Ошибка копирования робота', 'ERROR');
    }
}

async function arbPyLoadAccounts() {
    const data = await arbPyFetch('/accounts');
    if (data && data.accounts && el('arbAccount')) {
        const sel = el('arbAccount');
        const savedId = el('arbAccountSaved')?.value || '2049688';
        sel.innerHTML = '';
        data.accounts.forEach(a => {
            const id = typeof a === 'string' ? a : a.id;
            const name = typeof a === 'object' ? a.name : a;
            sel.innerHTML += `<option value="${id}" ${id === savedId ? 'selected' : ''}>${name}</option>`;
        });
    }
}

// === Arb Trade Journal ===
let _arbJournal = [];
let _arbJournalInstance = null; // null/'arbPy' = GAZP, 'sber' = SBER, 'brCal' = BR Calendar

// Parse entryTime/exitTime: supports both "2026-07-05 10:20:13" and "2026-07-05T10:20:13.542367+03:00"
function _arbParseTime(s) {
    if (!s) return {date: '', time: ''};
    if (s.includes('T')) {
        const m = s.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})/);
        return m ? {date: m[1], time: m[2]} : {date: s.slice(0,10), time: ''};
    }
    const parts = s.split(' ');
    return {date: parts[0] || '', time: parts[1] || ''};
}
function _arbDate(s) { return _arbParseTime(s).date; }

function arbJournalSetYesterday() {
    const d = new Date(Date.now() - 86400000);
    const s = d.toISOString().slice(0, 10);
    if (el('arbJournalDateFrom')) el('arbJournalDateFrom').value = s;
    if (el('arbJournalDateTo')) el('arbJournalDateTo').value = s;
    arbRenderJournal();
}

async function arbLoadJournal() {
    const inst = _arbJournalInstance === 'sber' ? sber : _arbJournalInstance === 'brCal' ? brCal : arbPy;
    const data = await inst.fetch('/status');
    if (!data) { _arbJournal = []; arbRenderJournal(); return; }
    _arbJournal = data.tradeHistory || [];
    arbRenderJournal();
}

async function arbResetStats() {
    if (!confirm('Сбросить статистику? Realized PnL → 0, история сделок очищена. positions сохраняются.')) return;
    const inst = _arbJournalInstance === 'sber' ? sber : _arbJournalInstance === 'brCal' ? brCal : arbPy;
    const r = await inst.fetch('/reset-stats', 'POST');
    if (r && r.ok) {
        const name = _arbJournalInstance === 'sber' ? 'SBER/SRU6' : _arbJournalInstance === 'brCal' ? 'BR Calendar' : 'GAZP/GZU6';
        arbPyLog(`🗑 Статистика ${name} сброшена`);
        arbLoadJournal();
        arbPyRefresh();
    } else {
        arbPyLog('Ошибка сброса статистики', 'ERROR');
    }
}

function arbRenderJournal() {
    const tbody = el('arbJournalBody');
    if (!tbody) return;
    const filter = el('arbJournalFilter')?.value || 'all';
    const dFrom = el('arbJournalDateFrom')?.value;
    const dTo = el('arbJournalDateTo')?.value;

    let trades = [..._arbJournal];
    // Filter
    if (filter === 'LONG') trades = trades.filter(t => t.side === 'LONG');
    else if (filter === 'SHORT') trades = trades.filter(t => t.side === 'SHORT');
    else if (filter === 'win') trades = trades.filter(t => t.pnl > 0);
    else if (filter === 'loss') trades = trades.filter(t => t.pnl <= 0);
    if (dFrom) trades = trades.filter(t => {
        const d1 = _arbDate(t.entryTime), d2 = _arbDate(t.exitTime);
        return d1 >= dFrom || d2 >= dFrom;
    });
    if (dTo) trades = trades.filter(t => {
        const d1 = _arbDate(t.entryTime), d2 = _arbDate(t.exitTime);
        return d1 <= dTo || d2 <= dTo;
    });

    // Reverse (newest first)
    trades.reverse();

    // Summary
    const totalPnl = trades.reduce((s,t) => s + (t.pnl||0), 0);
    const wins = trades.filter(t => t.pnl > 0);
    const losses = trades.filter(t => t.pnl <= 0);
    const winSum = wins.reduce((s,t) => s + t.pnl, 0);
    const lossSum = Math.abs(losses.reduce((s,t) => s + t.pnl, 0));
    const pf = lossSum > 0 ? (winSum / lossSum) : 0;
    const wr = trades.length > 0 ? (wins.length / trades.length * 100) : 0;
    const avgWin = wins.length > 0 ? winSum / wins.length : 0;
    const avgLoss = losses.length > 0 ? lossSum / losses.length : 0;

    const summaryEl = el('arbJournalSummary');
    if (summaryEl) {
        summaryEl.innerHTML = trades.length > 0 ? `
            <div class="metric-card"><div class="metric-label">Всего сделок</div><div style="font-size:18px;font-weight:bold">${trades.length}</div></div>
            <div class="metric-card"><div class="metric-label">Win Rate</div><div style="font-size:18px;font-weight:bold;color:${wr>=50?'var(--green)':'var(--red)'}">${wr.toFixed(1)}%</div></div>
            <div class="metric-card"><div class="metric-label">Profit Factor</div><div style="font-size:18px;font-weight:bold">${pf.toFixed(2)}</div></div>
            <div class="metric-card"><div class="metric-label">Avg Win</div><div style="font-size:18px;font-weight:bold;color:var(--green)">+${avgWin.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Avg Loss</div><div style="font-size:18px;font-weight:bold;color:var(--red)">−${avgLoss.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Итого PnL</div><div style="font-size:18px;font-weight:bold;color:${totalPnl>=0?'var(--green)':'var(--red)'}">${totalPnl>=0?'+':''}${totalPnl.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Комиссия</div><div style="font-size:18px;font-weight:bold;color:#9CA3AF">${trades.reduce((s,t)=>s+(t.commission||0),0).toFixed(0)}₽</div></div>
            <div class="metric-card" style="border:1px solid var(--border-color)"><div class="metric-label">&nbsp;</div><button class="btn btn-danger btn-sm" onclick="arbResetStats()" style="font-size:12px">🗑 Сбросить</button></div>
        ` : '';
    }

    // Table
    if (trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="14" style="text-align:center;padding:24px;color:#9CA3AF">Нет данных</td></tr>';
        if (el('arbJournalInfo')) el('arbJournalInfo').textContent = '';
        return;
    }

    let cumul = 0;
    tbody.innerHTML = trades.map(t => {
        cumul += (t.pnl || 0);
        const entryDate = _arbParseTime(t.entryTime).date;
        const entryTime = _arbParseTime(t.entryTime).time;
        const exitDate = _arbParseTime(t.exitTime).date;
        const exitTime = _arbParseTime(t.exitTime).time;
        // Show entry date if different from exit date (overnight)
        const displayDate = entryDate === exitDate ? exitDate : (entryDate + ' → ' + exitDate);
        const sideIcon = t.side === 'LONG' ? '🟢' : '🔴';
        const pnlCls = t.pnl > 0 ? 'color:var(--green)' : t.pnl < 0 ? 'color:var(--red)' : '';
        const cumulCls = cumul >= 0 ? 'color:var(--green)' : 'color:var(--red)';
        const entryAStr = (t.entryPriceA||0).toFixed(2);
        const entryBStr = (t.entryPriceB||0).toFixed(0);
        const exitAStr = (t.exitPriceA||0).toFixed(2);
        const exitBStr = (t.exitPriceB||0).toFixed(0);
        return `<tr style="border-bottom:1px solid var(--border-color)">
            <td style="padding:4px 6px;font-size:11px;color:#9CA3AF">${displayDate}</td>
            <td style="padding:4px 6px;font-size:11px">${entryTime}</td>
            <td style="padding:4px 6px;font-size:11px">${exitTime}</td>
            <td style="padding:4px 6px;text-align:center">${sideIcon}</td>
            <td style="padding:4px 6px;text-align:right;font-size:11px" title="A: ${entryAStr}  B: ${entryBStr}">${entryAStr}<span style="color:#666">/${entryBStr}</span></td>
            <td style="padding:4px 6px;text-align:right;font-size:11px" title="A: ${exitAStr}  B: ${exitBStr}">${exitAStr}<span style="color:#666">/${exitBStr}</span></td>
            <td style="padding:4px 6px;text-align:right;font-size:11px">${(t.entryBasis||0).toFixed(0)}</td>
            <td style="padding:4px 6px;text-align:right;font-size:11px">${(t.exitBasis||0).toFixed(0)}</td>
            <td style="padding:4px 6px;text-align:right;font-size:11px">${(t.entryZ||0).toFixed(2)}</td>
            <td style="padding:4px 6px;text-align:right;font-size:11px">${t.lotsA||0}/${t.lotsB||0}</td>
            <td style="padding:4px 6px;text-align:right;font-size:11px;color:#9CA3AF">${(t.holdMin||0).toFixed(0)}м</td>
            <td style="padding:4px 6px;text-align:right;font-size:11px;color:#9CA3AF">${(t.commission||0).toFixed(1)}₽</td>
            <td style="padding:4px 6px;text-align:right;font-weight:600;${pnlCls}">${t.pnl>=0?'+':''}${(t.pnl||0).toFixed(1)}</td>
            <td style="padding:4px 6px;text-align:right;${cumulCls}">${cumul>=0?'+':''}${cumul.toFixed(1)}</td>
        </tr>`;
    }).join('');

    if (el('arbJournalInfo')) {
        el('arbJournalInfo').textContent = `Показано ${trades.length} из ${_arbJournal.length} сделок`;
    }
}

// Init arb polling
setInterval(() => { if (el('arbPyStatus')) arbPyRefresh(); }, 3000);
setTimeout(() => { if (el('arbPyStatus')) arbPyRefresh(); }, 2000);

// === Init ===
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    loadConnectorStatus();
    initSignalR();
    // Default dates for backtest
    const today = new Date().toISOString().split('T')[0];
    const monthAgo = new Date(Date.now() - 30 * 86400000).toISOString().split('T')[0];
    el('testFrom').value = monthAgo;
    el('testTo').value = today;

    // Auto-refresh status
    setInterval(fetchStatus, 30000);

    // Автоподключение брокера если есть токен
    const savedToken = localStorage.getItem('finamToken');
    if (savedToken) {
        setTimeout(connectBroker, 1000);
    }
    
    // Загрузка сохранённых настроек Transaq
    const txLogin = localStorage.getItem('txLogin');
    if (txLogin && el('txLogin')) {
        el('txLogin').value = txLogin;
        el('txHost').value = localStorage.getItem('txHost') || 'tr.finam.ru';
        el('txPort').value = localStorage.getItem('txPort') || '39000';
    }

    // Load VP Scalp Grid params from saved config
    pythonRobotLoadConfig();
    
    // Проверка здоровья каждые 5 сек
    setInterval(checkHealth, 30000);

    // === Python Robot Polling ===
    setInterval(async () => { await pollPythonRobot(); renderRobots(); }, 2000);
    pollPythonRobot();

});

// === PYTHON ROBOT (VP Scalp Grid) ===
const ROBOT_API = 'http://' + window.location.hostname + ':5070';
let pythonRobot = null;

async function pollPythonRobot() {
    try {
        const resp = await fetch(ROBOT_API + '/status', {signal: AbortSignal.timeout(2000)});
        pythonRobot = await resp.json();
    } catch(e) {
        // Robot API down — try C# proxy for cached state
        try {
            const proxyResp = await fetch('/api/robot/status', {signal: AbortSignal.timeout(2000)});
            pythonRobot = await proxyResp.json();
        } catch(e2) {
            pythonRobot = null;
        }
    }
}

async function robotApi(action) {
    try {
        // For start/stop when robot is offline — use C# proxy
        if (action === 'start' || action === 'stop') {
            try {
                const proxyResp = await fetch('/api/robot/service/' + action, {method: 'POST'});
                const proxyData = await proxyResp.json();
                addLog(nowTime(), 'INFO', 'Robot ' + action + ' (proxy): ' + JSON.stringify(proxyData));
                // Wait for robot to start/stop
                await new Promise(r => setTimeout(r, action === 'start' ? 5000 : 2000));
                await pollPythonRobot();
                renderRobots();
                return;
            } catch(proxyErr) {
                // Fallback to direct API
            }
        }
        const resp = await fetch(ROBOT_API + '/' + action, {method: 'POST'});
        const data = await resp.json();
        addLog(nowTime(), 'INFO', 'Robot ' + action + ': ' + JSON.stringify(data));
        setTimeout(async () => { await pollPythonRobot(); renderRobots(); }, 500);
    } catch(e) {
        addLog(nowTime(), 'ERROR', 'Robot ' + action + ' failed: ' + e.message);
    }
}

async function pythonRobotLoadConfig() {
    const defaults = {max_levels:100, step_base:31, spread_base:31, max_hold_minutes:99999999999999, min_profit_per_lot:29, vp_lookback:33, vp_bin_size:50, vp_va_percent:0.70, rv_adaptation:false};
    let cfg = defaults;
    // Try robot API (5070), then C# proxy (5050)
    for (const base of [ROBOT_API, '']) {
        try {
            const resp = await fetch(base + '/api/robot/config', {signal: AbortSignal.timeout(2000)});
            const data = await resp.json();
            if (!data.error) { cfg = data; addLog(nowTime(), 'INFO', 'Config loaded'); break; }
        } catch(e) { continue; }
    }
    if (el('cfgVpMaxLevels')) el('cfgVpMaxLevels').value = cfg.max_levels;
    if (el('cfgVpStepBase')) el('cfgVpStepBase').value = cfg.step_base;
    if (el('cfgVpSpreadBase')) el('cfgVpSpreadBase').value = cfg.spread_base;
    if (el('cfgVpMaxHold')) el('cfgVpMaxHold').value = cfg.max_hold_minutes;
    if (el('cfgVpLookback')) el('cfgVpLookback').value = cfg.vp_lookback;
    if (el('cfgVpBinSize')) el('cfgVpBinSize').value = cfg.vp_bin_size;
    if (el('cfgVpVaPercent')) el('cfgVpVaPercent').value = cfg.vp_va_percent;
    if (el('cfgVpMinProfit')) el('cfgVpMinProfit').value = cfg.min_profit_per_lot;
    if (el('cfgVpRvAdapt')) el('cfgVpRvAdapt').checked = cfg.rv_adaptation;
}

async function pythonRobotSaveConfig() {
    // Only save to SiU6 (port 5070) when SiU6 is selected
    const ticker = el('cfgVpTicker')?.value || 'SiU6';
    if (ticker !== 'SiU6') return; // Don't overwrite SiU6 config with other ticker params

    const body = {
        max_levels: parseInt(el('cfgVpMaxLevels')?.value),
        step_base: parseInt(el('cfgVpStepBase')?.value),
        spread_base: parseInt(el('cfgVpSpreadBase')?.value),
        max_hold_minutes: parseInt(el('cfgVpMaxHold')?.value),
        min_profit_per_lot: parseInt(el('cfgVpMinProfit')?.value),
        vp_lookback: parseInt(el('cfgVpLookback')?.value),
        vp_bin_size: parseInt(el('cfgVpBinSize')?.value),
        vp_va_percent: parseFloat(el('cfgVpVaPercent')?.value),
        rv_adaptation: el('cfgVpRvAdapt')?.checked || false,
    };
    const json = JSON.stringify(body);
    // Try robot API (5070), then C# proxy (5050)
    let saved = false;
    for (const base of [ROBOT_API, '']) {
        try {
            const resp = await fetch(base + '/api/robot/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: json
            });
            const data = await resp.json();
            addLog(nowTime(), 'INFO', '💾 Config saved: ' + JSON.stringify(body));
            saved = true; break;
        } catch(e) { continue; }
    }
    if (!saved) addLog(nowTime(), 'ERROR', '💾 Save failed — all endpoints down');
}

function pythonRobotEditPanel() {
    const existing = el('robotEditPanel');
    if (existing) { existing.remove(); return; }

    // Read params from robot API (/status has current params), fallback to strategy.json
    const s = pythonRobot || {};
    const cfgML = s.max_levels || el('cfgVpMaxLevels')?.value || '100';
    const cfgStep = s.step_base || el('cfgVpStepBase')?.value || '31';
    const cfgSpread = s.spread_base || el('cfgVpSpreadBase')?.value || '31';
    const cfgHold = s.max_hold_minutes || el('cfgVpMaxHold')?.value || '99999999999999';
    const cfgLB = s.vp_lookback || el('cfgVpLookback')?.value || '33';
    const cfgBin = s.vp_bin_size || el('cfgVpBinSize')?.value || '50';
    const cfgVA = s.vp_va_percent || el('cfgVpVaPercent')?.value || '0.70';
    const cfgMinP = s.min_profit_per_lot || el('cfgVpMinProfit')?.value || '35';

    // Store port for main robot
    window._editRobotPort = 5070;

    const div = document.createElement('div');
    div.id = 'robotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            🐍 SiU6 VP Scalp Grid (PYTHON)
            <button class="btn btn-primary btn-sm" onclick="pythonRobotSaveFromPanel()">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="if(window._pyVpTimer){clearInterval(window._pyVpTimer);window._pyVpTimer=null;}el('robotEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:12px">
            <!-- VP индикаторы -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px">
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#60A5FA">VAH</div><div id="pyVAH" style="font-size:18px;font-weight:bold;color:#60A5FA">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#F59E0B">POC</div><div id="pyPOC" style="font-size:18px;font-weight:bold;color:#F59E0B">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #2D4A6D"><div class="metric-label" style="color:#34D399">VAL</div><div id="pyVAL" style="font-size:18px;font-weight:bold;color:#34D399">—</div></div>
                <div class="metric-card"><div class="metric-label">Цена</div><div id="pyPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Позиция</div><div id="pyDir" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Лоты</div><div id="pyLots" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Grid</div><div id="pyGrid" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Ср. цена</div><div id="pyAvgPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL/лот</div><div id="pyPnlPerLot" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL нереал.</div><div id="pyPnlUnreal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Hold</div><div id="pyHold" style="font-size:18px;font-weight:bold">0 мин</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Max Levels</div><input id="editPyMaxLevels" class="input" type="number" value="${cfgML}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Step Base (пт)</div><input id="editPyStepBase" class="input" type="number" value="${cfgStep}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Spread Base (пт)</div><input id="editPySpreadBase" class="input" type="number" value="${cfgSpread}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editPyMaxHold" class="input" type="number" value="${cfgHold}" style="width:100px"></div>
                <div class="metric-card"><div class="metric-label">VP Lookback</div><input id="editPyLookback" class="input" type="number" value="${cfgLB}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editPyBinSize" class="input" type="number" value="${cfgBin}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VA %</div><input id="editPyVaPercent" class="input" type="number" step="0.05" value="${cfgVA}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">PnL/лот (пт)</div><input id="editPyMinProfit" class="input" type="number" value="${cfgMinP}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">RV Adaptation</div><br><input id="editPyRvAdapt" type="checkbox" style="width:20px;height:20px;vertical-align:middle"></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал (QScalp style) -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>📋 Торговый журнал</strong>
            </div>
            <div id="pyJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="pyJournalFilter" class="input" style="width:120px" onchange="pyRenderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="pyJournalDateFrom" class="input" type="date" style="width:130px" onchange="pyLoadJournal()">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="pyJournalDateTo" class="input" type="date" style="width:130px" onchange="pyLoadJournal()">
                <button class="btn btn-secondary btn-sm" onclick="pyLoadJournal()">🔄</button>
            </div>
            <div style="max-height:350px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:8px;text-align:left">📅 Дата</th>
                            <th style="padding:8px;text-align:left">⏰ Вход</th>
                            <th style="padding:8px;text-align:left">⏰ Выход</th>
                            <th style="padding:8px;text-align:center">↔️</th>
                            <th style="padding:8px;text-align:right">💰 Вход</th>
                            <th style="padding:8px;text-align:right">💰 Выход</th>
                            <th style="padding:8px;text-align:right">Лоты</th>
                            <th style="padding:8px;text-align:right">PnL</th>
                            <th style="padding:8px;text-align:right">Кумул.</th>
                        </tr>
                    </thead>
                    <tbody id="pyJournalBody" style="background:var(--bg)"><tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Загрузка...</td></tr></tbody>
                </table>
            </div>
            <div id="pyJournalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px">Загрузка данных...</div>
        </div>
    `;
    document.body.appendChild(div);

    // Load config + start live VP update + journal
    pythonRobotLoadToPanel();
    pythonRobotUpdateVp();
    setTimeout(() => pyLoadJournal(), 300);
    if (window._pyVpTimer) clearInterval(window._pyVpTimer);
    window._pyVpTimer = setInterval(() => pythonRobotUpdateVp(), 2000);

    // Load trade journal for main robot (SiU6)
    _pyJournalTicker = 'SiU6';
    setTimeout(() => pyLoadJournal(), 300);
}

function pythonRobotUpdateVp() {
    if (!el('pyVAH')) return;
    const port = window._editRobotPort || 5070;
    if (port !== 5070) {
        // Fetch from instance API via C# proxy (CORS fix)
        fetch('/api/instance/status', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ port: port })
        }).then(r=>r.json()).then(s => {
            if (s.vah !== undefined && s.vah !== null) el('pyVAH').textContent = s.vah > 0 ? Math.round(s.vah) : '—';
            if (s.poc !== undefined && s.poc !== null) el('pyPOC').textContent = s.poc > 0 ? Math.round(s.poc) : '—';
            if (s.val !== undefined && s.val !== null) el('pyVAL').textContent = s.val > 0 ? Math.round(s.val) : '—';
            if (s.current_price !== undefined && s.current_price !== null) el('pyPrice').textContent = s.current_price > 0 ? s.current_price.toFixed(0) : '—';
            const dirText = s.direction > 0 ? 'Лонг' : s.direction < 0 ? 'Шорт' : 'Флэт';
            if (el('pyDir')) el('pyDir').textContent = dirText;
            if (el('pyLots')) el('pyLots').textContent = s.total_lots || 0;
            if (el('pyGrid')) el('pyGrid').textContent = (s.grid_levels||0) + ' (' + (s.filled_levels||0) + ' fill)';
            if (el('pyAvgPrice')) {
                const avgP = s.avg_price || s.entry_price;
                el('pyAvgPrice').textContent = s.direction !== 0 ? avgP.toFixed(0) : '—';
            }
            if (el('pyPnlPerLot')) {
                const perLot = s.pnl_per_lot || 0;
                el('pyPnlPerLot').textContent = s.direction !== 0 ? perLot.toFixed(0) + '₽' : '—';
                el('pyPnlPerLot').style.color = perLot >= 0 ? 'var(--green)' : 'var(--red)';
            }
            if (el('pyPnlUnreal')) { el('pyPnlUnreal').textContent = (s.pnl||0).toFixed(0)+'₽'; el('pyPnlUnreal').style.color = s.pnl >= 0 ? 'var(--green)' : 'var(--red)'; }
        }).catch(() => {});
        return;
    }
    // Main robot (port 5070)
    const s = pythonRobot;
    if (!s) return;
    if (s.vah !== undefined && s.vah !== null) el('pyVAH').textContent = s.vah > 0 ? Math.round(s.vah) : '—';
    if (s.poc !== undefined && s.poc !== null) el('pyPOC').textContent = s.poc > 0 ? Math.round(s.poc) : '—';
    if (s.val !== undefined && s.val !== null) el('pyVAL').textContent = s.val > 0 ? Math.round(s.val) : '—';
    if (s.current_price !== undefined && s.current_price !== null) el('pyPrice').textContent = s.current_price > 0 ? s.current_price.toFixed(0) : '—';
    const dirText = s.direction > 0 ? 'Лонг' : s.direction < 0 ? 'Шорт' : 'Флэт';
    if (el('pyDir')) el('pyDir').textContent = dirText;
    if (el('pyLots')) el('pyLots').textContent = s.total_lots || 0;
    if (el('pyGrid')) el('pyGrid').textContent = (s.grid_levels||0) + ' (' + (s.filled_levels||0) + ' fill)';
    if (el('pyAvgPrice')) {
        el('pyAvgPrice').textContent = s.direction !== 0 ? (s.avg_price || s.entry_price).toFixed(0) : '—';
    }
    if (el('pyPnlPerLot')) {
        const perLot = s.pnl_per_lot || 0;
        el('pyPnlPerLot').textContent = s.direction !== 0 ? perLot.toFixed(0) + '₽' : '—';
        el('pyPnlPerLot').style.color = perLot >= 0 ? 'var(--green)' : 'var(--red)';
    }
    if (el('pyPnlUnreal')) { el('pyPnlUnreal').textContent = (s.pnl||0).toFixed(0)+'₽'; el('pyPnlUnreal').style.color = s.pnl >= 0 ? 'var(--green)' : 'var(--red)'; }
    if (el('pyHold')) el('pyHold').textContent = (s.hold_minutes || 0) + ' мин';
}

// === Python Robot Trade Journal ===
let _pyJournalTrades = [];
let _pyJournalPositions = [];

let _pyJournalTicker = 'SiU6';

async function pyLoadJournal() {
    const info = el('pyJournalInfo');
    const body = el('pyJournalBody');
    if (!body) return;

    // Set default date range
    const today = new Date().toISOString().slice(0, 10);
    const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
    if (el('pyJournalDateFrom') && !el('pyJournalDateFrom').value) el('pyJournalDateFrom').value = weekAgo;
    if (el('pyJournalDateTo') && !el('pyJournalDateTo').value) el('pyJournalDateTo').value = today;

    if (info) info.textContent = 'Загрузка сделок...';
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:16px;color:#9CA3AF">⏳ Загрузка...</td></tr>';

    const dateFrom = el('pyJournalDateFrom')?.value;
    const dateTo = el('pyJournalDateTo')?.value;
    let params = '';
    if (dateFrom) params += `&dateFrom=${dateFrom}`;
    if (dateTo) params += `&dateTo=${dateTo}`;
    params = params ? '?' + params.substring(1) : '';

    // Fetch from C# server (Finam API)
    let trades = [];
    try {
        const resp = await fetch('/api/trades' + params, {signal: AbortSignal.timeout(15000)});
        if (resp.ok) {
            const raw = await resp.json();
            trades = Array.isArray(raw) ? raw : (raw.trades || []);
        }
    } catch (e) {
        console.error('pyLoadJournal error:', e);
    }

    // Normalize
    _pyJournalTrades = trades.map(t => ({
        time: t.timestamp || t.time || t.trade_date || t.datetime || '',
        ticker: t.symbol || t.ticker || t.Ticker || '',
        dir: (t.side || t.direction || t.dir || '').replace('SIDE_', '').toUpperCase(),
        price: parseFloat((t.price && t.price.value) ? t.price.value : (t.price || t.Price || 0)),
        lots: parseInt((t.size && t.size.value) ? t.size.value : (t.quantity || t.lots || t.Lots || t.qty || 0)),
        comment: t.comment || t.Comment || ''
    })).filter(t => t.price > 0 && t.lots > 0)
      .filter(t => !_pyJournalTicker || t.ticker.includes(_pyJournalTicker.replace('@RTSX','')));

    _pyJournalTrades.sort((a, b) => a.time.localeCompare(b.time));
    _pyJournalPositions = pyGroupPositions(_pyJournalTrades);
    pyRenderJournal();
}

function pyGroupPositions(trades) {
    const positions = [];
    let current = null;
    let netPos = 0;
    let cumPnl = 0;

    trades.forEach(t => {
        const isBuy = t.dir === 'BUY' || t.dir === '1' || t.dir === 'B';
        const lots = isBuy ? t.lots : -t.lots;

        if (!current) {
            current = {
                entryTime: t.time, exitTime: t.time,
                direction: isBuy ? 'LONG' : 'SHORT',
                entryPrice: t.price, exitPrice: t.price,
                maxLots: t.lots, trades: [t],
                netPnL: 0, commission: Math.round(t.lots * 0.90),
                cumPnl: 0, comments: t.comment ? [t.comment] : [],
                _buyCost: 0, _buyLots: 0,
                _sellCost: 0, _sellLots: 0
            };
            if (isBuy) { current._buyCost = t.price * t.lots; current._buyLots = t.lots; }
            else { current._sellCost = t.price * t.lots; current._sellLots = t.lots; }
            netPos = lots;
        } else {
            netPos += lots;
            if (isBuy) { current._buyCost += t.price * t.lots; current._buyLots += t.lots; }
            else { current._sellCost += t.price * t.lots; current._sellLots += t.lots; }
            current.exitTime = t.time;
            current.exitPrice = t.price;
            current.maxLots = Math.max(current.maxLots, Math.abs(netPos));
            current.trades.push(t);
            current.commission += Math.round(t.lots * 0.90);
            if (t.comment && !current.comments.includes(t.comment)) current.comments.push(t.comment);

            if (netPos === 0) {
                current.totalLots = current.maxLots;
                // PnL like broker: total sell proceeds - total buy cost - commission
                const totalSell = current._sellCost;
                const totalBuy = current._buyCost;
                current.netPnL = totalSell - totalBuy - current.commission;
                current.entryPrice = current._buyLots > 0 ? Math.round(current._buyCost / current._buyLots) : Math.round(current._sellCost / current._sellLots);
                current.exitPrice = current._sellLots > 0 ? Math.round(current._sellCost / current._sellLots) : Math.round(current._buyCost / current._buyLots);
                cumPnl += current.netPnL;
                current.cumPnl = cumPnl;
                positions.push(current);
                current = null; netPos = 0;
            }
        }
    });
    if (current) {
        current.totalLots = current.maxLots;
        const totalSell = current._sellCost;
        const totalBuy = current._buyCost;
        current.netPnL = totalSell - totalBuy - current.commission;
        current.entryPrice = current._buyLots > 0 ? Math.round(current._buyCost / current._buyLots) : Math.round(current._sellCost / current._sellLots);
        current.exitPrice = current._sellLots > 0 ? Math.round(current._sellCost / current._sellLots) : Math.round(current._buyCost / current._buyLots);
        current.isOpen = true;
        cumPnl += current.netPnL;
        current.cumPnl = cumPnl;
        positions.push(current);
    }
    return positions;
}

function pyRenderJournal() {
    const body = el('pyJournalBody');
    const info = el('pyJournalInfo');
    const summary = el('pyJournalSummary');
    if (!body) return;

    const filter = el('pyJournalFilter')?.value || 'all';
    let positions = _pyJournalPositions;
    if (filter === 'LONG') positions = positions.filter(p => p.direction === 'LONG');
    else if (filter === 'SHORT') positions = positions.filter(p => p.direction === 'SHORT');
    else if (filter === 'win') positions = positions.filter(p => p.netPnL > 0);
    else if (filter === 'loss') positions = positions.filter(p => p.netPnL < 0);

    const wins = positions.filter(p => p.netPnL > 0);
    const losses = positions.filter(p => p.netPnL < 0);
    const winRate = positions.length ? (wins.length / positions.length * 100).toFixed(1) : '—';
    const totalPnl = positions.reduce((s, p) => s + p.netPnL, 0);
    const avgWin = wins.length ? wins.reduce((s, p) => s + p.netPnL, 0) / wins.length : 0;
    const avgLoss = losses.length ? losses.reduce((s, p) => s + p.netPnL, 0) / losses.length : 0;
    const grossProfit = wins.reduce((s, p) => s + p.netPnL, 0);
    const grossLoss = Math.abs(losses.reduce((s, p) => s + p.netPnL, 0));
    const pf = grossLoss > 0 ? (grossProfit / grossLoss).toFixed(2) : grossProfit > 0 ? '∞' : '—';
    const totalComm = positions.reduce((s, p) => s + p.commission, 0);
    let maxDD = 0, peak = 0;
    positions.forEach(p => { if (p.cumPnl > peak) peak = p.cumPnl; const dd = peak - p.cumPnl; if (dd > maxDD) maxDD = dd; });

    if (summary) {
        summary.innerHTML = `
            <div class="metric-card"><div class="metric-label">Позиций</div><div style="font-size:1.1em;font-weight:700;color:var(--accent)">${positions.length}</div></div>
            <div class="metric-card"><div class="metric-label">W / L</div><div style="font-size:1.1em;font-weight:700"><span style="color:var(--green)">${wins.length}</span> / <span style="color:var(--red)">${losses.length}</span></div></div>
            <div class="metric-card"><div class="metric-label">WR</div><div style="font-size:1.1em;font-weight:700;color:${parseFloat(winRate) >= 50 ? 'var(--green)' : 'var(--red)'}">${winRate}%</div></div>
            <div class="metric-card"><div class="metric-label">PnL нетто</div><div style="font-size:1.1em;font-weight:700;color:${totalPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)} ₽</div></div>
            <div class="metric-card"><div class="metric-label">PF</div><div style="font-size:1.1em;font-weight:700">${pf}</div></div>
            <div class="metric-card"><div class="metric-label">Avg W</div><div style="font-size:1.1em;font-weight:700;color:var(--green)">${avgWin >= 0 ? '+' : ''}${avgWin.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Avg L</div><div style="font-size:1.1em;font-weight:700;color:var(--red)">${avgLoss.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Комиссия</div><div style="font-size:1.1em;font-weight:700;color:#9CA3AF">${totalComm.toLocaleString('ru-RU')}₽</div></div>
            <div class="metric-card"><div class="metric-label">Max DD</div><div style="font-size:1.1em;font-weight:700;color:var(--red)">${maxDD > 0 ? '-' : ''}${maxDD.toFixed(0)}₽</div></div>
        `;
    }

    if (!positions.length) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Нет сделок за выбранный период</td></tr>';
        if (info) info.textContent = 'Нет данных';
        return;
    }

    const sorted = [...positions].reverse();
    body.innerHTML = sorted.map((p, idx) => {
        const pnlCls = p.netPnL >= 0 ? 'var(--green)' : 'var(--red)';
        const dirCls = p.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
        const dirIcon = p.direction === 'LONG' ? '🟢' : '🔴';
        const entryT = p.entryTime ? p.entryTime.replace(/\.\d+Z$/, '').replace('T', ' ').slice(11, 19) : '—';
        const exitT = p.exitTime ? p.exitTime.replace(/\.\d+Z$/, '').replace('T', ' ').slice(11, 19) : '—';
        const dateStr = p.entryTime ? p.entryTime.slice(0, 10) : '';
        const bg = p.isOpen ? 'rgba(255,215,0,0.05)' : (idx % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)');
        const openBadge = p.isOpen ? ' <span style="color:#FFD700;font-size:10px">⚡ОТКРЫТА</span>' : '';
        return `<tr style="border-bottom:1px solid #2D2D44;background:${bg}">
            <td style="padding:6px 8px;font-size:11px;color:#9CA3AF">${dateStr}</td>
            <td style="padding:6px 8px;font-family:monospace;font-size:12px">${entryT}</td>
            <td style="padding:6px 8px;font-family:monospace;font-size:12px">${exitT}${openBadge}</td>
            <td style="padding:6px 8px;text-align:center;color:${dirCls};font-weight:700;font-size:12px">${dirIcon}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:600">${p.entryPrice.toFixed(2)}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:600">${p.exitPrice.toFixed(2)}</td>
            <td style="padding:6px 8px;text-align:right">${p.totalLots}</td>
            <td style="padding:6px 8px;text-align:right;font-weight:700;color:${pnlCls}">${p.netPnL >= 0 ? '+' : ''}${p.netPnL.toFixed(0)} ₽</td>
            <td style="padding:6px 8px;text-align:right;font-size:11px;color:${p.cumPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${p.cumPnl >= 0 ? '+' : ''}${p.cumPnl.toFixed(0)}</td>
        </tr>`;
    }).join('');

    if (info) info.textContent = `${positions.length} позиций · ${wins.length}W / ${losses.length}L · PnL ${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)}₽`;
}

async function pythonRobotLoadToPanel() {
    const defaults = {max_levels:100, step_base:31, spread_base:31, max_hold_minutes:99999999999999, min_profit_per_lot:35, vp_lookback:33, vp_bin_size:50, vp_va_percent:0.70, rv_adaptation:false};
    let cfg = defaults;
    try {
        const resp = await fetch(ROBOT_API + '/api/robot/config', {signal: AbortSignal.timeout(2000)});
        const data = await resp.json();
        if (!data.error) cfg = data;
    } catch(e) {
        try {
            const resp = await fetch('/api/robot/config', {signal: AbortSignal.timeout(2000)});
            const data = await resp.json();
            if (!data.error) cfg = data;
        } catch(e2) {}
    }
    if (el('editPyMaxLevels')) el('editPyMaxLevels').value = cfg.max_levels;
    if (el('editPyStepBase')) el('editPyStepBase').value = cfg.step_base;
    if (el('editPySpreadBase')) el('editPySpreadBase').value = cfg.spread_base;
    if (el('editPyMaxHold')) el('editPyMaxHold').value = cfg.max_hold_minutes;
    if (el('editPyLookback')) el('editPyLookback').value = cfg.vp_lookback;
    if (el('editPyBinSize')) el('editPyBinSize').value = cfg.vp_bin_size;
    if (el('editPyVaPercent')) el('editPyVaPercent').value = cfg.vp_va_percent;
    if (el('editPyMinProfit')) el('editPyMinProfit').value = cfg.min_profit_per_lot;
    if (el('editPyRvAdapt')) el('editPyRvAdapt').checked = cfg.rv_adaptation;
}

async function pythonRobotSaveFromPanel() {
    const body = {
        max_levels: parseInt(el('editPyMaxLevels')?.value),
        step_base: parseInt(el('editPyStepBase')?.value),
        spread_base: parseInt(el('editPySpreadBase')?.value),
        max_hold_minutes: parseInt(el('editPyMaxHold')?.value),
        min_profit_per_lot: parseInt(el('editPyMinProfit')?.value),
        vp_lookback: parseInt(el('editPyLookback')?.value),
        vp_bin_size: parseInt(el('editPyBinSize')?.value),
        vp_va_percent: parseFloat(el('editPyVaPercent')?.value),
        rv_adaptation: el('editPyRvAdapt')?.checked || false,
    };
    // Try live robot first, then C# proxy
    let saved = false;
    try {
        const resp = await fetch(ROBOT_API + '/api/robot/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await resp.json();
        addLog(nowTime(), 'INFO', '🐍 Config saved (live): ' + JSON.stringify(data));
        saved = true;
    } catch(e) {
        try {
            const resp = await fetch('/api/robot/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            });
            const data = await resp.json();
            addLog(nowTime(), 'INFO', '🐍 Config saved (proxy): ' + JSON.stringify(data));
            saved = true;
        } catch(e2) {
            addLog(nowTime(), 'ERROR', '🐍 Save failed: ' + e2.message);
        }
    }
    // Also update strategy tab fields
    if (saved) {
        if (el('cfgVpMaxLevels')) el('cfgVpMaxLevels').value = body.max_levels;
        if (el('cfgVpStepBase')) el('cfgVpStepBase').value = body.step_base;
        if (el('cfgVpSpreadBase')) el('cfgVpSpreadBase').value = body.spread_base;
        if (el('cfgVpMaxHold')) el('cfgVpMaxHold').value = body.max_hold_minutes;
        if (el('cfgVpMinProfit')) el('cfgVpMinProfit').value = body.min_profit_per_lot;
    }
}

async function pythonRobotLoadGridLevels() {
    const tbody = el('pyGridBody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#9CA3AF">Загрузка...</td></tr>';
    try {
        const resp = await fetch(ROBOT_API + '/api/robot/grid-levels');
        const data = await resp.json();
        if (!data.levels.length) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#9CA3AF">Нет grid уровней</td></tr>';
        } else {
            tbody.innerHTML = data.levels.map(l => `<tr>
                <td>${l.level}</td>
                <td>${l.side}</td>
                <td>${l.price.toFixed(0)}</td>
                <td>${l.status}</td>
                <td>${l.tp_price > 0 ? l.tp_price.toFixed(0) : '—'}</td>
                <td>${l.tp_closed_price > 0 ? l.tp_closed_price.toFixed(0) : '—'}</td>
            </tr>`).join('');
        }
    } catch(e) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#f44336">Ошибка: ' + e.message + '</td></tr>';
    }
}

function createVpRobotFromTest() {
    const ticker = el('testTicker')?.value || 'SiU6';
    const params = {
        max_levels: parseInt(el('tstVpMaxLevels')?.value) || 100,
        step_base: parseInt(el('tstVpStepBase')?.value) || 31,
        spread_base: parseInt(el('tstVpSpreadBase')?.value) || 31,
        min_profit_per_lot: parseInt(el('tstVpMinProfit')?.value) || 35,
        vp_lookback: parseInt(el('tstVpLookback')?.value) || 33,
        vp_bin_size: parseInt(el('tstVpBinSize')?.value) || 50,
        vp_va_percent: parseFloat(el('tstVpVaPercent')?.value) || 0.70,
        rv_adaptation: false,
        max_hold_minutes: 99999999999999,
        ticker: ticker
    };
    // Find next available port (5071+)
    const usedPorts = robots.map(r => r.port || 0);
    let port = 5071;
    while (usedPorts.includes(port)) port++;
    params.port = port;
    const id = ticker + '_' + port;

    // Create instance on server
    fetch('/api/instance/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ ...params, id })
    }).then(r => r.json()).then(data => {
        addLog(nowTime(), 'INFO', '🤖 Инстанс создан: ' + ticker + ' port=' + port);
    }).catch(e => {
        addLog(nowTime(), 'WARN', 'Instance create: ' + e.message);
    });

    // Create robot in UI
    const robot = {
        id: id,
        ticker: ticker,
        strategy: 'VP Scalp Grid',
        port: port,
        step_base: params.step_base,
        spread_base: params.spread_base,
        max_levels: params.max_levels,
        vp_lookback: params.vp_lookback,
        vp_bin_size: params.vp_bin_size,
        vp_va_percent: params.vp_va_percent,
        min_profit: params.min_profit_per_lot,
        hold_minutes: params.max_hold_minutes,
        status: 'stopped',
        position: '—',
        pnlToday: 0, pnlTotal: 0,
        lotsOpen: 0, go: 0,
        isInstance: true
    };
    robots.push(robot);
    saveRobots();
    renderRobots();
    // Switch to monitoring tab
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', '🤖 Робот создан: ' + ticker + ' step=' + params.step_base + ' spread=' + params.spread_base + ' port=' + port);
}

function pythonRobotTest() {
    // Copy params from strategy tab to testing tab fields
    const map = {'cfgVpMaxLevels':'tstVpMaxLevels','cfgVpStepBase':'tstVpStepBase','cfgVpSpreadBase':'tstVpSpreadBase','cfgVpMinProfit':'tstVpMinProfit','cfgVpLookback':'tstVpLookback','cfgVpBinSize':'tstVpBinSize','cfgVpVaPercent':'tstVpVaPercent'};
    for (const [src, dst] of Object.entries(map)) {
        if (el(src) && el(dst)) el(dst).value = el(src).value;
    }
    // Copy instrument
    const ticker = el('cfgVpTicker')?.value || 'SiU6';
    if (el('testTicker')) el('testTicker').value = ticker;
    // Switch to testing tab
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="testing"]').classList.add('active');
    el('testing')?.classList.add('active');
    addLog(nowTime(), 'INFO', '📋 Параметры ' + ticker + ' перенесены на вкладку тестирования');
}

function clearTestResults() {
    // Reset all metric values
    ['btPnl','btPpd','btAvgTrade','btAvgWin','btAvgLoss','btMaxDD','btMaxWin','btMaxLoss','btCommission'].forEach(id => {
        const e = el(id); if(e) { e.textContent = '—'; e.className = 'metric-value'; }
    });
    ['btTrades','btRtpd','btWR','btPF','btSharpe','btMaxDDpct','btMaxDDdur','btMaxPos','btMaxGO','btSessions','btSessionsWR','btMedSession','btMaxWinsStreak','btMaxLossStreak'].forEach(id => {
        const e = el(id); if(e) e.textContent = '—';
    });
    // Clear charts
    ['testEquityChart','testCandleChart'].forEach(id => {
        const e = el(id); if(e) e.innerHTML = '';
    });
    // Clear trades table
    const tbody = el('testTradesTable');
    if(tbody) tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:24px;color:#9CA3AF">Нет данных</td></tr>';
    // Hide results
    el('testResults').style.display = 'none';
    el('testStatus').textContent = '';
}

function startVpTest() {
    const params = {
        ticker: el('testTicker')?.value || 'SiU6',
        tf: parseInt(el('testTf')?.value) || 5,
        max_levels: parseInt(el('tstVpMaxLevels')?.value) || 100,
        step_base: parseInt(el('tstVpStepBase')?.value) || 31,
        spread_base: parseInt(el('tstVpSpreadBase')?.value) || 31,
        min_profit: parseInt(el('tstVpMinProfit')?.value) || 35,
        vp_lookback: parseInt(el('tstVpLookback')?.value) || 33,
        vp_bin_size: parseInt(el('tstVpBinSize')?.value) || 50,
        vp_va_percent: parseFloat(el('tstVpVaPercent')?.value) || 0.70,
        commission: parseFloat(el('tstVpComm')?.value) || 0.90,
        from: el('testFrom')?.value || undefined,
        to: el('testTo')?.value || undefined,
    };
    el('testResults').style.display = 'block';
    const status = el('testStatus');
    if (status) status.textContent = '⏳ Тестирование...';
    addLog(nowTime(), 'INFO', '🧪 VP Scalp Grid: ' + params.ticker + ' step=' + params.step_base + ' spread=' + params.spread_base + ' levels=' + params.max_levels);
    fetch('/api/vp-backtest', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(params),
        signal: AbortSignal.timeout(120000)
    }).then(r => r.json()).then(r => {
        if (r.error) { if (status) status.textContent = '❌ ' + r.error; return; }
        if (status) status.textContent = '✅ Готово';
        const fmt = v => (v||0).toLocaleString('ru-RU',{maximumFractionDigits:0});
        const cls = v => v >= 0 ? 'metric-value green' : 'metric-value red';
        const setVal = (id, v, s='') => { const e = el(id); if(e) { e.textContent = (v>=0?'+':'')+fmt(v)+s; e.className = cls(v); } };
        const setTxt = (id, v) => { const e = el(id); if(e) e.textContent = v; };
        setVal('btPnl', r.pnl, ' ₽');
        setVal('btPpd', r.ppd, ' ₽/д');
        setTxt('btTrades', r.sessions);
        setTxt('btRtpd', r.rtpd);
        setTxt('btWR', r.winRate + '%');
        setTxt('btPF', r.profitFactor);
        setTxt('btSharpe', '—');
        setVal('btAvgTrade', r.avgTrade, ' ₽');
        setVal('btAvgWin', r.avgWin, ' ₽');
        setVal('btAvgLoss', r.avgLoss, ' ₽');
        setVal('btMaxDD', r.maxDD, ' ₽');
        setTxt('btMaxDDpct', r.days > 0 ? (Math.abs(r.maxDD) / (r.maxDD + r.pnl || 1) * 100).toFixed(1) + '%' : '—');
        setTxt('btMaxDDdur', r.maxDDdur + ' сделок');
        setTxt('btMaxPos', r.maxPos);
        setTxt('btMaxGO', r.maxGO ? fmt(r.maxGO) + ' ₽' : '—');
        setTxt('btSessions', r.sessions);
        setTxt('btSessionsWR', r.winRate + '%');
        setTxt('btMedSession', '—');
        setVal('btMaxWin', r.maxWin, ' ₽');
        setVal('btMaxLoss', r.maxLoss, ' ₽');
        setTxt('btMaxWinsStreak', r.winStreak);
        setTxt('btMaxLossStreak', r.lossStreak);
        setVal('btCommission', r.totalComm, ' ₽');
        // Trades table
        const tbody = el('testTradesTable');
        if (tbody && r.trades) {
            tbody.innerHTML = r.trades.map(t => `<tr>
                <td>${t.et||''}</td><td>${t.xt||''}</td>
                <td style="color:${t.dir==='LONG'?'var(--green)':'var(--red)'}">${t.dir}</td>
                <td>${t.ep}</td><td>${t.xp}</td><td>${t.lots}</td>
                <td style="color:${t.pnl>=0?'var(--green)':'var(--red)'}">${t.pnl>=0?'+':''}${t.pnl}</td>
                <td>1</td></tr>`).join('');
        }
        // Equity chart
        drawVpEquity(r.equity || []);
        // Candle chart with trades
        drawVpCandles(r.candles || [], r.markers || []);
        addLog(nowTime(), 'INFO', '🧪 Результат: PnL=' + r.pnl + ' WR=' + r.winRate + '% PF=' + r.profitFactor);
    }).catch(e => {
        if (status) status.textContent = '❌ ' + e.message;
        addLog(nowTime(), 'ERROR', '🧪 Ошибка: ' + e.message);
    });
}

function drawVpEquity(pts) {
    const container = el('testEquityChart');
    if (!container || !pts.length) return;
    const W = container.clientWidth || 600, H = container.clientHeight || 300;
    const vals = pts.map(p => p.e);
    const mn = Math.min(...vals), mx = Math.max(...vals);
    const range = mx - mn || 1;
    let pathD = '';
    pts.forEach((p, i) => {
        const x = (i / (pts.length - 1)) * (W - 20) + 10;
        const y = H - 10 - ((p.e - mn) / range) * (H - 20);
        pathD += (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
    });
    const color = vals[vals.length-1] >= 0 ? '#4CAF50' : '#f44336';
    container.innerHTML = `<svg width="100%" height="100%" viewBox="0 0 ${W} ${H}">
        <text x="10" y="15" fill="#888" font-size="11">${fmtN(mx)}</text>
        <text x="10" y="${H-2}" fill="#888" font-size="11">${fmtN(mn)}</text>
        <path d="${pathD}" fill="none" stroke="${color}" stroke-width="1.5"/>
        <line x1="10" y1="${H-10-((0-mn)/range)*(H-20)}" x2="${W-10}" y2="${H-10-((0-mn)/range)*(H-20)}" stroke="#555" stroke-width="0.5" stroke-dasharray="4"/>
    </svg>`;
}

function drawVpCandles(candles, markers) {
    const container = el('testCandleChart');
    if (!container || !candles.length) return;
    const W = container.clientWidth || 600, H = container.clientHeight || 400;
    const closes = candles.map(c => c.c);
    const allH = candles.map(c => c.h), allL = candles.map(c => c.l);
    const mn = Math.min(...allL), mx = Math.max(...allH);
    const range = mx - mn || 1;
    const bw = Math.max(1, (W - 20) / candles.length * 0.7);
    const step = (W - 20) / candles.length;
    let svg = `<svg width="100%" height="100%" viewBox="0 0 ${W} ${H}">`;
    // Zero line
    candles.forEach((c, i) => {
        const x = 10 + i * step + step/2;
        const yO = H - 10 - ((c.o - mn) / range) * (H - 20);
        const yC = H - 10 - ((c.c - mn) / range) * (H - 20);
        const yH = H - 10 - ((c.h - mn) / range) * (H - 20);
        const yL = H - 10 - ((c.l - mn) / range) * (H - 20);
        const up = c.c >= c.o;
        const col = up ? '#4CAF50' : '#f44336';
        svg += `<line x1="${x}" y1="${yH}" x2="${x}" y2="${yL}" stroke="${col}" stroke-width="0.5"/>`;
        svg += `<rect x="${x-bw/2}" y="${Math.min(yO,yC)}" width="${bw}" height="${Math.max(Math.abs(yC-yO),1)}" fill="${col}"/>`;
    });
    // Trade markers
    const markerMap = {};
    markers.forEach(m => { markerMap[m.et] = m; });
    candles.forEach((c, i) => {
        const m = markerMap[c.t];
        if (!m) return;
        const x = 10 + i * step + step/2;
        if (m.dir === 'LONG') {
            const y = H - 10 - ((m.ep - mn) / range) * (H - 20) + 12;
            svg += `<polygon points="${x},${y-10} ${x-5},${y} ${x+5},${y}" fill="#4CAF50"/>`;
        } else {
            const y = H - 10 - ((m.ep - mn) / range) * (H - 20) - 12;
            svg += `<polygon points="${x},${y+10} ${x-5},${y} ${x+5},${y}" fill="#f44336"/>`;
        }
    });
    svg += `<text x="10" y="15" fill="#888" font-size="11">${fmtN(mx)}</text>`;
    svg += `<text x="10" y="${H-2}" fill="#888" font-size="11">${fmtN(mn)}</text>`;
    svg += '</svg>';
    container.innerHTML = svg;
}

function fmtN(v) { return Math.round(v).toLocaleString('ru-RU'); }

function pythonRobotCreate() {
    const ticker = el('cfgVpTicker')?.value || 'SiU6';
    const params = {
        max_levels: parseInt(el('cfgVpMaxLevels')?.value) || 100,
        step_base: parseInt(el('cfgVpStepBase')?.value) || 31,
        spread_base: parseInt(el('cfgVpSpreadBase')?.value) || 31,
        min_profit_per_lot: parseInt(el('cfgVpMinProfit')?.value) || 29,
        vp_lookback: parseInt(el('cfgVpLookback')?.value) || 33,
        vp_bin_size: parseInt(el('cfgVpBinSize')?.value) || 50,
        vp_va_percent: parseFloat(el('cfgVpVaPercent')?.value) || 0.70,
    };

    if (ticker === 'SiU6') {
        // SiU6 — save to main robot config (port 5070)
        fetch(ROBOT_API + '/api/robot/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                max_levels: params.max_levels,
                step_base: params.step_base,
                spread_base: params.spread_base,
                min_profit_per_lot: params.min_profit_per_lot,
                vp_lookback: params.vp_lookback,
                vp_bin_size: params.vp_bin_size,
                vp_va_percent: params.vp_va_percent,
            })
        }).then(r => r.json()).then(data => {
            addLog(nowTime(), 'INFO', '🤖 SiU6 конфиг сохранён');
        }).catch(e => {
            localStorage.setItem('vp_robot_params', JSON.stringify(params));
            addLog(nowTime(), 'INFO', '🤖 SiU6 конфиг сохранён локально');
        });
        return;
    }

    // Non-SiU6 — create instance
    const usedPorts = robots.map(r => r.port || 0).filter(p => p > 0);
    let port = 5071;
    while (usedPorts.includes(port)) port++;
    const id = ticker + '_' + port;
    params.ticker = ticker;
    params.port = port;
    params.id = id;

    fetch('/api/instance/create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(params)
    }).then(r => r.json()).then(data => {
        addLog(nowTime(), 'INFO', '🤖 Инстанс создан: ' + ticker + ' port=' + port);
    }).catch(e => {
        addLog(nowTime(), 'WARN', 'Instance create: ' + e.message);
    });

    const robot = {
        id: id,
        ticker: ticker,
        strategy: 'VP Scalp Grid',
        port: port,
        step_base: params.step_base,
        spread_base: params.spread_base,
        max_levels: params.max_levels,
        vp_lookback: params.vp_lookback,
        vp_bin_size: params.vp_bin_size,
        vp_va_percent: params.vp_va_percent,
        min_profit: params.min_profit_per_lot,
        hold_minutes: 99999999999999,
        status: 'stopped',
        position: '—',
        pnlToday: 0, pnlTotal: 0,
        lotsOpen: 0, go: 0,
        isInstance: true
    };
    robots.push(robot);
    saveRobots();
    renderRobots();
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.querySelector('[data-tab="monitoring"]').classList.add('active');
    el('monitoring')?.classList.add('active');
    addLog(nowTime(), 'INFO', '🤖 Робот создан: ' + ticker + ' step=' + params.step_base + ' spread=' + params.spread_base + ' port=' + port);
}

// === ORDER FLOW ROBOT (порт 5080) ===
const OF_ROBOT_API = 'http://' + window.location.hostname + ':5080';
let ofRobot = null;

// === ORDER FLOW EDIT PANEL ===
function ofRobotEditPanel() {
    const existing = el('ofRobotEditPanel');
    if (existing) { existing.remove(); return; }

    const s = ofRobot || {};
    const p = s.params || {};

    const div = document.createElement('div');
    div.id = 'ofRobotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            📊 SiU6 Order Flow (PYTHON)
            <button class="btn btn-primary btn-sm" onclick="ofRobotSaveFromPanel()">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="if(window._ofVpTimer){clearInterval(window._ofVpTimer);window._ofVpTimer=null;}el('ofRobotEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:12px">
            <!-- OF индикаторы -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px">
                <div class="metric-card" style="background:#1a2332;border:1px solid #9C27B0;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleCvd" type="checkbox" checked style="width:14px;height:14px;cursor:pointer" onchange="toggleOfSignal('use_cvd', this.checked)"><div class="metric-label" style="color:#CE93D8">CVD Trend</div></div><div id="ofCvd" style="font-size:18px;font-weight:bold;color:#CE93D8">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #9C27B0;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleDmWall" type="checkbox" style="width:14px;height:14px;cursor:pointer" onchange="toggleOfSignal('use_dm_wall', this.checked)"><div class="metric-label" style="color:#FF7043">dm_wall</div></div><div id="ofDelta" style="font-size:18px;font-weight:bold;color:#FF7043">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #9C27B0;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleCvdAccel" type="checkbox" checked style="width:14px;height:14px;cursor:pointer" onchange="toggleOfSignal('use_cvd_accel', this.checked)"><div class="metric-label" style="color:#66BB6A">CVD Accel</div></div><div id="ofCvdAccel" style="font-size:18px;font-weight:bold;color:#66BB6A">—</div></div>

                <div class="metric-card" style="background:#1a2332;border:1px solid #607D8B;display:flex;flex-direction:column;align-items:center;gap:2px"><div class="metric-label" style="color:#B0BEC5">Фильтр</div><select id="ofDirFilter" class="input" style="width:80px;font-size:13px;font-weight:bold;background:transparent;color:#fff;border:1px solid #607D8B;text-align:center;cursor:pointer" onchange="ofSetDirection(this.value)"><option value="both" style="color:#000">ОБА</option><option value="long" style="color:#000">LONG</option><option value="short" style="color:#000">SHORT</option></select></div>

                <div class="metric-card"><div class="metric-label">Цена</div><div id="ofPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Позиция</div><div id="ofDir" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Лоты</div><div id="ofLots" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Ср. цена</div><div id="ofAvgPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL/лот</div><div id="ofPnlPerLot" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL нереал.</div><div id="ofPnlUnreal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Realized</div><div id="ofRealized" style="font-size:18px;font-weight:bold">0₽</div></div>
                <div class="metric-card"><div class="metric-label">Сигнал</div><div id="ofSignal" style="font-size:16px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Hold</div><div id="ofHold" style="font-size:18px;font-weight:bold">0 мин</div></div>
                <div class="metric-card"><div class="metric-label">Avg Levels</div><div id="ofAvgLvl" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Pyr Levels</div><div id="ofPyrLvl" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">RT</div><div id="ofRt" style="font-size:18px;font-weight:bold">0</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Lots</div><input id="editOfLots" class="input" type="number" value="${p.lots||1}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Max Pyramid</div><input id="editOfMaxPyr" class="input" type="number" value="${p.max_pyramid_levels||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Max Average</div><input id="editOfMaxAvg" class="input" type="number" value="${p.max_average_levels||100}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Step Avg (пт)</div><input id="editOfStepAvg" class="input" type="number" value="${p.step_average||50}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Step Pyr (пт)</div><input id="editOfStepPyr" class="input" type="number" value="${p.step_pyramid||35}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Spread (пт)</div><input id="editOfSpread" class="input" type="number" value="${p.spread||50}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">SL Mode</div><select id="editOfSlMode" class="input" style="width:70px"><option value="rub" ${(p.stop_loss_mode||'rub')==='rub'?'selected':''}>₽</option><option value="pct" ${p.stop_loss_mode==='pct'?'selected':''}>%</option><option value="pts" ${p.stop_loss_mode==='pts'?'selected':''}>пт</option></select></div>
                <div class="metric-card"><div class="metric-label">SL Value</div><input id="editOfSlValue" class="input" type="number" value="${p.stop_loss_value||7000}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Min Profit/Lot</div><input id="editOfMinProfit" class="input" type="number" value="${p.min_profit_per_lot||30}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Close % All</div><select id="editOfCloseHalfPct" class="input" style="width:70px"><option value="0" ${(!p.close_half_pct)?'selected':''}>OFF</option><option value="50" ${p.close_half_pct===50?'selected':''}>50%</option><option value="40" ${p.close_half_pct===40?'selected':''}>40%</option><option value="30" ${p.close_half_pct===30?'selected':''}>30%</option><option value="20" ${p.close_half_pct===20?'selected':''}>20%</option><option value="10" ${p.close_half_pct===10?'selected':''}>10%</option></select></div>
                <div class="metric-card"><div class="metric-label">Close price All</div><input id="editOfClosePriceAll" class="input" type="number" placeholder="None" value="${p.close_price_all||''}" style="width:90px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editOfMaxHold" class="input" type="number" value="${p.max_hold_minutes||999}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">CVD EMA Fast</div><input id="editOfCvdEmaF" class="input" type="number" value="${p.cvd_ema_fast||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">CVD EMA Slow</div><input id="editOfCvdEmaS" class="input" type="number" value="${p.cvd_ema_slow||15}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">DM Lookback</div><input id="editOfDmLb" class="input" type="number" value="${p.dm_lookback||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Wall Window (с)</div><input id="editOfWallWin" class="input" type="number" value="${p.wall_window||120}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Wall Mult</div><input id="editOfWallMult" class="input" type="number" step="0.5" value="${p.wall_multiplier||3.0}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">CVD Accel Thresh</div><input id="editOfCvdAccelThresh" class="input" type="number" step="100" value="${p.cvd_accel_threshold||1000}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Agg Window</div><input id="editOfAggWindow" class="input" type="number" value="${p.agg_window||3}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Agg Ratio Thr</div><input id="editOfAggRatioThresh" class="input" type="number" step="0.1" value="${p.agg_ratio_threshold||1.0}" style="width:60px"></div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px"><label style="display:flex;align-items:center;gap:6px;cursor:pointer"><input id="editOfUseAggRatio" type="checkbox" ${(p.use_agg_ratio!==false)?'checked':''} style="width:18px;height:18px;cursor:pointer"><span style="font-size:13px;color:#66BB6A">Agg Ratio Filter</span></label></div>
                <div class="metric-card"><div class="metric-label">Confirm In</div><input id="editOfConfirm" class="input" type="number" min="1" max="3" value="${p.signal_confirm_count||1}" style="width:50px"></div>
                <div class="metric-card"><div class="metric-label">Confirm Out</div><input id="editOfConfirmOut" class="input" type="number" min="1" max="3" value="${p.signal_confirm_exit||1}" style="width:50px"></div>
                <div class="metric-card"><div class="metric-label">Timeframe</div><select id="editOfTf" class="input" style="width:70px"><option value="M1" ${p.timeframe==='M1'?'selected':''}>1 мин</option><option value="M5" ${(p.timeframe||'M5')==='M5'?'selected':''}>5 мин</option><option value="M15" ${p.timeframe==='M15'?'selected':''}>15 мин</option><option value="M30" ${p.timeframe==='M30'?'selected':''}>30 мин</option><option value="H1" ${p.timeframe==='H1'?'selected':''}>1 час</option></select></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- VWEMA Filter -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px;align-items:center">
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                        <input id="editOfUseVwema" type="checkbox" ${(p.use_vwema)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-weight:bold;color:#FF9800">VWEMA Filter</span>
                    </label>
                </div>
                <div class="metric-card"><div class="metric-label">VWEMA Fast</div><input id="editOfVwemaFast" class="input" type="number" value="${p.vwema_fast||20}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">VWEMA Slow</div><input id="editOfVwemaSlow" class="input" type="number" value="${p.vwema_slow||40}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Flat Threshold</div><input id="editOfVwemaFlat" class="input" type="number" step="0.1" value="${p.vwema_flat_th||1.0}" style="width:60px"></div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                        <input id="editOfVwemaBlock" type="checkbox" ${(p.vwema_block_counter!==false)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-size:13px">Block counter-trend</span>
                    </label>
                </div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer" title="Не усреднять против VWEMA + закрыть усреднённую позицию при развороте VWEMA">
                        <input id="editOfVwemaAvgExit" type="checkbox" ${(p.vwema_avg_exit)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-size:13px">VWEMA Avg Exit</span>
                    </label>
                </div>
                <div class="metric-card" style="min-width:120px"><div class="metric-label">VWEMA Trend</div><div id="ofVwemaTrend" style="font-size:16px;font-weight:bold;color:#9CA3AF">—</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- VAH/VAL Volume Profile Filter -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px;align-items:center">
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                        <input id="editOfUseVahVal" type="checkbox" ${(p.use_vah_val)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-weight:bold;color:#26C6DA">VAH/VAL Filter</span>
                    </label>
                </div>
                <div class="metric-card"><div class="metric-label">Value Area %</div><input id="editOfVaPct" class="input" type="number" value="${p.vah_val_pct||70}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editOfVpBin" class="input" type="number" value="${p.vah_val_bin_size||50}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">VP Mode</div><select id="editOfVpMode" class="input" style="width:100px"><option value="range" ${(p.vah_val_mode||'range')==='range'?'selected':''}>Range</option><option value="fade" ${p.vah_val_mode==='fade'?'selected':''}>Fade</option><option value="breakout" ${p.vah_val_mode==='breakout'?'selected':''}>Breakout</option></select></div>
                <div class="metric-card" style="min-width:60px"><div class="metric-label">VAH</div><div id="ofVpVah" style="font-size:16px;font-weight:bold;color:#F44336">—</div></div>
                <div class="metric-card" style="min-width:60px"><div class="metric-label">POC</div><div id="ofVpPoc" style="font-size:16px;font-weight:bold;color:#FF9800">—</div></div>
                <div class="metric-card" style="min-width:60px"><div class="metric-label">VAL</div><div id="ofVpVal" style="font-size:16px;font-weight:bold;color:#4CAF50">—</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>📋 Торговый журнал</strong>
                <button class="btn btn-danger btn-sm" onclick="ofResetStats()" style="font-size:12px">🗑 Сбросить</button>
            </div>
            <div id="ofJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="ofJournalFilter" class="input" style="width:120px" onchange="ofRenderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="ofJournalDateFrom" class="input" type="date" style="width:130px" onchange="ofRenderJournal()">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="ofJournalDateTo" class="input" type="date" style="width:130px" onchange="ofRenderJournal()">
                <button class="btn btn-secondary btn-sm" onclick="ofLoadJournal()">🔄</button>
            </div>
            <div style="max-height:350px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:8px;text-align:left">📅 Дата</th>
                            <th style="padding:8px;text-align:left">⏰ Вход</th>
                            <th style="padding:8px;text-align:left">⏰ Выход</th>
                            <th style="padding:8px;text-align:center">↔️</th>
                            <th style="padding:8px;text-align:right">💰 Вход</th>
                            <th style="padding:8px;text-align:right">💰 Выход</th>
                            <th style="padding:8px;text-align:right">Лоты</th>
                            <th style="padding:8px;text-align:right">PnL</th>
                            <th style="padding:8px;text-align:right">Кумул.</th>
                        </tr>
                    </thead>
                    <tbody id="ofJournalBody" style="background:var(--bg)"><tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Нет данных</td></tr></tbody>
                </table>
            </div>
            <div id="ofJournalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px"></div>
        </div>
    `;
    document.body.appendChild(div);

    // Start live updates
    ofUpdatePanel();
    if (window._ofVpTimer) clearInterval(window._ofVpTimer);
    window._ofVpTimer = setInterval(() => ofUpdatePanel(), 2000);

    // Load journal
    setTimeout(() => ofLoadJournal(), 300);
}

function ofUpdatePanel() {
    if (!el('ofCvd')) return;
    const s = ofRobot;
    if (!s) return;

    if (el('ofCvd')) {
        const t = s.cvdTrend;
        if (t && t.ready) {
            const dir = t.direction === 1 ? 'LONG' : t.direction === -1 ? 'SHORT' : '—';
            el('ofCvd').textContent = dir;
            el('ofCvd').style.color = t.direction === 1 ? 'var(--green)' : t.direction === -1 ? 'var(--red)' : '#CE93D8';
        } else {
            el('ofCvd').textContent = '—';
            el('ofCvd').style.color = '#CE93D8';
        }
    }
    if (el('ofDelta')) el('ofDelta').textContent = s.dmWallAgree || '—';
    if (el('ofCvdAccel') && s.cvdAccel) el('ofCvdAccel').textContent = s.cvdAccel.value !== null && s.cvdAccel.value !== undefined ? (s.cvdAccel.value > 0 ? '\u25B2' : '\u25BC') + Math.abs(s.cvdAccel.value).toFixed(0) + ' (R:' + s.cvdAccel.aggRatio.toFixed(2) + ')' : '\u2014';

    if (el('ofPrice')) el('ofPrice').textContent = s.currentPrice > 0 ? s.currentPrice.toFixed(0) : '—';

    // Sync signal toggles from params
    const p = s.params || {};
    if (el('toggleCvd')) el('toggleCvd').checked = p.use_cvd !== false;
    if (el('toggleDmWall')) el('toggleDmWall').checked = p.use_dm_wall !== false;
    if (el('toggleCvdAccel')) el('toggleCvdAccel').checked = p.use_cvd_accel !== false;
    if (el('ofDirFilter')) el('ofDirFilter').value = s.directionFilter || p.direction_filter || 'both';

    const dirText = s.direction === 'LONG' ? 'Лонг' : s.direction === 'SHORT' ? 'Шорт' : 'Флэт';
    const dirCls = s.direction === 'LONG' ? 'var(--green)' : s.direction === 'SHORT' ? 'var(--red)' : '';
    if (el('ofDir')) { el('ofDir').textContent = dirText; el('ofDir').style.color = dirCls; }

    if (el('ofLots')) el('ofLots').textContent = s.totalLots || 0;
    if (el('ofAvgPrice')) el('ofAvgPrice').textContent = s.avgPrice > 0 ? s.avgPrice.toFixed(0) : '—';

    if (el('ofPnlPerLot')) {
        const perLot = s.currentPrice > 0 && s.avgPrice > 0 && s.totalLots > 0
            ? ((s.currentPrice - s.avgPrice) * (s.direction === 'LONG' ? 1 : -1))
            : 0;
        el('ofPnlPerLot').textContent = s.totalLots > 0 ? perLot.toFixed(0) + '₽' : '—';
        el('ofPnlPerLot').style.color = perLot >= 0 ? 'var(--green)' : 'var(--red)';
    }

    if (el('ofPnlUnreal')) {
        const unreal = s.unrealizedPnL || 0;
        el('ofPnlUnreal').textContent = unreal.toFixed(0) + '₽';
        el('ofPnlUnreal').style.color = unreal >= 0 ? 'var(--green)' : 'var(--red)';
    }

    if (el('ofRealized')) {
        const real = s.realizedPnL || 0;
        el('ofRealized').textContent = real.toFixed(0) + '₽';
        el('ofRealized').style.color = real >= 0 ? 'var(--green)' : 'var(--red)';
    }

    if (el('ofSignal')) {
        el('ofSignal').textContent = s.signalType || '—';
        el('ofSignal').style.color = s.signalType ? '#CE93D8' : '';
    }

    if (el('ofHold')) el('ofHold').textContent = (s.holdMinutes || 0) + ' мин';
    if (el('ofAvgLvl')) el('ofAvgLvl').textContent = s.averageLevels || 0;
    if (el('ofPyrLvl')) el('ofPyrLvl').textContent = s.pyramidLevels || 0;
    if (el('ofRt')) el('ofRt').textContent = s.roundTrips || 0;

    // VAH/VAL state
    if (s.vp) {
        if (el('ofVpVah')) el('ofVpVah').textContent = s.vp.vah ? s.vp.vah.toFixed(0) : '—';
        if (el('ofVpPoc')) el('ofVpPoc').textContent = s.vp.poc ? s.vp.poc.toFixed(0) : '—';
        if (el('ofVpVal')) el('ofVpVal').textContent = s.vp.val ? s.vp.val.toFixed(0) : '—';
    } else {
        if (el('ofVpVah')) el('ofVpVah').textContent = '—';
        if (el('ofVpPoc')) el('ofVpPoc').textContent = '—';
        if (el('ofVpVal')) el('ofVpVal').textContent = '—';
    }

    // VWEMA state
    if (el('ofVwemaTrend')) {
        const vw = s.vwema;
        if (vw) {
            const dir = vw.direction;
            const trendText = dir > 0 ? '▲ UP' : dir < 0 ? '▼ DOWN' : '◆ FLAT';
            const color = dir > 0 ? '#4CAF50' : dir < 0 ? '#F44336' : '#9CA3AF';
            el('ofVwemaTrend').textContent = `${trendText} (${vw.spread_atr?.toFixed(1)||'?'})`;
            el('ofVwemaTrend').style.color = color;
        } else {
            el('ofVwemaTrend').textContent = 'OFF';
            el('ofVwemaTrend').style.color = '#6B7280';
        }
    }
}

async function ofRobotSaveFromPanel() {
    const body = {
        lots: parseInt(el('editOfLots')?.value) || 1,
        max_pyramid_levels: parseInt(el('editOfMaxPyr')?.value) || 5,
        max_average_levels: parseInt(el('editOfMaxAvg')?.value) || 100,
        step_average: parseInt(el('editOfStepAvg')?.value) || 50,
        step_pyramid: parseInt(el('editOfStepPyr')?.value) || 35,
        spread: parseInt(el('editOfSpread')?.value) || 50,
        stop_loss_mode: el('editOfSlMode')?.value || 'rub',
        stop_loss_value: parseFloat(el('editOfSlValue')?.value) || 7000,
        min_profit_per_lot: parseInt(el('editOfMinProfit')?.value) || 30,
        close_half_pct: parseInt(el('editOfCloseHalfPct')?.value) || 0,
        close_price_all: parseFloat(el('editOfClosePriceAll')?.value) || 0,
        max_hold_minutes: parseInt(el('editOfMaxHold')?.value) || 999,
        cvd_lookback: parseInt(el('editOfCvdLb')?.value) || 10,
        cvd_ema_fast: parseInt(el('editOfCvdEmaF')?.value) || 5,
        cvd_ema_slow: parseInt(el('editOfCvdEmaS')?.value) || 15,
        dm_lookback: parseInt(el('editOfDmLb')?.value) || 5,
        wall_window: parseFloat(el('editOfWallWin')?.value) || 120,
        wall_multiplier: parseFloat(el('editOfWallMult')?.value) || 3.0,
        cvd_accel_threshold: parseFloat(el('editOfCvdAccelThresh')?.value) || 1000,
        agg_window: parseInt(el('editOfAggWindow')?.value) || 3,
        agg_ratio_threshold: parseFloat(el('editOfAggRatioThresh')?.value) || 1.0,
        use_agg_ratio: el('editOfUseAggRatio')?.checked !== false,
        signal_confirm_count: parseInt(el('editOfConfirm')?.value) || 1,
        signal_confirm_exit: parseInt(el('editOfConfirmOut')?.value) || 1,
        timeframe: el('editOfTf')?.value || 'M5',
        use_dm_wall: el('toggleDmWall')?.checked !== false,
        use_cvd: el('toggleCvd')?.checked !== false,
        use_cvd_accel: el('toggleCvdAccel')?.checked !== false,

        use_vwema: el('editOfUseVwema')?.checked || false,
        vwema_fast: parseInt(el('editOfVwemaFast')?.value) || 20,
        vwema_slow: parseInt(el('editOfVwemaSlow')?.value) || 40,
        vwema_flat_th: parseFloat(el('editOfVwemaFlat')?.value) || 1.0,
        vwema_block_counter: el('editOfVwemaBlock')?.checked !== false,
        vwema_avg_exit: el('editOfVwemaAvgExit')?.checked || false,

        use_vah_val: el('editOfUseVahVal')?.checked || false,
        vah_val_pct: parseInt(el('editOfVaPct')?.value) || 70,
        vah_val_bin_size: parseInt(el('editOfVpBin')?.value) || 50,
        vah_val_mode: el('editOfVpMode')?.value || 'range',
    };
    try {
        const resp = await fetch(OF_ROBOT_API + '/params', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await resp.json();
        addLog(nowTime(), 'INFO', '💾 OF конфиг сохранён из панели');
    } catch(e) {
        addLog(nowTime(), 'WARN', 'OF save failed: ' + e.message);
    }
}

// === Order Flow Trade Journal ===
let _ofJournalTrades = [];

async function ofResetStats() {
    if (!confirm('Сбросить статистику? Realized PnL → 0, история сделок очищена.')) return;
    const r = await fetch(OF_ROBOT_API + '/reset-stats', {method:'POST'});
    if (r && r.ok) {
        ofLoadJournal();
    }
}

async function ofLoadJournal() {
    const info = el('ofJournalInfo');
    const body = el('ofJournalBody');
    if (!body) return;

    // Load trades from robot state file via API
    try {
        const resp = await fetch(OF_ROBOT_API + '/status', {signal: AbortSignal.timeout(2000)});
        const data = await resp.json();
        // Extract trade history from state
        _ofJournalTrades = data.tradeHistory || data.trades || [];
        if (!_ofJournalTrades.length) {
            body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Нет закрытых сделок</td></tr>';
            if (info) info.textContent = 'Нет данных';
            ofRenderJournalSummary(0, 0, 0, 0);
            return;
        }
        ofRenderJournal();
    } catch(e) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Робот недоступен</td></tr>';
        if (info) info.textContent = 'Ошибка загрузки: ' + e.message;
    }
}

function ofRenderJournal() {
    const body = el('ofJournalBody');
    if (!body) return;
    const filter = el('ofJournalFilter')?.value || 'all';
    const dateFrom = el('ofJournalDateFrom')?.value;
    const dateTo = el('ofJournalDateTo')?.value;

    let trades = [..._ofJournalTrades];

    // Apply date filter — check both entryTime and exitTime
    if (dateFrom || dateTo) {
        trades = trades.filter(t => {
            const dtEntry = t.entryTime ? new Date(t.entryTime) : null;
            const dtExit = t.exitTime ? new Date(t.exitTime) : null;
            if (!dtEntry && !dtExit) return true;
            const dEntry = dtEntry ? dtEntry.toISOString().slice(0, 10) : null;
            const dExit = dtExit ? dtExit.toISOString().slice(0, 10) : null;
            // Include if either entry or exit falls within range
            const afterStart = !dateFrom || (dEntry && dEntry >= dateFrom) || (dExit && dExit >= dateFrom);
            const beforeEnd = !dateTo || (dEntry && dEntry <= dateTo) || (dExit && dExit <= dateTo);
            return afterStart && beforeEnd;
        });
    }

    // Apply filter
    if (filter === 'LONG') trades = trades.filter(t => t.direction === 'LONG' || t.direction > 0);
    else if (filter === 'SHORT') trades = trades.filter(t => t.direction === 'SHORT' || t.direction < 0);
    else if (filter === 'win') trades = trades.filter(t => t.pnl > 0);
    else if (filter === 'loss') trades = trades.filter(t => t.pnl < 0);

    if (!trades.length) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:16px;color:#9CA3AF">Нет сделок по фильтру</td></tr>';
        return;
    }

    let cumulative = 0;
    const rows = trades.map(t => {
        cumulative += (t.pnl || 0);
        const dtOut = t.exitTime ? new Date(t.exitTime) : null;
        const dateStr = dtOut ? dtOut.toLocaleDateString('ru-RU') : '—';
        const timeIn = t.entryTime ? new Date(t.entryTime).toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit'}) : '—';
        const timeOut = dtOut ? dtOut.toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit'}) : '—';
        const dir = t.direction === 'LONG' || t.direction > 0 ? '🟢 L' : '🔴 S';
        const pnlCls = (t.pnl || 0) >= 0 ? 'green' : 'red';
        const cumCls = cumulative >= 0 ? 'green' : 'red';
        return `<tr style="border-bottom:1px solid var(--border-color)">
            <td style="padding:6px">${dateStr}</td>
            <td style="padding:6px">${timeIn}</td>
            <td style="padding:6px">${timeOut}</td>
            <td style="padding:6px;text-align:center">${dir}</td>
            <td style="padding:6px;text-align:right">${(t.entryPrice||0).toFixed(0)}</td>
            <td style="padding:6px;text-align:right">${(t.exitPrice||0).toFixed(0)}</td>
            <td style="padding:6px;text-align:right">${t.lots||1}</td>
            <td style="padding:6px;text-align:right" class="${pnlCls}">${(t.pnl||0) >= 0 ? '+' : ''}${(t.pnl||0).toFixed(0)}₽</td>
            <td style="padding:6px;text-align:right" class="${cumCls}">${cumulative >= 0 ? '+' : ''}${cumulative.toFixed(0)}₽</td>
        </tr>`;
    });
    body.innerHTML = rows.join('');

    // Summary
    const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
    const wins = trades.filter(t => (t.pnl || 0) > 0).length;
    const losses = trades.filter(t => (t.pnl || 0) < 0).length;
    ofRenderJournalSummary(trades.length, wins, losses, totalPnl);
}

function ofRenderJournalSummary(total, wins, losses, totalPnl) {
    const el2 = el('ofJournalSummary');
    if (!el2) return;
    const wr = total > 0 ? ((wins / total) * 100).toFixed(0) + '%' : '—';
    const winPnl = _ofJournalTrades.filter(t => (t.pnl||0) > 0).reduce((s,t) => s+(t.pnl||0), 0);
    const lossPnl = _ofJournalTrades.filter(t => (t.pnl||0) < 0).reduce((s,t) => s+(t.pnl||0), 0);
    const avgWin = wins > 0 ? (winPnl / wins).toFixed(0) : '—';
    const avgLoss = losses > 0 ? (lossPnl / losses).toFixed(0) : '—';
    const cls = totalPnl >= 0 ? 'green' : 'red';
    el2.innerHTML = `
        <div class="metric-card"><div class="metric-label">Всего сделок</div><div style="font-size:16px;font-weight:bold">${total}</div></div>
        <div class="metric-card"><div class="metric-label">Win Rate</div><div style="font-size:16px;font-weight:bold">${wr}</div></div>
        <div class="metric-card"><div class="metric-label">Wins / Loss</div><div style="font-size:16px;font-weight:bold"><span class="green">${wins}</span> / <span class="red">${losses}</span></div></div>
        <div class="metric-card"><div class="metric-label">Avg Win</div><div style="font-size:16px;font-weight:bold" class="green">${avgWin !== '—' ? '+' + avgWin + '₽' : '—'}</div></div>
        <div class="metric-card"><div class="metric-label">Avg Loss</div><div style="font-size:16px;font-weight:bold" class="red">${avgLoss !== '—' ? avgLoss + '₽' : '—'}</div></div>
        <div class="metric-card"><div class="metric-label">Итог PnL</div><div style="font-size:16px;font-weight:bold" class="${cls}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)}₽</div></div>
        <div class="metric-card" style="border:1px solid var(--border-color)"><div class="metric-label">&nbsp;</div><button class="btn btn-danger btn-sm" onclick="ofResetStats()" style="font-size:12px">🗑 Сбросить</button></div>
    `;
}

// === P3: Connection Error Banner ===
function showConnError(msg) {
    var b = el('connErrorBanner'); var m = el('connErrorMsg');
    if (b) b.classList.add('visible');
    if (m) m.textContent = msg || 'Connection lost';
}
function hideConnError() {
    var b = el('connErrorBanner'); if (b) b.classList.remove('visible');
}
setInterval(function() {
    if (typeof window._signalRConnected !== 'undefined') {
        if (window._signalRConnected) hideConnError(); else showConnError('SignalR disconnected - REST polling fallback');
    }
}, 5000);

// === ORDER FLOW MX ROBOT (порт 5081) ===
const OF_MX_ROBOT_API = 'http://' + window.location.hostname + ':5081';
let ofMxRobot = null;
let _ofMxJournalTrades = [];

let _ofMxPendingParams = null;
let _ofMxWasOnline = false;

async function ofMxRobotPoll() {
    try {
        const resp = await fetch(OF_MX_ROBOT_API + '/status', {signal: AbortSignal.timeout(2000)});
        ofMxRobot = await resp.json();
        // Robot just came online → sync pending params
        if (!_ofMxWasOnline) {
            _ofMxWasOnline = true;
            const saved = localStorage.getItem('ofMxSavedParams');
            if (saved) {
                try {
                    const params = JSON.parse(saved);
                    await fetch(OF_MX_ROBOT_API + '/params', {
                        method: 'POST',
                        headers: {'Content-Type':'application/json'},
                        body: JSON.stringify(params)
                    });
                    addLog(nowTime(), 'INFO', '📊 OF-MX: параметры синхронизированы из localStorage');
                    localStorage.removeItem('ofMxSavedParams');
                } catch(e) {}
            }
        }
    } catch(e) {
        ofMxRobot = null;
        _ofMxWasOnline = false;
    }
}

async function ofMxSetDirection(value) {
    try {
        await fetch(OF_MX_ROBOT_API + '/params', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({direction_filter: value})
        });
        await ofMxRobotPoll();
    } catch(e) {}
}

async function onOfMxRobotAccountChange(val) {
    localStorage.setItem('ofMxRobotAccount', val);
}

function onOfMxRobotInstrumentChange(val) {
    localStorage.setItem('ofMxRobotInstrument', val);
    renderRobots();
}

async function ofMxRobotApi(action) {
    try {
        if (action === 'start' || action === 'stop') {
            const acctVal = localStorage.getItem('ofMxRobotAccount') || '1225953';
            if (acctVal) {
                const acctResp = await fetch(OF_MX_ROBOT_API + '/account', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({account: acctVal})
                });
                const acctData = await acctResp.json();
                if (acctData.ok) {
                    addLog(nowTime(), 'INFO', '📊 OF-MX счёт: ' + acctData.account + ' (' + acctData.id + ')');
                }
            }
        }
        const resp = await fetch(OF_MX_ROBOT_API + '/' + action, {method: 'POST'});
        const data = await resp.json();
        addLog(nowTime(), 'INFO', 'OF-MX ' + action + ': ' + JSON.stringify(data));
        setTimeout(async () => { await ofMxRobotPoll(); renderRobots(); }, 500);
    } catch(e) {
        addLog(nowTime(), 'ERROR', 'OF-MX ' + action + ' failed: ' + e.message);
    }
}

async function toggleOfMxSignal(param, value) {
    try {
        const resp = await fetch(OF_MX_ROBOT_API + '/params', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({[param]: value})
        });
        const data = await resp.json();
        addLog(nowTime(), 'INFO', `OF-MX signal ${param}=${value}`);
    } catch(e) {
        addLog(nowTime(), 'ERROR', 'OF-MX toggle failed: ' + e.message);
    }
}

// Auto-poll MX
(function() {
    setInterval(async () => { await ofMxRobotPoll(); renderRobots(); }, 2000);
})();

// === ORDER FLOW MX EDIT PANEL ===
function ofMxRobotEditPanel() {
    const existing = el('ofMxRobotEditPanel');
    if (existing) { existing.remove(); return; }

    const s = ofMxRobot || {};
    const p = s.params || JSON.parse(localStorage.getItem('ofMxSavedParams') || '{}');
    // Fallback defaults for MXU6 if no params anywhere
    if (!p.step_average) p.step_average = 150;
    if (!p.step_pyramid) p.step_pyramid = 80;
    if (!p.spread) p.spread = 120;
    if (!p.stop_loss_value) p.stop_loss_value = 10000;
    if (!p.min_profit_per_lot) p.min_profit_per_lot = 100;
    if (!p.max_pyramid_levels) p.max_pyramid_levels = 3;
    if (!p.max_average_levels) p.max_average_levels = 50;

    const div = document.createElement('div');
    div.id = 'ofMxRobotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            📊 MXU6 Order Flow (PYTHON)
            <button class="btn btn-primary btn-sm" onclick="ofMxRobotSaveFromPanel()">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="if(window._ofMxVpTimer){clearInterval(window._ofMxVpTimer);window._ofMxVpTimer=null;}el('ofMxRobotEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:12px">
            <!-- OF индикаторы -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px">
                <div class="metric-card" style="background:#1a2332;border:1px solid #007ACC;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleMxCvd" type="checkbox" checked style="width:14px;height:14px;cursor:pointer" onchange="toggleOfMxSignal('use_cvd', this.checked)"><div class="metric-label" style="color:#CE93D8">CVD Trend</div></div><div id="ofMxCvd" style="font-size:18px;font-weight:bold;color:#CE93D8">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #007ACC;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleMxDmWall" type="checkbox" style="width:14px;height:14px;cursor:pointer" onchange="toggleOfMxSignal('use_dm_wall', this.checked)"><div class="metric-label" style="color:#FF7043">dm_wall</div></div><div id="ofMxDelta" style="font-size:18px;font-weight:bold;color:#FF7043">—</div></div>
                <div class="metric-card" style="background:#1a2332;border:1px solid #007ACC;display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleMxCvdAccel" type="checkbox" checked style="width:14px;height:14px;cursor:pointer" onchange="toggleOfMxSignal('use_cvd_accel', this.checked)"><div class="metric-label" style="color:#66BB6A">CVD Accel</div></div><div id="ofMxCvdAccel" style="font-size:18px;font-weight:bold;color:#66BB6A">—</div></div>

                <div class="metric-card" style="display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><input id="toggleMxVp" type="checkbox" style="width:14px;height:14px;cursor:pointer" onchange="toggleOfMxSignal('use_vah_val', this.checked)"><div class="metric-label" style="color:#42A5F5">VAH</div></div><div id="ofMxVah" style="font-size:18px;font-weight:bold;color:#42A5F5">—</div></div>
                <div class="metric-card" style="display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><div class="metric-label" style="color:#FFB74D">POC</div></div><div id="ofMxPoc" style="font-size:18px;font-weight:bold;color:#FFB74D">—</div></div>
                <div class="metric-card" style="display:flex;flex-direction:column;align-items:center;gap:2px"><div style="display:flex;align-items:center;gap:4px"><div class="metric-label" style="color:#42A5F5">VAL</div></div><div id="ofMxVal" style="font-size:18px;font-weight:bold;color:#42A5F5">—</div></div>

                <div class="metric-card" style="background:#1a2332;border:1px solid #607D8B;display:flex;flex-direction:column;align-items:center;gap:2px"><div class="metric-label" style="color:#B0BEC5">Фильтр</div><select id="ofMxDirFilter" class="input" style="width:80px;font-size:13px;font-weight:bold;background:transparent;color:#fff;border:1px solid #607D8B;text-align:center;cursor:pointer" onchange="ofMxSetDirection(this.value)"><option value="both" style="color:#000">ОБА</option><option value="long" style="color:#000">LONG</option><option value="short" style="color:#000">SHORT</option></select></div>

                <div class="metric-card"><div class="metric-label">Цена</div><div id="ofMxPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Позиция</div><div id="ofMxDir" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Лоты</div><div id="ofMxLots" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Ср. цена</div><div id="ofMxAvgPrice" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL/лот</div><div id="ofMxPnlPerLot" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL нереал.</div><div id="ofMxPnlUnreal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Realized</div><div id="ofMxRealized" style="font-size:18px;font-weight:bold">0₽</div></div>
                <div class="metric-card"><div class="metric-label">Сигнал</div><div id="ofMxSignal" style="font-size:16px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Hold</div><div id="ofMxHold" style="font-size:18px;font-weight:bold">0 мин</div></div>
                <div class="metric-card"><div class="metric-label">Avg Levels</div><div id="ofMxAvgLvl" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">Pyr Levels</div><div id="ofMxPyrLvl" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">RT</div><div id="ofMxRt" style="font-size:18px;font-weight:bold">0</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Lots</div><input id="editOfMxLots" class="input" type="number" value="${p.lots||1}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Max Pyramid</div><input id="editOfMxMaxPyr" class="input" type="number" value="${p.max_pyramid_levels||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Max Average</div><input id="editOfMxMaxAvg" class="input" type="number" value="${p.max_average_levels||100}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Step Avg (пт)</div><input id="editOfMxStepAvg" class="input" type="number" value="${p.step_average||50}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Step Pyr (пт)</div><input id="editOfMxStepPyr" class="input" type="number" value="${p.step_pyramid||35}" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Spread (пт)</div><input id="editOfMxSpread" class="input" type="number" value="${p.spread||50}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">SL Mode</div><select id="editOfMxSlMode" class="input" style="width:70px"><option value="rub" ${(p.stop_loss_mode||'rub')==='rub'?'selected':''}>₽</option><option value="pct" ${p.stop_loss_mode==='pct'?'selected':''}>%</option><option value="pts" ${p.stop_loss_mode==='pts'?'selected':''}>пт</option></select></div>
                <div class="metric-card"><div class="metric-label">SL Value</div><input id="editOfMxSlValue" class="input" type="number" value="${p.stop_loss_value||7000}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Min Profit/Lot</div><input id="editOfMxMinProfit" class="input" type="number" value="${p.min_profit_per_lot||30}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Close % All</div><select id="editOfMxCloseHalfPct" class="input" style="width:70px"><option value="0" ${(!p.close_half_pct)?'selected':''}>OFF</option><option value="50" ${p.close_half_pct===50?'selected':''}>50%</option><option value="40" ${p.close_half_pct===40?'selected':''}>40%</option><option value="30" ${p.close_half_pct===30?'selected':''}>30%</option><option value="20" ${p.close_half_pct===20?'selected':''}>20%</option><option value="10" ${p.close_half_pct===10?'selected':''}>10%</option></select></div>
                <div class="metric-card"><div class="metric-label">Close price All</div><input id="editOfMxClosePriceAll" class="input" type="number" placeholder="None" value="${p.close_price_all||''}" style="width:90px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editOfMxMaxHold" class="input" type="number" value="${p.max_hold_minutes||999}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">CVD EMA Fast</div><input id="editOfMxCvdEmaF" class="input" type="number" value="${p.cvd_ema_fast||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">CVD EMA Slow</div><input id="editOfMxCvdEmaS" class="input" type="number" value="${p.cvd_ema_slow||15}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">DM Lookback</div><input id="editOfMxDmLb" class="input" type="number" value="${p.dm_lookback||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Wall Window (с)</div><input id="editOfMxWallWin" class="input" type="number" value="${p.wall_window||120}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Wall Mult</div><input id="editOfMxWallMult" class="input" type="number" step="0.5" value="${p.wall_multiplier||3.0}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">CVD Accel Thresh</div><input id="editOfMxCvdAccelThresh" class="input" type="number" step="100" value="${p.cvd_accel_threshold||1000}" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Agg Window</div><input id="editOfMxAggWindow" class="input" type="number" value="${p.agg_window||3}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Agg Ratio Thr</div><input id="editOfMxAggRatioThresh" class="input" type="number" step="0.1" value="${p.agg_ratio_threshold||1.0}" style="width:60px"></div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px"><label style="display:flex;align-items:center;gap:6px;cursor:pointer"><input id="editOfMxUseAggRatio" type="checkbox" ${(p.use_agg_ratio!==false)?'checked':''} style="width:18px;height:18px;cursor:pointer"><span style="font-size:13px;color:#66BB6A">Agg Ratio Filter</span></label></div>
                <div class="metric-card"><div class="metric-label">Confirm In</div><input id="editOfMxConfirm" class="input" type="number" min="1" max="3" value="${p.signal_confirm_count||1}" style="width:50px"></div>
                <div class="metric-card"><div class="metric-label">Confirm Out</div><input id="editOfMxConfirmOut" class="input" type="number" min="1" max="3" value="${p.signal_confirm_exit||1}" style="width:50px"></div>
                <div class="metric-card"><div class="metric-label">Timeframe</div><select id="editOfMxTf" class="input" style="width:70px"><option value="M1" ${p.timeframe==='M1'?'selected':''}>1 мин</option><option value="M5" ${(p.timeframe||'M5')==='M5'?'selected':''}>5 мин</option><option value="M15" ${p.timeframe==='M15'?'selected':''}>15 мин</option><option value="M30" ${p.timeframe==='M30'?'selected':''}>30 мин</option><option value="H1" ${p.timeframe==='H1'?'selected':''}>1 час</option></select></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- VWEMA Filter -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px;align-items:center">
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                        <input id="editOfMxUseVwema" type="checkbox" ${(p.use_vwema)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-weight:bold;color:#FF9800">VWEMA Filter</span>
                    </label>
                </div>
                <div class="metric-card"><div class="metric-label">VWEMA Fast</div><input id="editOfMxVwemaFast" class="input" type="number" value="${p.vwema_fast||20}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">VWEMA Slow</div><input id="editOfMxVwemaSlow" class="input" type="number" value="${p.vwema_slow||40}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">Flat Threshold</div><input id="editOfMxVwemaFlat" class="input" type="number" step="0.1" value="${p.vwema_flat_th||1.0}" style="width:60px"></div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                        <input id="editOfMxVwemaBlock" type="checkbox" ${(p.vwema_block_counter!==false)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-size:13px">Block counter-trend</span>
                    </label>
                </div>
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer" title="Не усреднять против VWEMA + закрыть усреднённую позицию при развороте VWEMA">
                        <input id="editOfMxVwemaAvgExit" type="checkbox" ${(p.vwema_avg_exit)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-size:13px">VWEMA Avg Exit</span>
                    </label>
                </div>
                <div class="metric-card" style="min-width:120px"><div class="metric-label">VWEMA Trend</div><div id="ofMxVwemaTrend" style="font-size:16px;font-weight:bold;color:#9CA3AF">—</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- VAH/VAL Volume Profile Filter -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px;align-items:center">
                <div class="metric-card" style="display:flex;align-items:center;gap:8px">
                    <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
                        <input id="editOfMxUseVahVal" type="checkbox" ${(p.use_vah_val)?'checked':''} style="width:18px;height:18px;cursor:pointer">
                        <span style="font-weight:bold;color:#42A5F5">VAH/VAL Filter</span>
                    </label>
                </div>
                <div class="metric-card"><div class="metric-label">Value Area %</div><input id="editOfMxVahValPct" class="input" type="number" min="50" max="90" value="${p.vah_val_pct||70}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editOfMxVahValBin" class="input" type="number" min="1" max="500" value="${p.vah_val_bin_size||5}" style="width:60px"></div>
                <div class="metric-card"><div class="metric-label">VP Mode</div><select id="editOfMxVahValMode" class="input" style="width:90px;font-size:12px;background:transparent;color:#fff;border:1px solid #2D4A6D"><option value="fade" ${(p.vah_val_mode||'fade')==='fade'?'selected':''}>Fade</option><option value="breakout" ${(p.vah_val_mode||'fade')==='breakout'?'selected':''}>Breakout</option></select></div>
                <div class="metric-card" style="min-width:80px"><div class="metric-label">VAH</div><div id="ofMxVpVah" style="font-size:16px;font-weight:bold;color:#42A5F5">—</div></div>
                <div class="metric-card" style="min-width:80px"><div class="metric-label">POC</div><div id="ofMxVpPoc" style="font-size:16px;font-weight:bold;color:#FFB74D">—</div></div>
                <div class="metric-card" style="min-width:80px"><div class="metric-label">VAL</div><div id="ofMxVpVal" style="font-size:16px;font-weight:bold;color:#42A5F5">—</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>📋 Торговый журнал</strong>
            </div>
            <div id="ofMxJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="ofMxJournalFilter" class="input" style="width:120px" onchange="ofMxRenderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="ofMxJournalDateFrom" class="input" type="date" style="width:130px" onchange="ofMxRenderJournal()">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="ofMxJournalDateTo" class="input" type="date" style="width:130px" onchange="ofMxRenderJournal()">
                <button class="btn btn-secondary btn-sm" onclick="ofMxLoadJournal()">🔄 Обновить</button>
            </div>
            <table class="data-table" style="font-size:13px">
                <thead><tr><th>Дата</th><th>Вход</th><th>Выход</th><th>Напр.</th><th>Цена вх.</th><th>Цена вых.</th><th>Лоты</th><th>PnL</th><th>Σ</th></tr></thead>
                <tbody id="ofMxJournalBody"></tbody>
            </table>
        </div>
    `;
    document.body.appendChild(div);

    // Initialize direction filter dropdown
    if (p.direction_filter) {
        const sel = el('ofMxDirFilter');
        if (sel) sel.value = p.direction_filter;
    }
    // Initialize signal toggles from params
    const tCvd = el('toggleMxCvd'); if (tCvd) tCvd.checked = p.use_cvd !== false;
    const tDm = el('toggleMxDmWall'); if (tDm) tDm.checked = p.use_dm_wall === true;
    const tAcc = el('toggleMxCvdAccel'); if (tAcc) tAcc.checked = p.use_cvd_accel !== false;

    ofMxLoadJournal();
    ofMxUpdateLive();
    if (window._ofMxVpTimer) clearInterval(window._ofMxVpTimer);
    window._ofMxVpTimer = setInterval(ofMxUpdateLive, 1000);
}

function ofMxUpdateLive() {
    const s = ofMxRobot;
    if (!s) return;
    const setText = (id, val) => { const e = el(id); if (e) e.textContent = val; };
    const setColor = (id, val) => {
        const e = el(id); if (!e) return;
        e.textContent = val >= 0 ? '+' + val.toFixed(0) : val.toFixed(0);
        e.className = val >= 0 ? 'green' : 'red';
    };
    const price = s.currentPrice || 0;
    setText('ofMxPrice', price > 0 ? price.toFixed(0) : '—');
    const dirText = s.direction === 'LONG' ? '🟢 LONG' : s.direction === 'SHORT' ? '🔴 SHORT' : 'FLAT';
    setText('ofMxDir', dirText);
    setText('ofMxLots', s.totalLots || 0);
    setText('ofMxAvgPrice', s.avgPrice > 0 ? s.avgPrice.toFixed(0) : '—');
    const pnlPerLot = price > 0 && s.avgPrice > 0 ? (price - s.avgPrice) * (s.dir || 0) : 0;
    setText('ofMxPnlPerLot', pnlPerLot !== 0 ? (pnlPerLot > 0 ? '+' : '') + pnlPerLot.toFixed(0) + ' пт' : '—');
    setColor('ofMxPnlUnreal', s.unrealizedPnL || 0);
    setText('ofMxRealized', (s.realizedPnL || 0).toFixed(0) + '₽');
    setText('ofMxSignal', s.signalType || '—');
    setText('ofMxHold', s.holdMinutes ? s.holdMinutes + ' мин' : '0 мин');
    setText('ofMxAvgLvl', s.averageLevels || 0);
    setText('ofMxPyrLvl', s.pyramidLevels || 0);
    setText('ofMxRt', s.roundTrips || 0);
    // VAH/POC/VAL (Volume Profile)
    if (s.vp) {
        setText('ofMxVah', s.vp.vah ? s.vp.vah.toFixed(0) : '—');
        setText('ofMxPoc', s.vp.poc ? s.vp.poc.toFixed(0) : '—');
        setText('ofMxVal', s.vp.val ? s.vp.val.toFixed(0) : '—');
        setText('ofMxVpVah', s.vp.vah ? s.vp.vah.toFixed(0) : '—');
        setText('ofMxVpPoc', s.vp.poc ? s.vp.poc.toFixed(0) : '—');
        setText('ofMxVpVal', s.vp.val ? s.vp.val.toFixed(0) : '—');
    } else {
        setText('ofMxVah', '—');
        setText('ofMxPoc', '—');
        setText('ofMxVal', '—');
        setText('ofMxVpVah', '—');
        setText('ofMxVpPoc', '—');
        setText('ofMxVpVal', '—');
    }
    const p = s.params || {};
    const vpToggle = el('toggleMxVp');
    if (vpToggle) vpToggle.checked = (p.use_vah_val === true);
    if (s.cvdTrend) {
        const cvdDir = s.cvdTrend.direction > 0 ? '↑' : s.cvdTrend.direction < 0 ? '↓' : '→';
        const cvdVal = s.cvdTrend.emaFast !== null ? cvdDir + ' ' + s.cvdTrend.emaFast.toFixed(0) : '—';
        setText('ofMxCvd', cvdVal);
    }
    if (s.cvdAccel) {
        const accelVal = s.cvdAccel.value !== null ? s.cvdAccel.value.toFixed(0) + ' (' + (s.cvdAccel.aggRatio || '—') + ')' : '—';
        setText('ofMxCvdAccel', accelVal);
    }
    if (s.vwema) {
        const vwDir = s.vwema.direction > 0 ? '↑ UP' : s.vwema.direction < 0 ? '↓ DOWN' : '→ FLAT';
        setText('ofMxVwemaTrend', vwDir + ' (f=' + (s.vwema.ema_f ? s.vwema.ema_f.toFixed(0) : '—') + ' s=' + (s.vwema.ema_s ? s.vwema.ema_s.toFixed(0) : '—') + ')');
    }
}

async function ofMxRobotSaveFromPanel() {
    const body = {
        lots: parseInt(el('editOfMxLots')?.value) || 1,
        max_pyramid_levels: parseInt(el('editOfMxMaxPyr')?.value) || 5,
        max_average_levels: parseInt(el('editOfMxMaxAvg')?.value) || 100,
        step_average: parseInt(el('editOfMxStepAvg')?.value) || 50,
        step_pyramid: parseInt(el('editOfMxStepPyr')?.value) || 35,
        spread: parseInt(el('editOfMxSpread')?.value) || 50,
        stop_loss_mode: el('editOfMxSlMode')?.value || 'rub',
        stop_loss_value: parseFloat(el('editOfMxSlValue')?.value) || 7000,
        min_profit_per_lot: parseInt(el('editOfMxMinProfit')?.value) || 30,
        close_half_pct: parseInt(el('editOfMxCloseHalfPct')?.value) || 0,
        close_price_all: parseFloat(el('editOfMxClosePriceAll')?.value) || 0,
        max_hold_minutes: parseInt(el('editOfMxMaxHold')?.value) || 999,
        cvd_lookback: parseInt(el('editOfMxCvdLb')?.value) || 10,
        cvd_ema_fast: parseInt(el('editOfMxCvdEmaF')?.value) || 5,
        cvd_ema_slow: parseInt(el('editOfMxCvdEmaS')?.value) || 15,
        dm_lookback: parseInt(el('editOfMxDmLb')?.value) || 5,
        wall_window: parseFloat(el('editOfMxWallWin')?.value) || 120,
        wall_multiplier: parseFloat(el('editOfMxWallMult')?.value) || 3.0,
        cvd_accel_threshold: parseFloat(el('editOfMxCvdAccelThresh')?.value) || 1000,
        agg_window: parseInt(el('editOfMxAggWindow')?.value) || 3,
        agg_ratio_threshold: parseFloat(el('editOfMxAggRatioThresh')?.value) || 1.0,
        use_agg_ratio: el('editOfMxUseAggRatio')?.checked !== false,
        signal_confirm_count: parseInt(el('editOfMxConfirm')?.value) || 1,
        signal_confirm_exit: parseInt(el('editOfMxConfirmOut')?.value) || 1,
        timeframe: el('editOfMxTf')?.value || 'M5',
        use_dm_wall: el('toggleMxDmWall')?.checked !== false,
        use_cvd: el('toggleMxCvd')?.checked !== false,
        use_cvd_accel: el('toggleMxCvdAccel')?.checked !== false,
        use_vwema: el('editOfMxUseVwema')?.checked || false,
        vwema_fast: parseInt(el('editOfMxVwemaFast')?.value) || 20,
        vwema_slow: parseInt(el('editOfMxVwemaSlow')?.value) || 40,
        vwema_flat_th: parseFloat(el('editOfMxVwemaFlat')?.value) || 1.0,
        vwema_block_counter: el('editOfMxVwemaBlock')?.checked !== false,
        vwema_avg_exit: el('editOfMxVwemaAvgExit')?.checked || false,
        vah_val_pct: parseInt(el('editOfMxVahValPct')?.value) || 70,
        vah_val_bin_size: parseInt(el('editOfMxVahValBin')?.value) || 5,
        vah_val_mode: el('editOfMxVahValMode')?.value || 'fade',
        use_vah_val: el('editOfMxUseVahVal')?.checked || el('toggleMxVp')?.checked || false,
    };
    // Always save to localStorage first
    localStorage.setItem('ofMxSavedParams', JSON.stringify(body));
    try {
        const resp = await fetch(OF_MX_ROBOT_API + '/params', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await resp.json();
        localStorage.removeItem('ofMxSavedParams');
        addLog(nowTime(), 'INFO', '💾 OF-MX конфиг сохранён');
    } catch(e) {
        addLog(nowTime(), 'INFO', '💾 OF-MX конфиг сохранён локально (робот offline — применится при старте)');
    }
}

// === Order Flow MX Trade Journal ===
async function ofMxResetStats() {
    if (!confirm('Сбросить статистику MX? Realized PnL → 0, история сделок очищена.')) return;
    const r = await fetch(OF_MX_ROBOT_API + '/reset-stats', {method:'POST'});
    if (r && r.ok) {
        ofMxLoadJournal();
    }
}

async function ofMxLoadJournal() {
    const body = el('ofMxJournalBody');
    if (!body) return;
    try {
        const resp = await fetch(OF_MX_ROBOT_API + '/status', {signal: AbortSignal.timeout(2000)});
        const data = await resp.json();
        _ofMxJournalTrades = data.tradeHistory || data.trades || [];
        if (!_ofMxJournalTrades.length) {
            body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Нет закрытых сделок</td></tr>';
            ofMxRenderJournalSummary(0, 0, 0, 0);
            return;
        }
        ofMxRenderJournal();
    } catch(e) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Робот недоступен</td></tr>';
    }
}

function ofMxRenderJournal() {
    const body = el('ofMxJournalBody');
    if (!body) return;
    const filter = el('ofMxJournalFilter')?.value || 'all';
    const dateFrom = el('ofMxJournalDateFrom')?.value;
    const dateTo = el('ofMxJournalDateTo')?.value;
    let trades = [..._ofMxJournalTrades];
    if (dateFrom || dateTo) {
        trades = trades.filter(t => {
            const dEntry = t.entryTime ? t.entryTime.slice(0,10) : null;
            const dExit = t.exitTime ? t.exitTime.slice(0,10) : null;
            const afterStart = !dateFrom || (dEntry && dEntry >= dateFrom) || (dExit && dExit >= dateFrom);
            const beforeEnd = !dateTo || (dEntry && dEntry <= dateTo) || (dExit && dExit <= dateTo);
            return afterStart && beforeEnd;
        });
    }
    if (filter === 'LONG') trades = trades.filter(t => t.direction === 'LONG' || t.direction > 0);
    else if (filter === 'SHORT') trades = trades.filter(t => t.direction === 'SHORT' || t.direction < 0);
    else if (filter === 'win') trades = trades.filter(t => t.pnl > 0);
    else if (filter === 'loss') trades = trades.filter(t => t.pnl < 0);
    if (!trades.length) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:16px;color:#9CA3AF">Нет сделок по фильтру</td></tr>';
        return;
    }
    let cumulative = 0;
    const rows = trades.map(t => {
        cumulative += (t.pnl || 0);
        const dtOut = t.exitTime ? new Date(t.exitTime) : null;
        const dateStr = dtOut ? dtOut.toLocaleDateString('ru-RU') : '—';
        const timeIn = t.entryTime ? new Date(t.entryTime).toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit'}) : '—';
        const timeOut = dtOut ? dtOut.toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit'}) : '—';
        const dir = t.direction === 'LONG' || t.direction > 0 ? '🟢 L' : '🔴 S';
        const pnlCls = (t.pnl || 0) >= 0 ? 'green' : 'red';
        const cumCls = cumulative >= 0 ? 'green' : 'red';
        return '<tr style="border-bottom:1px solid var(--border-color)">' +
            '<td style="padding:6px">' + dateStr + '</td>' +
            '<td style="padding:6px">' + timeIn + '</td>' +
            '<td style="padding:6px">' + timeOut + '</td>' +
            '<td style="padding:6px;text-align:center">' + dir + '</td>' +
            '<td style="padding:6px;text-align:right">' + (t.entryPrice||0).toFixed(0) + '</td>' +
            '<td style="padding:6px;text-align:right">' + (t.exitPrice||0).toFixed(0) + '</td>' +
            '<td style="padding:6px;text-align:right">' + (t.lots||1) + '</td>' +
            '<td style="padding:6px;text-align:right" class="' + pnlCls + '">' + ((t.pnl||0) >= 0 ? '+' : '') + (t.pnl||0).toFixed(0) + '₽</td>' +
            '<td style="padding:6px;text-align:right" class="' + cumCls + '">' + (cumulative >= 0 ? '+' : '') + cumulative.toFixed(0) + '₽</td>' +
        '</tr>';
    });
    body.innerHTML = rows.join('');
    const totalPnl = trades.reduce((s, t) => s + (t.pnl || 0), 0);
    const wins = trades.filter(t => (t.pnl || 0) > 0).length;
    const losses = trades.filter(t => (t.pnl || 0) < 0).length;
    ofMxRenderJournalSummary(trades.length, wins, losses, totalPnl);
}

function ofMxRenderJournalSummary(total, wins, losses, totalPnl) {
    const el2 = el('ofMxJournalSummary');
    if (!el2) return;
    const wr = total > 0 ? ((wins / total) * 100).toFixed(0) + '%' : '—';
    const winPnl = _ofMxJournalTrades.filter(t => (t.pnl||0) > 0).reduce((s,t) => s+(t.pnl||0), 0);
    const lossPnl = _ofMxJournalTrades.filter(t => (t.pnl||0) < 0).reduce((s,t) => s+(t.pnl||0), 0);
    const avgWin = wins > 0 ? (winPnl / wins).toFixed(0) : '—';
    const avgLoss = losses > 0 ? (lossPnl / losses).toFixed(0) : '—';
    const cls = totalPnl >= 0 ? 'green' : 'red';
    el2.innerHTML = '' +
        '<div class="metric-card"><div class="metric-label">Всего сделок</div><div style="font-size:16px;font-weight:bold">' + total + '</div></div>' +
        '<div class="metric-card"><div class="metric-label">Win Rate</div><div style="font-size:16px;font-weight:bold">' + wr + '</div></div>' +
        '<div class="metric-card"><div class="metric-label">Wins / Loss</div><div style="font-size:16px;font-weight:bold"><span class="green">' + wins + '</span> / <span class="red">' + losses + '</span></div></div>' +
        '<div class="metric-card"><div class="metric-label">Avg Win</div><div style="font-size:16px;font-weight:bold" class="green">' + (avgWin !== '—' ? '+' + avgWin + '₽' : '—') + '</div></div>' +
        '<div class="metric-card"><div class="metric-label">Avg Loss</div><div style="font-size:16px;font-weight:bold" class="red">' + (avgLoss !== '—' ? avgLoss + '₽' : '—') + '</div></div>' +
        '<div class="metric-card"><div class="metric-label">Итог PnL</div><div style="font-size:16px;font-weight:bold" class="' + cls + '">' + (totalPnl >= 0 ? '+' : '') + totalPnl.toFixed(0) + '₽</div></div>' +
        '<div class="metric-card" style="border:1px solid var(--border-color)"><div class="metric-label">&nbsp;</div><button class="btn btn-danger btn-sm" onclick="ofMxResetStats()" style="font-size:12px">🗑 Сбросить</button></div>';
}
