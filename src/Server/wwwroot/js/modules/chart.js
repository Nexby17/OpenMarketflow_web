// === OpenMarketflow UI - Chart Controller Module ===
// Single source of truth for chart state + candle series writes.
// All candle/volume/indicator/overlay writes MUST go through ChartController.
// app.js keeps read-only legacy refs (chart, candleSeries, ...) for rendering queries only.

// Requires (loaded before this file): helpers.js (el), indicators.js (calcSAR, calcEMA, calcSTD)

const ChartController = (() => {
    'use strict';

    // === Private state — единственная точка истины ===
    let _chart = null;
    let _candleSeries = null, _volumeSeries = null;
    let _sarSeries = null, _emaSeries = null, _stdSeries = null;
    let _positionSeries = null;
    let _orderLineSeries = [];

    const _state = {
        ticker: null,        // текущий инструмент ('SiU6', без @RTSX)
        tf: 5,               // текущий TF в минутах
        epoch: 0,            // версия данных: инкремент при каждом loadHistory/reset
        lastCandleTime: 0,   // time последней свечи в серии (уже с MSK_OFFSET)
        loading: false,      // идёт loadHistory
        initialized: false,  // init() завершён
        atRightEdge: true,   // вьюпорт у правого края (auto-follow)
        markers: [],         // текущие маркеры (последний setMarkers)
        ohlcMerged: false,   // была ли обновлена последняя свеча OHLC-данными
    };

    let _epochCounter = 0;
    const MSK_OFFSET = 10800;

    // === Utils ===

    function _normTicker(t) {
        return String(t || '').replace('@RTSX', '');
    }

    function _log(level, msg) {
        try { console[level]('[ChartController]', msg); } catch (e) { /* noop */ }
    }

    // === Invariant checks ===

    function _isReady() {
        return _state.initialized && _candleSeries && _chart;
    }

    function _matchesContext(ticker, tf) {
        if (tf != null && parseInt(tf) !== _state.tf) return false;
        if (ticker != null && _normTicker(ticker) !== _normTicker(_state.ticker)) return false;
        return true;
    }

    function _validPrice(p) {
        return typeof p === 'number' && isFinite(p) && p > 0;
    }

    // === Internal helpers ===

    function _lastBar() {
        const bars = _candleSeries.data();
        return bars.length ? bars[bars.length - 1] : null;
    }

    function _syncLegacyGlobals() {
        // Compat: legacy app.js / third-party code читает эти window-поля
        try {
            window._currentChartTicker = _state.ticker;
            window._currentChartTf = _state.tf;
            window._lastCandleTime = _state.lastCandleTime;
            window._lastCandles = _candleSeries ? _candleSeries.data() : [];
        } catch (e) { /* noop */ }
    }

    function _clearAllSeries() {
        if (_candleSeries) _candleSeries.setData([]);
        if (_volumeSeries) _volumeSeries.setData([]);
        if (_sarSeries) _sarSeries.setData([]);
        if (_emaSeries) _emaSeries.setData([]);
        if (_stdSeries) _stdSeries.setData([]);
        if (_positionSeries) _positionSeries.setData([]);
        _clearOrderLinesImpl();
        if (_candleSeries) _candleSeries.setMarkers([]);
        _state.markers = [];
    }

    function _clearOrderLinesImpl() {
        _orderLineSeries.forEach(s => {
            try { _chart.removeSeries(s); } catch (e) { /* noop */ }
        });
        _orderLineSeries = [];
    }

    // === Indicators (на сырых bar-данных формата {t,o,h,l,c,v} БЕЗ offset) ===

    function _computeIndicators(data, sarParams, emaPeriod, stdParams) {
        const sar = (typeof calcSAR === 'function')
            ? calcSAR(data, sarParams?.start || 0.009, sarParams?.step || 0.01, sarParams?.max || 0.2)
            : [];
        const ema = (typeof calcEMA === 'function')
            ? calcEMA(data, emaPeriod || 30)
            : [];
        const std = (typeof calcSTD === 'function' && sar.length)
            ? calcSTD(data, stdParams?.period || 14, stdParams?.mult || 1.5, sar)
            : [];
        return { sar, ema, std };
    }

    // === Init ===

    function init(containerId, opts) {
        if (_state.initialized) {
            return Promise.resolve(_refs());
        }
        const container = document.getElementById(containerId || 'chartContainer');
        if (!container || container.offsetWidth === 0) {
            // retry 100ms, max 50 попыток (lazy tab activation)
            if ((init._retries = (init._retries || 0) + 1) <= 50) {
                return new Promise(resolve => {
                    setTimeout(() => resolve(init(containerId, opts)), 100);
                });
            }
            _log('error', 'init failed: container not visible after 50 retries');
            return Promise.reject(new Error('chart container not visible'));
        }
        init._retries = 0;

        const fmtTime = d => d.toISOString().slice(11, 16);
        const fmtDate = d => d.toISOString().slice(0, 10);

        _chart = LightweightCharts.createChart(container, {
            width: container.offsetWidth,
            height: container.offsetHeight || 400,
            layout: { background: { color: '#1A1A2E' }, textColor: '#9CA3AF' },
            grid: { vertLines: { color: '#2D2D44' }, horzLines: { color: '#2D2D44' } },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            timeScale: { timeVisible: true, secondsVisible: false },
            localization: {
                timeFormatter: t => fmtTime(new Date(t * 1000)),
                dateFormat: t => fmtDate(new Date(t * 1000)),
            },
        });

        _candleSeries = _chart.addCandlestickSeries({
            upColor: '#22C55E', downColor: '#EF4444',
            borderUpColor: '#22C55E', borderDownColor: '#EF4444',
            wickUpColor: '#22C55E', wickDownColor: '#EF4444',
        });

        _volumeSeries = _chart.addHistogramSeries({
            priceFormat: { type: 'volume' },
            priceScaleId: '',
        });
        _volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });

        _sarSeries = _chart.addLineSeries({
            color: '#FFD700',
            lineWidth: 1,
            pointMarkersVisible: true,
            priceLineVisible: false,
            lastValueVisible: false,
            title: 'SAR',
        });

        _emaSeries = _chart.addLineSeries({
            color: '#00BFFF',
            lineWidth: 2,
            priceLineVisible: false,
            lastValueVisible: false,
            title: 'EMA',
        });

        _stdSeries = _chart.addLineSeries({
            color: '#FF6B6B',
            lineWidth: 1,
            lineStyle: 2, // dashed
            priceLineVisible: false,
            lastValueVisible: false,
            title: 'STD',
            visible: true,
        });

        _positionSeries = _chart.addLineSeries({
            color: '#FFFFFF',
            lineWidth: 2,
            priceLineVisible: false,
            lastValueVisible: false,
            title: 'Position',
        });

        // Auto-follow: отслеживаем, находится ли вьюпорт у правого края
        try {
            _chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
                if (!range) return;
                const len = _candleSeries ? _candleSeries.data().length : 0;
                _state.atRightEdge = range.to >= len - 3;
            });
        } catch (e) {
            _log('warn', 'subscribeVisibleLogicalRangeChange failed: ' + e.message);
        }

        // dblclick → indicator menu (передан из app.js)
        container.addEventListener('dblclick', e => {
            e.preventDefault();
            if (opts && typeof opts.onDbClick === 'function') {
                opts.onDbClick(e.clientX, e.clientY);
            }
        });

        // Resize handling
        if (typeof ResizeObserver !== 'undefined') {
            new ResizeObserver(() => {
                resize();
            }).observe(container);
        }

        _state.initialized = true;
        _log('info', 'initialized');
        return Promise.resolve(_refs());
    }

    function _refs() {
        return {
            chart: _chart,
            candleSeries: _candleSeries,
            volumeSeries: _volumeSeries,
            sarSeries: _sarSeries,
            emaSeries: _emaSeries,
            stdSeries: _stdSeries,
            positionSeries: _positionSeries,
        };
    }

    function isReady() {
        return _isReady();
    }

    function getState() {
        return {
            ticker: _state.ticker,
            tf: _state.tf,
            epoch: _state.epoch,
            lastCandleTime: _state.lastCandleTime,
            loading: _state.loading,
            dataCount: _candleSeries ? _candleSeries.data().length : 0,
        };
    }

    // === История (канал 4) ===

    function loadHistory(ticker, tfMinutes) {
        const tf = parseInt(tfMinutes) || 5;
        const normTicker = _normTicker(ticker);

        _epochCounter++;
        const myEpoch = _epochCounter;
        _state.epoch = myEpoch;
        _state.loading = true;
        _state.ticker = normTicker;
        _state.tf = tf;
        _state.lastCandleTime = 0;
        _state.ohlcMerged = false;

        // Мгновенная очистка серий (пока грузится новая история)
        if (_isReady()) _clearAllSeries();
        _syncLegacyGlobals();

        const days = tf <= 1 ? 2 : tf <= 5 ? 10 : tf <= 60 ? 30 : 90;

        return fetch(`/api/candles?ticker=${encodeURIComponent(normTicker)}&tf=${tf}&days=${days}`)
            .then(r => r.json())
            .then(data => {
                if (myEpoch !== _epochCounter) {
                    return { count: 0, aborted: true, error: null };
                }
                if (data.error) {
                    _state.loading = false;
                    return { count: 0, aborted: false, error: data.error };
                }
                if (!data || !data.length) {
                    _state.loading = false;
                    _syncLegacyGlobals();
                    return { count: 0, aborted: false, error: null, empty: true };
                }

                // Нормализация: UTC epoch → chart time (+MSK)
                const candles = data.map(r => ({
                    time: r.t + MSK_OFFSET,
                    open: r.o, high: r.h, low: r.l, close: r.c,
                }));
                const volumes = data.map(r => ({
                    time: r.t + MSK_OFFSET, value: r.v || 0,
                    color: r.c >= r.o ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)',
                }));

                _candleSeries.setData(candles);
                if (_volumeSeries) _volumeSeries.setData(volumes);
                _state.lastCandleTime = candles[candles.length - 1].time;
                _state.ohlcMerged = true;

                // Индикаторы поверх истории
                const sarParams = window._sarParams || { start: 0.009, step: 0.01, max: 0.2 };
                const emaPeriod = window._emaPeriod || 30;
                const stdParams = {
                    period: parseInt(localStorage.getItem('v7_stdPeriod')) || 14,
                    mult: parseFloat(localStorage.getItem('v7_stdMult')) || 1.5,
                };
                _applyIndicatorData(data, sarParams, emaPeriod, stdParams);

                _state.loading = false;
                if (candles.length > 0) {
                    _chart.timeScale().fitContent();
                }
                _syncLegacyGlobals();
                return { count: candles.length, aborted: false, error: null };
            })
            .catch(e => {
                if (myEpoch !== _epochCounter) {
                    return { count: 0, aborted: true, error: null };
                }
                _state.loading = false;
                return { count: 0, aborted: false, error: e.message };
            });
    }

    // calcSAR/calcEMA/calcSTD работают на сырых bar-данных и возвращают {time: raw_t, value}
    function _applyIndicatorData(rawData, sarParams, emaPeriod, stdParams) {
        if (!rawData || !rawData.length) return;
        const { sar, ema, std } = _computeIndicators(rawData, sarParams, emaPeriod, stdParams);
        if (_sarSeries) {
            _sarSeries.setData(sar.length > 0 ? sar.map(d => ({ time: d.time + MSK_OFFSET, value: d.value })) : []);
        }
        if (_emaSeries) {
            _emaSeries.setData(ema.length > 0 ? ema.map(d => ({ time: d.time + MSK_OFFSET, value: d.value })) : []);
        }
        if (_stdSeries) {
            _stdSeries.setData(std.length > 0 ? std.map(d => ({ time: d.time + MSK_OFFSET, value: d.value })) : []);
        }
    }

    // === Live-тики (каналы 1, 2) ===

    function applyTick(ticker, price, serverTsSec) {
        if (!_isReady()) return;
        if (!_validPrice(price)) return;
        if (!_matchesContext(ticker, null)) return;

        const bars = _candleSeries.data();
        if (!bars.length) return; // история ещё не загружена
        const last = bars[bars.length - 1];

        // Guard: 5% price deviation
        if (last && last.close > 0 && Math.abs(price - last.close) / last.close > 0.05) return;

        const now = serverTsSec || Math.floor(Date.now() / 1000);
        const tfSec = _state.tf * 60;
        const candleOpen = now - (now % tfSec) + MSK_OFFSET;

        // ИНВАРИАНТ time-монотонности — главный фикс 'Value is null'
        if (candleOpen < last.time) return;

        if (candleOpen === last.time) {
            // merge с текущей свечой
            _candleSeries.update({
                time: candleOpen,
                open: last.open,
                high: Math.max(last.high, price),
                low: Math.min(last.low, price),
                close: price,
            });
            _state.lastCandleTime = candleOpen;
        } else {
            // новая свеча (следующий TF-период)
            _candleSeries.update({ time: candleOpen, open: price, high: price, low: price, close: price });
            _state.lastCandleTime = candleOpen;
            _state.ohlcMerged = false;
            if (_state.atRightEdge) {
                try { _chart.timeScale().scrollToRealTime(); } catch (e) { /* noop */ }
            }
            // volume новой свечи уточнит applyCandle (канал 3)
        }
        _syncLegacyGlobals();
    }

    // === Live-свеча из REST (канал 3) ===

    function applyCandle(ticker, tfMinutes, bar) {
        if (!_isReady()) return;
        if (!bar || !bar.t) return;
        if (!_matchesContext(ticker, tfMinutes)) return;

        const o = bar.o, h = bar.h, l = bar.l, c = bar.c;
        // OHLC-инвариант
        if (![_validPrice(o), _validPrice(h), _validPrice(l), _validPrice(c)].every(Boolean)) return;
        if (!(h >= Math.max(o, c) && l <= Math.min(o, c))) return;

        const barTime = bar.t + MSK_OFFSET;
        // Time-монотонность: пришедшая свеча старше последней в серии → ignore
        if (barTime < _state.lastCandleTime) return;

        const last = _lastBar();
        if (last && barTime < last.time) return;

        _candleSeries.update({ time: barTime, open: o, high: h, low: l, close: c });
        _state.lastCandleTime = barTime;
        _state.ohlcMerged = true;

        if (_volumeSeries) {
            _volumeSeries.update({
                time: barTime, value: bar.v || 0,
                color: c >= o ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)',
            });
        }
        if (_state.atRightEdge && last && barTime > last.time) {
            try { _chart.timeScale().scrollToRealTime(); } catch (e) { /* noop */ }
        }
        _syncLegacyGlobals();
    }

    // === Маркеры ===

    function setMarkers(ticker, markers) {
        if (!_isReady()) return;
        if (!_matchesContext(ticker, null)) return;
        if (!Array.isArray(markers)) return;

        // Валидация + сортировка по time
        const valid = markers
            .filter(m => m && Number.isFinite(m.time) && m.time > 0 && !isNaN(m.time))
            .sort((a, b) => a.time - b.time);

        _candleSeries.setMarkers(valid);
        _state.markers = valid;
    }

    // === Позиция ===

    function setPositionLine(position) {
        if (!_isReady() || !_positionSeries) return;
        if (!position || !_validPrice(position.price)) {
            _positionSeries.setData([]);
            return;
        }
        const now = Math.floor(Date.now() / 1000);
        const timeStart = Math.floor((now - 7200) / 60) * 60;
        const timeEnd = Math.floor((now + 3600) / 60) * 60;
        const isLong = position.isLong !== false; // default true для совместимости
        _positionSeries.applyOptions({ color: isLong ? '#22C55E' : '#EF4444' });
        _positionSeries.setData([
            { time: timeStart, value: position.price },
            { time: timeEnd, value: position.price },
        ]);
    }

    // === Ордер-линии ===

    function refreshOrderLines(activeOrders) {
        if (!_isReady()) return;
        _clearOrderLinesImpl();
        if (!Array.isArray(activeOrders) || !activeOrders.length) return;

        const now = Math.floor(Date.now() / 1000);
        const ts = Math.floor((now - 7200) / 60) * 60;
        const te = Math.floor((now + 3600) / 60) * 60;

        activeOrders.forEach(o => {
            const price = parseFloat(o.price);
            if (!_validPrice(price)) return;
            const isBuy = !!o.isBuy;
            const s = _chart.addLineSeries({
                color: isBuy ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)',
                lineWidth: 1,
                lineStyle: isBuy ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Dotted,
                priceLineVisible: true,
                lastValueVisible: true,
                priceFormat: { type: 'price', precision: 0, minMove: 1 },
                title: (isBuy ? '▲ B ' : '▼ S ') + price.toFixed(0),
                crosshairMarkerVisible: false,
            });
            s.setData([{ time: ts, value: price }, { time: te, value: price }]);
            _orderLineSeries.push(s);
        });
    }

    function clearOrderLines() {
        if (!_isReady()) return;
        _clearOrderLinesImpl();
    }

    // === Индикаторы: пересчёт по текущим данным ===

    function recalcIndicators(sarParams, emaPeriod, stdParams) {
        if (!_isReady()) return;
        const bars = _candleSeries.data();
        if (!bars || !bars.length) return;
        // Серии хранят chart-time; calc* ожидают raw t → откатываем offset
        const raw = bars.map(b => ({
            t: b.time - MSK_OFFSET,
            h: b.high, l: b.low, c: b.close,
        }));
        _applyIndicatorData(raw, sarParams, emaPeriod, stdParams);
    }

    // === Стратегийные серии (v6 /strategy endpoints) ===

    function setStrategySeries(data) {
        if (!_isReady() || !data) return;
        if (data.sar && data.sar.length > 0 && _sarSeries) {
            _sarSeries.setData(data.sar.filter(p => p.time > 0 && !isNaN(p.value)));
        }
        if (data.ema && data.ema.length > 0 && _emaSeries) {
            _emaSeries.setData(data.ema.filter(p => p.time > 0 && !isNaN(p.value)));
        }
    }

    // === Управление ===

    function resize() {
        if (!_isReady()) return;
        const container = document.getElementById('chartContainer');
        const target = container || (_chart.container ? _chart.container.parentElement : null);
        if (!target) return;
        try {
            _chart.applyOptions({ width: target.offsetWidth, height: target.offsetHeight || 400 });
        } catch (e) { /* noop */ }
    }

    function reset() {
        if (!_isReady()) return;
        _epochCounter++;
        _state.epoch = _epochCounter;
        _clearAllSeries();
        _state.ticker = null;
        _state.tf = 5;
        _state.lastCandleTime = 0;
        _state.loading = false;
        _state.ohlcMerged = false;
        _syncLegacyGlobals();
    }

    // === Экспорт ===

    window.ChartController = {
        init,
        isReady,
        getState,
        loadHistory,
        applyTick,
        applyCandle,
        setMarkers,
        setPositionLine,
        refreshOrderLines,
        clearOrderLines,
        recalcIndicators,
        setStrategySeries,
        resize,
        reset,
    };

    return window.ChartController;
})();
