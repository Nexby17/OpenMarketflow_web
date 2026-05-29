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
const MAX_LOG = 200;

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
        // Отключено — используем REST polling для стакана
        // renderOrderBook(snapshot);
    });

    connection.on('OnQuoteUpdate', quote => {
        // Отключено — используем REST polling для котировок
        // updateQuote(quote);
    });

    connection.on('OnError', msg => {
        addLog(new Date().toLocaleTimeString(), 'ERROR', msg);
    });

    connection.onreconnecting(() => {
        el('statusIndicator').className = 'status-dot yellow';
        el('statusText').textContent = 'Переподключение...';
    });

    connection.onreconnected(() => {
        el('statusIndicator').className = 'status-dot green';
        el('statusText').textContent = 'Подключён';
        fetchStatus();
    });

    connection.onclose(() => {
        el('statusIndicator').className = 'status-dot red';
        el('statusText').textContent = 'Отключён';
    });

    connection.start()
        .then(() => {
            el('statusIndicator').className = 'status-dot green';
            el('statusText').textContent = 'Подключён к серверу';
            addLog(nowTime(), 'INFO', '✅ SignalR подключён');
            fetchStatus();
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
    console.log("[updateChartLines] Called. positionSeries:", !!positionSeries, "buyOrdersSeries:", !!buyOrdersSeries, "sellOrdersSeries:", !!sellOrdersSeries);
    
    // Обновить линии позиций
    fetch('/api/positions').then(r => r.json()).then(positions => {
        window._lastPositions = positions;
        console.log("[updateChartLines] Positions:", positions);
        if (!positionSeries || !positions || positions.length === 0) {
            if (positionSeries) positionSeries.setData([]);
            return;
        }
        const now = Math.floor(Date.now() / 1000);
        const timeStart = Math.floor((now - 7200) / 60) * 60;
        const timeEnd = Math.floor((now + 3600) / 60) * 60;
        console.log("[updateChartLines] Position time range:", timeStart, "-", timeEnd);

        if (positions.length === 1) {
            const p = positions[0];
            const price = p.entries?.[0]?.price || p.avgPrice || 0;
            const isLong = p.dir === 'Buy';
            console.log("[updateChartLines] Position:", isLong ? "LONG" : "SHORT", "@", price);
            positionSeries.applyOptions({ color: isLong ? '#22C55E' : '#EF4444' });
            positionSeries.setData([
                { time: timeStart, value: price },
                { time: timeEnd, value: price }
            ]);
        } else {
            positionSeries.setData([]);
        }
    }).catch(e => console.error("[ERROR] updateChartLines positions:", e));

    // Обновить линии заявок
    fetch('/api/orders').then(r => r.json()).then(data => {
        window._lastOrders = (data.orders || data || []);
        if (typeof clearOrderLines === 'function') clearOrderLines();
        const orders = data.orders || data || [];
        const activeOrders = orders.filter(o => o.status === 'ORDER_STATUS_NEW');
        if (!activeOrders.length) return;
        activeOrders.forEach(o => {
            const isBuy = o.order?.side === 'BUY' || o.order?.side === 'SIDE_BUY';
            const price = parseFloat(o.order?.limit_price?.value || 0);
            if (price > 0) addOrderLine(price, isBuy);
        });
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
    const tickers = ['SiM6','SiU6','MXM6','GDM6','BRK6','RIU6'];
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
        // Таблица счетов
        const tbody = el('accountsTable');
        if (tbody && data?.accounts) {
            tbody.innerHTML = data.accounts.map(a => {
                const balance = a.balance || 0;
                const free = a.free || 0;
                const margin = a.margin || (balance - free);
                const go = a.go || margin;
                const pnlToday = a.pnlToday || 0;
                const pnlTotal = a.pnlTotal || 0;
                const fmt2 = v => v.toLocaleString('ru-RU',{maximumFractionDigits:2});
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
        // Select в панели робота
        const sel = el('robotAccount');
        if (sel) {
            sel.innerHTML = (data?.accounts || []).map(a => `<option value="${a.id}">${a.id} — ${a.name} (${(a.balance||0).toLocaleString('ru-RU')}₽)</option>`).join('');
        }
    } catch (e) {}
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
function initChart() {
    if (chart) return;
    const container = el('chartContainer');
    if (!container || container.offsetWidth === 0) {
        setTimeout(initChart, 100);
        return;
    }
    const msOffset = 3 * 3600000; // MSK = UTC+3
    const fmtTime = d => {
        return d.toISOString().slice(11, 16);
    };
    const fmtDate = d => {
        return d.toISOString().slice(0, 10);
    };
    chart = LightweightCharts.createChart(container, {
        width: container.offsetWidth,
        height: container.offsetHeight || 400,
        layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
        grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        timeScale: { timeVisible: true, secondsVisible: false },
        localization: {
            timeFormatter: t => fmtTime(new Date(t * 1000)),
            dateFormat: t => fmtDate(new Date(t * 1000)),
        },
    });
    candleSeries = chart.addCandlestickSeries({
        upColor: '#22C55E', downColor: '#EF4444',
        borderUpColor: '#22C55E', borderDownColor: '#EF4444',
        wickUpColor: '#22C55E', wickDownColor: '#EF4444',
    });
    volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: '',
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

    // SAR line (dots)
    sarSeries = chart.addLineSeries({
        color: '#FFD700',
        lineWidth: 1,
        pointMarkersVisible: true,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'SAR',
    });

    // EMA line
    emaSeries = chart.addLineSeries({
        color: '#00BFFF',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'EMA',
    });

    // STD line (порог выхода)
    stdSeries = chart.addLineSeries({
        color: '#FF6B6B',
        lineWidth: 1,
        lineStyle: 2,  // dashed
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'STD',
        visible: true,
    });

    // Position lines (LONG=green, SHORT=red)
    positionSeries = chart.addLineSeries({
        color: '#FFFFFF',
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'Position',
    });

let _orderLineSeries = []; // dynamic order line series
window._lastTradeCount = 0;

function clearOrderLines() {
    _orderLineSeries.forEach(s => { try { chart.removeSeries(s); } catch(e) {} });
    _orderLineSeries = [];
}

function addOrderLine(price, isBuy) {
    if (!chart) return;
    const s = chart.addLineSeries({
        color: isBuy ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)',
        lineWidth: 1,
        lineStyle: isBuy ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Dotted,
        priceLineVisible: true,
        lastValueVisible: true,
        priceFormat: { type: 'price', precision: 0, minMove: 1 },
        title: (isBuy ? '🟢 B ' : '🔴 S ') + price.toFixed(0),
        crosshairMarkerVisible: false,
    });
    const now = Math.floor(Date.now() / 1000);
    const ts = Math.floor((now - 7200) / 60) * 60;
    const te = Math.floor((now + 3600) / 60) * 60;
    s.setData([{ time: ts, value: price }, { time: te, value: price }]);
    _orderLineSeries.push(s);
}

    // Order lines — replaced with dynamic series below
    buyOrdersSeries = null;
    sellOrdersSeries = null;


    // Двойной клик по графику → меню индикаторы
    container.addEventListener('dblclick', (e) => {
        e.preventDefault();
        showIndicatorMenu(e.clientX, e.clientY);
    });

    new ResizeObserver(() => {
        chart.applyOptions({ width: container.offsetWidth, height: container.offsetHeight || 400 });
    }).observe(container);
}

function loadCandles(ticker, tf) {
    const tfMinutes = parseInt(tf) || 5;
    const days = tfMinutes <= 5 ? 10 : tfMinutes <= 60 ? 30 : 90;

    // Загружаем через Finam API (сервер)
    fetch(`/api/candles?ticker=${ticker}&tf=${tfMinutes}&days=${days}`)
        .then(r => r.json())
        .then(data => {
            if (data.error) { addLog(nowTime(), 'ERROR', data.error); return; }
            if (!data.length) { addLog(nowTime(), 'INFO', `Нет данных для ${ticker}`); return; }

        const candles = data.map(r => ({ time: r.t + 10800, open: r.o, high: r.h, low: r.l, close: r.c }));
            const volumes = data.map(r => ({
                time: r.t + 10800, value: r.v || 0,
                color: r.c >= r.o ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)'
            }));

            // Store instrument for reinit detection
            window._currentChartTicker = ticker;
            window._currentChartTf = tfMinutes;

            if (candleSeries) candleSeries.setData(candles);
            if (volumeSeries) volumeSeries.setData(volumes);
            window._lastCandles = candles;
            // Clear old order lines
            if (typeof clearOrderLines === 'function') clearOrderLines();
            updateChartLines();

            // Рассчитываем SAR и EMA на клиенте
            const sarData = calcSAR(data, window._sarParams?.start || 0.009, window._sarParams?.step || 0.01, window._sarParams?.max || 0.2);
            const emaData = calcEMA(data, window._emaPeriod || 30);

            if (sarSeries && sarData.length > 0) sarSeries.setData(sarData.map(d => ({ time: d.time + 10800, value: d.value })));
            if (emaSeries && emaData.length > 0) emaSeries.setData(emaData.map(d => ({ time: d.time + 10800, value: d.value })));
            
            // STD band around SAR
            const stdMult = parseFloat(localStorage.getItem('v7_stdMult')) || 1.5;
            const stdPeriod = parseInt(localStorage.getItem('v7_stdPeriod')) || 14;
            const stdBand = [];
            for (let i = stdPeriod - 1; i < data.length; i++) {
                let sum=0,sq=0;
                for (let j=i-stdPeriod+1;j<=i;j++){sum+=data[j].c;sq+=data[j].c*data[j].c;}
                const std=Math.sqrt(sq/stdPeriod-Math.pow(sum/stdPeriod,2));
                const sar=d=>d.time===data[i].t;
                const sv=sarData.find(sar);
                if(sv&&std>0){stdBand.push({time:data[i].t+10800,value:sv.value+std*stdMult});stdBand.push({time:data[i].t+10800,value:sv.value-std*stdMult});}
            }
            if(stdSeries&&stdBand.length>0)stdSeries.setData(stdBand);

            el('obCandleCount').textContent = `${candles.length} свечей`;
            addLog(nowTime(), 'INFO', `📊 ${ticker}: ${candles.length} свечей (${tfMinutes}м) SAR=${sarData.length} EMA=${emaData.length}`);
            
            // Load trade markers on chart
            loadTradeMarkers(ticker, candles);
        })
        .catch(e => addLog(nowTime(), 'ERROR', `Свечи: ${e.message}`));
}

// === Trade markers on main chart ===
async function loadTradeMarkers(ticker, candles) {
    if (!candleSeries || !candles.length) return;
    
    try {
        // Only today's trades for live refresh
        const today = new Date().toISOString().slice(0, 10);
        const resp = await fetch(`/api/trades?date=${today}`);
        const raw = await resp.json();
        let trades = raw.trades || (Array.isArray(raw) ? raw : []);
        
        // Filter by ticker
        const tTicker = ticker.replace('@RTSX', '');
        trades = trades.filter(t => {
            const sym = (t.symbol || '').replace('@RTSX', '');
            return sym.includes(tTicker) || tTicker.includes(sym);
        });
        
        if (!trades.length) { candleSeries.setMarkers([]); return; }
        
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
                text: isBuy ? '▲' : '▼',
                price: price,
                size: 1
            };
        }).filter(m => m.time > 0 && !isNaN(m.time));
        
        markers.sort((a, b) => a.time - b.time);
        candleSeries.setMarkers(markers);
    } catch (e) {
        // silently fail
    }
}

// === Индикаторы: SAR и EMA на клиенте ===
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
    if (candleSeries && sarSeries && emaSeries) {
        const candles = candleSeries.data();
        if (candles && candles.length > 0) {
            const bars = candles.map(c => ({ t: c.time, h: c.high, l: c.low, c: c.close }));
            const sarData = calcSAR(bars, start, step, max);
            const emaData = calcEMA(bars, emaPeriod);
            if (sarData.length > 0) sarSeries.setData(sarData);
            if (emaData.length > 0) emaSeries.setData(emaData);
        }
    }
    el('indicatorMenu')?.remove();
}

// Инициализация параметров
window._sarParams = { start: 0.009, step: 0.01, max: 0.2 };
window._emaPeriod = 30;

function switchOrderBookInstrument() {
    const ticker = el('obInstrument')?.value;
    const tf = el('obTimeframe')?.value;
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
            
            // Trade markers — incremental
            const newTrades = tradesResp.trades || [];
            const newCount = newTrades.length;
            if (newCount !== window._lastTradeCount && candleSeries) {
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
                candleSeries.setMarkers(markers);
            }
            
            // Re-render orderbook only if changed
            if ((posChanged || ordChanged) && window._lastObData) {
                _renderOrderBookCached();
            }
            // Update chart order lines
            if (ordChanged) {
                clearOrderLines();
                window._lastOrders.filter(o => o.status === 'ORDER_STATUS_NEW').forEach(o => {
                    const isBuy = o.order?.side === 'BUY' || o.order?.side === 'SIDE_BUY';
                    const price = parseFloat(o.order?.limit_price?.value || 0);
                    if (price > 0) addOrderLine(price, isBuy);
                });
            }
        });
    }, 500);  // 500ms — fast as QUIK
    // Orderbook + candles — 2s
    if (window._slowInterval) clearInterval(window._slowInterval);
    window._slowInterval = setInterval(() => {
        const t = el('obInstrument')?.value;
        loadOrderBook(t);
        updateLiveCandle(t);
        if (t && t.startsWith('Si')) loadStrategyIndicators();
    }, 2000);  // Стакан + свечи каждые 3с
    
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
    loadCandles(ticker, tf);
}

// === Живой график: обновляем последнюю свечу и добавляем новые ===
let _lastCandleTime = 0;
let _currentTf = 5;

// Тик-баи-тик обновление через /api/quote (быстрый, каждые 500мс)
function updateLivePrice(ticker) {
    if (!ticker) return;
    fetch(`/api/quote?ticker=${ticker}`)
        .then(r => r.json())
        .then(q => {
            if (!q || q.error || !q.last || !candleSeries) return;
            const price = q.last;
            // Защита от чужой цены: если цена отличается от последней свечи > 5% — не обновлять
            const bars = candleSeries.data();
            const last = bars.length > 0 ? bars[bars.length - 1] : null;
            if (last && last.close > 0 && Math.abs(price - last.close) / last.close > 0.05) return;
            const now = Math.floor(Date.now() / 1000);
            const tf = parseInt(el('obTimeframe')?.value) || 5;
            const candleOpen = now - (now % (tf * 60)) + 10800; // MSK offset

            // Обновляем/создаём текущую свечу
            if (_lastCandleTime !== candleOpen) {
                // Новая свеча
                _lastCandleTime = candleOpen;
                candleSeries.update({ time: candleOpen, open: price, high: price, low: price, close: price });
            } else {
                // Обновляем текущую — берём предыдущие OHLC из серии
                if (last && last.time === candleOpen) {
                    candleSeries.update({
                        time: candleOpen,
                        open: last.open,
                        high: Math.max(last.high, price),
                        low: Math.min(last.low, price),
                        close: price
                    });
                } else {
                    candleSeries.update({ time: candleOpen, open: price, high: price, low: price, close: price });
                }
            }
        })
        .catch(e => console.error("[ERROR]", e));
        updateChartLines();
}

function updateLiveCandle(ticker) {
    if (!ticker) return;
    const tf = parseInt(el('obTimeframe')?.value) || 5;
    _currentTf = tf;
    fetch(`/api/candles?ticker=${ticker}&tf=${tf}&days=1`)
        .then(r => r.json())
        .then(data => {
            if (!data || data.error || !data.length || !candleSeries) return;

            // Только обновляем последнюю свечу
            const last = data[data.length - 1];
            candleSeries.update({ time: last.t + 10800, open: last.o, high: last.h, low: last.l, close: last.c });
            if (volumeSeries) {
                volumeSeries.update({ time: last.t + 10800, value: last.v || 0, color: last.c >= last.o ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)' });
            }
        })
        .catch(e => console.error("[ERROR]", e));
}

            updateChartLines();
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
                const sarData = data.indicators
                    .filter(p => p.sar > 0 && !isNaN(p.sar))
                    .map(p => ({ time: toChartTime(p.time), value: p.sar }));
                const emaData = data.indicators
                    .filter(p => p.ema > 0 && !isNaN(p.ema))
                    .map(p => ({ time: toChartTime(p.time), value: p.ema }));
                
                if (sarSeries && sarData.length > 0) sarSeries.setData(sarData);
                if (emaSeries && emaData.length > 0) emaSeries.setData(emaData);
            }
            
            if (data.trades && data.trades.length > 0 && candleSeries) {
                const markers = data.trades.map(t => ({
                    time: toChartTime(t.time),
                    position: t.dir === 1 ? 'belowBar' : 'aboveBar',
                    color: t.dir === 1 ? '#22C55E' : '#EF4444',
                    shape: t.dir === 1 ? 'arrowUp' : 'arrowDown',
                    text: `${t.dir === 1 ? 'B' : 'S'} ${t.lots}x@${t.price.toFixed(0)}`,
                }));
                markers.sort((a, b) => a.time > b.time ? 1 : -1);
                candleSeries.setMarkers(markers);
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
    'v7-SiM6': {sarStart:0.009,sarStep:0.01,sarMax:0.2,ema:30,gridStep:35,gridSpread:35,maxGrid:70,stdPeriod:14,stdMult:1.5,gridHold:true,closePct:0.15},
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
    const instr = el('stratInstrument')?.value || 'SiM6';
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
        ticker: 'SiM6',
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
        ticker: 'SiM6',
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
        ticker: el('robotTicker')?.value || 'SiM6',
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

async function renderRobots() {
    const tbody = el('robotsTable');
    const noMsg = el('noRobots');
    if (!tbody) return;

    // C# server strategies removed — Python is primary
    let serverStrategies = [];
    try { /* no-op, kept for compatibility */ } catch(e) {}

    // localStorage robots removed — Python robot only
    let localRows = [];
    try { /* no-op */ } catch(e) {}

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
            <td><strong>SiM6</strong></td>
            <td><strong>VP Scalp Grid</strong> <span class="badge" style="background:#2196F3">PYTHON</span>${paper}</td>
            <td>Финам</td>
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
            <td><strong>SiM6</strong></td>
            <td><strong>VP Scalp Grid</strong> <span class="badge" style="background:#2196F3">PYTHON</span></td>
            <td>Финам</td>
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

    const allRows = [...serverStrategies, ...localRows, pythonRobotRow];
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
            instrument: r.ticker || 'SiM6',
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
    const apiBase = getRobotApiBase(r);
    r._stopping = true; renderRobots();
    addLog(nowTime(), 'INFO', `⏹ Остановка робота ${r.ticker}, закрытие позиций...`);
    try {
        const resp = await fetch(apiBase + '/stop', { method: 'POST' });
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
renderRobots();
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
        const ticker = robot.ticker || 'SiM6';
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

// === Arbitrage ===
async function arbInit() {
    try {
        const token = localStorage.getItem('finamToken') || '';
        if (!token) {
            arbLog('ERROR', 'Токен не задан. Сначала укажите токен во вкладке Настройки');
            return;
        }
        el('arbStatusText').textContent = '⏳ Инициализация...';
        arbLog('INFO', '🔄 Инициализация арбитража...');
        
        const resp = await fetch('/arb/init', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token })
        });
        
        const text = await resp.text();
        arbLog('INFO', `Ответ сервера: ${resp.status} ${text.substring(0, 200)}`);
        
        let data;
        try { data = JSON.parse(text); } catch { data = { error: text || 'Пустой ответ от сервера' }; }
        
        if (resp.ok && !data.error) {
            el('arbStatusText').textContent = '✅ Инициализирован';
            el('arbStartBtn').disabled = false;
            el('arbPauseBtn').disabled = false;
            el('arbStopBtn').disabled = false;
            arbLog('INFO', `✅ Арбитраж инициализирован. Капитал: ${(data.capital||0).toLocaleString('ru-RU')} ₽`);
            arbRefreshStatus();
        } else {
            el('arbStatusText').textContent = '❌ Ошибка';
            arbLog('ERROR', data.error || `Ошибка инициализации (${resp.status})`);
        }
    } catch (e) {
        el('arbStatusText').textContent = '❌ Ошибка';
        arbLog('ERROR', `Исключение: ${e.message}`);
    }
}

async function arbStart() {
    try {
        const resp = await fetch('/arb/start', { method: 'POST' });
        const data = await resp.json();
        el('arbStatusText').textContent = '▶️ Торгует';
        el('arbStatusText').style.color = 'var(--green)';
        arbLog('INFO', '▶️ Арбитраж запущен');
        arbRefreshStatus();
    } catch (e) { arbLog('ERROR', e.message); }
}

async function arbPause() {
    try {
        await fetch('/arb/pause', { method: 'POST' });
        el('arbStatusText').textContent = '⏸ Пауза';
        el('arbStatusText').style.color = 'var(--yellow)';
        arbLog('INFO', '⏸ Арбитраж на паузе');
    } catch (e) { arbLog('ERROR', e.message); }
}

async function arbStop() {
    try {
        await fetch('/arb/stop', { method: 'POST' });
        el('arbStatusText').textContent = '⏹ Остановлен';
        el('arbStatusText').style.color = 'var(--red)';
        arbLog('INFO', '⏹ Арбитраж остановлен, позиции закрыты');
        arbRefreshStatus();
    } catch (e) { arbLog('ERROR', e.message); }
}

async function arbPairStart(spot) {
    try {
        await fetch(`/arb/pair/${spot}/start`, { method: 'POST' });
        arbLog('INFO', `▶ ${spot} запущена`);
    } catch (e) { arbLog('ERROR', e.message); }
}

async function arbPairStop(spot) {
    try {
        await fetch(`/arb/pair/${spot}/stop`, { method: 'POST' });
        arbLog('INFO', `⏹ ${spot} остановлена`);
    } catch (e) { arbLog('ERROR', e.message); }
}

async function arbRefreshStatus() {
    try {
        const resp = await fetch('/arb/status');
        const data = await resp.json();
        if (data.status === 'not_initialized') return;
        
        el('arbConnected').textContent = data.connected ? '✅' : '❌';
        el('arbConnected').className = 'metric-value ' + (data.connected ? 'green' : 'red');
        
        // Parse detail string for metrics
        const detail = data.detail || '';
        const pnlMatch = detail.match(/PnL: ([+-]?[\d,]+)/);
        const tradesMatch = detail.match(/Сделок: (\d+)/);
        if (pnlMatch) {
            const pnl = parseInt(pnlMatch[1].replace(/,/g, ''));
            el('arbTotalPnl').textContent = pnl.toLocaleString('ru-RU') + ' ₽';
            el('arbTotalPnl').className = 'metric-value ' + (pnl > 0 ? 'green' : pnl < 0 ? 'red' : '');
        }
        if (tradesMatch) {
            el('arbTotalTrades').textContent = tradesMatch[1];
        }
        
        const dteMatch = detail.match(/DaysToExpiry: (\d+)/);
        if (dteMatch) el('arbDTE').textContent = dteMatch[1] + ' дн';
        
        // Enable buttons
        el('arbStartBtn').disabled = false;
        el('arbPauseBtn').disabled = false;
        el('arbStopBtn').disabled = false;
        
        // Refresh pairs table
        arbRefreshPairs();
    } catch (e) { /* silent */ }
}

async function arbRefreshPairs() {
    try {
        const resp = await fetch('/arb/pairs');
        const data = await resp.json();
        if (!data.pairs || data.pairs.length === 0) return;
        
        const tbody = el('arbPairsTable');
        const fmtRub = n => n ? Math.round(n).toLocaleString('ru-RU') : '0';
        
        const rows = data.pairs.map(p => {
            const pnlCls = p.pnl > 0 ? 'pnl-positive' : p.pnl < 0 ? 'pnl-negative' : '';
            const posText = p.isOpen 
                ? `акц:${p.posSpotLots} фью:${p.posFutLots}`
                : '—';
            const modeDot = p.mode === 'Running' ? '🟢' : p.mode === 'Paused' ? '🟡' : '🔴';
            const zCls = Math.abs(p.zScore) > 1.0 ? 'accent' : '';
            
            return `<tr>
                <td><b>${p.spot}</b><br><small>${p.futures}</small></td>
                <td>
                    <input type="number" class="input" style="width:65px;padding:2px 4px;text-align:center" 
                           value="${p.spotLots}" min="1" max="10000" id="sl_${p.spot}"
                           onchange="arbSetLots('${p.spot}')" 
                           title="Лоты акций (1 лот=${p.sharesPerSpotLot} шт)">
                </td>
                <td>
                    <input type="number" class="input" style="width:55px;padding:2px 4px;text-align:center" 
                           value="${p.futLots}" min="1" max="1000" id="fl_${p.spot}"
                           onchange="arbSetLots('${p.spot}')" 
                           title="Контракты фьючерса (ГО=${fmtRub(p.futuresGO)})">
                </td>
                <td style="text-align:right">${fmtRub(p.spotValueRub)}</td>
                <td style="text-align:right">${fmtRub(p.futGORub)}</td>
                <td style="text-align:right"><b>${fmtRub(p.totalValueRub)}</b></td>
                <td class="${zCls}">${p.zScore.toFixed(2)}</td>
                <td>${p.basisAnnual.toFixed(1)}%</td>
                <td>${posText}</td>
                <td class="${pnlCls}">${fmtRub(p.pnl)}</td>
                <td>${modeDot}</td>
                <td>
                    <button class="btn btn-primary btn-sm" onclick="arbPairStart('${p.spot}')">▶</button>
                    <button class="btn btn-danger btn-sm" onclick="arbPairStop('${p.spot}')">⏹</button>
                </td>
            </tr>`;
        }).join('');
        
        tbody.innerHTML = rows;
    } catch (e) { /* silent */ }
}

async function arbSetLots(spot) {
    const spotLots = parseInt(el(`sl_${spot}`)?.value) || 0;
    const futLots = parseInt(el(`fl_${spot}`)?.value) || 1;
    if (futLots < 1) { arbLog('ERROR', 'Минимум 1 контракт'); return; }
    try {
        const resp = await fetch(`/arb/pair/${spot}/lots`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ spotLots, futLots })
        });
        const data = await resp.json();
        if (resp.ok) {
            arbLog('INFO', `✅ ${spot}: акции=${data.spotLots} лот, фьючерс=${data.futLots} контр.`);
        } else {
            arbLog('ERROR', data.error || 'Ошибка установки лотов');
        }
    } catch (e) { arbLog('ERROR', e.message); }
}

function arbLog(level, msg) {
    const container = el('arbLogContainer');
    if (!container) return;
    const time = new Date().toLocaleTimeString();
    const cls = level === 'ERROR' ? 'log-error' : level === 'TRADE' ? 'log-trade' : 'log-info';
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.innerHTML = `<span class="log-time">[${time}]</span> <span class="${cls}">${msg}</span>`;
    container.appendChild(div);
    while (container.children.length > MAX_LOG) container.removeChild(container.firstChild);
    container.scrollTop = container.scrollHeight;
    // Also to main log
    addLog(time, level, `[ARB] ${msg}`);
}

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
    setInterval(arbRefreshStatus, 30000);

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
    try {
        const resp = await fetch(ROBOT_API + '/api/robot/config', {signal: AbortSignal.timeout(2000)});
        const data = await resp.json();
        if (!data.error) { cfg = data; addLog(nowTime(), 'INFO', 'Config loaded'); }
        else { addLog(nowTime(), 'WARN', 'Robot offline'); }
    } catch(e) {
        addLog(nowTime(), 'WARN', 'Robot offline');
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
    try {
        const resp = await fetch(ROBOT_API + '/api/robot/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        const data = await resp.json();
        addLog(nowTime(), 'INFO', '🐍 Config saved: ' + JSON.stringify(data));
    } catch(e) {
        addLog(nowTime(), 'ERROR', '🐍 Save config failed: ' + e.message);
    }
}

function pythonRobotEditPanel() {
    const existing = el('robotEditPanel');
    if (existing) { existing.remove(); return; }

    const div = document.createElement('div');
    div.id = 'robotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            🐍 VP Scalp Grid (PYTHON)
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
                <div class="metric-card"><div class="metric-label">Round Trips</div><div id="pyRT" style="font-size:18px;font-weight:bold">0</div></div>
                <div class="metric-card"><div class="metric-label">PnL реал.</div><div id="pyPnlReal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">PnL нереал.</div><div id="pyPnlUnreal" style="font-size:18px;font-weight:bold">—</div></div>
                <div class="metric-card"><div class="metric-label">Hold</div><div id="pyHold" style="font-size:18px;font-weight:bold">0 мин</div></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:16px">
                <div class="metric-card"><div class="metric-label">Max Levels</div><input id="editPyMaxLevels" class="input" type="number" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Step Base (пт)</div><input id="editPyStepBase" class="input" type="number" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Spread Base (пт)</div><input id="editPySpreadBase" class="input" type="number" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Max Hold (мин)</div><input id="editPyMaxHold" class="input" type="number" style="width:100px"></div>
                <div class="metric-card"><div class="metric-label">VP Lookback</div><input id="editPyLookback" class="input" type="number" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VP Bin Size</div><input id="editPyBinSize" class="input" type="number" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">VA %</div><input id="editPyVaPercent" class="input" type="number" step="0.05" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">PnL/лот (пт)</div><input id="editPyMinProfit" class="input" type="number" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">RV Adaptation</div><br><input id="editPyRvAdapt" type="checkbox" style="width:20px;height:20px;vertical-align:middle"></div>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Grid Levels -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>📊 Grid Levels</strong>
                <button class="btn btn-secondary btn-sm" onclick="pythonRobotLoadGridLevels()">🔄 Обновить</button>
            </div>
            <div style="max-height:250px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table class="data-table" style="font-size:13px">
                    <thead><tr><th>#</th><th>Side</th><th>Цена</th><th>Статус</th><th>TP</th><th>TP Fill</th></tr></thead>
                    <tbody id="pyGridBody"><tr><td colspan="6" style="text-align:center;color:#9CA3AF">Нажмите 🔄 для загрузки</td></tr></tbody>
                </table>
            </div>
        </div>
    `;
    document.body.appendChild(div);

    // Load config + start live VP update
    pythonRobotLoadToPanel();
    pythonRobotUpdateVp();
    if (window._pyVpTimer) clearInterval(window._pyVpTimer);
    window._pyVpTimer = setInterval(() => pythonRobotUpdateVp(), 2000);
}

function pythonRobotUpdateVp() {
    if (!el('pyVAH')) return;
    const s = pythonRobot;
    if (!s) return;
    if (s.vah) el('pyVAH').textContent = Math.round(s.vah);
    if (s.poc) el('pyPOC').textContent = Math.round(s.poc);
    if (s.val) el('pyVAL').textContent = Math.round(s.val);
    if (s.current_price) el('pyPrice').textContent = s.current_price.toFixed(0);
    const dirText = s.direction > 0 ? 'Лонг' : s.direction < 0 ? 'Шорт' : 'Флэт';
    if (el('pyDir')) el('pyDir').textContent = dirText;
    if (el('pyLots')) el('pyLots').textContent = s.total_lots || 0;
    if (el('pyGrid')) el('pyGrid').textContent = (s.grid_levels||0) + ' (' + (s.filled_levels||0) + ' fill)';
    if (el('pyRT')) el('pyRT').textContent = s.round_trips || 0;
    if (el('pyPnlReal')) { el('pyPnlReal').textContent = (s.realized_pnl||0).toFixed(0)+'₽'; el('pyPnlReal').style.color = s.realized_pnl >= 0 ? 'var(--green)' : 'var(--red)'; }
    if (el('pyPnlUnreal')) { el('pyPnlUnreal').textContent = (s.pnl||0).toFixed(0)+'₽'; el('pyPnlUnreal').style.color = s.pnl >= 0 ? 'var(--green)' : 'var(--red)'; }
    if (el('pyHold')) el('pyHold').textContent = (s.hold_minutes || 0) + ' мин';
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
        if (el('cfgVpMaxLevels')) el('cfgVpMaxLevels').value = body.max_levels;
        if (el('cfgVpStepBase')) el('cfgVpStepBase').value = body.step_base;
        if (el('cfgVpSpreadBase')) el('cfgVpSpreadBase').value = body.spread_base;
        if (el('cfgVpMaxHold')) el('cfgVpMaxHold').value = body.max_hold_minutes;
        if (el('cfgVpMinProfit')) el('cfgVpMinProfit').value = body.min_profit_per_lot;
    } catch(e) {
        addLog(nowTime(), 'ERROR', '🐍 Save failed: ' + e.message);
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
