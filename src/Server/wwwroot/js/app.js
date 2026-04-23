// === OpenMarketflow Web UI ===

let connection = null;
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let sarSeries = null;
let emaSeries = null;
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
            <td>${p.entries?.[0]?.price?.toFixed(2) || p.avgPrice?.toFixed(2) || '—'}</td>
            <td>${p.qty || 0}</td>
        </tr>`).join('');
    }).catch(e => console.error("[ERROR]", e));
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
    const tickers = ['SiM6','SiU6','MXM6','GDM6','BRK6','RIU6'];
    const tbody = el('quotesTable');
    if (!tbody) return;
    // Создаём строки только если их ещё нет
    if (!tbody.children.length || tbody.dataset.tickerv !== tickers.join(',')) {
        tbody.innerHTML = tickers.map(t => `<tr id="q_${t}"><td><b>${t}</b></td><td class="q_last" style="color:var(--text-muted)">—</td><td class="q_bid" style="color:var(--text-muted)">—</td><td class="q_ask" style="color:var(--text-muted)">—</td><td class="q_vol" style="color:var(--text-muted)">—</td></tr>`).join('');
        tbody.dataset.tickerv = tickers.join(',');
    }
    tickers.forEach(t => {
        fetch(`/api/quote?ticker=${t}`).then(r => r.json()).then(q => {
            const row = el(`q_${t}`);
            if (!row) return;
            const cells = row.children;
            if (q.last) cells[1].textContent = q.last.toFixed(0);
            if (q.bid) cells[2].textContent = q.bid.toFixed(0);
            if (q.ask) cells[3].textContent = q.ask.toFixed(0);
            cells[4].textContent = q.volume ? (q.volume >= 1e6 ? (q.volume/1e6).toFixed(1)+'M' : q.volume >= 1e3 ? (q.volume/1e3).toFixed(0)+'K' : q.volume.toFixed(0)) : '—';
        }).catch(() => {});
    });
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

    let html = '';
    const sorted = [...entries].sort((a, b) => (b.price || b.Price) - (a.price || a.Price));
    
    // Ограничим ±25 от спреда
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

        html += `<div class="ob-row${isLast ? ' spread-row' : ''}">
            <div class="ob-bid"><div class="ob-bar-bid" style="width:${bidW}%"></div>${bid || ''}</div>
            <div class="ob-price${isLast ? ' ob-price-last' : ''}">${price.toFixed(2)}</div>
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
        const msk = new Date(d.getTime() + msOffset);
        return msk.toISOString().slice(11, 16);
    };
    const fmtDate = d => {
        const msk = new Date(d.getTime() + msOffset);
        return msk.toISOString().slice(0, 10);
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

    // Двойной клик по графику → меню индикаторов
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

            const candles = data.map(r => ({ time: r.t, open: r.o, high: r.h, low: r.l, close: r.c }));
            const volumes = data.map(r => ({
                time: r.t, value: r.v || 0,
                color: r.c >= r.o ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)'
            }));

            if (candleSeries) candleSeries.setData(candles);
            if (volumeSeries) volumeSeries.setData(volumes);

            // Рассчитываем SAR и EMA на клиенте
            const sarData = calcSAR(data, window._sarParams?.start || 0.009, window._sarParams?.step || 0.01, window._sarParams?.max || 0.2);
            const emaData = calcEMA(data, window._emaPeriod || 30);

            if (sarSeries && sarData.length > 0) sarSeries.setData(sarData);
            if (emaSeries && emaData.length > 0) emaSeries.setData(emaData);

            el('obCandleCount').textContent = `${candles.length} свечей`;
            addLog(nowTime(), 'INFO', `📊 ${ticker}: ${candles.length} свечей (${tfMinutes}м) SAR=${sarData.length} EMA=${emaData.length}`);
        })
        .catch(e => addLog(nowTime(), 'ERROR', `Свечи: ${e.message}`));
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
    }, 2000);  // Реал-тайм котировки каждые 2с
    // Индикаторы и стакан — реже
    if (window._slowInterval) clearInterval(window._slowInterval);
    window._slowInterval = setInterval(() => {
        const t = el('obInstrument')?.value;
        loadOrderBook(t);
        updateLiveCandle(t);
        if (t && t.startsWith('Si')) loadStrategyIndicators();
    }, 3000);  // Стакан + свечи каждые 3с
    
    // Первая загрузка индикаторов
    loadStrategyIndicators();
    
    // Инициализация графика с ретраем (контейнер может быть ещё скрыт)
    initChart();
    if (!chart) setTimeout(() => { initChart(); loadCandles(ticker, tf); }, 300);
}

function loadQuote(ticker) {
    if (!ticker) return;
    fetch(`/api/quote?ticker=${ticker}`)
        .then(r => r.json())
        .then(q => {
            if (q.last > 0) el('obLast').textContent = q.last.toFixed(2);
            if (q.bid > 0) el('obBid').textContent = q.bid.toFixed(2);
            if (q.ask > 0) el('obAsk').textContent = q.ask.toFixed(2);
            if (q.spread > 0) el('obSpread').textContent = q.spread.toFixed(2);
        })
        .catch(e => console.error("[ERROR]", e));
}

function loadOrderBook(ticker) {
    if (!ticker) return;
    fetch(`/api/orderbook?ticker=${ticker}`)
        .then(r => r.json())
        .then(data => {
            if (!data.rows || data.rows.length === 0) return;
            const rows = data.rows;
            const maxVol = Math.max(...rows.map(r => Math.max(r.bid || 0, r.ask || 0)), 1);
            
            // Сортируем по убыванию цены
            rows.sort((a, b) => b.price - a.price);
            
            // Находим границу bid/ask и ограничиваем ±25
            const spreadIdx = rows.findIndex(r => (r.bid || 0) > 0);
            const si = spreadIdx >= 0 ? spreadIdx : Math.floor(rows.length / 2);
            const start = Math.max(0, si - 25);
            const end = Math.min(rows.length, si + 25);
            const visible = rows.slice(start, end);
            
            let html = '';
            for (const r of visible) {
                const bidW = ((r.bid || 0) / maxVol * 100).toFixed(0);
                const askW = ((r.ask || 0) / maxVol * 100).toFixed(0);
                const isSpread = r.bid > 0 && visible.indexOf(r) === visible.findIndex(x => x.bid > 0);
                html += `<div class="ob-row${isSpread ? ' spread-row' : ''}">
                    <div class="ob-bid"><div class="ob-bar-bid" style="width:${bidW}%"></div>${r.bid || ''}</div>
                    <div class="ob-price">${r.price.toFixed(2)}</div>
                    <div class="ob-ask"><div class="ob-bar-ask" style="width:${askW}%"></div>${r.ask || ''}</div>
                </div>`;
            }
            el('orderbookLadder').innerHTML = html;
        })
        .catch(e => console.error("[ERROR]", e));
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
            const candleOpen = now - (now % (tf * 60)); // начало текущей свечи

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
            candleSeries.update({ time: last.t, open: last.o, high: last.h, low: last.l, close: last.c });
            if (volumeSeries) {
                volumeSeries.update({ time: last.t, value: last.v || 0, color: last.c >= last.o ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)' });
            }
        })
        .catch(e => console.error("[ERROR]", e));
}

// === Strategy Indicators on Chart ===
function loadStrategyIndicators() {
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
    return Math.floor(d.getTime() / 1000);
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
}, 500);

function saveStrategyConfig() {
    const cfg = readStratCfg();
    fetch('/strategy/grid-mm/config', {
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
            const eqData = r.equityCurve.map(p => ({ time: p.time, value: p.value }));
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
            cs.setData(r.candles.map(c => ({ time: c.t, open: c.o, high: c.h, low: c.l, close: c.c })));
            if (r.sar?.length) { const s = btCandleChart.addLineSeries({ color: '#F59E0B', lineWidth: 1, priceLineVisible: false, lastValueVisible: false }); s.setData(r.sar); }
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
    if (el('robotForceEntry')) el('robotForceEntry').checked = cfg.forceEntry === 'true' || cfg.forceEntryOnStart === true;
    el('robotPanel').style.display = 'block';
}

function confirmCreateRobot() {
    const robot = {
        id: Date.now(),
        ticker: el('robotTicker')?.value || 'SiM6',
        account: el('robotAccount')?.value || '',
        accountName: el('robotAccount')?.selectedOptions?.[0]?.text || '',
        strategy: 'SAR×EMA Grid MM v6',
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

    // Загружаем активные стратегии с сервера
    let serverStrategies = [];
    try {
        const resp = await fetch('/api/active-strategies');
        const data = await resp.json();
        if (data.strategies) {
            serverStrategies = data.strategies.map(s => {
                const posText = s.posDir > 0 ? 'Лонг' : s.posDir < 0 ? 'Шорт' : 'Флэт';
                const modeText = s.mode === 'Running' ? '🟢 Работает' : s.mode === 'Paused' ? '🟡 Пауза' : '🔴 Остановлен';
                const modeCls = s.mode === 'Running' ? 'green' : s.mode === 'Paused' ? 'yellow' : 'red';
                const pnlCls = v => v >= 0 ? 'green' : 'red';
                return `<tr style="background:#f0fdf4">
                    <td><strong>${s.instrument}</strong></td>
                    <td><strong>${s.name}</strong> <span class="badge">СЕРВЕР</span></td>
                    <td>Финам</td>
                    <td class="${s.posDir > 0 ? 'green' : s.posDir < 0 ? 'red' : ''}">${posText}${s.entryPrice > 0 ? ' @ ' + s.entryPrice.toFixed(0) : ''}</td>
                    <td>—</td>
                    <td class="${pnlCls(s.totalPnL)}">${s.totalPnL >= 0 ? '+' : ''}${s.totalPnL.toFixed(0)} ₽</td>
                    <td>${s.openLots || s.lots || 0}</td>
                    <td>—</td>
                    <td>
                        ${s.id === 'grid-mm' ? `
                            <button class="btn btn-warning btn-sm" onclick="fetch('/strategy/grid-mm/pause',{method:'POST'})">⏸</button>
                            <button class="btn btn-danger btn-sm" onclick="fetch('/strategy/grid-mm/stop',{method:'POST'})">⏹</button>
                            <button class="btn btn-success btn-sm" onclick="fetch('/strategy/grid-mm/run',{method:'POST'})">▶</button>
                        ` : ''}
                        ${s.id === 'vol-rev' ? `
                            <button class="btn btn-warning btn-sm" onclick="fetch('/strategy/vol-rev/pause',{method:'POST'})">⏸</button>
                            <button class="btn btn-danger btn-sm" onclick="fetch('/strategy/vol-rev/stop',{method:'POST'})">⏹</button>
                            <button class="btn btn-success btn-sm" onclick="fetch('/strategy/vol-rev/run',{method:'POST'})">▶</button>
                        ` : ''}
                    </td>
                    <td class="${modeCls}">${modeText} ${s.connected ? '🔗' : '⚠️'}</td>
                </tr>`;
            });
        }
    } catch(e) { console.warn('active-strategies fetch failed:', e); }

    // localStorage роботы
    let localRows = robots.map((r, i) => {
        const statusCls = r.status === 'running' ? 'green' : r.status === 'paused' ? 'yellow' : 'red';
        const statusText = r.status === 'running' ? '🟢 Работает' : r.status === 'paused' ? '🟡 Пауза' : '🔴 Остановлен';
        const posCls = r.position === 'Лонг' ? 'green' : r.position === 'Шорт' ? 'red' : '';
        const pnlCls = v => v >= 0 ? 'green' : 'red';
        return `<tr ondblclick="editRobot(${i})" style="cursor:pointer" title="Двойной клик — параметры">
            <td><strong>${r.ticker}</strong></td>
            <td>${r.strategy}</td>
            <td>${r.accountName || r.account || '—'}</td>
            <td class="${posCls}">${r.position}</td>
            <td class="${pnlCls(r.pnlToday)}">${r.pnlToday >= 0 ? '+' : ''}${r.pnlToday.toLocaleString('ru-RU')} ₽</td>
            <td class="${pnlCls(r.pnlTotal)}">${r.pnlTotal >= 0 ? '+' : ''}${r.pnlTotal.toLocaleString('ru-RU')} ₽</td>
            <td>${r.lotsOpen}</td>
            <td>${r.go.toLocaleString('ru-RU')}</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="robotStart(${i})" ${r.status === 'running' ? 'disabled' : ''}>▶</button>
                <button class="btn btn-warning btn-sm" onclick="robotPause(${i})" ${r.status !== 'running' ? 'disabled' : ''}>⏸</button>
                <button class="btn btn-danger btn-sm" onclick="robotStop(${i})" ${r.status === 'stopped' ? 'disabled' : ''}>⏹</button>
                <button class="btn btn-secondary btn-sm" onclick="robotRemove(${i})" title="Удалить">🗑</button>
            </td>
            <td class="${statusCls}">${statusText}</td>
        </tr>`;
    });

    const allRows = [...serverStrategies, ...localRows];
    if (!allRows.length) { tbody.innerHTML = ''; if (noMsg) noMsg.style.display = 'block'; return; }
    if (noMsg) noMsg.style.display = 'none';
    tbody.innerHTML = allRows.join('');
}

async function robotStart(i) {
    const r = robots[i];
    if (!r) return;
    addLog(nowTime(), 'INFO', `▶ Запуск робота ${r.ticker}...`);
    // Сначала сохраняем параметры стратегии
    try {
        await fetch('/strategy/grid-mm/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sarStart: parseFloat(r.sarStart) || 0.009,
                sarStep: parseFloat(r.sarStep) || 0.01,
                sarMax: parseFloat(r.sarMax) || 0.2,
                emaPeriod: parseInt(r.ema) || 30,
                gridStep: parseFloat(r.gridStep) || 45,
                gridSpread: parseFloat(r.gridSpread) || 50,
                maxGridLevels: parseInt(r.maxGrid) || 70,
                closePct: parseFloat(r.closePct) || 0.30,
                minProfitPerLot: parseFloat(r.minProfit) || 28,
                commission: parseFloat(r.commission) || 0.90,
                maxLots: parseInt(r.lots) || 1,
                forceEntryOnStart: r.forceEntry === 'true'
            })
        });
    } catch (e) {}
    
    // Обновляем параметры для графика
    window._sarParams = { 
        start: parseFloat(r.sarStart) || 0.009, 
        step: parseFloat(r.sarStep) || 0.01, 
        max: parseFloat(r.sarMax) || 0.2 
    };
    window._emaPeriod = parseInt(r.ema) || 30;
    
    try {
        const resp = await fetch('/strategy/grid-mm/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticker: r.ticker })
        });
        const data = await resp.json();
        if (data.error) { addLog(nowTime(), 'ERROR', data.error); return; }
        await fetch('/strategy/grid-mm/run', { method: 'POST' });
        r.status = 'running';
        r.exchangeStatus = 'Подключен';
        saveRobots(); renderRobots();
        addLog(nowTime(), 'INFO', `🤖 Робот ${r.ticker} запущен`);
    } catch (e) { addLog(nowTime(), 'ERROR', e.message); }
}

async function robotPause(i) {
    const r = robots[i];
    if (!r) return;
    try {
        await fetch('/strategy/grid-mm/pause', { method: 'POST' });
        r.status = 'paused';
        saveRobots(); renderRobots();
        addLog(nowTime(), 'INFO', `⏸ Робот ${r.ticker} на паузе`);
    } catch (e) { addLog(nowTime(), 'ERROR', e.message); }
}

async function robotStop(i) {
    const r = robots[i];
    if (!r) return;
    addLog(nowTime(), 'INFO', `⏹ Остановка робота ${r.ticker}, закрытие позиций...`);
    try {
        await fetch('/strategy/grid-mm/stop', { method: 'POST' });
        r.status = 'stopped';
        r.position = '—'; r.lotsOpen = 0; r.go = 0;
        saveRobots(); renderRobots();
        addLog(nowTime(), 'INFO', `⏹ Робот ${r.ticker} остановлен, позиции закрыты`);
    } catch (e) { addLog(nowTime(), 'ERROR', e.message); }
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
    try {
        const resp = await fetch('/strategy/grid-mm/status');
        const data = await resp.json();
        if (data.status === 'not_initialized') return;
        robots.forEach(r => {
            if (r.status === 'running' || r.status === 'paused') {
                r.pnlToday = data.pnl || 0;
                r.pnlTotal = data.totalPnl || data.pnl || 0;
                r.position = data.direction === 1 ? 'Лонг' : data.direction === -1 ? 'Шорт' : '—';
                r.lotsOpen = data.openLots || 0;
                r.go = data.margin || r.lotsOpen * 12000;
                r.exchangeStatus = data.connected ? 'Биржа OK' : 'Нет связи';
                if (data.mode === 'Stopped') r.status = 'stopped';
                if (data.mode === 'Paused') r.status = 'paused';
            }
        });
        saveRobots(); renderRobots();
    } catch (e) {}
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
        if (robotEditChartInstance) { robotEditChartInstance.remove(); robotEditChartInstance = null; }
        panel.remove(); 
    }
    // Create floating edit panel
    const div = document.createElement('div');
    div.id = 'robotEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:900px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8">
            🤖 Параметры: ${r.ticker}
            <button class="btn btn-primary btn-sm" onclick="saveRobotEdit(${i})">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="if(robotEditChartInstance)robotEditChartInstance.remove();robotEditChartInstance=null;el('robotEditPanel')?.remove()">✕</button>
        </div>
        <div style="padding:12px">
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
            </div>
            <div id="robotEditChart" style="height:400px;border:1px solid #2D2D44;border-radius:8px;margin-bottom:12px"></div>
            <div id="robotEditInfo" style="font-size:12px;color:#9CA3AF">Загрузка данных...</div>
        </div>`;
    document.body.appendChild(div);
    // Load accounts into edit select
    loadAccountsInto('editAccount', r.account);
    // Load chart
    loadRobotChart(r);
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

let robotEditChartInstance = null;

async function loadRobotChart(robot) {
    const container = el('robotEditChart');
    const info = el('robotEditInfo');
    if (!container) return;

    try { if (robotEditChartInstance) { robotEditChartInstance.remove(); robotEditChartInstance = null; } } catch(e) {}

    try {
        // 1. Load candles
        const resp = await fetch(`/api/candles?ticker=${robot.ticker}&tf=5&days=5`);
        const raw = await resp.json();
        const allCandles = Array.isArray(raw) ? raw : [];
        if (!allCandles.length) { if (info) info.textContent = 'Нет данных'; return; }

        // 2. Filter out non-trading (flat) candles
        const candles = allCandles.filter(c => c.h > c.l);
        if (!candles.length) { if (info) info.textContent = 'Нет торговых данных'; return; }

        // 3. Take last 200 candles max — enough for chart + indicators
        const data = candles.slice(-200);

        // 4. Wait for container dimensions
        await new Promise(r => setTimeout(r, 100));
        const width = container.offsetWidth || 860;

        // 5. Create chart — MSK timezone
        const tzOffset = 3 * 3600; // UTC+3
        robotEditChartInstance = LightweightCharts.createChart(container, {
            width, height: 400,
            layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
            grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
            timeScale: { timeVisible: true, secondsVisible: false },
            localization: {
                timeFormatter: t => {
                    const d = new Date((t + tzOffset) * 1000);
                    return d.toISOString().slice(11, 16);
                },
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        });

        // 6. Candlestick series
        const candleSeries = robotEditChartInstance.addCandlestickSeries({
            upColor: '#22C55E', downColor: '#EF4444',
            borderUpColor: '#22C55E', borderDownColor: '#EF4444',
            wickUpColor: '#22C55E', wickDownColor: '#EF4444',
        });
        candleSeries.setData(data.map(c => ({
            time: c.t, open: c.o, high: c.h, low: c.l, close: c.c
        })));

        // 7. Volume as histogram
        const volSeries = robotEditChartInstance.addHistogramSeries({
            priceFormat: { type: 'volume' },
            priceScaleId: 'vol',
        });
        volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
        volSeries.setData(data.map(c => ({
            time: c.t, value: c.v || 0,
            color: c.c >= c.o ? 'rgba(34,197,94,0.25)' : 'rgba(239,68,68,0.25)',
        })));

        // 8. SAR indicator
        const sarStart = parseFloat(robot.sarStart) || 0.005;
        const sarStep = parseFloat(robot.sarStep) || 0.01;
        const sarMax = parseFloat(robot.sarMax) || 0.2;
        const sarPoints = calcSAR(data, sarStart, sarStep, sarMax);
        if (sarPoints.length) {
            const sarLine = robotEditChartInstance.addLineSeries({
                color: '#FFD700', lineWidth: 1, pointMarkersVisible: true,
                priceLineVisible: false, lastValueVisible: false, title: 'SAR',
            });
            sarLine.setData(sarPoints);
        }

        // 9. EMA indicator
        const emaPeriod = parseInt(robot.ema) || 30;
        const emaPoints = calcEMA(data, emaPeriod);
        if (emaPoints.length) {
            const emaLine = robotEditChartInstance.addLineSeries({
                color: '#00BFFF', lineWidth: 2,
                priceLineVisible: false, lastValueVisible: false, title: 'EMA',
            });
            emaLine.setData(emaPoints);
        }

        // 10. Scroll to latest bars — show ~100 last candles with proper width
        robotEditChartInstance.applyOptions({
            timeScale: { barSpacing: 8, rightOffset: 5 }
        });
        robotEditChartInstance.timeScale().scrollToRealTime();

        // 11. Resize handling
        new ResizeObserver(() => {
            const w = container.offsetWidth;
            if (w > 0 && robotEditChartInstance) robotEditChartInstance.applyOptions({ width: w });
        }).observe(container);

        if (info) info.textContent = `${data.length} свечей · SAR(${sarStart}/${sarStep}/${sarMax}) · EMA(${emaPeriod})`;

    } catch(e) {
        console.error('[RobotChart]', e);
        if (info) info.textContent = 'Ошибка: ' + e.message;
    }
}

async function saveRobotEdit(i) {
    const r = robots[i];
    if (!r) return;
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
    
    // Отправляем параметры в стратегию
    try {
        await fetch('/strategy/grid-mm/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sarStart: parseFloat(r.sarStart),
                sarStep: parseFloat(r.sarStep),
                sarMax: parseFloat(r.sarMax),
                emaPeriod: parseInt(r.ema),
                gridStep: parseFloat(r.gridStep),
                gridSpread: parseFloat(r.gridSpread),
                maxGridLevels: parseInt(r.maxGrid),
                closePct: parseFloat(r.closePct),
                maxLots: parseInt(r.lots),
                commission: parseFloat(r.commission) || 0.90
            })
        });
        // Обновляем параметры для графика
        window._sarParams = { start: parseFloat(r.sarStart), step: parseFloat(r.sarStep), max: parseFloat(r.sarMax) };
        window._emaPeriod = parseInt(r.ema);
        // Пересчитываем индикаторы на графике
        if (candleSeries && sarSeries && emaSeries) {
            const candles = candleSeries.data();
            if (candles && candles.length > 0) {
                const bars = candles.map(c => ({ t: c.time, h: c.high, l: c.low, c: c.close }));
                const sarData = calcSAR(bars, window._sarParams.start, window._sarParams.step, window._sarParams.max);
                const emaData = calcEMA(bars, window._emaPeriod);
                if (sarData.length > 0) sarSeries.setData(sarData);
                if (emaData.length > 0) emaSeries.setData(emaData);
            }
        }
        addLog(nowTime(), 'INFO', `⚙️ Параметры стратегии обновлены: SAR(${r.sarStart}/${r.sarStep}/${r.sarMax}) EMA(${r.ema})`);
    } catch (e) {
        addLog(nowTime(), 'ERROR', `Ошибка обновления параметров: ${e.message}`);
    }
    
    saveRobots(); renderRobots();
    if (robotEditChartInstance) robotEditChartInstance.remove();
    robotEditChartInstance = null;
    el('robotEditPanel')?.remove();
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
});
