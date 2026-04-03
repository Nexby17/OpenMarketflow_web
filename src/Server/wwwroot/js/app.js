// === OpenMarketflow Web UI ===

let connection = null;
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let equityChart = null;
let equitySeries = null;
let isConnected = false;
let isBotRunning = false;
const MAX_LOG = 200;

// === Tabs ===
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(tab.dataset.tab).classList.add('active');
        if (tab.dataset.tab === 'orderbook') initChart();
        if (tab.dataset.tab === 'monitoring') initEquityChart();
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
        el('monEquity').textContent = fmt(equity);
    });

    connection.on('OnOrderBookUpdate', snapshot => {
        renderOrderBook(snapshot);
    });

    connection.on('OnQuoteUpdate', quote => {
        updateQuote(quote);
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

// === Actions ===
async function connectBroker() {
    const token = localStorage.getItem('finamToken') || '';
    if (!token) {
        addLog(nowTime(), 'ERROR', '⚠ Токен Финам не указан. Укажите во вкладке Настройки.');
        return;
    }
    addLog(nowTime(), 'INFO', '🔌 Подключение к Финам...');
    try {
        const resp = await fetch('/connect-broker', { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            isConnected = true;
            el('brokerStatus').textContent = '✅ Подключён';
            el('startBotBtn').disabled = false;
            addLog(nowTime(), 'INFO', `✅ ${data.broker} подключён`);
            fetchStatus();
        } else {
            addLog(nowTime(), 'ERROR', `❌ ${data.error || 'Не удалось подключиться'}`);
        }
    } catch (e) {
        addLog(nowTime(), 'ERROR', `❌ ${e.message}`);
    }
}

async function startBot() {
    if (!connection) return;
    const strategy = el('strategySelect').value;
    const ticker = el('instrumentSelect').value;
    try {
        await connection.invoke('StartStrategy', strategy, ticker, {});
        isBotRunning = true;
        el('stopBotBtn').disabled = false;
        addLog(nowTime(), 'INFO', `🤖 Запущен: ${strategy} на ${ticker}`);
    } catch (e) {
        addLog(nowTime(), 'ERROR', `❌ ${e.message}`);
    }
}

async function stopBot() {
    if (!connection) return;
    try {
        const strategy = el('strategySelect').value;
        await connection.invoke('StopStrategy', strategy);
        isBotRunning = false;
        el('stopBotBtn').disabled = true;
        addLog(nowTime(), 'INFO', '⏹ Бот остановлен');
    } catch (e) {
        addLog(nowTime(), 'ERROR', `❌ ${e.message}`);
    }
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
    } catch (e) { }
}

function updateStatus(s) {
    if (!s) return;
    // Поддержка обоих форматов (camelCase и PascalCase)
    const v = (a, b) => s[a] ?? s[b] ?? 0;
    
    el('balance').textContent = fmt(v('balance','Balance'));
    el('monBalance').textContent = fmt(v('balance','Balance'));
    el('monEquity').textContent = fmt(v('equity','Equity'));
    
    const pnlToday = v('todayPnL','TodayPnL');
    const pnlTotal = v('totalPnL','TotalPnL');
    el('monPnlToday').textContent = fmtPnl(pnlToday);
    el('monPnlToday').className = 'metric-value ' + (pnlToday > 0 ? 'green' : pnlToday < 0 ? 'red' : '');
    el('monPnlTotal').textContent = fmtPnl(pnlTotal);
    el('monPnlTotal').className = 'metric-value ' + (pnlTotal > 0 ? 'green' : pnlTotal < 0 ? 'red' : '');
    
    el('monTrades').textContent = v('todayTrades','TodayTrades');
    el('monPositions').textContent = v('openPositions','OpenPositions');
    
    const brokerStatus = s.brokerStatus || s.BrokerStatus || '';
    const connected = s.isConnectedToBroker ?? s.IsConnectedToBroker ?? false;
    if (connected) {
        isConnected = true;
        el('brokerStatus').textContent = '✅ Подключён';
        el('startBotBtn').disabled = false;
        el('statusIndicator').className = 'status-dot green';
        el('statusText').textContent = `Подключён | ${brokerStatus}`;
    }
}

// === Orderbook ===
let orderBookData = {};

function renderOrderBook(snapshot) {
    if (!snapshot) return;
    const ladder = el('orderbookLadder');
    const entries = snapshot.entries || snapshot.Entries || [];
    if (entries.length === 0) return;

    const lastPrice = snapshot.lastPrice || snapshot.LastPrice || 0;
    el('obLast').textContent = lastPrice.toFixed(2);
    el('obBid').textContent = (snapshot.bestBid || snapshot.BestBid || 0).toFixed(2);
    el('obAsk').textContent = (snapshot.bestAsk || snapshot.BestAsk || 0).toFixed(2);
    const spread = (snapshot.bestAsk || snapshot.BestAsk || 0) - (snapshot.bestBid || snapshot.BestBid || 0);
    el('obSpread').textContent = spread.toFixed(2);

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
    el('obLast').textContent = (q.last || q.Last || 0).toFixed(2);
    el('obBid').textContent = (q.bid || q.Bid || 0).toFixed(2);
    el('obAsk').textContent = (q.ask || q.Ask || 0).toFixed(2);
    const spread = (q.ask || q.Ask || 0) - (q.bid || q.Bid || 0);
    el('obSpread').textContent = spread.toFixed(2);
}

function updatePosition(pos) {
    // Обновляем таблицу позиций
    const tbody = el('positionsTable');
    const pnl = pos.pnL || pos.PnL || 0;
    const cls = pnl > 0 ? 'pnl-positive' : pnl < 0 ? 'pnl-negative' : 'pnl-zero';
    // Simplified: just replace all
    tbody.innerHTML = `<tr>
        <td>${pos.ticker || pos.Ticker}</td>
        <td>${pos.direction || pos.Direction}</td>
        <td>${(pos.entryPrice || pos.EntryPrice || 0).toFixed(2)}</td>
        <td>${pos.volume || pos.Volume}</td>
        <td class="${cls}">${pnl.toFixed(2)}</td>
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
    chart = LightweightCharts.createChart(container, {
        width: container.offsetWidth,
        height: container.offsetHeight || 400,
        layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
        grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        timeScale: { timeVisible: true, secondsVisible: false },
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

    new ResizeObserver(() => {
        chart.applyOptions({ width: container.offsetWidth, height: container.offsetHeight || 400 });
    }).observe(container);
}

function loadCandles(ticker, tf) {
    const tfMinutes = parseInt(tf) || 5;
    const days = tfMinutes <= 5 ? 3 : tfMinutes <= 60 ? 7 : 30;

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
            el('obCandleCount').textContent = `${candles.length} свечей`;
            addLog(nowTime(), 'INFO', `📊 ${ticker}: ${candles.length} свечей (${tfMinutes}м)`);
        })
        .catch(e => addLog(nowTime(), 'ERROR', `Свечи: ${e.message}`));
}

function switchOrderBookInstrument() {
    const ticker = el('obInstrument').value;
    const tf = el('obTimeframe').value;
    initChart();
    loadCandles(ticker, tf);
    if (connection) {
        connection.invoke('SubscribeOrderBook', ticker).catch(() => {});
    }
}

function switchTimeframe() {
    const ticker = el('obInstrument').value;
    const tf = el('obTimeframe').value;
    loadCandles(ticker, tf);
}

// === Equity Chart ===
function initEquityChart() {
    if (equityChart) return;
    const container = el('equityChart');
    if (!container || container.offsetWidth === 0) { setTimeout(initEquityChart, 100); return; }
    equityChart = LightweightCharts.createChart(container, {
        width: container.offsetWidth, height: 250,
        layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
        grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
    });
    equitySeries = equityChart.addLineSeries({ color: '#22C55E', lineWidth: 2 });
    new ResizeObserver(() => {
        equityChart.applyOptions({ width: container.offsetWidth });
    }).observe(container);
}

// === Settings ===
function saveSettings() {
    const finam = el('finamToken').value;
    const alfa = el('alfaToken').value;
    if (finam) localStorage.setItem('finamToken', finam);
    if (alfa) localStorage.setItem('alfaToken', alfa);
    el('settingsStatus').textContent = '✅ Сохранено';
    updateTokenStatus();
}

function loadSettings() {
    const finam = localStorage.getItem('finamToken');
    const alfa = localStorage.getItem('alfaToken');
    if (finam) el('finamToken').value = finam;
    if (alfa) el('alfaToken').value = alfa;
    updateTokenStatus();
}

function updateTokenStatus() {
    const finam = localStorage.getItem('finamToken');
    const alfa = localStorage.getItem('alfaToken');
    el('finamTokenStatus').textContent = finam ? `✅ ${finam.slice(0,4)}...${finam.slice(-4)} (${finam.length} симв.)` : '❌ Не задан';
    el('alfaTokenStatus').textContent = alfa ? `✅ ${alfa.slice(0,4)}...${alfa.slice(-4)} (${alfa.length} симв.)` : '❌ Не задан';
}

// === Backtest ===
async function runBacktest() {
    el('testStatus').textContent = '⏳ Запуск...';
    // TODO: implement via SignalR or REST
    el('testStatus').textContent = '⚠ Бэктест пока доступен только через WPF приложение';
}

// === Strategy list ===
function renderStrategies() {
    const strategies = [
        { name: 'PSAR Grid MM (1 мин)', ticker: 'SiM6', state: 'stopped', pnl: 0, trades: 0 },
        { name: 'PSAR+EMA Combo (5 мин)', ticker: 'SiM6', state: 'stopped', pnl: 0, trades: 0 },
        { name: 'VStop Pure (30 сек, бумага)', ticker: 'SiM6', state: 'stopped', pnl: 0, trades: 0 },
    ];
    const list = el('strategyList');
    list.innerHTML = strategies.map(s => {
        const dotColor = s.state === 'running' ? 'var(--green)' : s.state === 'paused' ? 'var(--yellow)' : 'var(--red)';
        const pnlCls = s.pnl > 0 ? 'pnl-positive' : s.pnl < 0 ? 'pnl-negative' : 'pnl-zero';
        return `<div class="strategy-item">
            <div class="dot" style="background:${dotColor}"></div>
            <div class="name">${s.name}</div>
            <div class="ticker">${s.ticker}</div>
            <div class="pnl ${pnlCls}">${s.pnl > 0 ? '+' : ''}${s.pnl.toFixed(0)} ₽</div>
            <div>${s.trades} сделок</div>
            <button class="btn btn-primary btn-sm" onclick="startStrategy('${s.name}')">▶</button>
            <button class="btn btn-secondary btn-sm" onclick="pauseStrategy('${s.name}')">⏸</button>
            <button class="btn btn-danger btn-sm" onclick="stopStrategy('${s.name}')">⏹</button>
        </div>`;
    }).join('');
}

async function startStrategy(name) {
    if (!connection) return;
    try {
        await connection.invoke('StartStrategy', name, el('obInstrument')?.value || 'SiM6', {});
        addLog(nowTime(), 'INFO', `▶ ${name} запущена`);
    } catch(e) { addLog(nowTime(), 'ERROR', e.message); }
}
async function pauseStrategy(name) {
    if (!connection) return;
    try { await connection.invoke('PauseStrategy', name); addLog(nowTime(), 'INFO', `⏸ ${name}`); }
    catch(e) { addLog(nowTime(), 'ERROR', e.message); }
}
async function stopStrategy(name) {
    if (!connection) return;
    try { await connection.invoke('StopStrategy', name); addLog(nowTime(), 'INFO', `⏹ ${name}`); }
    catch(e) { addLog(nowTime(), 'ERROR', e.message); }
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

// === Init ===
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    initSignalR();
    renderStrategies();

    // Default dates for backtest
    const today = new Date().toISOString().split('T')[0];
    const monthAgo = new Date(Date.now() - 30 * 86400000).toISOString().split('T')[0];
    el('testFrom').value = monthAgo;
    el('testTo').value = today;

    // Auto-refresh status
    setInterval(fetchStatus, 10000);

    // Автозагрузка свечей для дефолтного инструмента
    setTimeout(() => {
        initChart();
        switchOrderBookInstrument();
    }, 500);
});
