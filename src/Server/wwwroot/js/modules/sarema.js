// ============================================================================
// === F-061: SAREMA — Роботы Акции (SAR×EMA H1, per-ticker инстансы) ===
// === 1 робот = 1 акция; экземпляры на портах 5084-5089/5094-5097. ===
// === Лимиты Дмитрия: max_qty (штуки, null=∞, 0=запрет), max_value_rub (₽). ===
// === Панель параметров — popup по образцу OF/VP-панелей (двойной клик по строке): ===
// === метрики + параметры + торговый журнал внизу (сводка, фильтры, позиции). ===
// ============================================================================

// Регистр экземпляров: добавляется строкой при подъёме нового робота.
const SAR_INSTANCES = {
    sber: { port: 5101, ticker: 'SBER', label: 'SBER' },
    gazp: { port: 5102, ticker: 'GAZP', label: 'GAZP' },
    lkoh: { port: 5103, ticker: 'LKOH', label: 'LKOH' },
    rosn: { port: 5104, ticker: 'ROSN', label: 'ROSN' },
    tatn: { port: 5105, ticker: 'TATN', label: 'TATN' },
    gmkn: { port: 5106, ticker: 'GMKN', label: 'GMKN' },
    nvtk: { port: 5107, ticker: 'NVTK', label: 'NVTK' },
    plzl: { port: 5108, ticker: 'PLZL', label: 'PLZL' },
    mtss: { port: 5109, ticker: 'MTSS', label: 'MTSS' },
    sngs: { port: 5110, ticker: 'SNGS', label: 'SNGS' },
};
const SAR_INSTANCE_ORDER = ['sber', 'gazp', 'lkoh', 'rosn', 'tatn', 'gmkn', 'nvtk', 'plzl', 'mtss', 'sngs'];   // все 10 (Дмитрий 09.10)
SAR_INSTANCE_ORDER.forEach(k => { window['sarRobot_' + k] = null; window['_sarWasOnline_' + k] = false; });
let sarPanelInst = null;            // экземпляр, чей popup открыт (null = закрыт)
let _sarTimer = null;
let _sarJournalOpen = [];    // открытые позиции (новые сверху)
let _sarJournalClosed = [];  // закрытые позиции (новые сверху)

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
                <div class="metric-card"><div class="metric-label">Предельн. ЛОНГ (шт, 0=нет)</div><input id="sarMaxLong" class="input" placeholder="без лимита" style="width:90px"></div>
                <div class="metric-card"><div class="metric-label">Предельн. ШОРТ (шт, 0=нет)</div><input id="sarMaxShort" class="input" placeholder="без лимита" style="width:90px"></div>
                <div class="metric-card"><div class="metric-label">Макс. стоимость ₽ (пусто = ∞)</div><input id="sarMaxValue" class="input" placeholder="без лимита" style="width:110px"></div>
                <div class="metric-card"><div class="metric-label">База R (₽)</div><input id="sarBase" class="input" style="width:110px"></div>
                <div class="metric-card"><div class="metric-label">Риск на сделку %</div><input id="sarRiskPct" class="input" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Kill-switch день (R, пусто = выкл)</div><input id="sarDayKill" class="input" placeholder="выкл" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">Лимит участия (доля объёма)</div><input id="sarLiqFrac" class="input" style="width:70px"></div>
            </div>
            <div id="sarPanelMsg" class="text-secondary" style="font-size:12.5px;min-height:18px"></div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Сигнальные параметры SAR/EMA (редактируемые, для обоих роботов) -->
            <div style="font-weight:700;margin-bottom:8px">Стратегия SAR/EMA <span class="text-secondary" style="font-size:11.5px;font-weight:400">(пусто = значение по умолчанию · применяются при пересчёте баров)</span></div>
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:6px">
                <div class="metric-card"><div class="metric-label">SAR AF0 (0.005–0.05)</div><input id="sarAf0" class="input" placeholder="0.02" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">SAR Step (0.002–0.2)</div><input id="sarStep" class="input" placeholder="0.2" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">SAR Max (0.05–0.5)</div><input id="sarMax" class="input" placeholder="0.2" style="width:80px"></div>
                <div class="metric-card"><div class="metric-label">EMA период (5–50)</div><input id="sarEmaN" class="input" placeholder="12" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Стоп = N×ATR (1–10)</div><input id="sarStopAtr" class="input" placeholder="3" style="width:70px"></div>
                <div class="metric-card"><div class="metric-label">Таймфрейм мин (5–240)</div><input id="sarTfMin" class="input" placeholder="60" style="width:70px"></div>
            </div>
            <div class="text-secondary" style="font-size:11.5px;margin-bottom:4px">Штат бэктеста: AF0 0.02 · Step 0.2 · Max 0.2 · EMA 12 · стоп 3×ATR · H1 · полярность std (не менять — урок WF) · трейлинг PSAR · овернайты разрешены</div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал: логика таблицы Дмитрия — открытые позиции сверху, закрытие дозаписывается справа -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>Журнал торговых операций</strong>
                <span class="text-secondary" style="font-size:12px">открытые сверху · закрытие в той же строке</span>
            </div>
            <div id="sarJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="max-height:360px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:12.5px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:#1a2332;border-bottom:2px solid var(--accent)">
                            <th colspan="2" style="padding:6px 8px;text-align:center;border-right:2px solid var(--accent);color:#60A5FA">Открытие</th>
                            <th colspan="3" style="padding:6px 8px;text-align:center;border-right:2px solid var(--accent);color:#60A5FA">Позиция</th>
                            <th colspan="3" style="padding:6px 8px;text-align:center;border-right:2px solid var(--accent);color:#60A5FA">Стоп</th>
                            <th colspan="5" style="padding:6px 8px;text-align:center;color:#34D399">Закрытие</th>
                        </tr>
                        <tr style="background:var(--card);border-bottom:2px solid var(--accent)">
                            <th style="padding:6px 8px;text-align:left">Дата</th>
                            <th style="padding:6px 8px;text-align:left">Время</th>
                            <th style="padding:6px 8px;text-align:center">L/S</th>
                            <th style="padding:6px 8px;text-align:right">Цена</th>
                            <th style="padding:6px 8px;text-align:right">Шт</th>
                            <th style="padding:6px 8px;text-align:right">Риск ₽</th>
                            <th style="padding:6px 8px;text-align:right">Стоп</th>
                            <th style="padding:6px 8px;text-align:right">Риск %</th>
                            <th style="padding:6px 8px;text-align:left">Дата</th>
                            <th style="padding:6px 8px;text-align:left">Время</th>
                            <th style="padding:6px 8px;text-align:right">Цена</th>
                            <th style="padding:6px 8px;text-align:right">Комис.</th>
                            <th style="padding:6px 8px;text-align:right">PnL ₽</th>
                            <th style="padding:6px 8px;text-align:right">R</th>
                        </tr>
                    </thead>
                    <tbody id="sarJournalBody" style="background:var(--bg)"><tr><td colspan="14" style="text-align:center;padding:24px;color:#9CA3AF">Загрузка...</td></tr></tbody>
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
    set('sarMaxLong', ''); set('sarMaxShort', ''); set('sarMaxValue', ''); set('sarBase', 1000000);
    set('sarRiskPct', ''); set('sarDayKill', ''); set('sarLiqFrac', 0.05);
    set('sarAf0', 0.02); set('sarStep', 0.2); set('sarMax', 0.2);
    set('sarEmaN', 12); set('sarStopAtr', 3); set('sarTfMin', 60);
    // черновик этого экземпляра (оффлайн-сохранение)
    try {
        const raw = localStorage.getItem('sarSavedParams_' + inst);
        if (raw) {
            const d = JSON.parse(raw);
            set('sarMaxLong', d.max_long_qty ?? '');
            set('sarMaxShort', d.max_short_qty ?? '');
            set('sarMaxValue', d.max_value_rub ?? '');
            set('sarBase', d.base ?? 1000000);
            set('sarRiskPct', d.risk_pct != null ? (d.risk_pct * 100).toFixed(1) : '');
            set('sarDayKill', d.day_kill_R ?? '');
            set('sarLiqFrac', d.liq_frac ?? 0.05);
            set('sarAf0', d.sar_af0 ?? 0.02);
            set('sarStep', d.sar_step ?? 0.2);
            set('sarMax', d.sar_max ?? 0.2);
            set('sarEmaN', d.ema_n ?? 12);
            set('sarStopAtr', d.init_stop_atr ?? 3);
            set('sarTfMin', d.tf_min ?? 60);
        }
    } catch (e) {}
    // живой робот — источник истины (только его /params)
    try {
        const resp = await fetch(sarApi(inst) + '/params', {signal: AbortSignal.timeout(2500)});
        if (resp.ok) {
            const p = await resp.json();
            set('sarMaxLong', p.max_long_qty ?? '');
            set('sarMaxShort', p.max_short_qty ?? '');
            set('sarMaxValue', p.max_value_rub ?? '');
            set('sarBase', p.base ?? 1000000);
            set('sarRiskPct', p.risk_pct != null ? (p.risk_pct * 100).toFixed(1) : '');
            set('sarDayKill', p.day_kill_R ?? '');
            set('sarLiqFrac', p.liq_frac ?? 0.05);
            set('sarAf0', p.sar_af0 ?? 0.02);
            set('sarStep', p.sar_step ?? 0.2);
            set('sarMax', p.sar_max ?? 0.2);
            set('sarEmaN', p.ema_n ?? 12);
            set('sarStopAtr', p.init_stop_atr ?? 3);
            set('sarTfMin', p.tf_min ?? 60);
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
    const maxLong = numOrNull('sarMaxLong', true);
    const maxShort = numOrNull('sarMaxShort', true);
    const maxValue = numOrNull('sarMaxValue', false);
    const baseRaw = (document.getElementById('sarBase')?.value || '').trim();
    const riskRaw = (document.getElementById('sarRiskPct')?.value || '').trim().replace(',', '.');
    const killRaw = (document.getElementById('sarDayKill')?.value || '').trim().replace(',', '.');
    const liqRaw = (document.getElementById('sarLiqFrac')?.value || '').trim().replace(',', '.');
    const msg = document.getElementById('sarPanelMsg');
    if (maxLong === undefined || maxShort === undefined || maxValue === undefined) { if (msg) msg.textContent = '❌ Лимиты: число ≥ 0 или пусто (без лимита)'; return; }
    if (riskRaw !== '' && (!isFinite(parseFloat(riskRaw)) || parseFloat(riskRaw) <= 0 || parseFloat(riskRaw) > 10)) { if (msg) msg.textContent = '❌ Риск %: (0, 10]'; return; }
    if (killRaw !== '' && !isNaN(parseFloat(killRaw)) && parseFloat(killRaw) >= 0) { if (msg) msg.textContent = '❌ Kill-switch: отрицательный R или пусто (выкл)'; return; }
    const numField = (id, lo, hi) => {
        const raw = (document.getElementById(id)?.value || '').trim().replace(',', '.');
        if (raw === '') return undefined;                     // пусто = дефолт (не отправляем)
        const n = parseFloat(raw);
        if (!isFinite(n) || n < lo || n > hi) return 'ERR';
        return n;
    };
    const af0 = numField('sarAf0', 0.005, 0.05);
    const step = numField('sarStep', 0.002, 0.2);
    const smax = numField('sarMax', 0.05, 0.5);
    const emaN = numField('sarEmaN', 5, 50);
    const stopAtr = numField('sarStopAtr', 1, 10);
    const tfMin = numField('sarTfMin', 5, 240);
    const sigErr = [['SAR AF0', af0], ['SAR Step', step], ['SAR Max', smax], ['EMA', emaN], ['Стоп×ATR', stopAtr], ['ТФ', tfMin]]
        .filter(([, v]) => v === 'ERR').map(([n_]) => n_);
    if (sigErr.length) { if (msg) msg.textContent = '❌ Вне диапазона: ' + sigErr.join(', '); return; }
    const patch = {
        max_long_qty: maxLong,
        max_short_qty: maxShort,
        max_value_rub: maxValue,
        base: baseRaw !== '' ? parseFloat(baseRaw.replace(',', '.')) : undefined,
        risk_pct: riskRaw !== '' ? parseFloat(riskRaw) / 100 : undefined,
        day_kill_R: killRaw === '' ? null : parseFloat(killRaw),
        liq_frac: liqRaw === '' ? null : parseFloat(liqRaw),
        sar_af0: af0, sar_step: step, sar_max: smax,
        ema_n: emaN != null ? Math.round(emaN) : undefined,
        init_stop_atr: stopAtr,
        tf_min: tfMin != null ? Math.round(tfMin) : undefined,
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
// Паттерн OF-роботов (F-012): Старт = запуск процесса через C# proxy (/api/of/process/sar_xxx/start)
// + /start API; Стоп = /stop API (close FLAT) + stop процесса; Пауза/Резюм — прямые API.
// Работает даже когда робот был никогда не запущен (proxy порождает процесс).
const SAR_ACTION_COOLDOWN = {};

async function sarRobotApi(inst, action) {
    const cfg = sarCfg(inst);
    const procName = 'sar_' + inst;
    const now = Date.now();
    if (SAR_ACTION_COOLDOWN[procName] && now - SAR_ACTION_COOLDOWN[procName] < 5000) {
        sarLog('SAREMA[' + cfg.label + ']: команда ' + action + ' слишком часто — подождите 5с', 'WARN');
        return;
    }
    SAR_ACTION_COOLDOWN[procName] = now;
    try {
        if (action === 'start') {
            // F-012: старт процесса = spawn через C# RobotProcessManager (probe /health уже внутри)
            showToast('⏳ Запуск процесса ' + cfg.label + ' (прогрев Finam ~15с)...', 'info');
            const pr = await fetch('/api/of/process/' + procName + '/start', {method: 'POST', signal: AbortSignal.timeout(40000)});
            const pd = await pr.json();
            if (!pd.ok) {
                showToast('❌ Процесс ' + cfg.label + ' не запустился: ' + (pd.error || '?'), 'error');
                sarLog('SAREMA[' + cfg.label + ']: process start failed: ' + (pd.error || '?'), 'ERROR');
                return;
            }
            showToast('✅ Процесс ' + cfg.label + ' запущен' + (pd.alreadyRunning ? ' (уже работал)' : '') + (pd.alreadyOnPort ? ' (порт уже занят живым роботом)' : ''), 'ok');
            sarLog('SAREMA[' + cfg.label + ']: process started (pid=' + (pd.pid || '?') + ')', 'INFO');
            // F-059: робот стартует в stopped — после spawn переводим в running
            await new Promise(r => setTimeout(r, 800));
            await fetch(sarApi(inst) + '/start', {method: 'POST', signal: AbortSignal.timeout(3000)});
            sarLog('SAREMA[' + cfg.label + ']: trading started (mode=running)', 'INFO');
        }
        if (action === 'stop') {
            // OF-паттерн: сначала /stop API (позиция FLAT, отмена ордеров), затем процесс stop
            try {
                await fetch(sarApi(inst) + '/stop', {method: 'POST', signal: AbortSignal.timeout(3000)});
            } catch (e) { /* робот мог уже умереть — не критично */ }
            const pr = await fetch('/api/of/process/' + procName + '/stop', {method: 'POST', signal: AbortSignal.timeout(10000)});
            const pd = await pr.json().catch(() => ({}));
            sarLog('SAREMA[' + cfg.label + ']: stopped (FLAT check + process kill)', 'INFO');
            showToast('⏹ ' + cfg.label + ' остановлен' + (pd.killed ? ' (процесс убит)' : ''), 'info');
        }
        if (action === 'pause' || action === 'resume') {
            // прямые API (процесс обязан быть жив; кнопки disabled по mode)
            await fetch(sarApi(inst) + '/' + action, {method: 'POST', signal: AbortSignal.timeout(3000)});
            sarLog('SAREMA[' + cfg.label + ']: ' + action + ' OK', 'INFO');
        }
    } catch (e) {
        sarLog('SAREMA[' + cfg.label + ']: ' + action + ' ошибка: ' + e.message, 'ERROR');
        showToast('❌ ' + cfg.label + ': ' + action + ' — ' + e.message, 'error');
    }
    setTimeout(() => sarPollAll(), 1500);
}

async function sarRobotRestart(inst) {
    const cfg = sarCfg(inst);
    // OF-паттерн: restart только из FLAT (робот сам проверит), cooldown 90с
    if (window['_sarRestarting_' + inst]) return;
    window['_sarRestarting_' + inst] = true;
    try {
        const resp = await fetch(sarApi(inst) + '/restart', {method: 'POST', signal: AbortSignal.timeout(5000)});
        const data = await resp.json();
        if (data.ok) {
            showToast('🔄 ' + cfg.label + ': перезапуск запланирован (~10с)', 'info');
            sarLog('SAREMA[' + cfg.label + ']: restart scheduled (' + (data.reason || 'manual') + ')', 'INFO');
        } else {
            showToast('❌ ' + cfg.label + ': restart отклонён — ' + (data.reason || data.error || '?'), 'error');
        }
    } catch (e) {
        // робот оффлайн: полный process stop + start через proxy
        try {
            await fetch('/api/of/process/sar_' + inst + '/stop', {method: 'POST', signal: AbortSignal.timeout(10000)});
            await fetch('/api/of/process/sar_' + inst + '/start', {method: 'POST', signal: AbortSignal.timeout(40000)});
            showToast('🔄 ' + cfg.label + ': процесс перезапущен (proxy)', 'info');
        } catch (e2) {
            showToast('❌ ' + cfg.label + ': restart failed — ' + e2.message, 'error');
        }
    } finally {
        setTimeout(() => { window['_sarRestarting_' + inst] = false; sarPollAll(); }, 12000);
    }
}

async function sarAction(inst, action) {
    // легаси-обёртка (использовалась до кнопок OF-паттерна)
    return sarRobotApi(inst, action);
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
    if (noMsg) noMsg.style.display = SAR_INSTANCE_ORDER.length ? 'none' : '';
    for (const inst of SAR_INSTANCE_ORDER) {
        const cfg = sarCfg(inst);
        const st = window['sarRobot_' + inst];
        const activeBg = (sarPanelInst === inst) ? ' style="background:rgba(79,143,247,.12)"' : '';
        if (!st) {
            if (noMsg) noMsg.style.display = 'none';
            rows += `<tr data-inst="${inst}"${activeBg} style="cursor:pointer" title="Двойной клик — панель робота (доступна оффлайн)">
                <td><strong>${cfg.label}</strong></td>
                <td class="red">● оффлайн</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>
                <td>
                    <button class="btn btn-success btn-sm" onclick="sarRobotApi('${inst}','start')" title="Запустить процесс робота">▶</button>
                    <button class="btn btn-danger btn-sm" onclick="sarRobotApi('${inst}','stop')" title="Остановить процесс (kill по порту)">⏹</button>
                </td>
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
        const isLong = p.dir === 1;
        const limDir = isLong ? (lim.max_long_qty ?? '∞') : (p.dir === -1 ? (lim.max_short_qty ?? '∞') : (lim.max_long_qty ?? '∞'));
        const limMaxVal = lim.max_value_rub != null ? Number(lim.max_value_rub).toFixed(0) : '∞';
        const limUsed = `L:${lim.max_long_qty ?? '∞'} / S:${lim.max_short_qty ?? '∞'} · ${(lim.value_used ?? 0).toFixed(0)}/${limMaxVal} ₽`;
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
                <button class="btn btn-success btn-sm" onclick="sarRobotApi('${inst}','start')" ${mode==='running'?'disabled':''} title="Запустить (start + resume)">▶</button>
                <button class="btn btn-warning btn-sm" onclick="sarRobotApi('${inst}','pause')" ${mode!=='running'?'disabled':''} title="Пауза торговли (процесс живёт)">⏸</button>
                <button class="btn btn-danger btn-sm" onclick="sarRobotApi('${inst}','stop')" ${mode==='stopped'?'disabled':''} title="Стоп: close FLAT + stop процесса (как OF)">⏹</button>
                <button class="btn btn-sm" style="background:#2196F3;color:#fff" title="Перезапуск процесса (FLAT-проверка, cooldown 90с)" onclick="sarRobotRestart('${inst}')" ${window['_sarRestarting_'+inst]?'disabled':''}>${window['_sarRestarting_'+inst]?'⏳':'🔄'}</button>
                <button class="btn btn-secondary btn-sm" onclick="sarSwitchInstance('${inst}')" title="Панель параметров + журнал">⚙</button>
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
// === Торговый журнал SAREMA: логика таблицы Дмитрия ===
// === Источник — ЖУРНАЛ САМОГО РОБОТА (/journal у инстанса), не сделки счёта: ===
// === робот пишет открытие (тикер, ТФ, дата/время, L/S, цена, шт, объём, ===
// === риск %/пункты, стоп) при входе и дозаписывает закрытие при выходе. ===
// === Порядок: открытые позиции сверху (новые первые), затем закрытые (новые первые). ===
// ============================================================================

async function sarLoadJournal(inst) {
    inst = inst || sarPanelInst;
    if (!inst) return;
    const cfg = sarCfg(inst);
    const body = document.getElementById('sarJournalBody');
    if (!body) return;
    body.innerHTML = '<tr><td colspan="14" style="text-align:center;padding:16px;color:#9CA3AF">Загрузка...</td></tr>';
    try {
        let resp = await fetch('/api/sarema/journal/' + inst, {signal: AbortSignal.timeout(4000)});
        if (resp.status === 404) {
            // C# proxy ещё без нового эндпоинта (сервер не перезапущен) — читаем у робота напрямую
            resp = await fetch(sarApi(inst) + '/journal', {signal: AbortSignal.timeout(4000)});
        }
        const data = await resp.json();
        _sarJournalOpen = data.open || [];
        _sarJournalClosed = data.closed || [];
        sarRenderJournal();
    } catch (e) {
        body.innerHTML = '<tr><td colspan="14" style="text-align:center;padding:16px;color:#9CA3AF">Робот оффлайн — журнал недоступен</td></tr>';
        const info = document.getElementById('sarJournalInfo');
        if (info) info.textContent = '';
    }
}

function sarRenderJournal() {
    const body = document.getElementById('sarJournalBody');
    const info = document.getElementById('sarJournalInfo');
    const summary = document.getElementById('sarJournalSummary');
    if (!body) return;

    const open = _sarJournalOpen || [];
    const closed = _sarJournalClosed || [];

    // --- сводка (по закрытым): Σ₽, ΣR, W/L, средние ---
    const Rs = closed.map(r => (r.close && r.close.R != null) ? r.close.R : null).filter(v => v !== null);
    const pnls = closed.map(r => (r.close && r.close.pnl != null) ? r.close.pnl : null).filter(v => v !== null);
    const wins = pnls.filter(v => v > 0).length;
    const losses = pnls.filter(v => v <= 0).length;
    const sumPnl = pnls.reduce((s, v) => s + v, 0);
    const sumR = Rs.reduce((s, v) => s + v, 0);
    const openUnreal = open.length ? ' · открытых: ' + open.length : '';
    if (summary) {
        summary.innerHTML = `
            <div class="metric-card"><div class="metric-label">Открытых</div><div style="font-size:1.1em;font-weight:700;color:#FFD700">${open.length}</div></div>
            <div class="metric-card"><div class="metric-label">Закрытых</div><div style="font-size:1.1em;font-weight:700;color:var(--accent)">${closed.length}</div></div>
            <div class="metric-card"><div class="metric-label">W / L</div><div style="font-size:1.1em;font-weight:700"><span style="color:var(--green)">${wins}</span> / <span style="color:var(--red)">${losses}</span></div></div>
            <div class="metric-card"><div class="metric-label">Σ Прибыль/убыток ₽</div><div style="font-size:1.1em;font-weight:700;color:${sumPnl >= 0 ? 'var(--green)' : 'var(--red)'}">${sumPnl >= 0 ? '+' : ''}${sumPnl.toFixed(0)} ₽</div></div>
            <div class="metric-card"><div class="metric-label">Σ R</div><div style="font-size:1.1em;font-weight:700;color:${sumR >= 0 ? 'var(--green)' : 'var(--red)'}">${sumR >= 0 ? '+' : ''}${sumR.toFixed(2)}R</div></div>
        `;
    }

    // --- строки: открытые сверху, затем закрытые ---
    const rows = [];
    for (const r of open) {
        rows.push(`<tr style="border-bottom:1px solid #2D2D44;background:rgba(255,215,0,0.06)">
            <td style="padding:5px 8px;font-size:11.5px">${r.date_open || '—'}</td>
            <td style="padding:5px 8px;font-family:monospace;font-size:12px">${r.time_open || '—'}</td>
            <td style="padding:5px 8px;text-align:center;font-weight:700;font-size:12px;color:${r.dir === 'L' ? 'var(--green)' : 'var(--red)'}">${r.dir || '—'}</td>
            <td style="padding:5px 8px;text-align:right;font-weight:600">${r.entry != null ? Number(r.entry).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right">${r.qty ?? '—'}</td>
            <td style="padding:5px 8px;text-align:right;color:#F59E0B">${r.risk_pts != null ? Number(r.risk_pts).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right">${r.stop != null ? Number(r.stop).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right;color:#F59E0B">${r.risk_pct != null ? Number(r.risk_pct).toFixed(2) + '%' : '—'}</td>
            <td colspan="2" style="padding:5px 8px;text-align:center;color:#FFD700;font-size:11px;letter-spacing:.4em">— открыта —</td>
            <td style="padding:5px 8px"></td><td style="padding:5px 8px"></td><td style="padding:5px 8px"></td><td style="padding:5px 8px"></td>
        </tr>`);
    }
    for (const r of closed) {
        const c = r.close || {};
        const pnl = c.pnl ?? 0;
        const R = c.R;
        const pnlCls = pnl >= 0 ? 'var(--green)' : 'var(--red)';
        const pnlBg = pnl >= 0 ? 'rgba(62,207,142,.15)' : (pnl >= -50 ? 'rgba(245,176,76,.18)' : 'rgba(239,100,97,.18)');
        const rCls = R >= 0 ? 'var(--green)' : 'var(--red)';
        rows.push(`<tr style="border-bottom:1px solid #2D2D44">
            <td style="padding:5px 8px;font-size:11.5px;color:#9CA3AF">${r.date_open || '—'}</td>
            <td style="padding:5px 8px;font-family:monospace;font-size:12px;color:#9CA3AF">${r.time_open || '—'}</td>
            <td style="padding:5px 8px;text-align:center;font-weight:700;font-size:12px;color:${r.dir === 'L' ? 'var(--green)' : 'var(--red)'}">${r.dir || '—'}</td>
            <td style="padding:5px 8px;text-align:right">${r.entry != null ? Number(r.entry).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right">${r.qty ?? '—'}</td>
            <td style="padding:5px 8px;text-align:right;color:#F59E0B">${r.risk_pts != null ? Number(r.risk_pts).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right">${r.stop != null ? Number(r.stop).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right;color:#F59E0B">${r.risk_pct != null ? Number(r.risk_pct).toFixed(2) + '%' : '—'}</td>
            <td style="padding:5px 8px;font-size:11.5px;color:#9CA3AF">${c.date_close || '—'}</td>
            <td style="padding:5px 8px;font-family:monospace;font-size:12px">${c.time_close || '—'}</td>
            <td style="padding:5px 8px;text-align:right;font-weight:600">${c.exit != null ? Number(c.exit).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px;text-align:right;font-size:11px;color:#9CA3AF">${c.commission != null ? Number(c.commission).toFixed(0) : '—'}</td>
            <td style="padding:5px 8px;text-align:right;font-weight:700;color:${pnlCls};background:${pnlBg}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)}</td>
            <td style="padding:5px 8px;text-align:right;font-weight:700;color:${rCls}">${R != null ? ((R >= 0 ? '+' : '') + R.toFixed(3)) : '—'}</td>
        </tr>`);
    }
    body.innerHTML = rows.length ? rows.join('') :
        '<tr><td colspan="14" style="text-align:center;padding:24px;color:#9CA3AF">Сделок ещё не было — робот начнёт писать журнал при первом входе</td></tr>';
    if (info) info.textContent = `${open.length} открытых · ${closed.length} закрытых · Σ ${sumPnl >= 0 ? '+' : ''}${sumPnl.toFixed(0)}₽ / ${sumR >= 0 ? '+' : ''}${sumR.toFixed(2)}R`;
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
