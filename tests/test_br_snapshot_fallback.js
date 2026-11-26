// F-245: проверка фолбэка снапшота в ofBrRobotPoll.
// Сценарий: браузер не может достучаться до порта робота (fetch бросает), но /data/<name>.json
// с того же origin отдаётся — строка должна получить данные, а не «нет связи».
const fs = require('fs');
const vm = require('vm');
const APP = process.argv[2];

function sliceFn(src, name) {
    const re = new RegExp('(?:async )?function ' + name + '\\s*\\(');
    const m = re.exec(src);
    if (!m) throw new Error('function not found: ' + name);
    let i = m.index, depth = 0, started = false, inS = null, inT = false, inC = null;
    for (let k = i; k < src.length; k++) {
        const ch = src[k], nx = src[k + 1];
        if (inS) { if (ch === '\\') { k++; continue; } if (ch === inS) inS = null; continue; }
        if (inT) { if (ch === '\\') { k++; continue; } if (ch === '`') inT = false; continue; }
        if (inC === '//') { if (ch === '\n') inC = null; continue; }
        if (inC === '/*') { if (ch === '*' && nx === '/') { inC = null; k++; } continue; }
        if (ch === '/' && nx === '/') { inC = '//'; k++; continue; }
        if (ch === '/' && nx === '*') { inC = '/*'; k++; continue; }
        if (ch === '"' || ch === "'") { inS = ch; continue; }
        if (ch === '`') { inT = true; continue; }
        if (ch === '{') { depth++; started = true; }
        else if (ch === '}') { depth--; if (started && depth === 0) return src.slice(i, k + 1); }
    }
    throw new Error('unterminated function ' + name);
}

const src = fs.readFileSync(APP, 'utf8');
if (!/var _ROBOT_START_LOG\s*=\s*\[\];/.test(src)) throw new Error('no _ROBOT_START_LOG');
const code = 'var _ROBOT_START_LOG = [];\n' +
    ['makeTimeoutSignal', 'robotStartLog', 'ofBrRobotPoll'].map(n => sliceFn(src, n)).join('\n');

const sandbox = {
    console,
    setTimeout: (fn, ms) => 0,
    clearTimeout: () => {},
    document: { getElementById: () => null },
    localStorage: { getItem: () => null, removeItem: () => {}, setItem: () => {} },
    window: { location: { hostname: 'localhost' } },
    addLog: () => {},
    nowTime: () => '14:00:00',
    renderRobots: () => {},
    AbortController, AbortSignal,
    OF_BR_INSTANCES: {
        edp2: { procName: 'of_br_edp2', stateKey: 'ofBrRobot_edp2', label: 'BRF7 edp-2',
                api: () => 'http://localhost:5086' }
    },
    ofBrCfg: (inst) => sandbox.OF_BR_INSTANCES[inst],
    _ofBrWasOnline_edp2: false,
};
vm.createContext(sandbox);

const calls = [];
sandbox.fetch = async (url, opts) => {
    const u = String(url);
    calls.push(u);
    const J = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
    if (u.indexOf(':5086/status') >= 0) throw new Error('Failed to fetch');       // порт недоступен
    if (u.indexOf('/data/of_br_edp2.json') >= 0) {
        return J(200, { dir: 1, direction: 'LONG', totalLots: 2, brokerLots: 2, mode: 'running',
                        _snapshot_ts: Date.now() - 1500, avgPrice: 90.1, realizedPnL: 12.3 });
    }
    return J(404, {});
};

vm.runInContext(code + '\nglobalThis.__poll = ofBrRobotPoll;', sandbox);

(async () => {
    await sandbox.__poll('edp2');
    const state = sandbox.window['ofBrRobot_edp2'];
    const err = sandbox.window['_ofBrLastErr_edp2'];
    const fails = sandbox.window['_ofBrFail_edp2'];
    const lines = [];
    lines.push('вызовы fetch: ' + calls.join(' | '));
    lines.push('данные получены: ' + (state ? ('да (' + state.direction + ' ' + state.totalLots + '/' + state.brokerLots + ')') : 'НЕТ'));
    lines.push('причина помечена: ' + (err || '—'));
    lines.push('счётчик сбоев: ' + fails);
    const ok = !!state && state.totalLots === 2 && state.brokerLots === 2 && fails === 0;
    lines.push('РЕЗУЛЬТАТ: ' + (ok ? 'PASS (строка получит реальные лоты через снапшот)' : 'FAIL'));
    fs.writeFileSync(process.argv[3], lines.join('\n') + '\n', 'utf8');
    process.exit(ok ? 0 : 1);
})();
