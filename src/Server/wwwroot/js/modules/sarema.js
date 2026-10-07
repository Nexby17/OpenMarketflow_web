// ============================================================================
// === F-061: SAREMA — Роботы Акции (SAR×EMA H1, per-ticker инстансы) ===
// === 1 робот = 1 акция; экземпляры на портах 5084-5089/5094-5097. ===
// === Лимиты Дмитрия: max_qty (штуки, null=∞, 0=запрет), max_value_rub (₽). ===
// === Панель параметров — popup по образцу OF/VP-панелей (двойной клик по строке): ===
// === метрики + параметры + торговый журнал внизу (сводка, фильтры, позиции). ===
// ============================================================================

// Регистр экземпляров: добавляется строкой при подъёме нового робота.
const SAR_INSTANCES = {
    sber: { port: 5084, ticker: 'SBER', label: 'SBER' },
    gazp: { port: 5085, ticker: 'GAZP', label: 'GAZP' },
    lkoh: { port: 5086, ticker: 'LKOH', label: 'LKOH' },
    rosn: { port: 5087, ticker: 'ROSN', label: 'ROSN' },
    tatn: { port: 5088, ticker: 'TATN', label: 'TATN' },
    gmkn: { port: 5089, ticker: 'GMKN', label: 'GMKN' },
    nvtk: { port: 5094, ticker: 'NVTK', label: 'NVTK' },
    plzl: { port: 5095, ticker: 'PLZL', label: 'PLZL' },
    mtss: { port: 5096, ticker: 'MTSS', label: 'MTSS' },
    sngs: { port: 5097, ticker: 'SNGS', label: 'SNGS' },
};
const SAR_INSTANCE_ORDER = ['sber', 'gazp'];   // первая волна; остальные вписываются по мере запуска
SAR_INSTANCE_ORDER.forEach(k => { window['sarRobot_' + k] = null; window['_sarWasOnline_' + k] = false; });
let sarPanelInst = null;            // экземпляр, чей popup открыт (null = закрыт)
let _sarTimer = null;
let _sarJournalTrades = [];
let _sarJournalPositions = [];

function sarCfg(inst) { return SAR_INSTANCES[inst]; }
function sarApi(inst) { return 'http://' + window.location.hostname + ':' + sarCfg(inst).port; }

function sarLog(msg, level) {
    if (typeof arbPyLog === 'function') arbPyLog(msg, level, 'sarLogContainer');
    else console.log('[SAREMA]', level, msg);
}

// ============================================================================
// === POPUP-ПАНЕЛЬ (как у OF/VP-роботов): двойной клик по строке ===
// ============================================================================

function sarSwitchInstance(inst) {
    // повторный двойной клик по открытой панели — закрыть (как у pythonRobotEditPanel)
    const existing = document.getElementById('sarEditPanel');
    if (existing && sarPanelInst === inst) { existing.remove(); sarPanelInst = null; return; }
    if (existing) existing.remove();

    sarPanelInst = inst;
    const cfg = sarCfg(inst);
    const div = document.createElement('div');
    div.id = 'sarEditPanel';
    div.className = 'card';
    div.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:1000;width:960px;max-height:90vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5)';
    div.innerHTML = `
        <div class="card-header row gap-8" style="display:flex;align-items:center">
            📈 SAREMA ${cfg.label} — SAR×EMA H1 <span class="badge" style="background:#2e7d32">PYTHON</span>
            <span class="text-secondary" style="font-size:12px">лимиты владельца · сигнальные параметры read-only</span>
            <span style="flex:1"></span>
            <button class="btn btn-primary btn-sm" onclick="sarSaveParams()">💾 Сохранить</button>
            <button class="btn btn-secondary btn-sm" onclick="document.getElementById('sarEditPanel')?.remove(); sarPanelInst=null;">✖ Закрыть</button>
        </div>
        <div style="padding:12px">
            <!-- Живые метрики -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px" id="sarLiveMetrics"></div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div style="font-weight:700;margin-bottom:8px">Лимиты и риск</div>
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:8px">
                <div class="metric-card"><div class="metric-label">Макс. штук (пусто = ∞)</div><input id="sarMaxQty" class="input" placeholder="без лимита" style="width:90px"></div>
                <div class="metric-card"><div class="metric-label">Макс. стоимость ₽ (пусто = ∞)</div><input id="sarMaxValue" class="input" placeholder="без лимита" style="width:110px"></div>
                <div class="metric-card"><div class="metric-label">База R (₽)</div><input id="sarBase" class="input" style="width:110px"></div>
                <div class="metric-card"><div class="metric-label">Риск на сделку %</div><input id="sarRiskPct" class="input" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Kill-switch день (R, пусто = выкл)</div><input id="sarDayKill" class="input" placeholder="выкл" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Лимит участия (доля объёма)</div><input id="sarLiqFrac" class="input" style="width:70px"></div>
            </div>
            <div class="text-secondary" style="font-size:12px;margin-bottom:6px">Сигнальные (read-only): SAR 0.02/0.2/0.2 · EMA12 · стоп 3×ATR · H1 · std-полярность · трейлинг PSAR · овернайты разрешены</div>
            <div id="sarPanelMsg" class="text-secondary" style="font-size:12.5px;min-height:18px"></div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал (как у OF/VP) -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>Журнал торговых операций</strong>
            </div>
            <div id="sarJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
                <select id="sarJournalFilter" class="input" style="width:120px" onchange="sarRenderJournal()">
                    <option value="all">Все позиции</option>
                    <option value="LONG">Лонги</option>
                    <option value="SHORT">Шорты</option>
                    <option value="win">Прибыльные</option>
                    <option value="loss">Убыточные</option>
                </select>
                <span style="color:#9CA3AF;font-size:13px">с</span>
                <input id="sarJournalDateFrom" class="input" type="date" style="width:130px" onchange="sarLoadJournal()">
                <span style="color:#9CA3AF;font-size:13px">по</span>
                <input id="sarJournalDateTo" class="input" type="date" style="width:130px" onchange="sarLoadJournal()">
                <button class="btn btn-secondary btn-sm" onclick="sarLoadJournal()">🔄</button>
            </div>
            <div style="max-height:320px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:8px;text-align:left">Дата</th>
                            <th style="padding:8px;text-align:left">Время входа</th>
                            <th style="padding:8px;text-align:left">Время выхода</th>
                            <th style="padding:8px;text-align:center">Направление</th>
                            <th style="padding:8px;text-align:right">Цена входа</th>
                            <th style="padding:8px;text-align:right">Цена выхода</th>
                            <th style="padding:8px;text-align:right">Штуки</th>
                            <th style="padding:8px;text-align:right">PnL</th>
                            <th style="padding:8px;text-align:right">Накопл.</th>
                        </tr>
                    </thead>
                    <tbody id="sarJournalBody" style="background:var(--bg)"><tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">Загрузка...</td></tr></tbody>
                </table>
            </div>
            <div id="sarJournalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px"></div>
        </div>
    `;
    document.body.appendChild(div);

    // загрузка: очистка → черновик → живой /params (только этого робота)
    sarLoadPanelData(inst);
    // журнал + живые метрики (таймер панели)
    if (window._sarPanelTimer) clearInterval(window._sarPanelTimer);
    window._sarPanelTimer = setInterval(() => sarUpdateLiveMetrics(inst), 2000);
    sarUpdateLiveMetrics(inst);
    setTimeout(() => sarLoadJournal(inst), 300);
}

async function sarLoadPanelData(inst) {
    const cfg = sarCfg(inst);
    const msg = document.getElementById('sarPanelMsg');
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
    // дефолты
    set('sarMaxQty', ''); set('sarMaxValue', ''); set('sarBase', 1000000);
    set('sarRiskPct', ''); set('sarDayKill', ''); set('sarLiqFrac', 0.05);
    // черновик этого экземпляра (оффлайн-сохранение)
    try {
        const raw = localStorage.getItem('sarSavedParams_' + inst);
        if (raw) {
            const d = JSON.parse(raw);
            set('sarMaxQty', d.max_qty ?? '');
            set('sarMaxValue', d.max_value_rub ?? '');
            set('sarBase', d.base ?? 1000000);
            set('sarRiskPct', d.risk_pct != null ? (d.risk_pct * 100).toFixed(1) : '');
            set('sarDayKill', d.day_kill_R ?? '');
            set('sarLiqFrac', d.liq_frac ?? 0.05);
        }
    } catch (e) {}
    // живой робот — источник истины (только его /params)
    try {
        const resp = await fetch(sarApi(inst) + '/params', {signal: AbortSignal.timeout(2500)});
        if (resp.ok) {
            const p = await resp.json();
            set('sarMaxQty', p.max_qty ?? '');
            set('sarMaxValue', p.max_value_rub ?? '');
            set('sarBase', p.base ?? 1000000);
            set('sarRiskPct', p.risk_pct != null ? (p.risk_pct * 100).toFixed(1) : '');
            set('sarDayKill', p.day_kill_R ?? '');
            set('sarLiqFrac', p.liq_frac ?? 0.05);
            if (msg) msg.textContent = '';
        } else if (msg) {
            msg.textContent = '⚠️ Робот оффлайн — показаны сохранённые значения (применятся при его старте)';
        }
    } catch (e) {
        if (msg) msg.textContent = '⚠️ Робот оффлайн — показаны сохранённые значения (применятся при его старте)';
    }
}

// ---------- живые метрики в popup ----------
async function sarUpdateLiveMetrics(inst) {
    const box = document.getElementById('sarLiveMetrics');
    if (!box || sarPanelInst !== inst) return;
    const cfg = sarCfg(inst);
    const st = window['sarRobot_' + inst];
    let mode = 'оффлайн', modeColor = '#ef6461';
    let dirText = '—', entryText = '—', stopText = '—', pnlText = '—', priceSrc = '', lastBar = '—';
    if (st) {
        mode = st.mode === 'running' ? '▶ работает' : (st.mode === 'paused' ? '⏸ пауза' : '■ остановлен');
        modeColor = st.mode === 'running' ? '#3ecf8e' : '#9CA3AF';
        const p = st.position || {};
        dirText = p.dir === 1 ? 'LONG' : (p.dir === -1 ? 'SHORT' : 'ФЛЭТ');
        dirText = p.dir ? `${dirText} ${p.qty} шт` : 'ФЛЭТ';
        entryText = p.entry ? p.entry.toFixed(2) : '—';
        stopText = p.stop ? p.stop.toFixed(2) : '—';
        priceSrc = p.priceSource ? ` (${p.priceSource})` : '';
        const pnl = st.pnl ? st.pnl.total : 0;
        pnlText = (pnl >= 0 ? '+' : '') + pnl.toFixed(0) + ' ₽';
        lastBar = st.last_bar ? String(st.last_bar).replace('T', ' ').slice(0, 16) : '—';
    }
    box.innerHTML = `
        <div class="metric-card"><div class="metric-label">Режим</div><div style="font-size:1.05em;font-weight:700;color:${modeColor}">${mode}</div></div>
        <div class="metric-card"><div class="metric-label">Позиция</div><div style="font-size:1.05em;font-weight:700">${dirText}</div></div>
        <div class="metric-card"><div class="metric-label">Вход</div><div style="font-size:1.05em;font-weight:700">${entryText}</div></div>
        <div class="metric-card"><div class="metric-label">Стоп</div><div style="font-size:1.05em;font-weight:700">${stopText}</div></div>
        <div class="metric-card"><div class="metric-label">PnL (брокер)${priceSrc}</div><div style="font-size:1.05em;font-weight:700;color:${pnlText.startsWith('-') ? 'var(--red)' : 'var(--green)'}">${pnlText}</div></div>
        <div class="metric-card"><div class="metric-label">Последний H1</div><div style="font-size:0.95em;font-weight:600">${lastBar}</div></div>
    `;
}

// ---------- сохранение лимитов (только открытый инстанс — урок 22.09) ----------
async function sarSaveParams() {
    const inst = sarPanelInst;
    if (!inst) { return; }
    const cfg = sarCfg(inst);
    const sentFor = inst;
    const numOrNull = (id, integer) => {
        const e = document.getElementById(id);
        const raw = (e ? e.value : '').trim();
        if (raw === '') return null;                       // пусто = без лимита
        const n = parseFloat(String(raw).replace(',', '.'));
        if (!isFinite(n) || n < 0) return undefined;       // ошибка валидации
        return integer ? Math.floor(n) : n;
    };
    const maxQty = numOrNull('sarMaxQty', true);
    const maxValue = numOrNull('sarMaxValue', false);
    const baseRaw = (document.getElementById('sarBase')?.value || '').trim();
    const riskRaw = (document.getElementById('sarRiskPct')?.value || '').trim().replace(',', '.');
    const killRaw = (document.getElementById('sarDayKill')?.value || '').trim().replace(',', '.');
    const liqRaw = (document.getElementById('sarLiqFrac')?.value || '').trim().replace(',', '.');
    const msg = document.getElementById('sarPanelMsg');
    if (maxQty === undefined || maxValue === undefined) { if (msg) msg.textContent = '❌ Лимиты: число ≥ 0 или пусто (без лимита)'; return; }
    if (riskRaw !== '' && (!isFinite(parseFloat(riskRaw)) || parseFloat(riskRaw) <= 0 || parseFloat(riskRaw) > 10)) { if (msg) msg.textContent = '❌ Риск %: (0, 10]'; return; }
    if (killRaw !== '' && !isNaN(parseFloat(killRaw)) && parseFloat(killRaw) >= 0) { if (msg) msg.textContent = '❌ Kill-switch: отрицательный R или пусто (выкл)'; return; }
    const patch = {
        max_qty: maxQty,
        max_value_rub: maxValue,
        base: baseRaw !== '' ? parseFloat(baseRaw.replace(',', '.')) : undefined,
        risk_pct: riskRaw !== '' ? parseFloat(riskRaw) / 100 : undefined,
        day_kill_R: killRaw === '' ? null : parseFloat(killRaw),
        liq_frac: liqRaw === '' ? null : parseFloat(liqRaw),
    };
    Object.keys(patch).forEach(k => patch[k] === undefined && delete patch[k]);
    try {
        const resp = await fetch(sarApi(inst) + '/params', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(patch), signal: AbortSignal.timeout(4000)
        });
        if (!resp.ok) {
            const e = await resp.json().catch(() => ({}));
            const errs = e.detail && e.detail.errors ? e.detail.errors.join('; ') : ('HTTP ' + resp.status);
            if (msg) msg.textContent = '❌ Сервер отклонил: ' + errs;
            return;
        }
        if (sarPanelInst !== sentFor) return;      // панель уже переключили — не пугаем чужим сообщением
        if (msg) msg.textContent = '✅ Параметры сохранены (' + cfg.label + ')';
        sarLog('SAREMA[' + cfg.label + ']: параметры обновлены: ' + JSON.stringify(patch), 'INFO');
    } catch (e) {
        // робот оффлайн: сохраняем в localStorage ЭТОГО экземпляра, приём при первом /status (паттерн OF-BR)
        localStorage.setItem('sarSavedParams_' + inst, JSON.stringify(patch));
        if (sarPanelInst === sentFor && msg) msg.textContent = '⚠️ Робот оффлайн — параметры применятся при его старте';
    }
}

// ---------- управление ----------
async function sarAction(inst, action) {
    const cfg = sarCfg(inst);
    try {
        const resp = await fetch(sarApi(inst) + '/' + action, {method: 'POST', signal: AbortSignal.timeout(3000)});
        if (!resp.ok) { sarLog('SAREMA[' + cfg.label + ']: ' + action + ' → HTTP ' + resp.status, 'ERROR'); return; }
        sarLog('SAREMA[' + cfg.label + ']: ' + action + ' OK', 'INFO');
    } catch (e) {
        sarLog('SAREMA[' + cfg.label + ']: ' + action + ' недоступен (робот оффлайн?)', 'ERROR');
    }
    sarPollAll();
}

// ---------- polling + строка ----------
async function sarPollInstance(inst) {
    const cfg = sarCfg(inst);
    try {
        const resp = await fetch(sarApi(inst) + '/status', {signal: AbortSignal.timeout(2000)});
        window['sarRobot_' + inst] = await resp.json();
        if (!window['_sarWasOnline_' + inst]) {
            window['_sarWasOnline_' + inst] = true;
            const saved = localStorage.getItem('sarSavedParams_' + inst);
            if (saved) {
                try {
                    await fetch(sarApi(inst) + '/params', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: saved});
                    localStorage.removeItem('sarSavedParams_' + inst);
                    sarLog('SAREMA[' + cfg.label + ']: параметры из localStorage применены', 'INFO');
                } catch (e) {}
            }
        }
    } catch (e) {
        window['sarRobot_' + inst] = null;
        window['_sarWasOnline_' + inst] = false;
    }
}

async function sarPollAll() {
    for (const inst of SAR_INSTANCE_ORDER) await sarPollInstance(inst);
    sarRenderTable();
}

function sarRenderTable() {
    const tbody = document.getElementById('sarRobotsTable');
    if (!tbody) return;
    const noMsg = document.getElementById('sarNoRobots');
    let rows = '';
    for (const inst of SAR_INSTANCE_ORDER) {
        const cfg = sarCfg(inst);
        const st = window['sarRobot_' + inst];
        const activeBg = (sarPanelInst === inst) ? ' style="background:rgba(79,143,247,.12)"' : '';
        if (!st) {
            if (noMsg) noMsg.style.display = '';
            rows += `<tr data-inst="${inst}"${activeBg} style="cursor:pointer" title="Двойной клик — панель робота (доступна оффлайн)">
                <td><strong>${cfg.label}</strong></td>
                <td class="red">● оффлайн</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>
                <td><span class="text-secondary">нет связи :${cfg.port}</span></td>
            </tr>`;
            continue;
        }
        if (noMsg) noMsg.style.display = 'none';
        const mode = st.mode || 'stopped';
        const modeCls = mode === 'running' ? 'green' : 'red';
        const modeText = mode === 'running' ? '▶ работает' : (mode === 'paused' ? '⏸ пауза' : '■ остановлен');
        const p = st.position || {};
        const dirText = p.dir === 1 ? 'LONG' : (p.dir === -1 ? 'SHORT' : 'ФЛЭТ');
        const posText = p.dir ? `${dirText} ${p.qty} шт` : 'ФЛЭТ';
        const priceText = p.entry ? `${p.entry.toFixed(2)} (${st.position.priceSource || 'n/a'})` : '—';
        const stopText = p.stop ? p.stop.toFixed(2) : '—';
        const pnl = st.pnl ? st.pnl.total : 0;
        const lim = st.limits || {};
        const limMaxQty = lim.max_qty ?? '∞';
        const limMaxVal = lim.max_value_rub != null ? Number(lim.max_value_rub).toFixed(0) : '∞';
        const limUsed = `${lim.qty_used ?? 0}/${limMaxQty} шт · ${(lim.value_used ?? 0).toFixed(0)}/${limMaxVal} ₽`;
        const risk = st.risk || {};
        const killText = risk.day_kill_R != null ? `kill ${risk.day_kill_R}R` : 'kill выкл';
        const online = window['_sarWasOnline_' + inst];
        rows += `<tr data-inst="${inst}"${activeBg} style="cursor:pointer" title="Двойной клик — панель робота с торговым журналом">
            <td><strong>${cfg.label}</strong> <span class="badge" style="background:#2e7d32">SAR</span></td>
            <td class="${modeCls}">${modeText}</td>
            <td>${posText}</td>
            <td>${p.entry ? p.entry.toFixed(2) : '—'}</td>
            <td>${stopText}</td>
            <td>${priceText}</td>
            <td>${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)} ₽</td>
            <td style="font-size:12px">${limUsed}<br><span class="text-secondary">${killText} · risk ${(risk.risk_pct * 100).toFixed(1) ?? '?'}%</span></td>
            <td style="font-size:12px">${st.last_bar ? String(st.last_bar).replace('T', ' ').slice(0, 16) : '—'}${st.stale ? ' <span class="red">⚠ stale</span>' : ''}</td>
            <td>
                <button class="btn btn-success btn-sm" onclick="sarAction('${inst}','start')" ${!online ? 'disabled' : ''}>▶</button>
                <button class="btn btn-warning btn-sm" onclick="sarAction('${inst}','pause')" ${!online ? 'disabled' : ''}>⏸</button>
                <button class="btn btn-danger btn-sm" onclick="sarAction('${inst}','stop')" ${!online ? 'disabled' : ''}>⏹</button>
                <button class="btn btn-secondary btn-sm" onclick="sarSwitchInstance('${inst}')" ${!online ? 'disabled' : ''}>⚙</button>
            </td>
        </tr>`;
    }
    tbody.innerHTML = rows;
    // селектор в шапке вкладки (быстрое открытие панели)
    const sel = document.getElementById('sarInstSelect');
    if (sel && (!sel.options || sel.options.length !== SAR_INSTANCE_ORDER.length)) {
        sel.innerHTML = SAR_INSTANCE_ORDER.map(k => `<option value="${k}">${sarCfg(k).label} :${sarCfg(k).port}</option>`).join('');
        if (sarPanelInst) sel.value = sarPanelInst;
    }
}

// ============================================================================
// === Торговый журнал (как у OF/VP): /api/trades брокера → группировка позиций ===
// ============================================================================

async function sarLoadJournal(inst) {
    inst = inst || sarPanelInst;
    if (!inst) return;
    const cfg = sarCfg(inst);
    const info = document.getElementById('sarJournalInfo');
    const body = document.getElementById('sarJournalBody');
    if (!body) return;

    const today = new Date().toISOString().slice(0, 10);
    const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
    const fromEl = document.getElementById('sarJournalDateFrom');
    const toEl = document.getElementById('sarJournalDateTo');
    if (fromEl && !fromEl.value) fromEl.value = weekAgo;
    if (toEl && !toEl.value) toEl.value = today;

    let params = '';
    const dateFrom = fromEl ? fromEl.value : '';
    const dateTo = toEl ? toEl.value : '';
    if (dateFrom) params += `&dateFrom=${dateFrom}`;
    if (dateTo) params += `&dateTo=${dateTo}`;
    params = params ? '?' + params.substring(1) : '';

    if (info) info.textContent = 'Загрузка журнала...';
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:16px;color:#9CA3AF">Загрузка...</td></tr>';

    let trades = [];
    try {
        const resp = await fetch('/api/trades' + params, {signal: AbortSignal.timeout(15000)});
        if (resp.ok) {
            const raw = await resp.json();
            trades = Array.isArray(raw) ? raw : (raw.trades || []);
        }
    } catch (e) {
        console.error('sarLoadJournal error:', e);
    }

    // нормализация + фильтр по тикеру этого робота
    const tic = cfg.ticker;
    _sarJournalTrades = trades.map(t => ({
        time: t.timestamp || t.time || t.trade_date || t.datetime || '',
        ticker: t.symbol || t.ticker || t.Ticker || '',
        dir: (t.side || t.direction || t.dir || '').replace('SIDE_', '').toUpperCase(),
        price: parseFloat((t.price && t.price.value) ? t.price.value : (t.price || t.Price || 0)),
        lots: parseInt((t.size && t.size.value) ? t.size.value : (t.quantity || t.lots || t.Lots || t.qty || 0)),
        comment: t.comment || t.Comment || ''
    })).filter(t => t.price > 0 && t.lots > 0)
      .filter(t => !tic || t.ticker.includes(tic));

    _sarJournalTrades.sort((a, b) => a.time.localeCompare(b.time));
    _sarJournalPositions = sarGroupPositions(_sarJournalTrades);
    sarRenderJournal();
}

// Группировка сделок в позиции (порт pyGroupPositions: комиссия 0.9₽/шт,
// PnL = продажи − покупки − комиссия, open-позиция помечается)
function sarGroupPositions(trades) {
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
                _buyCost: 0, _buyLots: 0, _sellCost: 0, _sellLots: 0
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

function sarRenderJournal() {
    const body = document.getElementById('sarJournalBody');
    const info = document.getElementById('sarJournalInfo');
    const summary = document.getElementById('sarJournalSummary');
    if (!body) return;

    const filterEl = document.getElementById('sarJournalFilter');
    const filter = filterEl ? filterEl.value : 'all';
    let positions = _sarJournalPositions;
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
    const pf = grossLoss > 0 ? (grossProfit / grossLoss).toFixed(2) : (grossProfit > 0 ? '∞' : '—');
    const totalComm = positions.reduce((s, p) => s + p.commission, 0);
    let maxDD = 0, peak = 0;
    positions.forEach(p => { if (p.cumPnl > peak) peak = p.cumPnl; const dd = peak - p.cumPnl; if (dd > maxDD) maxDD = dd; });

    if (summary) {
        summary.innerHTML = `
            <div class="metric-card"><div class="metric-label">Позиции</div><div style="font-size:1.1em;font-weight:700;color:var(--accent)">${positions.length}</div></div>
            <div class="metric-card"><div class="metric-label">W / L</div><div style="font-size:1.1em;font-weight:700"><span style="color:var(--green)">${wins.length}</span> / <span style="color:var(--red)">${losses.length}</span></div></div>
            <div class="metric-card"><div class="metric-label">WR</div><div style="font-size:1.1em;font-weight:700;color:${parseFloat(winRate) >= 50 ? 'var(--green)' : 'var(--red)'}">${winRate}%</div></div>
            <div class="metric-card"><div class="metric-label">PnL всего</div><div style="font-size:1.1em;font-weight:700;color:${totalPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(0)} ₽</div></div>
            <div class="metric-card"><div class="metric-label">PF</div><div style="font-size:1.1em;font-weight:700">${pf}</div></div>
            <div class="metric-card"><div class="metric-label">Avg W</div><div style="font-size:1.1em;font-weight:700;color:var(--green)">${avgWin >= 0 ? '+' : ''}${avgWin.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Avg L</div><div style="font-size:1.1em;font-weight:700;color:var(--red)">${avgLoss.toFixed(0)}₽</div></div>
            <div class="metric-card"><div class="metric-label">Комиссия</div><div style="font-size:1.1em;font-weight:700;color:#9CA3AF">${totalComm.toLocaleString('ru-RU')}₽</div></div>
            <div class="metric-card"><div class="metric-label">Max DD</div><div style="font-size:1.1em;font-weight:700;color:var(--red)">${maxDD > 0 ? '-' : ''}${maxDD.toFixed(0)}₽</div></div>
        `;
    }

    if (!positions.length) {
        body.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:24px;color:#9CA3AF">За период сделок не было</td></tr>';
        if (info) info.textContent = 'Нет данных';
        return;
    }

    const sorted = [...positions].reverse();
    body.innerHTML = sorted.map((p, idx) => {
        const pnlCls = p.netPnL >= 0 ? 'var(--green)' : 'var(--red)';
        const dirCls = p.direction === 'LONG' ? 'var(--green)' : 'var(--red)';
        const dirIcon = p.direction === 'LONG' ? '▲ LONG' : '▼ SHORT';
        const entryT = p.entryTime ? p.entryTime.replace(/\.\d+Z$/, '').replace('T', ' ').slice(11, 19) : '—';
        const exitT = p.exitTime ? p.exitTime.replace(/\.\d+Z$/, '').replace('T', ' ').slice(11, 19) : '—';
        const dateStr = p.entryTime ? p.entryTime.slice(0, 10) : '';
        const bg = p.isOpen ? 'rgba(255,215,0,0.05)' : (idx % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.02)');
        const openBadge = p.isOpen ? ' <span style="color:#FFD700;font-size:10px">открыта</span>' : '';
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

function sarStartPolling() {
    if (_sarTimer) clearInterval(_sarTimer);
    sarPollAll();
    _sarTimer = setInterval(sarPollAll, 5000);
}

// --- Надёжное переключение на вкладку «Роботы Акции» ---
// Штатный обработчик app.js иногда не срабатывает для этой кнопки (событие доходит,
// но active не переключается). Ставим собственный обработчик в capture-фазе.
document.addEventListener('click', function (ev) {
    const tab = ev.target.closest('button[data-tab="stocksrobots"]');
    if (!tab) return;
    document.querySelectorAll('.tab').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    const content = document.getElementById('stocksrobots');
    if (content) content.classList.add('active');
}, true);

// --- Двойной клик по строке → popup-панель робота (делегированный, урок 22.09) ---
document.addEventListener('dblclick', function (ev) {
    const tr = ev.target.closest('#sarRobotsTable tr');
    if (!tr) return;
    // основной носитель: data-inst на строке (переживает перерисовки); fallback — тикер из ячейки
    const inst = tr.getAttribute('data-inst') || (() => {
        const strong = tr.querySelector('td strong');
        const ticker = strong ? strong.textContent.trim() : '';
        return SAR_INSTANCE_ORDER.find(k => sarCfg(k).ticker === ticker);
    })();
    if (inst && sarCfg(inst)) sarSwitchInstance(inst);
});

// --- Клик по другой вкладке: закрыть popup (как у других панелей) ---
document.addEventListener('click', function (ev) {
    const other = ev.target.closest('button[data-tab]:not([data-tab="stocksrobots"])');
    if (!other) return;
    const panel = document.getElementById('sarEditPanel');
    if (panel) { panel.remove(); sarPanelInst = null; }
    if (window._sarPanelTimer) { clearInterval(window._sarPanelTimer); window._sarPanelTimer = null; }
}, true);

// селектор в шапке вкладки: открыть панель выбранного экземпляра
document.addEventListener('change', function (ev) {
    if (ev.target && ev.target.id === 'sarInstSelect') sarSwitchInstance(ev.target.value);
});

// автостарт polling (вкладка может быть скрыта — polling лёгкий, 2 req/5с)
document.addEventListener('DOMContentLoaded', () => setTimeout(sarStartPolling, 1500));
