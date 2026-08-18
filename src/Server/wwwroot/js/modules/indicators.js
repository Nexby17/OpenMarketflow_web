// === OpenMarketflow UI - Technical Indicators Module ===
// Client-side SAR, EMA, STD calculations for chart overlay

function calcSAR(bars, start, step, max) {
    if (bars.length < 2) return [];
    let af = start, isLong = bars[1].c > bars[0].c;
    let sar = isLong ? bars[0].l : bars[0].h;
    let ep = isLong ? bars[0].h : bars[0].l;
    const result = [];
    for (let i = 1; i < bars.length; i++) {
        const prevSar = sar;
        sar = prevSar + af * (ep - prevSar);
        if (isLong) {
            sar = Math.min(sar, bars[i-1].l, i >= 2 ? bars[i-2].l : bars[i-1].l);
            if (bars[i].l < sar) { isLong = false; sar = ep; ep = bars[i].l; af = start; }
            else { if (bars[i].h > ep) { ep = bars[i].h; af = Math.min(af + step, max); } }
        } else {
            sar = Math.max(sar, bars[i-1].h, i >= 2 ? bars[i-2].h : bars[i-1].h);
            if (bars[i].h > sar) { isLong = true; sar = ep; ep = bars[i].h; af = start; }
            else { if (bars[i].l < ep) { ep = bars[i].l; af = Math.min(af + step, max); } }
        }
        result.push({ time: bars[i].t, value: sar });
    }
    return result;
}

function calcEMA(bars, period) {
    if (bars.length < period) return [];
    const k = 2 / (period + 1);
    let ema = 0;
    for (let i = 0; i < period; i++) ema += bars[i].c;
    ema /= period;
    const result = [{ time: bars[period-1].t, value: ema }];
    for (let i = period; i < bars.length; i++) {
        ema = bars[i].c * k + ema * (1 - k);
        result.push({ time: bars[i].t, value: ema });
    }
    return result;
}

function calcSTD(bars, period, mult, sarData) {
    if (bars.length < period) return [];
    const result = [];
    for (let i = period - 1; i < bars.length; i++) {
        let sum = 0, sumSq = 0;
        for (let j = i - period + 1; j <= i; j++) { sum += bars[j].c; sumSq += bars[j].c * bars[j].c; }
        const std = Math.sqrt(sumSq / period - Math.pow(sum / period, 2));
        const sar = sarData.find(s => s.time === bars[i].t);
        if (sar && std > 0) {
            result.push({ time: bars[i].t, value: sar.value + std * mult });
            result.push({ time: bars[i].t, value: sar.value - std * mult });
        }
    }
    return result;
}