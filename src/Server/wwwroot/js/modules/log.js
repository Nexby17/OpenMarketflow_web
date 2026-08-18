// === OpenMarketflow UI - Log Module ===

function addLog(time, level, msg) {
    const container = el('logContainer');
    if (!container) return;
    const cls = level === 'ERROR' ? 'log-error' : level === 'TRADE' ? 'log-trade' : level === 'WARN' ? 'log-warn' : 'log-info';
    const div = document.createElement('div');
    div.className = 'log-entry';
    div.innerHTML = '<span class="log-time">[' + time + ']</span> <span class="' + cls + '">' + msg + '</span>';
    container.appendChild(div);
    while (container.children.length > MAX_LOG) container.removeChild(container.firstChild);
    container.scrollTop = container.scrollHeight;
}