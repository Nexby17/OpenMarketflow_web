// ============================================================================
// === F-061: SAREMA — Роботы Акции (SAR×EMA H1, per-ticker инстансы) ===
// === 1 робот = 1 акция; экземпляры на портах 5084-5089/5094-5097. ===
// === Лимиты Дмитрия: max_qty (штуки, null=∞, 0=запрет), max_value_rub (₽). ===
// === Панель параметров — popup по образцу OF/VP-панелей (двойной клик по строке): ===
// === метрики + параметры + торговый журнал внизу (сводка, фильтры, позиции). ===
// ============================================================================

// Регистр экземпляров: добавляется строкой при подъёме нового робота.
const SAR_INSTANCES = {
    mtlr: { port: 5111, ticker: 'MTLR', label: 'MTLR' },
};
const SAR_INSTANCE_ORDER = ['mtlr'];   // все 10 (Дмитрий 09.10)
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
    if (existing && sarPanelInst === inst) { _sarClosePanelDom(); sarPanelInst = null; return; }
    if (existing) _sarClosePanelDom();

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
            <button class="btn btn-secondary btn-sm" onclick="sarClosePanel()">✖ Закрыть</button>
        </div>
        <div style="padding:12px">
            <!-- Живые метрики -->
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px" id="sarLiveMetrics"></div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Ручная корректировка позиции (как у OF-роботов): синхронизация с брокером БЕЗ ордеров -->
            <div style="font-weight:700;margin-bottom:6px">Ручная корректировка позиции <span class="text-secondary" style="font-size:11.5px;font-weight:400">— после ручной сделки в терминале; ордеров не ставит, только синхронизирует робота</span></div>
            <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
                <input id="sarAdjQty" type="number" step="1" placeholder="шт: 2 / -1 / 0" title="Абсолютная позиция в штуках со знаком: 2 = LONG 2, -1 = SHORT 1, 0 = FLAT" style="width:110px;padding:6px 8px;background:#0D1117;color:#fff;border:1px solid #607D8B;border-radius:4px;font-size:13px">
                <input id="sarAdjPrice" type="number" step="0.01" placeholder="средняя цена" title="Средняя цена входа (обязательна при шт != 0)" style="width:120px;padding:6px 8px;background:#0D1117;color:#fff;border:1px solid #607D8B;border-radius:4px;font-size:13px">
                <button class="btn btn-primary btn-sm" onclick="sarAdjustPosition()" title="Установить позицию робота = указанной (ордеров НЕ выставляет)">Корректировать</button>
                <span id="sarAdjResult" style="font-size:12px;color:#B0BEC5"></span>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Параметры -->
            <div style="font-weight:700;margin-bottom:8px">Лимиты и риск</div>
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:8px">
                <div class="metric-card" style="flex:0 0 150px;min-width:150px;min-height:66px;text-align:left"><div class="metric-label">Предельн. ЛОНГ (шт, 0=нет)</div><input id="sarMaxLong" class="input" placeholder="без лимита" style="width:90px"></div>
                <div class="metric-card" style="flex:0 0 150px;min-width:150px;min-height:66px;text-align:left"><div class="metric-label">Предельн. ШОРТ (шт, 0=нет)</div><input id="sarMaxShort" class="input" placeholder="без лимита" style="width:90px"></div>
                <div class="metric-card" style="flex:0 0 150px;min-width:150px;min-height:66px;text-align:left"><div class="metric-label">Капитал (₽)</div><input id="sarBase" class="input" style="width:110px"></div>
                <div class="metric-card" style="flex:0 0 150px;min-width:150px;min-height:66px;text-align:left"><div class="metric-label">Стоп-лосс</div><div style="display:flex;gap:4px"><select id="sarSlUnit" class="select" style="flex:0 0 64px;width:64px;min-width:64px;padding-left:4px;padding-right:2px"><option value="rub_pnl">руб уб</option><option value="pct">%</option><option value="rub">руб</option><option value="points">пт</option></select><input id="sarSlValue" class="input" placeholder="—" style="flex:1 1 auto;width:auto;min-width:60px"></div></div>
                <div class="metric-card" style="flex:0 0 150px;min-width:150px;min-height:66px;text-align:left"><div class="metric-label">Трейлинг-стоп</div><div style="display:flex;gap:4px"><select id="sarTrUnit" class="select" style="width:auto;flex:0 0 auto"><option value="pct">%</option><option value="rub">рубли</option><option value="points">пункты</option></select><input id="sarTrValue" class="input" placeholder="—" style="width:100%;min-width:56px"></div></div>
                <div class="metric-card" style="flex:0 0 150px;min-width:150px;min-height:66px;text-align:left"><div class="metric-label">Безубыток</div><div style="display:flex;gap:4px"><select id="sarBeUnit" class="select" style="width:auto;flex:0 0 auto"><option value="pct">%</option><option value="rub">рубли</option><option value="points">пункты</option></select><input id="sarBeValue" class="input" placeholder="выкл" style="width:100%;min-width:56px"></div></div>
                
            </div>
            <div id="sarPanelMsg" class="text-secondary" style="font-size:12.5px;min-height:18px"></div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Сигнальные параметры SAR/EMA (редактируемые, для обоих роботов) -->
            <div style="font-weight:700;margin-bottom:8px">Стратегия SAR/EMA <span class="text-secondary" style="font-size:11.5px;font-weight:400">(пусто = значение по умолчанию · применяются при пересчёте баров)</span></div>
            <div class="metrics-row" style="flex-wrap:wrap;margin-bottom:6px">
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">SAR AF0 (0.005–0.05)</div><input id="sarAf0" class="input" placeholder="0.02" style="width:80px"></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">SAR Step (0.002–0.2)</div><input id="sarStep" class="input" placeholder="0.2" style="width:80px"></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">SAR Max (0.05–0.5)</div><input id="sarMax" class="input" placeholder="0.2" style="width:80px"></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">EMA период (5–50)</div><input id="sarEmaN" class="input" placeholder="12" style="width:70px"></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">TP режим</div><div style="display:flex;gap:8px;align-items:center"><label style="display:flex;gap:4px;align-items:center"><input type="checkbox" id="sarTpFixed" checked> Fixed</label><label style="display:flex;gap:4px;align-items:center"><input type="checkbox" id="sarTpSar"> SAR</label></div></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">TP, R (пусто=выкл)</div><input id="sarTpR" class="input" placeholder="2" style="width:100%"></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">Таймфрейм мин (5–240)</div><input id="sarTfMin" class="input" placeholder="60" style="width:70px"></div>
                <div class="metric-card" style="flex:0 0 132px;min-width:132px;min-height:66px;text-align:left"><div class="metric-label">Окно сигн. (бары)</div><input id="sarMaxSigAge" class="input" placeholder="8" style="width:70px"></div>
            </div>
            <div class="text-secondary" style="font-size:11.5px;margin-bottom:4px">Штат бэктеста: AF0 0.02 · Step 0.2 · Max 0.2 · EMA 12 · стоп 3×ATR · H1 · полярность std (не менять — урок WF) · трей
            <hr style="border-color:#2D2D44;margin:12px 0">
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- График инструмента: свечи + EMA + PSAR + позиции + трейлинг-стоп -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>График <span id="sarChartTicker" style="color:var(--accent)"></span> — SAR/EMA</strong>
                <div style="display:flex;gap:6px;align-items:center">
                    <span class="text-secondary" style="font-size:12px">Таймфрейм</span>
                    <select id="sarChartTf" class="input" style="width:70px" onchange="this.dataset.auto='0';try{localStorage.setItem('sarChartTf_'+(sarPanelInst||'mtlr'), this.value);}catch(e){}sarChartReload()">
                        <option value="5">M5</option>
                        <option value="15">M15</option>
                        <option value="30">M30</option>
                        <option value="60">H1</option>
                        <option value="120">H2</option>
                        <option value="240">H4</option>
                    </select>
                    <label class="text-secondary" style="font-size:12px;display:flex;gap:3px;align-items:center"><input type="checkbox" id="sarChkPos" checked onchange="sarChartReload()"> позиции</label>
                    <label class="text-secondary" style="font-size:12px;display:flex;gap:3px;align-items:center"><input type="checkbox" id="sarChkStop" checked onchange="sarChartReload()"> стоп</label>
                </div>
            </div>
            <div id="sarChartSlot" style="min-height:320px;border:1px solid #2D2D44;border-radius:8px;background:#121826"><div id="sarChartHost" style="height:320px;background:#121826"></div></div>
            <div id="sarChartInfo" class="text-secondary" style="font-size:11.5px;margin-top:4px"></div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- F-182: Сделки брокера (лента) с выбором периода -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>Сделки брокера (лента)</strong>
                <span style="display:flex;gap:8px;align-items:center">
                    <span class="text-secondary" style="font-size:12px">с</span>
                    <input type="date" id="sarTradesFrom" class="input" style="width:140px">
                    <span class="text-secondary" style="font-size:12px">по</span>
                    <input type="date" id="sarTradesTo" class="input" style="width:140px">
                    <button id="sarTradesLoad" class="btn btn-sm" style="padding:2px 10px;font-size:12px">Загрузить</button>
                </span>
            </div>
            <div style="max-height:260px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px;margin-bottom:12px">
                <table style="width:100%;border-collapse:collapse;font-size:12.5px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:#1a2332;border-bottom:2px solid var(--accent)">
                            <th style="padding:5px 8px;text-align:left">Время (МСК)</th>
                            <th style="padding:5px 8px;text-align:left">Направление</th>
                            <th style="padding:5px 8px;text-align:right">Объём</th>
                            <th style="padding:5px 8px;text-align:right">Цена</th>
                            <th style="padding:5px 8px;text-align:left">Комментарий</th>
                        </tr>
                    </thead>
                    <tbody id="sarTradesTable">
                        <tr><td colspan="5" class="text-secondary" style="padding:8px">Выберите период и нажмите «Загрузить»</td></tr>
                    </tbody>
                </table>
            </div>
            <hr style="border-color:#2D2D44;margin:12px 0">
            <!-- Торговый журнал: логика таблицы Дмитрия — открытые позиции сверху, закрытие дозаписывается справа -->
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>Журнал торговых операций</strong>
                <span style="display:flex;gap:10px;align-items:center">
                    <span class="text-secondary" style="font-size:12px">открытые сверху · закрытие в той же строке</span>
                    <button id="sarJournalClear" class="btn btn-danger btn-sm" style="padding:2px 10px;font-size:12px" title="Очистить журнал (только при ФЛЭТ)">Очистить</button>
                </span>
            </div>
            <div id="sarJournalSummary" class="metrics-row" style="flex-wrap:wrap;margin-bottom:12px"></div>
            <div style="max-height:360px;overflow-y:auto;border:1px solid var(--border-color);border-radius:8px">
                <table style="width:100%;border-collapse:collapse;font-size:12.5px">
                    <thead style="position:sticky;top:0;z-index:1">
                        <tr style="background:#1a2332;border-bottom:2px solid var(--accent)">
                            <th colspan="3" style="padding:6px 8px;text-align:center;border-right:2px solid var(--accent);color:#60A5FA">Открытие</th>
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
                            <th style="padding:6px 8px;text-align:right">Объём ₽</th>
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
                    <tbody id="sarJournalBody" style="background:var(--bg)"><tr><td colspan="15" style="text-align:center;padding:24px;color:#9CA3AF">Загрузка...</td></tr></tbody>
                </table>
            </div>
            <div id="sarJournalInfo" style="font-size:12px;color:#9CA3AF;margin-top:8px"></div>
        </div>
    `;
    document.body.appendChild(div);

    // загрузка: очистка → черновик → живой /params (только этого робота)
        // F-105: взаимоисключающие чекбоксы TP режима
    var _fx0 = document.getElementById('sarTpFixed'), _sr0 = document.getElementById('sarTpSar');
    if (_fx0 && _sr0) {
        _fx0.addEventListener('change', function(){ if (_fx0.checked) _sr0.checked = false; });
        _sr0.addEventListener('change', function(){ if (_sr0.checked) _fx0.checked = false; });
    }
    sarLoadPanelData(inst);
    // журнал + живые метрики (таймер панели)
    if (window._sarPanelTimer) clearInterval(window._sarPanelTimer);
    window._sarPanelTimer = setInterval(() => sarUpdateLiveMetrics(inst), 2000);
    sarUpdateLiveMetrics(inst);
    setTimeout(() => sarLoadJournal(inst), 300);
    // F-071a: журнал живой — обновление каждые 10с пока попап открыт
    if (window._sarJournalTimer) clearInterval(window._sarJournalTimer);
    window._sarJournalTimer = setInterval(() => { if (sarPanelInst && document.getElementById('sarEditPanel')) sarLoadJournal(sarPanelInst); }, 10000);
    setTimeout(() => sarChartLoad(inst), 300);
}

async function sarLoadPanelData(inst) {
    const cfg = sarCfg(inst);
    const msg = document.getElementById('sarPanelMsg');
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.value = v; };
    // дефолты
    set('sarMaxLong', ''); set('sarMaxShort', ''); set('sarBase', 1000000);
    set('sarSlUnit', 'rub_pnl'); set('sarSlValue', '');
    set('sarTrUnit', 'pct'); set('sarTrValue', '');     set('sarBeUnit', 'pct'); set('sarBeValue', '');
    set('sarAf0', 0.02); set('sarStep', 0.2); set('sarMax', 0.2);
    set('sarEmaN', 12); set('sarTfMin', 60);
    // F-065: VP дефолты
    // черновик этого экземпляра (оффлайн-сохранение)
    try {
        const raw = localStorage.getItem('sarSavedParams_' + inst);
        if (raw) {
            const d = JSON.parse(raw);
            set('sarMaxLong', d.max_long_qty ?? '');
            set('sarMaxShort', d.max_short_qty ?? '');

            set('sarBase', d.base ?? 1000000);
            set('sarSlUnit', d.stop_loss_unit || 'rub_pnl');
            set('sarSlValue', d.stop_loss_value ?? '');
            set('sarTrUnit', d.trail_unit || 'pct');
            set('sarTrValue', d.trail_value ?? ''); set('sarBeUnit', d.be_unit || 'pct'); set('sarBeValue', d.be_value ?? '');
            set('sarAf0', d.sar_af0 ?? 0.02);
            set('sarStep', d.sar_step ?? 0.2);
            set('sarMax', d.sar_max ?? 0.2);
            set('sarEmaN', d.ema_n ?? 12);
        set('sarTpR', d.tp_R ?? '');
        set('sarMaxSigAge', d.max_signal_age ?? 8);
            set('sarTfMin', d.tf_min ?? 60);
            // F-065
        }
    } catch (e) {}
    // живой робот — источник истины (только его /params)
    try {
        const resp = await fetch(sarApi(inst) + '/params', {signal: AbortSignal.timeout(2500)});
        if (resp.ok) {
            const p = await resp.json();
            set('sarMaxLong', p.max_long_qty ?? '');
            set('sarMaxShort', p.max_short_qty ?? '');

            set('sarBase', p.base ?? 1000000);

            set('sarSlUnit', p.stop_loss_unit || 'rub_pnl');
            set('sarSlValue', p.stop_loss_value ?? '');
            set('sarTrUnit', p.trail_unit || 'pct');
            set('sarTrValue', p.trail_value ?? ''); set('sarBeUnit', p.be_unit || 'pct'); set('sarBeValue', p.be_value ?? '');
            
            set('sarAf0', p.sar_af0 ?? 0.02);
            set('sarStep', p.sar_step ?? 0.2);
            set('sarMax', p.sar_max ?? 0.2);
            set('sarEmaN', p.ema_n ?? 12);
            set('sarTpR', p.tp_R ?? '');
            var _tm = (p.tp_mode || 'fixed');
            var _fx = document.getElementById('sarTpFixed'), _sr = document.getElementById('sarTpSar');
            if (_fx && _sr) { _fx.checked = _tm === 'fixed'; _sr.checked = _tm === 'sar'; }
            set('sarMaxSigAge', p.max_signal_age ?? 8);
            set('sarTfMin', p.tf_min ?? 60);
            // F-065
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
    var tpText = '—';
    if (st) {
        var _tp = st.tp || {};
        if (_tp.price) tpText = _tp.price + ((_tp.mode === 'sar') ? ' · SAR' : '');
        mode = st.mode === 'running' ? '▶ работает' : (st.mode === 'paused' ? '⏸ пауза' : '■ остановлен');
        modeColor = st.mode === 'running' ? '#3ecf8e' : '#9CA3AF';
        const p = st.position || {};
        dirText = p.dir === 1 ? 'LONG' : (p.dir === -1 ? 'SHORT' : 'ФЛЭТ');
        dirText = p.dir ? `${dirText} ${p.qty} шт` : 'ФЛЭТ';
        entryText = p.entry ? p.entry.toFixed(2) : '—';
        stopText = p.stop ? p.stop.toFixed(2) : '—';
        priceSrc = p.priceSource ? ` (${p.priceSource})` : '';
        // F-072: нереализованный PnL по открытой позиции (отметка по последнему закрытому H1)
        const pTotal = st.pnl ? st.pnl.total : 0;
        const pUnreal = st.pnl ? st.pnl.unrealized : null;
        if (pUnreal != null && st.position && st.position.dir) {
            pnlText = (pUnreal >= 0 ? '+' : '') + pUnreal.toFixed(0) + ' ₽ (тек.) · Σ ' +
                      ((pTotal + pUnreal) >= 0 ? '+' : '') + (pTotal + pUnreal).toFixed(0) + ' ₽';
        } else if (p.dir) {
            pnlText = (pTotal >= 0 ? '+' : '') + pTotal.toFixed(0) + ' ₽';
        } else {
            pnlText = '—'; // F-147: ФЛЭТ → PnL пустой
        }
        lastBar = st.last_bar ? String(st.last_bar).replace('T', ' ').slice(0, 16) : '—';
    }
    box.innerHTML = `
        <div class="metric-card"><div class="metric-label">Режим</div><div style="font-size:1.05em;font-weight:700;color:${modeColor}">${mode}</div></div>
        <div class="metric-card"><div class="metric-label">Позиция</div><div style="font-size:1.05em;font-weight:700">${dirText}</div></div>
        <div class="metric-card"><div class="metric-label">Вход</div><div style="font-size:1.05em;font-weight:700">${entryText}</div></div>
        <div class="metric-card"><div class="metric-label">Стоп</div><div style="font-size:1.05em;font-weight:700">${stopText}</div></div>
        <div class="metric-card"><div class="metric-label">TP (цель)</div><div style="font-size:1.05em;font-weight:700;color:#4F8FF7">${tpText}</div></div>
        <div class="metric-card"><div class="metric-label">PnL (брокер)${priceSrc}</div><div style="font-size:1.05em;font-weight:700;color:${pnlText.startsWith('-') ? 'var(--red)' : 'var(--green)'}">${pnlText}</div></div>
        <div class="metric-card"><div class="metric-label">Последний H1</div><div style="font-size:0.95em;font-weight:600">${lastBar}</div></div>
    `;
}


// ---------- ручная корректировка позиции (как у OF) ----------
async function sarAdjustPosition() {
    const inst = sarPanelInst;
    if (!inst) return;
    const cfg = sarCfg(inst);
    const qtyEl = document.getElementById('sarAdjQty');
    const priceEl = document.getElementById('sarAdjPrice');
    const resEl = document.getElementById('sarAdjResult');
    const qty = parseInt(qtyEl.value);
    const price = parseFloat(priceEl.value);
    if (isNaN(qty)) { resEl.textContent = '❌ Введите штуки (2 / -1 / 0)'; resEl.style.color = '#ff9800'; return; }
    if (qty !== 0 && (isNaN(price) || price <= 0)) { resEl.textContent = '❌ При шт != 0 укажите цену > 0'; resEl.style.color = '#ff9800'; return; }
    const posText = qty === 0 ? 'FLAT' : (qty > 0 ? 'LONG ' + qty : 'SHORT ' + Math.abs(qty));
    if (!confirm(`Установить позицию ${cfg.label}: ${posText}${qty !== 0 ? ' @ ' + price : ''}?\nОрдера НЕ выставляются — только синхронизация робота с фактом.`)) return;
    try {
        const resp = await fetch(sarApi(inst) + '/position/adjust', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({qty: qty, price: qty === 0 ? 0 : price}),
            signal: AbortSignal.timeout(6000)
        });
        const data = await resp.json();
        if (data.ok) {
            resEl.textContent = '✓ ' + (qty === 0 ? 'FLAT' : (data.dir === 1 ? 'LONG' : 'SHORT') + ' ' + data.qty + ' @ ' + data.entry + ', стоп ' + data.stop);
            resEl.style.color = '#4caf50';
            sarLog('SAREMA[' + cfg.label + ']: ручная корректировка: ' + posText + (qty !== 0 ? ' @ ' + price : ''), 'INFO');
            setTimeout(() => sarPollAll(), 500);
        } else {
            const err = data.error || (data.detail && data.detail.error) || 'ошибка';
            resEl.textContent = '❌ ' + err;
            resEl.style.color = '#ef4444';
            sarLog('SAREMA[' + cfg.label + ']: adjust error: ' + err, 'ERROR');
        }
    } catch (e) {
        resEl.textContent = '❌ Робот недоступен';
        resEl.style.color = '#ef4444';
    }
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

    const baseRaw = (document.getElementById('sarBase')?.value || '').trim();
    const slUnit = (document.getElementById('sarSlUnit')?.value || 'rub_pnl');
    const slRaw = (document.getElementById('sarSlValue')?.value || '').trim().replace(',', '.');
    const trUnit = (document.getElementById('sarTrUnit')?.value || 'pct');
    const trRaw = (document.getElementById('sarTrValue')?.value || '').trim().replace(',', '.');

    const msg = document.getElementById('sarPanelMsg');
    if (maxLong === undefined || maxShort === undefined) { if (msg) msg.textContent = '❌ Лимиты: число ≥ 0 или пусто (без лимита)'; return; }
    const slNum = slRaw === '' ? null : parseFloat(slRaw);
    if (slRaw !== '' && (!isFinite(slNum) || slNum <= 0)) { if (msg) msg.textContent = '❌ Стоп-лосс: число > 0 или пусто (ATR×N)'; return; }
    const trNum = trRaw === '' ? null : parseFloat(trRaw);
    if (trRaw !== '' && (!isFinite(trNum) || trNum <= 0)) { if (msg) msg.textContent = '❌ Трейлинг-стоп: число > 0 или пусто (SAR)'; return; }
    const numField = (id, lo, hi) => {
        const raw = (document.getElementById(id)?.value || '').trim().replace(',', '.');
        if (raw === '') return undefined;                     // пусто = дефолт (не отправляем)
        const n = parseFloat(raw);
        if (!isFinite(n) || n < lo || n > hi) return 'ERR';
        return n;
    };
    const sigErr = [];
    const checkField = (id, v, label) => { if (v === 'ERR') sigErr.push(label); };
    const af0 = numField('sarAf0', 0.005, 0.05);   checkField('sarAf0', af0, 'SAR af0');
    const step = numField('sarStep', 0.002, 0.2);  checkField('sarStep', step, 'SAR step');
    const smax = numField('sarMax', 0.05, 0.5);    checkField('sarMax', smax, 'SAR max');
    const emaN = numField('sarEmaN', 5, 50);       checkField('sarEmaN', emaN, 'EMA N');
    const tpR = numField('sarTpR', 0.1, 20);       checkField('sarTpR', tpR, 'TP R');
    const maxSigAge = numField('sarMaxSigAge', 1, 50); checkField('sarMaxSigAge', maxSigAge, 'Signal window');
    const tfMin = numField('sarTfMin', 5, 240);    checkField('sarTfMin', tfMin, 'TF min');
    if (sigErr.length) { if (msg) msg.textContent = '❌ Вне диапазона: ' + sigErr.join(', '); return; }
    const patch = {
        max_long_qty: maxLong,
        max_short_qty: maxShort,

        base: baseRaw !== '' ? parseFloat(baseRaw.replace(',', '.')) : undefined,

        stop_loss_unit: slUnit,
        stop_loss_value: slNum,
        trail_unit: trUnit,
        trail_value: trNum,
        be_unit: (document.getElementById('sarBeUnit')?.value || 'pct'),
        be_value: (function(){ var v = (document.getElementById('sarBeValue')?.value || '').trim().replace(',', '.'); return v === '' ? null : parseFloat(v); })(),
        sar_af0: af0, sar_step: step, sar_max: smax,
        ema_n: emaN != null ? Math.round(emaN) : undefined,
        tp_mode: (function(){ var fx = document.getElementById('sarTpFixed'), sr = document.getElementById('sarTpSar');
                   if (sr && sr.checked) return 'sar';
                   return 'fixed'; })(),
        tp_R: tpR != null ? tpR : null,
        max_signal_age: maxSigAge != null ? maxSigAge : null,
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
                <td class="red">● оффлайн</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td id="sarLivePrice_off" class="text-secondary">—</td><td>—</td><td>—</td><td>—</td>
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
        // F-077: объём позиции (шт × цена входа) вместо «Цена (источник)»
        const volumeText = (p.dir && p.entry) ? `${(p.qty * p.entry).toFixed(0)} ₽` : '—';
        const stopText = (p.dir && p.stop) ? p.stop.toFixed(2) : '—';   // F-111: FLAT → «—»
        const pnlData = st.pnl || {};
        const jrnl = (typeof st.journal_pnl === 'number') ? st.journal_pnl : 0;   // F-091: Σ закрытых сделок журнала
        // F-109: PnL колонки = ПнЛ ТЕКУЩЕЙ позиции. FLAT → 0 (не накопленный lifetime).
        const pnl = p.dir ? ((pnlData.unrealized != null) ? pnlData.unrealized : (pnlData.total_with_unrealized || 0)) : 0;
        const lim = st.limits || {};
        const isLong = p.dir === 1;
        const limDir = isLong ? (lim.max_long_qty ?? '∞') : (p.dir === -1 ? (lim.max_short_qty ?? '∞') : (lim.max_long_qty ?? '∞'));
        const limMaxVal = lim.max_value_rub != null ? Number(lim.max_value_rub).toFixed(0) : '∞';
        const limUsed = `L:${lim.max_long_qty ?? '∞'} / S:${lim.max_short_qty ?? '∞'} · ${(lim.value_used ?? 0).toFixed(0)}/${limMaxVal} ₽`;
        const risk = st.risk || {};

        const online = window['_sarWasOnline_' + inst];
        rows += `<tr data-inst="${inst}"${activeBg} style="cursor:pointer" title="Двойной клик — панель робота с торговым журналом">
            <td><strong>${cfg.label}</strong> <span class="badge" style="background:#2e7d32">SAR</span></td>
            <td class="${modeCls}">${modeText}</td>
            <td>${posText}</td>
            <td>${p.dir && p.entry ? p.entry.toFixed(2) : '—'}</td>
            <td>${stopText}</td>
            <td id="sarLivePrice_${inst}" style="font-weight:600">${(window._sarLivePrices || {})[inst] || '—'}</td>
            <td>${volumeText}</td>
            <td>${pnl >= 0 ? '+' : ''}${pnl.toFixed(0)} ₽</td>
            <td id="sarJournalPnl_${inst}" style="font-weight:600">${jrnl >= 0 ? '+' : ''}${jrnl.toFixed(0)} ₽</td>
            <td style="font-size:12px">${limUsed}<br><span class="text-secondary">risk ${(risk.risk_pct * 100).toFixed(1) ?? '?'}%</span></td>
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
    // F-081b: ПОСТРОЧНЫЙ диф-рендер — PnL/цены меняются каждый тик, полный innerHTML мигал.
    const newRows = rows.split('</tr>').filter(s => s.trim()).map(s => s.trim() + '</tr>');
    const oldRows = tbody._lastRows || [];
    if (oldRows.length !== newRows.length) {
        tbody.innerHTML = newRows.join('');
        tbody._lastRows = newRows;
        return;
    }
    const trows = tbody.rows;
    for (let i = 0; i < newRows.length; i++) {
        if (oldRows[i] !== newRows[i]) {
            if (trows[i]) {
                // F-096: ячейковый диф - без замены всей строки (мигание + сброс hover)
                const tmp = document.createElement('tbody');
                tmp.innerHTML = newRows[i];
                const newRow = tmp.firstElementChild;
                const oldRow = trows[i];
                if (newRow && oldRow && newRow.children.length === oldRow.children.length) {
                    for (let c = 0; c < newRow.children.length; c++) {
                        const oc = oldRow.children[c], nc = newRow.children[c];
                        if (oc.className !== nc.className) oc.className = nc.className; // F-096c
                        if (oc.innerHTML !== nc.innerHTML) oc.innerHTML = nc.innerHTML;
                    }
                } else if (oldRow) {
                    oldRow.outerHTML = newRows[i];
                }
            } else {
                tbody.insertAdjacentHTML('beforeend', newRows[i]);
            }
        }
    }
    tbody._lastRows = newRows;
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
    body.innerHTML = '<tr><td colspan="15" style="text-align:center;padding:16px;color:#9CA3AF">Загрузка...</td></tr>';
    // F-071a fix2: proxy может быть 401/404/500 (auth/нет эндпоинта/ошибка) — при ЛЮБОМ не-OK идём к роботу.
    // Робот — источник истины (пишет state-файл сам). Только если и он недоступен — «оффлайн».
    const fetchJson = async (url, ms) => {
        const r = await fetch(url, {signal: AbortSignal.timeout(ms)});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return await r.json();
    };
    try {
        let data = null;
        try { data = await fetchJson('/api/sarema/journal/' + inst, 4000); }
        catch (e1) { data = await fetchJson(sarApi(inst) + '/journal', 4000); }
        if (!data || typeof data !== 'object') throw new Error('bad payload');
        _sarJournalOpen = data.open || [];
        _sarJournalClosed = data.closed || [];
        sarRenderJournal();
    } catch (e) {
        // одна повторная попытка через 2с (робот/сеть могли мигнуть), потом «оффлайн»
        setTimeout(async () => {
            if (sarPanelInst !== inst || !document.getElementById('sarEditPanel')) return;
            try {
                let data = null;
                try { data = await fetchJson('/api/sarema/journal/' + inst, 4000); }
                catch (e2) { data = await fetchJson(sarApi(inst) + '/journal', 4000); }
                _sarJournalOpen = data.open || [];
                _sarJournalClosed = data.closed || [];
                sarRenderJournal();
            } catch (e3) {
                const b2 = document.getElementById('sarJournalBody');
                if (b2) b2.innerHTML = '<tr><td colspan="15" style="text-align:center;padding:16px;color:#9CA3AF">Робот оффлайн — журнал недоступен</td></tr>';
                const info = document.getElementById('sarJournalInfo');
                if (info) info.textContent = '';
            }
        }, 2000);
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
            <td style="padding:5px 8px;text-align:right;color:#B0BEC5">${r.volume != null ? Number(r.volume).toFixed(0) : '—'}</td>
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
            <td style="padding:5px 8px;text-align:right;color:#B0BEC5">${r.volume != null ? Number(r.volume).toFixed(0) : '—'}</td>
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
        '<tr><td colspan="15" style="text-align:center;padding:24px;color:#9CA3AF">Сделок ещё не было — робот начнёт писать журнал при первом входе</td></tr>';
    if (info) info.textContent = `${open.length} открытых · ${closed.length} закрытых · Σ ${sumPnl >= 0 ? '+' : ''}${sumPnl.toFixed(0)}₽ / ${sumR >= 0 ? '+' : ''}${sumR.toFixed(2)}R`;
}

function sarStartPolling() {
    if (_sarTimer) clearInterval(_sarTimer);
    sarPollAll();
    _sarTimer = setInterval(sarPollAll, 5000);
    // F-079: реал-тайм цена в таблице роботов — опрос тика раз в 5с
    if (window._sarQuoteTimer) clearInterval(window._sarQuoteTimer);
    sarPollQuotes();
    window._sarQuoteTimer = setInterval(sarPollQuotes, 5000);
}

// F-079: live-цены всех инстансов одним проходом (кэш 4с в C# нет — но Finam отдаёт быстро)
window._sarLivePrices = window._sarLivePrices || {};
async function sarPollQuotes() {
    for (const inst of SAR_INSTANCE_ORDER) {
        const cfg = sarCfg(inst);
        try {
            const r = await fetch('http://' + window.location.hostname + ':5050/api/quote?ticker=' + cfg.ticker, {signal: AbortSignal.timeout(4000)});
            if (!r.ok) continue;
            const q = await r.json();
            if (q.last > 0) {
                window._sarLivePrices[inst] = q.last.toFixed(2);
                // F-079 fix4: обновляем td только если текст реально меняется (нет мигания)
                const el = document.getElementById('sarLivePrice_' + inst);
                if (el && el.textContent !== window._sarLivePrices[inst]) {
                    el.textContent = window._sarLivePrices[inst];
                }
            }
        } catch (e) { /* тихо: следующий тик через 5с */ }
    }
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
    _sarChartDestroy();
    const panel = document.getElementById('sarEditPanel');
    if (panel) { panel.remove(); sarPanelInst = null; }
    if (window._sarPanelTimer) { clearInterval(window._sarPanelTimer); window._sarPanelTimer = null; }
    if (window._sarChartTimer) { clearInterval(window._sarChartTimer); window._sarChartTimer = null; }
    if (window._sarJournalTimer) { clearInterval(window._sarJournalTimer); window._sarJournalTimer = null; }
}, true);

// селектор в шапке вкладки: открыть панель выбранного экземпляра
document.addEventListener('change', function (ev) {
    if (ev.target && ev.target.id === 'sarInstSelect') sarSwitchInstance(ev.target.value);
});

// автостарт polling (вкладка может быть скрыта — polling лёгкий, 2 req/5с)
document.addEventListener('DOMContentLoaded', () => setTimeout(sarStartPolling, 1500));


// ============================================================================
// === График инструмента: lightweight-charts (как вкладка «График») ===
// === zoom/pan из коробки; данные: /api/candles (C#) + индикаторы SAR/EMA ===
// === по параметрам ЭТОГО экземпляра; позиции — маркеры из /journal. ===
// ============================================================================



const MSK_OFFSET_SAR = 10800;   // F-069: MSK offset (локальная константа, не из chart.js)
let _sarChartRefreshTimer = null;

function _sarChartDestroy() {
    try { if (window._sarLC && window._sarLC.chart) { try { window._sarLC.chart.remove(); } catch (e) {} } } catch (e) {}
    window._sarLC = null; window._sarLCInst = null; window._sarLCStopLine = null;
    if (_sarChartRefreshTimer) { clearInterval(_sarChartRefreshTimer); _sarChartRefreshTimer = null; }
}

function _sarClosePanelDom() {
    if (window._sarJournalTimer) { clearInterval(window._sarJournalTimer); window._sarJournalTimer = null; }
    _sarChartDestroy();
    const panel = document.getElementById('sarEditPanel');
    if (panel) panel.remove();
    sarPanelInst = null;
}

function sarClosePanel() { _sarClosePanelDom(); }

function _sarChartEnsure(inst) {
    if (window._sarLC) return window._sarLC;
    const host = document.getElementById('sarChartHost');
    if (!host || !host.offsetWidth) return null;
    const w = host.clientWidth || 900, h = host.clientHeight || 320;
    const chart = LightweightCharts.createChart(host, {
        width: w, height: h,
        layout: { background: { color: '#121826' }, textColor: '#9CA3AF' },
        grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        timeScale: { timeVisible: true, secondsVisible: false },
        localization: { timeFormatter: t => new Date(t * 1000).toISOString().slice(11, 16),
                        dateFormat: t => new Date(t * 1000).toISOString().slice(0, 10) },
    });
    const candle = chart.addCandlestickSeries({
        upColor: '#22C55E', downColor: '#EF4444',
        borderUpColor: '#22C55E', borderDownColor: '#EF4444',
        wickUpColor: '#22C55E', wickDownColor: '#EF4444',
    });
    const sar = chart.addLineSeries({ color: '#FFD700', lineWidth: 1, priceLineVisible: false, lastValueVisible: false, title: 'SAR' });
    const ema = chart.addLineSeries({ color: '#00BFFF', lineWidth: 2, priceLineVisible: false, lastValueVisible: false, title: 'EMA' });
    window._sarLC = { chart, candle, sar, ema };
    window._sarLCInst = inst;
    // страховка: попап удалён мимо наших обработчиков → убить chart
    const mo = new MutationObserver(() => {
        if (!document.getElementById('sarEditPanel')) { _sarChartDestroy(); mo.disconnect(); }
    });
    mo.observe(document.body, { childList: true });
    return window._sarLC;
}

async function _sarFetchCandles(inst, cfg, tf) {
    // 1) C# сервер (Finam REST, лучший источник; на зеркалах 401/404 — молча идём дальше)
    try {
        const r = await fetch('/api/candles?ticker=' + cfg.ticker + '&tf=' + tf + '&days=30', { signal: AbortSignal.timeout(6000) });
        if (r.ok) {
            const arr = await r.json();
            if (Array.isArray(arr) && arr.length)
                return { src: 'ЦБ /api/candles', bars: arr.map(x => ({ t: x.t + MSK_OFFSET_SAR, o: x.o, h: x.h, l: x.l, c: x.c, v: x.v || 0 })) };
        }
    } catch (e) {}
    // 2) DataProvider :5060 (работает при живом DP независимо от робота)
    try {
        const r2 = await fetch('http://' + window.location.hostname + ':5060/candles/' + cfg.ticker + '@MISX?tf=H1&limit=300', { signal: AbortSignal.timeout(6000) });
        if (r2.ok) {
            const d2 = await r2.json();
            if (d2 && Array.isArray(d2.bars) && d2.bars.length)
                return { src: 'DP :5060', bars: d2.bars.map(b => ({
                    t: Math.floor(new Date(b.timestamp.replace(/\.\d+/, '').replace('T', ' ')).getTime() / 1000) + MSK_OFFSET_SAR,
                    o: b.open, h: b.high, l: b.low, c: b.close, v: b.volume || 0 })) };
        }
    } catch (e) {}
    // 3) сам робот /bars (может быть оффлайн — тогда已是 без свечей)
    try {
        const r3 = await fetch(sarApi(inst) + '/bars?tf_min=' + tf + '&limit=300', { signal: AbortSignal.timeout(6000) });
        if (r3.ok) {
            const d3 = await r3.json();
            if (d3 && d3.bars && d3.bars.length)
                return { src: 'робот /bars', bars: d3.bars.map(b => ({
                    t: Math.floor(new Date(b.t.replace(' ', 'T') + 'Z').getTime() / 1000) + MSK_OFFSET_SAR,
                    o: b.o, h: b.h, l: b.l, c: b.c, v: b.v || 0 })) };
        }
    } catch (e) {}
    return null;
}

// --- браузерный расчёт SAR/EMA (как indicators.js, но привязан к фактическим точкам) ---
function _sarLocalPSAR(bars, af0, afStep, afMax) {
    if (bars.length < 2) return [];
    let af = af0, isLong = bars[1].c > bars[0].c;
    let sar = isLong ? bars[0].l : bars[0].h;
    let ep = isLong ? bars[0].h : bars[0].l;
    const out = [];
    for (let i = 1; i < bars.length; i++) {
        const prev = sar;
        sar = prev + af * (ep - prev);
        if (isLong) {
            sar = Math.min(sar, bars[i-1].l, i >= 2 ? bars[i-2].l : bars[i-1].l);
            if (bars[i].l < sar) { isLong = false; sar = ep; ep = bars[i].l; af = af0; }
            else if (bars[i].h > ep) { ep = bars[i].h; af = Math.min(af + afStep, afMax); }
        } else {
            sar = Math.max(sar, bars[i-1].h, i >= 2 ? bars[i-2].h : bars[i-1].h);
            if (bars[i].h > sar) { isLong = true; sar = ep; ep = bars[i].h; af = af0; }
            else if (bars[i].l < ep) { ep = bars[i].l; af = Math.min(af + afStep, afMax); }
        }
        out.push({ t: bars[i].t, value: sar });
    }
    return out;
}

function _sarLocalEMA(bars, period) {
    if (bars.length < period) return [];
    const k = 2 / (period + 1);
    let e = 0;
    for (let i = 0; i < period; i++) e += bars[i].c;
    e /= period;
    const out = [{ t: bars[period-1].t, value: e }];
    for (let i = period; i < bars.length; i++) { e = bars[i].c * k + e * (1 - k); out.push({ t: bars[i].t, value: e }); }
    return out;
}

async function sarChartLoad(inst) {
    inst = inst || sarPanelInst;
    if (!inst) return;
    const cfg = sarCfg(inst);
    const info = document.getElementById('sarChartInfo');
    const tfSel = document.getElementById('sarChartTf');
    // F-163: TF графика по умолчанию = tf_min робота (/params); ручной выбор пользователя сохраняется
    let tf = tfSel ? (parseInt(tfSel.value, 10) || 0) : 0;
    const lsKey = 'sarChartTf_' + inst;
    const lsTf = parseInt(localStorage.getItem(lsKey) || '0', 10);
    const userPicked = tfSel && tfSel.dataset.auto === '0'; // менял вручную в этой сессии
    if (!userPicked && lsTf > 0 && tfSel) { tf = lsTf; tfSel.value = String(lsTf); } // F-163: сохранённый выбор
    const needAuto = !userPicked && lsTf <= 0;
    if (!tf || needAuto) {
        try {
            const pr = await fetch(sarApi(inst) + '/params', { signal: AbortSignal.timeout(4000) });
            const pp = await pr.json();
            const tfr = parseInt(pp.tf_min, 10);
            if (tfr > 0 && tfSel) {
                if ([...tfSel.options].some(o => parseInt(o.value,10) === tfr)) {
                    tfSel.value = String(tfr);
                    tfSel.dataset.auto = '0';
                    localStorage.setItem(lsKey, String(tfr)); // F-163: сохранить авто-TF
                }
            }
        } catch (e) {}
        tf = tfSel ? (parseInt(tfSel.value, 10) || 60) : 60;
    }
    const tickerEl = document.getElementById('sarChartTicker');
    if (tickerEl) tickerEl.textContent = cfg.label + ' · порт ' + cfg.port;
    if (info) info.textContent = 'Загрузка графика...';
    window._sarLastClose = window._sarLastClose || {};

    if (window._sarLC && window._sarLCInst !== inst) _sarChartDestroy();
    if (!document.getElementById('sarEditPanel')) return;
    let lc = _sarChartEnsure(inst);
    if (!lc) {
        await new Promise(r => setTimeout(r, 150));
        if (!document.getElementById('sarEditPanel')) return;
        lc = _sarChartEnsure(inst);
        if (!lc) { if (info) info.textContent = 'График: контейнер не виден'; return; }
    }
    if (sarPanelInst !== inst) return;

    try {
        const got = await _sarFetchCandles(inst, cfg, tf);
        if (sarPanelInst !== inst) return;
        if (!got) {
            lc.candle.setData([]); lc.sar.setData([]); lc.ema.setData([]);
            if (info) info.textContent = 'Нет данных: /api/candles, DP :5060 и робот недоступны';
            return;
        }
        // дедуп + сортировка по времени
        const seen = new Set();
        const bars = got.bars.filter(b => b.t > 0 && !seen.has(b.t) && seen.add(b.t)).sort((a, b) => a.t - b.t);
        lc.candle.setData(bars.map(b => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
        try {
            const from = Math.max(0, bars.length - 60);
            lc.chart.timeScale().setVisibleLogicalRange({ from: from - 1, to: bars.length + 2 });
        } catch (e) { lc.chart.timeScale().fitContent(); }
        window._sarLastClose[inst] = bars.length ? bars[bars.length - 1].c : null;

        // SAR/EMA — ВСЕГДА считаем в браузере из фактических свечей (не зависим от робота)
        const raw = bars.map(b => ({ t: b.t - MSK_OFFSET_SAR, h: b.h, l: b.l, c: b.c }));
        const sarP = {
            start: parseFloat((document.getElementById('sarAf0') || {}).value) || 0.02,
            step: parseFloat((document.getElementById('sarStep') || {}).value) || 0.2,
            max: parseFloat((document.getElementById('sarMax') || {}).value) || 0.2,
        };
        const emaN = parseInt((document.getElementById('sarEmaN') || {}).value) || 12;
        const sarPts = _sarLocalPSAR(raw, sarP.start, sarP.step, sarP.max).map(d => ({ time: d.t + MSK_OFFSET_SAR, value: d.value }));
        const emaPts = _sarLocalEMA(raw, emaN).map(d => ({ time: d.t + MSK_OFFSET_SAR, value: d.value }));
        lc.sar.setData(sarPts);
        lc.ema.setData(emaPts);

        // маркеры сделок из журнала + линия стопа/входа
        if (lc.candle.setMarkers) {
            const markers = [];
            (_sarJournalClosed || []).forEach(r => {
                const tOpen = Math.floor(new Date((r.date_open || '') + 'T' + (r.time_open || '00:00:00')).getTime() / 1000) + MSK_OFFSET_SAR;
                if (Number.isFinite(tOpen) && r.entry) {
                    markers.push({ time: tOpen, position: r.dir === 'L' ? 'belowBar' : 'aboveBar',
                                   color: r.dir === 'L' ? '#3ecf8e' : '#ef6461',
                                   shape: r.dir === 'L' ? 'arrowUp' : 'arrowDown', text: (r.dir === 'L' ? 'L ' : 'S ') + r.qty + 'шт' });
                }
                if (r.close) {
                    const tClose = Math.floor(new Date((r.close.date_close || '') + 'T' + (r.close.time_close || '00:00:00')).getTime() / 1000) + MSK_OFFSET_SAR;
                    if (Number.isFinite(tClose) && r.close.exit) {
                        markers.push({ time: tClose, position: 'aboveBar', color: '#e6ebf5', shape: 'circle', text: (r.close.pnl >= 0 ? '+' : '') + r.close.pnl.toFixed(0) + '₽' });
                    }
                }
            });
            markers.sort((a, b) => a.time - b.time);
            lc.candle.setMarkers(markers);
        }
        if (lc.candle.createPriceLine) {
            if (window._sarLCStopLine) { try { lc.candle.removePriceLine(window._sarLCStopLine); } catch (e) {} window._sarLCStopLine = null; }
            const st = window['sarRobot_' + inst];
            const openPos = (_sarJournalOpen && _sarJournalOpen[0]) || null;
            if (openPos && st && st.position && st.position.stop) {
                window._sarLCStopLine = lc.candle.createPriceLine({ price: st.position.stop, color: '#ef6461', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, title: 'стоп' });
            } else if (openPos && openPos.entry) {
                window._sarLCStopLine = lc.candle.createPriceLine({ price: openPos.entry, color: '#e6ebf5', lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: 'вход ' + openPos.dir });
            }
        }
        if (info) info.textContent = cfg.label + ' · ' + (tf < 60 ? 'M' + tf : 'H' + tf / 60) + ' · ' + bars.length + ' баров (' + got.src + ') · SAR/EMA: локальный расчёт';

        if (_sarChartRefreshTimer) clearInterval(_sarChartRefreshTimer);
        _sarChartRefreshTimer = setInterval(() => {
            if (!document.getElementById('sarEditPanel') || !sarPanelInst) { clearInterval(_sarChartRefreshTimer); _sarChartRefreshTimer = null; return; }
            sarChartLoad(sarPanelInst);
        }, 60000);
    } catch (e) {
        if (info) info.textContent = 'Ошибка графика: ' + e.message;
    }
}

// F-149: кнопка очистки торгового журнала (делегированный обработчик — панель перерисовывается)
document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('#sarJournalClear');
    if (!btn) return;
    ev.preventDefault();
    if (!confirm('Очистить торговый журнал? Все записи будут удалены безвозвратно.')) return;
    try {
        const instSel = document.getElementById('sarInstSelect');
        const inst = instSel ? instSel.value : 'mtlr';
        let ok = false, errTxt = '';
        try { const r = await fetch('/api/sarema/journal/' + inst, { method: 'DELETE' }); ok = r.ok; if (!ok) errTxt = r.status; }
        catch (e1) {
            try { const r2 = await fetch(sarApi(inst) + '/journal', { method: 'DELETE' }); ok = r2.ok; if (!ok) errTxt = r2.status; }
            catch (e2) { errTxt = e2.message; }
        }
        if (!ok) { alert('Не удалось очистить: ' + errTxt); return; }
        if (typeof window.sarRenderJournal === 'function') { try { window.sarRenderJournal(); } catch (e3) {} }
        if (typeof sarLoadJournal === 'function') { try { sarLoadJournal(); } catch (e4) {} }
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { alert('Не удалось очистить: ' + (d.detail || r.status)); return; }
        // перерисовать журнал
        if (typeof window.sarRenderJournal === 'function') window.sarRenderJournal();
        else location.reload();
    } catch (e) {
        alert('Ошибка: ' + e.message);
    }
});

// F-163: быстрый reload графика (переключение TF) — sarChartLoad сам решает: если инстанс тот же,
// график НЕ дестроится и данные обновятся через setData (быстро даже в Я.браузере).
window.sarChartReload = function() {
    if (typeof sarPanelInst !== 'undefined' && sarPanelInst && typeof sarChartLoad === 'function') {
        sarChartLoad(sarPanelInst);
    }
};

// F-182: Сделки брокера — выбор периода и загрузка ленты
document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest('#sarTradesLoad');
    if (!btn) return;
    ev.preventDefault();
    const fromV = document.getElementById('sarTradesFrom')?.value || '';
    const toV = document.getElementById('sarTradesTo')?.value || '';
    const tbl = document.getElementById('sarTradesTable');
    if (!tbl) return;
    tbl.innerHTML = '<tr><td colspan="5" class="text-secondary" style="padding:8px">Загрузка...</td></tr>';
    try {
        const inst = (typeof sarPanelInst !== 'undefined' && sarPanelInst) ? sarPanelInst : 'mtlr';
        const cfgC = sarCfg(inst);
        const qs = '/bars-journal-trades?' + new URLSearchParams({ from: fromV, to: toV });
        const data = await fetchJson('/api/sarema/journal-trades/' + inst + (fromV || toV ? '?' + new URLSearchParams({ from: fromV, to: toV }) : ''), 30000).catch(async () =>
            await fetchJson(sarApi(inst) + '/journal_trades?' + new URLSearchParams({ from_date: fromV, to_date: toV }), 30000));
        const rows = (data.trades || []).map(t => {
            const sideCls = t.side === 'BUY' ? 'color:var(--green)' : 'color:var(--red)';
            const sideRu = t.side === 'BUY' ? 'Покупка' : 'Продажа';
            return '<tr>' +
                '<td style="padding:4px 8px">' + t.time + '</td>' +
                '<td style="padding:4px 8px;color:' + sideCls + '">' + sideRu + '</td>' +
                '<td style="padding:4px 8px;text-align:right">' + t.qty + '</td>' +
                '<td style="padding:4px 8px;text-align:right">' + t.price.toFixed(2) + '</td>' +
                '<td style="padding:4px 8px" class="text-secondary">' + (t.comment || '—') + '</td></tr>';
        });
        tbl.innerHTML = rows.length ? rows.join('') :
            '<tr><td colspan="5" class="text-secondary" style="padding:8px">Сделок за период нет</td></tr>';
    } catch (e) {
        tbl.innerHTML = '<tr><td colspan="5" style="padding:8px;color:var(--red)">Ошибка: ' + e.message + '</td></tr>';
    }
});
// дефолтные даты: сегодня
document.addEventListener('DOMContentLoaded', () => {
    const setD = (id, v) => { const el = document.getElementById(id); if (el && !el.value) el.value = v; };
    const today = new Date().toISOString().slice(0, 10);
    setD('sarTradesFrom', today); setD('sarTradesTo', today);
});
