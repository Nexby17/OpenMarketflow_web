// === OpenMarketflow UI - Helpers Module ===
// Exposes: el, nowTime, fmt, fmtPnl, fmtN, toChartTime

function el(id) { return document.getElementById(id); }
function nowTime() { return new Date().toLocaleTimeString(); }
function fmt(n) { return n ? Math.round(n).toLocaleString('ru-RU') : '—'; }
function fmtN(v, d) { return v != null ? v.toLocaleString('ru-RU', {maximumFractionDigits: d||2}) : '—'; }
function fmtPnl(n) {
    if (!n) return '0';
    const s = n > 0 ? '+' : '';
    return s + Math.round(n).toLocaleString('ru-RU');
}
function toChartTime(isoStr) {
    if (!isoStr) return 0;
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return 0;
    return Math.floor(d.getTime() / 1000) + 10800;
}

// Max log entries
const MAX_LOG = 200;