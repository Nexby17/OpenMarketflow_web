// === OpenMarketflow Web UI ===

let connection = null;
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let sarSeries = null;
let emaSeries = null;
let tradeMarkers = [];
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
    addLog(nowTime(), 'INFO', '🔌 Подключение к Финам... (ожидание до 30 сек)');
    el('brokerStatus').textContent = '⏳ Подключаюсь...';
    try {
        const resp = await fetch('/connect-broker', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token })
        });
        const data = await resp.json();
        if (resp.ok) {
            isConnected = true;
            el('brokerStatus').textContent = '✅ Подключён';
            el('startBotBtn').disabled = false;
            addLog(nowTime(), 'INFO', `✅ ${data.broker} подключён`);
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
    // Стратегия работает на 1м — ставим 1м TF для графика
    el('obTimeframe').value = '1';
    const tf = '1';
    initChart();
    loadCandles(ticker, tf);
    loadQuote(ticker);
    loadOrderBook(ticker);
    // Поллинг котировок + стакан + индикаторы
    if (window._quoteInterval) clearInterval(window._quoteInterval);
    window._quoteInterval = setInterval(() => {
        const t = el('obInstrument').value;
        loadQuote(t);
        loadOrderBook(t);
        loadStrategyIndicators();
    }, 2000);
    
    // Первая загрузка индикаторов
    loadStrategyIndicators();
}

function loadQuote(ticker) {
    fetch(`/api/quote?ticker=${ticker}`)
        .then(r => r.json())
        .then(q => {
            if (q.last > 0) el('obLast').textContent = q.last.toFixed(2);
            if (q.bid > 0) el('obBid').textContent = q.bid.toFixed(2);
            if (q.ask > 0) el('obAsk').textContent = q.ask.toFixed(2);
            if (q.spread > 0) el('obSpread').textContent = q.spread.toFixed(2);
        })
        .catch(() => {});
}

function loadOrderBook(ticker) {
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
        .catch(() => {});
}

function switchTimeframe() {
    const ticker = el('obInstrument').value;
    const tf = el('obTimeframe').value;
    loadCandles(ticker, tf);
}

// === Strategy Indicators on Chart ===
function loadStrategyIndicators() {
    fetch('/strategy/grid-mm/chart-data')
        .then(r => r.json())
        .then(data => {
            if (data.error) return;
            
            // SAR/EMA series
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
            
            // Trade markers
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
            
            // Update metrics
            if (data.current) updateIndicatorMetrics(data.current);
        })
        .catch(() => {});
}

function toChartTime(isoStr) {
    // ISO 8601 → unix timestamp for lightweight-charts
    const d = new Date(isoStr);
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

// === Alfa-Direct ===
let alfaUrl = 'http://localhost:15200';
let alfaQuotes = {};

async function alfaConnect() {
    alfaUrl = el('alfaBridgeUrl')?.value || 'http://localhost:15200';
    el('alfaStatusText').textContent = '⏳ Подключаюсь...';
    try {
        const resp = await fetch(alfaUrl + '/status');
        const data = await resp.json();
        el('alfaStatusText').textContent = data.connected ? '✅ Подключён' : '❌ Нет связи';
        el('alfaStatusText').style.color = data.connected ? 'var(--green)' : 'var(--red)';
        el('alfaAuthStatus').textContent = data.authorized ? '✅ Авторизован' : '❌';
        el('alfaAuthStatus').className = 'metric-value ' + (data.authorized ? 'green' : 'red');
        el('alfaAccount').textContent = data.account || '—';
        el('alfaAssets').textContent = data.assetsLoaded || 0;
        el('alfaSign').textContent = data.authorized ? 'Готова' : 'Нет';
        if (data.connected) {
            alfaLoadPositions();
            alfaLoadOrders();
            if (!window._alfaInterval) window._alfaInterval = setInterval(alfaRefresh, 5000);
        }
    } catch (e) {
        el('alfaStatusText').textContent = '❌ ' + e.message;
        el('alfaStatusText').style.color = 'var(--red)';
    }
}

async function alfaSearch() {
    const q = el('alfaSearchInput')?.value?.trim();
    if (!q) return;
    try {
        const resp = await fetch(alfaUrl + '/instruments?name=' + encodeURIComponent(q));
        const data = await resp.json();
        const tbody = el('alfaSearchBody');
        if (!Array.isArray(data)) { tbody.innerHTML = '<tr><td colspan=8>' + JSON.stringify(data) + '</td></tr>'; return; }
        const groups = {1:'Акции',2:'Облигации',3:'Фонды',4:'Фьючерсы',5:'Опционы'};
        tbody.innerHTML = data.map(a => {
            const instrs = (a.instruments || []).map(i => 
                `<div>idFi=<b>${i.idFi}</b> board=${i.idMarketBoard} ${i.rcode} ${i.isLiquid ? '✅' : ''}</div>`
            ).join('');
            const liquidFi = (a.instruments || []).find(i => i.isLiquid);
            const idFi = liquidFi ? liquidFi.idFi : (a.instruments?.[0]?.idFi || 0);
            return `<tr>
                <td><b>${a.ticker}</b></td>
                <td>${a.name}</td>
                <td>${groups[a.idObjectGroup] || a.idObjectGroup}</td>
                <td>${a.idObject}</td>
                <td>${instrs}</td>
                <td>${liquidFi?.rcode || ''}</td>
                <td>${liquidFi?.isLiquid ? '✅' : ''}</td>
                <td><button class="btn btn-secondary btn-sm" onclick="alfaGetQuote(${idFi},'${a.ticker}')">📊</button></td>
            </tr>`;
        }).join('');
    } catch (e) { el('alfaSearchBody').innerHTML = '<tr><td colspan=8>Ошибка: ' + e.message + '</td></tr>'; }
}

async function alfaGetQuote(idFi, ticker) {
    try {
        const resp = await fetch(alfaUrl + '/fininfo?idFi=' + idFi);
        const data = await resp.json();
        alfaQuotes[ticker] = { ...data, ticker, idFi };
        alfaRenderQuotes();
    } catch (e) { console.error(e); }
}

function alfaRenderQuotes() {
    const tbody = el('alfaQuotesBody');
    tbody.innerHTML = Object.values(alfaQuotes).map(q => `<tr>
        <td><b>${q.ticker}</b></td>
        <td>${q.idFi}</td>
        <td class="accent">${q.last || '—'}</td>
        <td class="green">${q.bid || '—'}</td>
        <td class="red">${q.ask || '—'}</td>
        <td>${q.high || '—'}</td>
        <td>${q.low || '—'}</td>
        <td>${q.volume || '—'}</td>
    </tr>`).join('');
}

async function alfaLoadPositions() {
    try {
        const resp = await fetch(alfaUrl + '/positions');
        const data = await resp.json();
        if (Array.isArray(data) && data.length > 0) {
            let html = '<table class="data-table"><thead><tr><th>Инструмент</th><th>Позиция</th><th>Покупки</th><th>Продажи</th><th>PnL</th></tr></thead><tbody>';
            data.forEach(p => {
                html += `<tr><td>${p.IdObject || p.idObject || ''}</td><td>${p.TorgPos || p.BackPos || ''}</td><td>${p.DailyBuyQuantity || p.BuyQty || ''}</td><td>${p.DailySellQuantity || p.SellQty || ''}</td><td>${p.DailyPL || p.TrdPL || ''}</td></tr>`;
            });
            html += '</tbody></table>';
            el('alfaPositions').innerHTML = html;
        } else {
            el('alfaPositions').innerHTML = '<i>Нет открытых позиций</i>';
        }
    } catch (e) { el('alfaPositions').innerHTML = 'Ошибка: ' + e.message; }
}

async function alfaLoadOrders() {
    try {
        const resp = await fetch(alfaUrl + '/orders');
        const data = await resp.json();
        if (Array.isArray(data) && data.length > 0) {
            let html = '<table class="data-table"><thead><tr><th>#</th><th>Инструмент</th><th>Напр.</th><th>Кол-во</th><th>Цена</th><th>Статус</th></tr></thead><tbody>';
            data.forEach(o => {
                const dir = (o.BuySell || o.buySell) == 1 ? 'Buy' : 'Sell';
                html += `<tr><td>${o.NumEDocument || o.numEDocument || ''}</td><td>${o.IdObject || o.idObject || ''}</td><td>${dir}</td><td>${o.Quantity || o.quantity || ''}</td><td>${o.LimitPrice || o.limitPrice || o.Price || ''}</td><td>${o.IdOrderStatus || o.status || ''}</td></tr>`;
            });
            html += '</tbody></table>';
            el('alfaOrders').innerHTML = html;
        } else {
            el('alfaOrders').innerHTML = '<i>Нет заявок</i>';
        }
    } catch (e) { el('alfaOrders').innerHTML = 'Ошибка: ' + e.message; }
}

async function alfaRefresh() {
    try {
        // Обновляем котировки
        for (const [ticker, q] of Object.entries(alfaQuotes)) {
            if (q.idFi) alfaGetQuote(q.idFi, ticker);
        }
        alfaLoadPositions();
        alfaLoadOrders();
    } catch { }
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
    setInterval(arbRefreshStatus, 5000);

    // Автозагрузка свечей для дефолтного инструмента
    setTimeout(() => {
        initChart();
        switchOrderBookInstrument();
    }, 500);
});
