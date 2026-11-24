// F-228: прогон РЕАЛЬНЫХ функций app.js в фейковом браузере (DOM + fetch).
// Извлекаем showToast, robotStartLog, makeTimeoutSignal, ofMxRobotApi из настоящего файла
// и выполняем их в песочнице. Любое исключение пишется в выходной файл.
const fs = require('fs');
const vm = require('vm');
const APP = process.argv[2];
const SCENARIO = process.argv[3] || 'alive-ok';
const OUT = process.argv[4];

const lines = [];
const say = (s) => lines.push(s);

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

function finish() {
    try { fs.writeFileSync(OUT, lines.join('\n') + '\n', 'utf8'); } catch (e) { console.error('write fail', e.message); }
}

try {
    const src = fs.readFileSync(APP, 'utf8');
    // var _ROBOT_START_LOG стоит ОТДЕЛЬНОЙ строкой перед функцией — без неё журнал в песочнице пуст.
    if (!/var _ROBOT_START_LOG\s*=\s*\[\];/.test(src)) throw new Error('в app.js нет объявления var _ROBOT_START_LOG = [];');
    const code = 'var _ROBOT_START_LOG = [];\n' + ['showToast', 'robotStartLog', 'makeTimeoutSignal', 'ofMxRobotApi'].map(n => sliceFn(src, n)).join('\n');

    // ---------- фейковый браузер ----------
    const journal = [];      // addLog
    const toasts = [];       // {text, timers}
    const timers = [];
    const host = {
        style: {},
        appendChild(t) { toasts.push(t); },
    };
    const doc = {
        title: 'OpenMarketflow',
        getElementById(id) { return id === 'toastHost' ? host : null; },
        createElement() { return { style: {}, textContent: '', remove() {} }; },
        body: { appendChild() {} },
        querySelector() { return null; },
    };

    const sandbox = {
        console,
        setTimeout: (fn, ms) => {
            timers.push(ms);
            if (typeof fn === 'function') { try { fn(); } catch (e) { lines.push('TIMER CB ERROR: ' + (e && e.message)); } }
            return 0;
        },
        clearTimeout: () => {},
        document: doc,
        localStorage: { getItem: () => null, setItem: () => {} },
        window: { location: { hostname: 'localhost' } },
        addLog: (t, lvl, m) => journal.push(lvl + ' ' + m),
        nowTime: () => '19:30:00',
        ofMxRobotPoll: async () => {},
        renderRobots: () => {},
        OF_MX_ROBOT_API: 'http://localhost:5081',
    };
    if (SCENARIO !== 'old-browser') {
        sandbox.AbortController = AbortController;
        sandbox.AbortSignal = AbortSignal;
    }
    vm.createContext(sandbox);
    vm.runInContext(code + '\nglobalThis.__A = ofMxRobotApi;', sandbox);

    // ---------- fetch ----------
    const calls = [];
    sandbox.fetch = async (url, opts) => {
        const u = String(url);
        calls.push(((opts && opts.method) || 'GET') + ' ' + u);
        const J = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
        if (u.endsWith('/health')) {
            if (SCENARIO === 'robot-dead') throw new Error('ECONNREFUSED');
            if (SCENARIO === 'session-401') throw new Error('ECONNREFUSED');   // робот мёртв + сессия истекла
            return J(200, { ok: true });
        }
        if (u.includes('/api/of/process/of_mx/start')) {
            if (SCENARIO === 'session-401') return J(401, { error: 'Unauthorized' });
            return J(200, { ok: true });
        }
        if (u.endsWith('/account')) return J(200, { ok: true, account: 'edp', id: '2049688' });
        if (u.endsWith('/status')) return J(200, { mode: 'running' });
        if (u.endsWith('/start')) return J(200, { ok: true, mode: 'running' });
        return J(200, {});
    };

    (async () => {
        say('СЦЕНАРИЙ: ' + SCENARIO);
        let thrown = null;
        try { await sandbox.__A('start'); } catch (e) { thrown = e; }
        say('  исключение: ' + (thrown ? String(thrown && thrown.stack || thrown).slice(0, 300) : 'нет'));
        say('  вызовы fetch: ' + calls.map(c => c.replace('http://localhost', '')).join('  |  '));
        say('  /start отправлен: ' + calls.some(c => c === 'POST http://localhost:5081/start'));
        say('  тосты (' + toasts.length + '): ' + JSON.stringify(toasts.map(t => t.textContent)));
        say('  таймеры тостов (мс): ' + JSON.stringify(timers));
        const errs = journal.filter(l => l.startsWith('ERROR'));
        say('  ERROR в журнале: ' + (errs.length ? errs.join(' || ') : 'нет'));
        var rl = [];
        try {
            vm.runInContext('globalThis.__RL = (typeof _ROBOT_START_LOG !== "undefined") ? _ROBOT_START_LOG : [];', sandbox);
            rl = sandbox.__RL || [];
        } catch (e) { lines.push('RL READ ERROR: ' + (e && e.message)); }
        say('  журнал запуска (' + rl.length + '): ' + JSON.stringify(rl.slice().reverse()));
        finish();
    })().catch(e => { say('SCRIPT ERROR(async): ' + (e && e.stack || e)); finish(); });
} catch (e) {
    say('SCRIPT ERROR: ' + (e && e.stack || e));
    finish();
}
