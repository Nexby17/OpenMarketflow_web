// ============================================================================
// === F-061: SAREMA — Роботы Акции (SAR×EMA H1, per-ticker инстансы) ===
// === 1 робот = 1 акция; экземпляры на портах 5084-5089/5094-5097. ===
// === Лимиты Дмитрия: max_qty (штуки, null=∞, 0=запрет), max_value_rub (₽). ===
// === Паттерн мультиинстанса: OF_BR_INSTANCES / ARB_INSTANCE_MAP. ===
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
let sarPanelInst = null;
let _sarTimer = null;

function sarCfg(inst) { return SAR_INSTANCES[inst]; }
function sarApi(inst) { return 'http://' + window.location.hostname + ':' + sarCfg(inst).port; }

function sarLog(msg, level) {
    if (typeof arbPyLog === 'function') arbPyLog(msg, level, 'sarLogContainer');
    else console.log('[SAREMA]', level, msg);
}

// ---------- переключение инстанса в панели ----------
// Форма всегда жёстко перепривязывается к выбранному экземпляру:
// 1) очистка всех полей; 2) предзаполнение ТОЛЬКО его данными:
//    онлайн /status → его /params (GET) → его черновик localStorage → дефолт.
// Так значения одного робота не могут уйти другому при сохранении.
const SAR_FIELDS = ['sarMaxQty', 'sarMaxValue', 'sarBase', 'sarRiskPct', 'sarDayKill', 'sarLiqFrac'];

function sarClearPanelForm() {
    SAR_FIELDS.forEach(id => { el(id).value = ''; });
    el('sarPanelMsg').textContent = '';
}

function sarFillPanelFromStatus(inst, st) {
    if (st && st.risk) {
        el('sarBase').value = st.risk.base ?? '';
        el('sarRiskPct').value = st.risk.risk_pct != null ? (st.risk.risk_pct * 100).toFixed(1) : '';
        el('sarDayKill').value = st.risk.day_kill_R ?? '';
    }
    if (st && st.limits) {
        el('sarMaxQty').value = st.limits.max_qty ?? '';
        el('sarMaxValue').value = st.limits.max_value_rub ?? '';
        el('sarLiqFrac').value = st.limits.liq_frac ?? 0.05;
    }
}

function sarFillPanelFromDraft(inst) {
    const raw = localStorage.getItem('sarSavedParams_' + inst);
    if (!raw) return false;
    try {
        const d = JSON.parse(raw);
        el('sarMaxQty').value = d.max_qty ?? '';
        el('sarMaxValue').value = d.max_value_rub ?? '';
        el('sarBase').value = d.base ?? '';
        el('sarRiskPct').value = d.risk_pct != null ? (d.risk_pct * 100).toFixed(1) : '';
        el('sarDayKill').value = d.day_kill_R ?? '';
        el('sarLiqFrac').value = d.liq_frac ?? 0.05;
        return true;
    } catch (e) { return false; }
}

async function sarSwitchInstance(inst) {
    sarPanelInst = inst;
    const cfg = sarCfg(inst);
    document.getElementById('sarPanelCard').style.display = '';
    document.getElementById('sarPanelTicker').textContent = cfg.label;
    el('sarRoParams').textContent = 'SAR 0.02/0.2/0.2 · EMA12 · стоп 3×ATR · H1 · std-полярность · трейлинг PSAR';
    // 1) полная очистка — ничего от предыдущего робота не остаётся
    sarClearPanelForm();
    el('sarPanelMsg').textContent = '⏳ Загрузка параметров ' + cfg.label + '…';
    el('sarLiqFrac').value = 0.05;                      // дефолт
    el('sarBase').value = 1000000;                      // дефолт
    // 2) его черновик (оффлайн-сохранение) поверх дефолтов
    sarFillPanelFromDraft(inst);
    // 3) живой робот: /status + /params (его собственные значения) — источник истины
    try {
        const resp = await fetch(sarApi(inst) + '/params', {signal: AbortSignal.timeout(2500)});
        if (resp.ok) {
            const p = await resp.json();
            el('sarMaxQty').value = p.max_qty ?? '';
            el('sarMaxValue').value = p.max_value_rub ?? '';
            el('sarBase').value = p.base ?? 1000000;
            el('sarRiskPct').value = p.risk_pct != null ? (p.risk_pct * 100).toFixed(1) : '';
            el('sarDayKill').value = p.day_kill_R ?? '';
            el('sarLiqFrac').value = p.liq_frac ?? 0.05;
            el('sarPanelMsg').textContent = '';
        } else {
            el('sarPanelMsg').textContent = '⚠️ Робот оффлайн — показаны сохранённые значения (применятся при его старте)';
        }
    } catch (e) {
        el('sarPanelMsg').textContent = '⚠️ Робот оффлайн — показаны сохранённые значения (применятся при его старте)';
    }
    // 4) подсветка активной строки
    document.querySelectorAll('#sarRobotsTable tr').forEach(tr => tr.style.background = '');
    const tbody = el('sarRobotsTable');
    const row = tbody && tbody.rows ? tbody.rows[SAR_INSTANCE_ORDER.indexOf(inst)] : null;
    if (row) row.style.background = 'rgba(79,143,247,.12)';
    // селектор синхронно
    const sel = el('sarInstSelect');
    if (sel && sel.value !== inst) sel.value = inst;
}

function sarClosePanel() {
    document.getElementById('sarPanelCard').style.display = 'none';
    sarPanelInst = null;
}

// ---------- сохранение лимитов (только открытый инстанс — урок 22.09) ----------
async function sarSaveParams() {
    const inst = sarPanelInst;
    if (!inst) { return; }
    const cfg = sarCfg(inst);
    // защита от гонки: панель может быть переключена во время сохранения
    const sentFor = inst;
    const numOrNull = (id, integer) => {
        const raw = el(id).value.trim();
        if (raw === '') return null;                       // пусто = без лимита
        const n = parseFloat(String(raw).replace(',', '.'));
        if (!isFinite(n) || n < 0) return undefined;       // ошибка валидации
        return integer ? Math.floor(n) : n;
    };
    const maxQty = numOrNull('sarMaxQty', true);
    const maxValue = numOrNull('sarMaxValue', false);
    const baseRaw = el('sarBase').value.trim();
    const riskRaw = el('sarRiskPct').value.trim().replace(',', '.');
    const killRaw = el('sarDayKill').value.trim().replace(',', '.');
    const liqRaw = el('sarLiqFrac').value.trim().replace(',', '.');
    const msg = el('sarPanelMsg');
    if (maxQty === undefined || maxValue === undefined) { msg.textContent = '❌ Лимиты: число ≥ 0 или пусто (без лимита)'; return; }
    if (riskRaw !== '' && (!isFinite(parseFloat(riskRaw)) || parseFloat(riskRaw) <= 0 || parseFloat(riskRaw) > 10)) { msg.textContent = '❌ Риск %: (0, 10]'; return; }
    if (killRaw !== '' && !isNaN(parseFloat(killRaw)) && parseFloat(killRaw) >= 0) { msg.textContent = '❌ Kill-switch: отрицательный R или пусто (выкл)'; return; }
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
            msg.textContent = '❌ Сервер отклонил: ' + errs;
            return;
        }
        if (sarPanelInst !== sentFor) return;      // панель уже переключили — не пугаем чужим сообщением
        msg.textContent = '✅ Параметры сохранены (' + cfg.label + ')';
        sarLog('SAREMA[' + cfg.label + ']: параметры обновлены: ' + JSON.stringify(patch), 'INFO');
    } catch (e) {
        // робот оффлайн: сохраняем в localStorage ЭТОГО экземпляра, приём при первом /status (паттерн OF-BR)
        localStorage.setItem('sarSavedParams_' + inst, JSON.stringify(patch));
        if (sarPanelInst === sentFor) msg.textContent = '⚠️ Робот оффлайн — параметры применятся при его старте';
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
        window[cfg.stateKey || ('sarRobot_' + inst)] = await resp.json();
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
    const tbody = el('sarRobotsTable');
    if (!tbody) return;
    const noMsg = el('sarNoRobots');
    let rows = '';
    for (const inst of SAR_INSTANCE_ORDER) {
        const cfg = sarCfg(inst);
        const st = window['sarRobot_' + inst];
        const activeBg = (sarPanelInst === inst) ? ' style="background:rgba(79,143,247,.12)"' : '';
        if (!st) {
            noMsg.style.display = '';
            rows += `<tr${activeBg} ondblclick="sarSwitchInstance('${inst}')" style="cursor:pointer" title="Двойной клик — параметры экземпляра (доступны оффлайн)">
                <td><strong>${cfg.label}</strong></td>
                <td class="red">● оффлайн</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>
                <td><span class="text-secondary">нет связи :${cfg.port}</span></td>
            </tr>`;
            continue;
        }
        noMsg.style.display = 'none';
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
        rows += `<tr${activeBg} ondblclick="sarSwitchInstance('${inst}')" style="cursor:pointer" title="Двойной клик — параметры экземпляра">
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
    // селектор панели
    const sel = el('sarInstSelect');
    if (sel && (!sel.options || sel.options.length !== SAR_INSTANCE_ORDER.length)) {
        sel.innerHTML = SAR_INSTANCE_ORDER.map(k => `<option value="${k}">${sarCfg(k).label} :${sarCfg(k).port}</option>`).join('');
        if (sarPanelInst) sel.value = sarPanelInst;
    }
}

function sarStartPolling() {
    if (_sarTimer) clearInterval(_sarTimer);
    sarPollAll();
    _sarTimer = setInterval(sarPollAll, 5000);
}

// --- Надёжное переключение на вкладку «Роботы Акции» ---
// Штатный обработчик app.js иногда не срабатывает для этой кнопки (событие доходит,
// но active не переключается). Ставим собственный обработчик в capture-фазе: он
// выполняет ту же логику и НЕ мешает остальным вкладкам.
document.addEventListener('click', function (ev) {
    const tab = ev.target.closest('button[data-tab="stocksrobots"]');
    if (!tab) return;
    // не мешаем остальным вкладкам: снимаем aria только с других вкладок
    document.querySelectorAll('.tab').forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    tab.setAttribute('aria-selected', 'true');
    const content = document.getElementById('stocksrobots');
    if (content) content.classList.add('active');
}, true);

// --- Отображение панели параметров: синхронизировано с переключением вкладок ---
// (панель внутри #stocksrobots; двойной клик по строке открывает её)
// Делегированный обработчик: работает и когда строка перерисована между кликами (урок 22.09)
document.addEventListener('dblclick', function (ev) {
    const tr = ev.target.closest('#sarRobotsTable tr');
    if (!tr) return;
    const inst = SAR_INSTANCE_ORDER[tr.rowIndex - 1];   // rowIndex с учётом thead
    if (inst) sarSwitchInstance(inst);
});
document.addEventListener('click', function (ev) {
    // если пользователь кликнул ДРУГУЮ вкладку — скрыть панель вместе с вкладкой (её состояние сохраняется)
    const other = ev.target.closest('button[data-tab]:not([data-tab="stocksrobots"])');
    if (!other) return;
    const card = document.getElementById('sarPanelCard');
    if (card) card.style.display = 'none';
}, true);

// автостарт polling при загрузке (вкладка может быть скрыта — polling лёгкий, 2 req/5с)
document.addEventListener('DOMContentLoaded', () => setTimeout(sarStartPolling, 1500));
