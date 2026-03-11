/**
 * Lobsterminal — Main Application
 * Handles: market data, chart, watchlist, news, WebSocket, keyboard shortcuts
 */

// Splash screen — fade out after 2 seconds
setTimeout(function() {
    var splash = document.getElementById('splash-screen');
    if (splash) {
        splash.style.opacity = '0';
        setTimeout(function() { splash.remove(); }, 500);
    }
}, 2000);

// ============================================================
// STATE
// ============================================================
const state = {
    currentSymbol: 'SPY',
    selectedRow: 0,
    focusedPane: 1,
    quotes: [],
    watchlist: [],
    ws: null,
    wsRetryCount: 0,
    chart: null,
    candleSeries: null,
    volumeSeries: null,
    resizeObserver: null,
    user_id: null,
    portfolios: [],
    activePortfolioId: null,
};

// ============================================================
// INITIALIZATION
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    startClock();
    loadMarketData();
    loadChart(state.currentSymbol);
    loadWatchlist();
    loadNews();
    connectWebSocket();
    setupKeyboard();
    setupCommandBar();
    setupPeriodButtons();
    setupWatchlistControls();

    // Auto-refresh market data every 15s
    setInterval(loadMarketData, 15000);
    // Auto-refresh news every 60s
    setInterval(loadNews, 60000);
});

// ============================================================
// CLOCK
// ============================================================
function startClock() {
    const el = document.getElementById('clock');
    function tick() {
        const now = new Date();
        el.textContent = now.toLocaleTimeString('en-US', { hour12: false }) +
            ' ' + now.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }
    tick();
    setInterval(tick, 1000);
}

// ============================================================
// DOM HELPERS (safe — no innerHTML)
// ============================================================
function createEl(tag, attrs = {}, children = []) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'className') el.className = v;
        else if (k === 'textContent') el.textContent = v;
        else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (k.startsWith('data')) el.dataset[k.slice(4)] = v;
        else el.setAttribute(k, v);
    }
    for (const child of children) {
        if (typeof child === 'string') el.appendChild(document.createTextNode(child));
        else if (child) el.appendChild(child);
    }
    return el;
}

// ============================================================
// MARKET DATA
// ============================================================
async function loadMarketData() {
    try {
        const resp = await fetch('/api/quotes');
        if (!resp.ok) { console.error('Quotes API error:', resp.status); return; }
        const quotes = await resp.json();
        state.quotes = quotes;
        renderMarketTable(quotes);
        updateFooter();
    } catch (e) {
        console.error('Failed to load quotes:', e);
    }
}

function renderMarketTable(quotes) {
    const tbody = document.getElementById('market-tbody');
    tbody.textContent = '';  // safe clear
    quotes.forEach((q, i) => {
        const changeClass = q.change >= 0 ? 'price-up' : 'price-down';
        const sign = q.change >= 0 ? '+' : '';

        const tr = createEl('tr', { dataSymbol: q.symbol }, [
            createEl('td', { textContent: q.symbol }),
            createEl('td', { textContent: q.price.toFixed(2) }),
            createEl('td', { className: changeClass, textContent: `${sign}${q.change.toFixed(2)}` }),
            createEl('td', { className: changeClass, textContent: `${sign}${q.changePercent.toFixed(2)}%` }),
            createEl('td', { textContent: formatVolume(q.volume) }),
            createEl('td', { textContent: q.high.toFixed(2) }),
            createEl('td', { textContent: q.low.toFixed(2) }),
        ]);

        if (i === state.selectedRow || q.symbol === state.currentSymbol) {
            tr.classList.add('selected');
        }

        tr.addEventListener('click', () => selectSymbol(q.symbol, i));
        tbody.appendChild(tr);
    });
}

function selectSymbol(symbol, rowIndex) {
    state.currentSymbol = symbol;
    if (rowIndex !== undefined) state.selectedRow = rowIndex;
    loadChart(symbol);
    loadNews(symbol);
    loadDexterRatios(symbol);
    loadFundScore(symbol);
    updateFooter();
    // Highlight selected row
    document.querySelectorAll('#market-tbody tr').forEach(tr => {
        tr.classList.toggle('selected', tr.dataset.symbol === symbol);
    });
}

function updateFooter() {
    const q = state.quotes.find(q => q.symbol === state.currentSymbol);
    document.getElementById('footer-symbol').textContent = state.currentSymbol;
    if (q) {
        document.getElementById('footer-price').textContent = q.price.toFixed(2);
        const sign = q.change >= 0 ? '+' : '';
        const el = document.getElementById('footer-change');
        el.textContent = `${sign}${q.change.toFixed(2)} (${sign}${q.changePercent.toFixed(2)}%)`;
        el.className = q.change >= 0 ? 'price-up' : 'price-down';
    }
}

// ============================================================
// CHART (TradingView Lightweight Charts)
// ============================================================
async function loadChart(symbol, period = '6mo', interval = '1d') {
    document.getElementById('chart-title').textContent = `CHART \u2014 ${symbol}`;

    try {
        const resp = await fetch(`/api/history/${encodeURIComponent(symbol)}?period=${period}&interval=${interval}`);
        if (!resp.ok) { console.error('History API error:', resp.status); return; }
        const data = await resp.json();
        if (data.error || !Array.isArray(data) || data.length === 0) {
            console.error('No chart data for', symbol);
            return;
        }
        renderChart(data);
    } catch (e) {
        console.error('Failed to load chart:', e);
    }
}

function renderChart(data) {
    const container = document.getElementById('chart-container');

    // Remove existing chart
    if (state.chart) {
        state.chart.remove();
        state.chart = null;
    }

    const chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,
        layout: {
            background: { type: 'solid', color: '#111125' },
            textColor: '#8888aa',
            fontSize: 10,
            fontFamily: "'Cascadia Mono', 'SF Mono', monospace",
        },
        grid: {
            vertLines: { color: 'rgba(26, 26, 62, 0.5)' },
            horzLines: { color: 'rgba(26, 26, 62, 0.5)' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: 'rgba(255, 140, 0, 0.4)', width: 1, style: 2 },
            horzLine: { color: 'rgba(255, 140, 0, 0.4)', width: 1, style: 2 },
        },
        rightPriceScale: {
            borderColor: '#1a1a3e',
            scaleMargins: { top: 0.1, bottom: 0.25 },
        },
        timeScale: {
            borderColor: '#1a1a3e',
            timeVisible: true,
        },
        handleScroll: { vertTouchDrag: false },
    });

    const candleSeries = chart.addCandlestickSeries({
        upColor: '#00e676',
        downColor: '#ff1744',
        borderUpColor: '#00e676',
        borderDownColor: '#ff1744',
        wickUpColor: '#00e676',
        wickDownColor: '#ff1744',
    });
    candleSeries.setData(data);

    const volumeSeries = chart.addHistogramSeries({
        color: 'rgba(255, 140, 0, 0.2)',
        priceFormat: { type: 'volume' },
        priceScaleId: '',
    });
    volumeSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
    });
    volumeSeries.setData(data.map(d => ({
        time: d.time,
        value: d.volume,
        color: d.close >= d.open ? 'rgba(0, 230, 118, 0.3)' : 'rgba(255, 23, 68, 0.3)',
    })));

    // Calculate RSI(14)
    const closePrices = data.map(d => d.close);
    const rsiValues = calculateRSI(closePrices, 14);

    const rsiSeries = chart.addLineSeries({
        priceScaleId: 'rsi',
        color: '#FFD700',
        title: 'RSI(14)',
    });
    rsiSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 },
    });
    rsiSeries.setData(
        rsiValues.map((v, i) => v !== null ? { time: data[i].time, value: v } : null).filter(Boolean)
    );

    // Calculate MACD(12, 26, 9)
    const macdResult = calculateMACD(closePrices, 12, 26, 9);

    const macdLineSeries = chart.addLineSeries({
        priceScaleId: 'macd',
        color: '#1f77b4',
        title: 'MACD',
    });
    macdLineSeries.setData(data.map((d, i) => ({ time: d.time, value: macdResult.macd[i] })));

    const signalLineSeries = chart.addLineSeries({
        priceScaleId: 'macd',
        color: '#ff7f0e',
        title: 'Signal',
    });
    signalLineSeries.setData(data.map((d, i) => ({ time: d.time, value: macdResult.signal[i] })));

    const macdHistogramSeries = chart.addHistogramSeries({
        color: 'rgba(255, 140, 0, 0.2)',
        priceFormat: { type: 'volume' },
        priceScaleId: 'macd',
    });
    macdHistogramSeries.priceScale().applyOptions({
        scaleMargins: { top: 0.85, bottom: 0 },
    });
    macdHistogramSeries.setData(data.map((d, i) => ({
        time: d.time,
        value: macdResult.histogram[i],
        color: macdResult.histogram[i] > 0 ? 'rgba(0, 230, 118, 0.3)' : 'rgba(255, 23, 68, 0.3)',
    })));

    chart.timeScale().fitContent();

    state.chart = chart;
    state.candleSeries = candleSeries;
    state.volumeSeries = volumeSeries;
    state.rsiSeries = rsiSeries;
    state.macdLineSeries = macdLineSeries;
    state.signalLineSeries = signalLineSeries;
    state.macdHistogramSeries = macdHistogramSeries;

    // I3 fix: disconnect previous observer before creating new one
    if (state.resizeObserver) state.resizeObserver.disconnect();
    const ro = new ResizeObserver(() => {
        chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    });
    ro.observe(container);
    state.resizeObserver = ro;
}

// ============================================================
// INDICATOR CALCULATIONS
// ============================================================
function calculateRSI(prices, period) {
    // Wilder's smoothing RSI — returns array of values (null for first `period` entries)
    if (prices.length < period + 1) return prices.map(() => null);

    const result = new Array(period).fill(null);
    let avgGain = 0, avgLoss = 0;

    for (let i = 1; i <= period; i++) {
        const diff = prices[i] - prices[i - 1];
        if (diff > 0) avgGain += diff;
        else avgLoss += -diff;
    }
    avgGain /= period;
    avgLoss /= period;

    result.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));

    for (let i = period + 1; i < prices.length; i++) {
        const diff = prices[i] - prices[i - 1];
        avgGain = (avgGain * (period - 1) + (diff > 0 ? diff : 0)) / period;
        avgLoss = (avgLoss * (period - 1) + (diff < 0 ? -diff : 0)) / period;
        result.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));
    }
    return result;
}

function calculateEMA(values, period) {
    // values = array of numbers, returns array of numbers
    const multiplier = 2 / (period + 1);
    const ema = [values[0]];
    for (let i = 1; i < values.length; i++) {
        ema.push((values[i] - ema[i - 1]) * multiplier + ema[i - 1]);
    }
    return ema;
}

function calculateMACD(values, fastPeriod, slowPeriod, signalPeriod) {
    // values = array of numbers, returns {macd, signal, histogram} as number arrays
    const emaFast = calculateEMA(values, fastPeriod);
    const emaSlow = calculateEMA(values, slowPeriod);
    const macdLine = emaFast.map((v, i) => v - emaSlow[i]);
    const signalLine = calculateEMA(macdLine, signalPeriod);
    const histogram = macdLine.map((v, i) => v - signalLine[i]);
    return { macd: macdLine, signal: signalLine, histogram };
}

function setupPeriodButtons() {
    document.querySelectorAll('.period-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.period-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            loadChart(state.currentSymbol, btn.dataset.period, btn.dataset.interval);
        });
    });
}

// ============================================================
// WATCHLIST
// ============================================================
async function loadWatchlist() {
    try {
        const resp = await fetch('/api/watchlist');
        if (!resp.ok) { console.error('Watchlist API error:', resp.status); return; }
        state.watchlist = await resp.json();
        renderWatchlist();
    } catch (e) {
        console.error('Failed to load watchlist:', e);
    }
}

function renderWatchlist() {
    const ul = document.getElementById('watchlist-items');
    ul.textContent = '';  // safe clear
    state.watchlist.forEach(sym => {
        const q = state.quotes.find(q => q.symbol === sym);
        const price = q ? q.price.toFixed(2) : '--';
        const change = q ? `${q.change >= 0 ? '+' : ''}${q.change.toFixed(2)}` : '--';
        const changeClass = q && q.change >= 0 ? 'price-up' : 'price-down';

        const removeBtn = createEl('span', {
            className: 'wl-remove',
            textContent: '\u2715',
            title: 'Remove',
        });

        const li = createEl('li', { className: 'watchlist-item' + (sym === state.currentSymbol ? ' selected' : ''), dataSymbol: sym }, [
            createEl('span', { className: 'wl-symbol', textContent: sym }),
            createEl('span', { className: 'wl-price', textContent: price }),
            createEl('span', { className: `wl-change ${changeClass}`, textContent: change }),
            removeBtn,
        ]);

        removeBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await fetch(`/api/watchlist/${sym}`, { method: 'DELETE' });
            loadWatchlist();
        });

        li.addEventListener('click', () => selectSymbol(sym));
        ul.appendChild(li);
    });
}

function setupWatchlistControls() {
    const input = document.getElementById('watchlist-input');
    const btn = document.getElementById('watchlist-add-btn');

    async function addSymbol() {
        const sym = input.value.trim().toUpperCase();
        if (!sym) return;
        await fetch(`/api/watchlist/${sym}`, { method: 'POST' });
        input.value = '';
        loadWatchlist();
    }

    btn.addEventListener('click', addSymbol);
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') addSymbol();
    });
}

// ============================================================
// NEWS FEED
// ============================================================
async function loadNews(symbol = '') {
    try {
        const url = symbol ? `/api/news?symbol=${encodeURIComponent(symbol)}` : '/api/news';
        const resp = await fetch(url);
        if (!resp.ok) { console.error('News API error:', resp.status); return; }
        const articles = await resp.json();
        renderNews(articles);
    } catch (e) {
        console.error('Failed to load news:', e);
    }
}

function renderNews(articles) {
    const ul = document.getElementById('news-list');
    ul.textContent = '';  // safe clear
    articles.forEach(a => {
        const timeStr = a.datetime ? new Date(a.datetime * 1000).toLocaleTimeString('en-US', {
            hour: '2-digit', minute: '2-digit', hour12: false
        }) : '';

        const children = [
            createEl('div', { className: 'news-meta' }, [
                createEl('span', { className: 'news-time', textContent: timeStr }),
                createEl('span', { className: 'news-source', textContent: a.source || 'Market' }),
            ]),
            createEl('div', { className: 'news-headline', textContent: a.headline }),
        ];

        if (a.summary) {
            children.push(createEl('div', { className: 'news-summary', textContent: a.summary }));
        }

        const li = createEl('li', { className: 'news-item' }, children);

        if (a.url && a.url !== '#') {
            li.addEventListener('click', () => window.open(a.url, '_blank', 'noopener'));
        }
        ul.appendChild(li);
    });
}

// ============================================================
// WEBSOCKET
// ============================================================
function connectWebSocket() {
    const statusDot = document.getElementById('ws-status');
    statusDot.className = 'status-dot connecting';
    statusDot.title = 'Connecting...';

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${location.host}/ws/prices`);

    ws.onopen = () => {
        statusDot.className = 'status-dot connected';
        statusDot.title = 'Connected \u2014 live prices';
        state.ws = ws;
        state.wsRetryCount = 0;  // reset on successful connect
    };

    ws.onmessage = (event) => {
        try {
            const tick = JSON.parse(event.data);
            if (tick.type === 'trade' && /^[A-Z]{1,10}$/.test(tick.symbol)) {
                updatePriceTick(tick);
            }
        } catch (e) {
            console.error('Invalid WebSocket message:', e);
        }
    };

    ws.onclose = () => {
        statusDot.className = 'status-dot disconnected';
        statusDot.title = 'Disconnected \u2014 reconnecting...';
        state.ws = null;
        // I4 fix: exponential backoff (3s, 6s, 12s, ..., max 60s), max 20 retries
        if (state.wsRetryCount < 20) {
            const delay = Math.min(3000 * Math.pow(2, state.wsRetryCount), 60000);
            state.wsRetryCount++;
            setTimeout(connectWebSocket, delay);
        } else {
            statusDot.title = 'Disconnected \u2014 max retries reached';
        }
    };

    ws.onerror = () => {
        ws.close();
    };
}

function updatePriceTick(tick) {
    // Update market table cell
    const row = document.querySelector(`#market-tbody tr[data-symbol="${tick.symbol}"]`);
    if (row) {
        const priceCell = row.cells[1];
        const oldPrice = parseFloat(priceCell.textContent);
        priceCell.textContent = tick.price.toFixed(2);

        // Flash effect
        if (tick.price !== oldPrice) {
            priceCell.classList.add('price-flash');
            setTimeout(() => priceCell.classList.remove('price-flash'), 400);
        }
    }

    // Update watchlist item
    const wlPrice = document.querySelector(`.watchlist-item[data-symbol="${tick.symbol}"] .wl-price`);
    if (wlPrice) {
        wlPrice.textContent = tick.price.toFixed(2);
    }

    // Update footer if current symbol
    if (tick.symbol === state.currentSymbol) {
        document.getElementById('footer-price').textContent = tick.price.toFixed(2);
    }
}

// ============================================================
// COMMAND BAR
// ============================================================
function setupCommandBar() {
    const bar = document.getElementById('command-bar');

    bar.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const cmd = bar.value.trim();
            handleCommand(cmd);
            bar.value = '';
            bar.blur();
        }
        if (e.key === 'Escape') {
            bar.value = '';
            bar.blur();
        }
    });
}

function handleCommand(cmd) {
    if (!cmd) return;

    // Direct ticker symbol
    if (/^[A-Za-z]{1,5}$/.test(cmd)) {
        selectSymbol(cmd.toUpperCase());
        return;
    }

    // Slash commands
    if (cmd.startsWith('/')) {
        const parts = cmd.slice(1).split(/\s+/);
        const action = parts[0].toLowerCase();

        if (action === 'help') {
            toggleShortcuts();
        } else if (action === 'add' && parts[1]) {
            fetch(`/api/watchlist/${parts[1].toUpperCase()}`, { method: 'POST' }).then(() => loadWatchlist());
        } else if (action === 'remove' && parts[1]) {
            fetch(`/api/watchlist/${parts[1].toUpperCase()}`, { method: 'DELETE' }).then(() => loadWatchlist());
        } else if (action === 'chart' && parts[1]) {
            selectSymbol(parts[1].toUpperCase());
        } else if (action === 'screen' && parts.length > 1) {
            document.getElementById('screener-input').value = parts.slice(1).join(' ');
            screenStocks();
        } else if (action === 'backtest') {
            runBacktest();
        } else if (action === 'dcf') {
            var dcfSym = parts[1] ? parts[1].toUpperCase() : state.currentSymbol;
            if (dcfSym) runDCF(dcfSym);
        } else if (action === 'score') {
            var scoreSym = parts[1] ? parts[1].toUpperCase() : state.currentSymbol;
            if (scoreSym) { selectSymbol(scoreSym); }
        } else if (action === 'insider' || action === 'insiders') {
            var insSym = parts[1] ? parts[1].toUpperCase() : state.currentSymbol;
            if (insSym) { selectSymbol(insSym); document.querySelector('.intel-tab[data-tab="insiders"]').click(); }
        } else if (action === 'estimates' || action === 'est') {
            var estSym = parts[1] ? parts[1].toUpperCase() : state.currentSymbol;
            if (estSym) { selectSymbol(estSym); document.querySelector('.intel-tab[data-tab="estimates"]').click(); }
        } else if (action === 'filings' || action === 'sec') {
            var filSym = parts[1] ? parts[1].toUpperCase() : state.currentSymbol;
            if (filSym) { selectSymbol(filSym); document.querySelector('.intel-tab[data-tab="filings"]').click(); }
        }
    }
}

// ============================================================
// KEYBOARD SHORTCUTS
// ============================================================
function setupKeyboard() {
    document.addEventListener('keydown', (e) => {
        // Skip if typing in input
        if (e.target.tagName === 'INPUT') return;

        switch (e.key) {
            case 'Escape':
                document.getElementById('command-bar').focus();
                e.preventDefault();
                break;
            case '?':
                toggleShortcuts();
                e.preventDefault();
                break;
            case '/':
                document.getElementById('command-bar').focus();
                e.preventDefault();
                break;
            case '1': case '2': case '3': case '4': case '5': case '6':
                focusPane(parseInt(e.key));
                e.preventDefault();
                break;
            case 'j':
                navigateList(1);
                e.preventDefault();
                break;
            case 'k':
                navigateList(-1);
                e.preventDefault();
                break;
            case 'Enter':
                if (state.quotes[state.selectedRow]) {
                    selectSymbol(state.quotes[state.selectedRow].symbol, state.selectedRow);
                }
                e.preventDefault();
                break;
        }
    });

    // Close overlay on click
    document.getElementById('shortcuts-overlay').addEventListener('click', toggleShortcuts);
}

function focusPane(n) {
    state.focusedPane = n;
    document.querySelectorAll('.pane').forEach(p => p.classList.remove('focused'));
    const pane = document.querySelector(`.pane[data-pane="${n}"]`);
    if (pane) pane.classList.add('focused');
}

function navigateList(direction) {
    const maxIdx = state.quotes.length - 1;
    state.selectedRow = Math.max(0, Math.min(maxIdx, state.selectedRow + direction));
    renderMarketTable(state.quotes);
}

function toggleShortcuts() {
    document.getElementById('shortcuts-overlay').classList.toggle('hidden');
}

// ============================================================
// UTILS
// ============================================================
function formatVolume(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return v.toString();
}

// ============================================================
// SCREENER
// ============================================================
async function screenStocks() {
    const inputValue = document.getElementById('screener-input').value.trim();
    var activeRulesCheck = getActiveFilters();
    if (!inputValue && (!activeRulesCheck || Object.keys(activeRulesCheck).length === 0)) return;

    const resultsContainer = document.getElementById('screener-results');
    resultsContainer.textContent = '';
    resultsContainer.appendChild(createEl('div', { className: 'loading-indicator', textContent: 'Screening stocks...' }));

    try {
        var requestBody = { prompt: inputValue };
        var activeRules = getActiveFilters();
        if (activeRules && Object.keys(activeRules).length > 0) {
            requestBody.rules = activeRules;
        }
        const resp = await fetch('/api/generate-asset/screen', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody)
        });
        if (!resp.ok) {
            resultsContainer.textContent = '';
            resultsContainer.appendChild(createEl('div', { style: 'color:var(--red);padding:8px;', textContent: 'Screener error: ' + resp.status }));
            console.error('Screener API error:', resp.status);
            return;
        }
        const result = await resp.json();
        resultsContainer.textContent = '';  // safe clear

        // Show active metric filters used
        if (activeRules && Object.keys(activeRules).length > 0) {
            var filterSummary = createEl('div', { style: 'background:rgba(255,140,0,0.08);border:1px solid var(--accent);padding:4px 8px;margin-bottom:4px;font-size:9px;color:var(--accent);' });
            filterSummary.appendChild(createEl('span', { style: 'font-weight:bold;', textContent: 'Active filters: ' }));
            Object.entries(activeRules).forEach(function(e) {
                var ruleKey = e[0], ruleVal = e[1];
                filterSummary.appendChild(createEl('span', { style: 'margin-right:8px;color:var(--text-primary);', textContent: ruleKey.replace(/_/g, ' ') + ' = ' + ruleVal }));
            });
            resultsContainer.appendChild(filterSummary);
        }

        // Show notes if present
        if (result.notes) {
            resultsContainer.appendChild(createEl('div', { style: 'color:var(--text-secondary);font-size:9px;padding:4px 8px;margin-bottom:4px;', textContent: result.notes }));
        }

        // Show unresolved criteria warnings
        if (result.unresolved && result.unresolved.length > 0) {
            var warnDiv = createEl('div', { style: 'background:rgba(255,215,0,0.1);border:1px solid #FFD700;padding:6px 8px;margin-bottom:6px;font-size:9px;color:#FFD700;' });
            warnDiv.appendChild(createEl('div', { style: 'font-weight:bold;margin-bottom:2px;', textContent: 'Cannot filter on:' }));
            result.unresolved.forEach(function(u) {
                warnDiv.appendChild(createEl('div', { textContent: '\u2022 ' + u }));
            });
            warnDiv.appendChild(createEl('div', { style: 'margin-top:3px;color:var(--text-secondary);', textContent: 'These criteria require manual research. Results filtered by quantitative rules only.' }));
            resultsContainer.appendChild(warnDiv);
        }

        if (result.tickers.length === 0) {
            resultsContainer.appendChild(createEl('div', { textContent: 'No matches found.' }));
        } else {
            var selectAllDiv = createEl('div', { style: 'margin-bottom:4px;display:flex;gap:6px;align-items:center;' }, [
                createEl('label', { style: 'font-size:9px;cursor:pointer;color:var(--text-secondary);' }, [
                    createEl('input', { type: 'checkbox', id: 'screener-select-all', style: 'margin-right:3px;' }),
                    'Select All'
                ])
            ]);
            resultsContainer.appendChild(selectAllDiv);

            result.tickers.forEach(function(match) {
                var li = createEl('li', { className: 'screener-match', style: 'display:flex;align-items:center;gap:6px;' }, [
                    createEl('input', { type: 'checkbox', className: 'screener-check', value: match, style: 'cursor:pointer;' }),
                    createEl('span', { textContent: match })
                ]);
                resultsContainer.appendChild(li);
            });

            document.getElementById('screener-select-all').addEventListener('change', function(e) {
                document.querySelectorAll('.screener-check').forEach(function(cb) { cb.checked = e.target.checked; });
            });

            var addBtn = createEl('button', {
                style: 'margin-top:6px;background:var(--blue,#4488ff);color:#fff;border:none;padding:4px 10px;cursor:pointer;font-family:var(--mono);font-weight:bold;font-size:10px;',
                textContent: 'ADD TO PORTFOLIO'
            });
            addBtn.addEventListener('click', function() { showAddToPortfolioMenu('screener'); });
            resultsContainer.appendChild(addBtn);
        }
    } catch (e) {
        resultsContainer.textContent = '';
        resultsContainer.appendChild(createEl('div', { style: 'color:var(--red);padding:8px;', textContent: 'Screener error: ' + e.message }));
        console.error('Failed to screen stocks:', e);
    }
}

async function loadStrategyTemplates() {
    try {
        const resp = await fetch('/api/strategy/templates');
        if (!resp.ok) return;
        const templates = await resp.json();
        const container = document.getElementById('strategy-templates');
        if (!container) return;
        container.textContent = '';
        Object.entries(templates).forEach(([key, tmpl]) => {
            const btn = createEl('button', {
                style: 'background: var(--bg-secondary); color: var(--text-primary); border: 1px solid var(--border); padding: 2px 6px; cursor: pointer; font-family: var(--mono); font-size: 9px; border-radius: 2px;',
                textContent: tmpl.name,
                title: tmpl.description
            });
            btn.addEventListener('click', () => {
                document.getElementById('screener-input').value = '';
                createStrategy(key);
            });
            container.appendChild(btn);
        });
    } catch (e) {
        console.error('Failed to load strategy templates:', e);
    }
}

var ratioExplanations = {};

async function loadRatioExplanations() {
    try {
        var resp = await fetch('/api/strategy/ratios');
        if (!resp.ok) return;
        ratioExplanations = await resp.json();

        var guide = document.getElementById('ratio-guide');
        if (!guide) return;
        guide.textContent = '';
        guide.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;margin-bottom:4px;', textContent: 'Available Ratios (click for info):' }));
        Object.entries(ratioExplanations).forEach(function(entry) {
            var key = entry[0], info = entry[1];
            var row = createEl('div', { style: 'padding:2px 0;cursor:pointer;border-bottom:1px solid var(--border);' });
            row.appendChild(createEl('span', { style: 'color:var(--accent);', textContent: info.name }));
            row.appendChild(createEl('span', { style: 'color:var(--text-secondary);margin-left:6px;', textContent: info.good_range }));
            row.addEventListener('click', function() { showRatioTooltip(key); });
            guide.appendChild(row);
        });
    } catch (e) {
        console.error('Failed to load ratio explanations:', e);
    }
}

function showRatioTooltip(key) {
    var info = ratioExplanations[key];
    if (!info) return;

    // Remove existing tooltip
    var existing = document.getElementById('ratio-tooltip');
    if (existing) existing.remove();

    var tooltip = createEl('div', {
        id: 'ratio-tooltip',
        style: 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a2e;border:2px solid var(--accent);padding:16px;z-index:9998;max-width:400px;font-family:var(--mono);font-size:11px;box-shadow:0 4px 20px rgba(0,0,0,0.5);'
    });
    tooltip.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;font-size:14px;margin-bottom:8px;', textContent: info.name }));
    tooltip.appendChild(createEl('div', { style: 'color:#888;font-size:10px;margin-bottom:6px;', textContent: 'Formula: ' + info.formula }));
    tooltip.appendChild(createEl('div', { style: 'color:var(--text-primary);margin-bottom:8px;', textContent: info.explanation }));
    tooltip.appendChild(createEl('div', { style: 'color:#00c853;font-size:10px;', textContent: 'Good range: ' + info.good_range }));

    var closeBtn = createEl('button', {
        style: 'position:absolute;top:4px;right:8px;background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:14px;font-family:var(--mono);',
        textContent: 'X'
    });
    closeBtn.addEventListener('click', function() { tooltip.remove(); });
    tooltip.appendChild(closeBtn);

    // Close on click outside
    tooltip.addEventListener('click', function(e) { e.stopPropagation(); });
    document.body.appendChild(tooltip);
    var handler = function() { tooltip.remove(); document.removeEventListener('click', handler); };
    setTimeout(function() { document.addEventListener('click', handler); }, 100);
}

async function createStrategy(templateKey) {
    const prompt = document.getElementById('screener-input').value.trim();
    const amount = parseFloat(document.getElementById('strategy-amount').value) || 10000;
    const period = document.getElementById('strategy-period').value;
    const resultsContainer = document.getElementById('screener-results');

    if (!prompt && !templateKey) return;

    resultsContainer.textContent = '';
    resultsContainer.appendChild(createEl('div', { className: 'loading-indicator', textContent: 'Parsing strategy...' }));

    const body = { amount, period };
    if (templateKey) body.template = templateKey;
    if (prompt) body.prompt = prompt;

    try {
        const resp = await fetch('/api/strategy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (!resp.ok) {
            resultsContainer.textContent = '';
            resultsContainer.appendChild(createEl('div', { style: 'color:var(--red);padding:8px;', textContent: 'Strategy error: ' + resp.status }));
            return;
        }

        const result = await resp.json();
        resultsContainer.textContent = '';

        // Strategy name header
        resultsContainer.appendChild(createEl('div', {
            style: 'color:var(--accent);font-weight:bold;padding:4px 0;font-size:12px;',
            textContent: result.strategy_name
        }));

        // Filter summary
        resultsContainer.appendChild(createEl('div', {
            style: 'color:var(--text-secondary);font-size:10px;padding:2px 0 6px;',
            textContent: result.universe_size + ' \u2192 ' + result.filtered_count + ' (API) \u2192 ' + result.count + ' matched'
        }));

        if (result.count === 0) {
            resultsContainer.appendChild(createEl('div', { textContent: 'No stocks matched the strategy.' }));
            return;
        }

        // Matched tickers with reasons
        result.tickers.forEach(function(ticker) {
            var reason = result.research_results[ticker] || '';
            var row = createEl('div', { style: 'padding:3px 0;border-bottom:1px solid var(--border);' }, [
                createEl('span', { style: 'color:var(--accent);font-weight:bold;margin-right:8px;', textContent: ticker }),
                createEl('span', { style: 'color:var(--text-secondary);font-size:10px;', textContent: reason })
            ]);
            resultsContainer.appendChild(row);
        });

        // Backtest metrics
        if (result.backtest && result.backtest.metrics && result.backtest.metrics.total_return !== undefined) {
            var m = result.backtest.metrics;
            var metricsDiv = createEl('div', { style: 'margin-top:8px;padding:6px;background:var(--bg-secondary);border:1px solid var(--border);font-size:10px;' });
            metricsDiv.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;margin-bottom:4px;', textContent: 'Score: ' + result.backtest.score + '/100' }));
            metricsDiv.appendChild(createEl('div', { textContent: 'Return: ' + (m.total_return != null ? m.total_return.toFixed(1) : 'N/A') + '%  |  Sharpe: ' + (m.sharpe_ratio != null ? m.sharpe_ratio.toFixed(2) : 'N/A') + '  |  Vol: ' + (m.annualized_volatility != null ? m.annualized_volatility.toFixed(1) : 'N/A') + '%  |  MaxDD: ' + (m.max_drawdown != null ? m.max_drawdown.toFixed(1) : 'N/A') + '%' }));
            metricsDiv.appendChild(createEl('div', { style: 'color:var(--text-secondary);', textContent: 'Benchmark (SPY): ' + (m.benchmark_return != null ? m.benchmark_return.toFixed(1) : 'N/A') + '%' }));
            resultsContainer.appendChild(metricsDiv);
        }

        // Show which ratio filters were applied (if any)
        if (result.rules && result.rules.api_rules) {
            var ratioKeys = ['max_debt_to_ebitda', 'min_ffo_to_debt', 'max_ffo_to_debt',
                'min_focf_to_debt', 'max_focf_to_debt', 'min_ebitda_margin',
                'max_ebitda_margin', 'min_gross_margin', 'max_gross_margin',
                'min_pe_ratio', 'max_pe_ratio', 'max_debt_to_equity', 'min_roe',
                'min_profit_margin', 'min_dividend_yield', 'max_dividend_yield'];
            var usedRatios = [];
            ratioKeys.forEach(function(k) {
                if (result.rules.api_rules[k] != null) {
                    var base = k.replace(/^(min|max)_/, '');
                    if (ratioExplanations[base] && usedRatios.indexOf(base) === -1) {
                        usedRatios.push(base);
                    }
                }
            });
            if (usedRatios.length > 0) {
                var ratioDiv = createEl('div', { style: 'margin-top:6px;font-size:9px;color:var(--text-secondary);' });
                ratioDiv.appendChild(createEl('span', { textContent: 'Ratios: ' }));
                usedRatios.forEach(function(r) {
                    var badge = createEl('span', {
                        style: 'color:var(--accent);cursor:pointer;margin-right:6px;text-decoration:underline;',
                        textContent: ratioExplanations[r].name,
                        title: 'Click for explanation'
                    });
                    badge.addEventListener('click', function() { showRatioTooltip(r); });
                    ratioDiv.appendChild(badge);
                });
                resultsContainer.appendChild(ratioDiv);
            }
        }

        // Deploy button
        var deployBtn = createEl('button', {
            style: 'margin-top:8px;background:var(--accent);color:#000;border:none;padding:6px 16px;cursor:pointer;font-family:var(--mono);font-weight:bold;font-size:11px;',
            textContent: 'DEPLOY $' + amount.toLocaleString()
        });
        deployBtn.addEventListener('click', function() {
            document.getElementById('portfolio-tickers').value = result.tickers.join(',');
            document.getElementById('portfolio-capital').value = amount;
            deployActivePortfolio();
        });
        resultsContainer.appendChild(deployBtn);

        var addPortBtn = createEl('button', {
            style: 'margin-top:4px;margin-left:6px;background:var(--blue,#4488ff);color:#fff;border:none;padding:6px 12px;cursor:pointer;font-family:var(--mono);font-weight:bold;font-size:10px;',
            textContent: 'ADD TO PORTFOLIO'
        });
        addPortBtn.addEventListener('click', function() {
            var container = document.getElementById('screener-results');
            result.tickers.forEach(function(t) {
                var cb = container.querySelector('.screener-check[value="' + t + '"]');
                if (cb) cb.checked = true;
            });
            var checked = document.querySelectorAll('.screener-check:checked');
            if (checked.length === 0) {
                result.tickers.forEach(function(t) {
                    var hidden = createEl('input', { type: 'checkbox', className: 'screener-check', value: t, style: 'display:none;' });
                    hidden.checked = true;
                    container.appendChild(hidden);
                });
            }
            showAddToPortfolioMenu('strategy');
        });
        resultsContainer.appendChild(addPortBtn);

    } catch (e) {
        resultsContainer.textContent = '';
        resultsContainer.appendChild(createEl('div', { style: 'color:var(--red);padding:8px;', textContent: 'Strategy error: ' + e.message }));
        console.error('Strategy error:', e);
    }
}

document.getElementById('screener-btn').addEventListener('click', screenStocks);
document.getElementById('screener-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        screenStocks();
    }
});

document.getElementById('strategy-btn').addEventListener('click', function() { createStrategy(); });

// Load strategy templates on page load
loadStrategyTemplates();

loadRatioExplanations();

document.getElementById('ratio-guide-btn').addEventListener('click', function() {
    var picker = document.getElementById('metric-picker');
    if (picker) {
        var isHidden = picker.style.display === 'none';
        picker.style.display = isHidden ? 'block' : 'none';
        if (isHidden && picker.children.length === 0) loadMetricPicker();
    }
    // Also toggle the old ratio guide
    var guide = document.getElementById('ratio-guide');
    if (guide) guide.style.display = guide.style.display === 'none' ? 'block' : 'none';
});

// ============================================================
// METRIC PICKER & FILTER BUILDER
// ============================================================
async function loadMetricPicker() {
    var picker = document.getElementById('metric-picker');
    if (!picker) return;
    picker.textContent = '';

    // Use already-loaded ratioExplanations or fetch
    var explanations = ratioExplanations;
    if (!explanations || Object.keys(explanations).length === 0) {
        try {
            var resp = await fetch('/api/strategy/ratios');
            if (resp.ok) {
                explanations = await resp.json();
                ratioExplanations = explanations;
            }
        } catch (e) { return; }
    }

    picker.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;margin-bottom:4px;font-size:10px;', textContent: 'Click INFO to learn, ADD to filter:' }));

    Object.entries(explanations).forEach(function(entry) {
        var key = entry[0], info = entry[1];
        var card = createEl('div', { style: 'display:flex;align-items:center;justify-content:space-between;padding:3px 4px;border-bottom:1px solid rgba(255,255,255,0.05);' });

        var nameSpan = createEl('span', { style: 'flex:1;color:var(--text-primary);font-size:9px;cursor:pointer;', textContent: info.name, title: info.explanation });
        nameSpan.addEventListener('click', function() { showRatioTooltip(key); });

        var rangeSpan = createEl('span', { style: 'color:var(--text-secondary);font-size:8px;margin-right:6px;', textContent: info.good_range || '' });

        var infoBtn = createEl('button', {
            style: 'background:none;border:1px solid var(--border);color:var(--accent);padding:1px 4px;font-family:var(--mono);font-size:7px;cursor:pointer;margin-right:3px;',
            textContent: 'INFO'
        });
        infoBtn.addEventListener('click', function() { showRatioTooltip(key); });

        var addBtn = createEl('button', {
            style: 'background:var(--accent);color:#000;border:none;padding:1px 4px;font-family:var(--mono);font-size:7px;cursor:pointer;font-weight:bold;',
            textContent: 'ADD'
        });
        addBtn.addEventListener('click', function() { addFilter(key, info); });

        card.appendChild(nameSpan);
        card.appendChild(rangeSpan);
        card.appendChild(infoBtn);
        card.appendChild(addBtn);
        picker.appendChild(card);
    });
}

function addFilter(key, info) {
    var filtersDiv = document.getElementById('active-filters');
    if (!filtersDiv) return;

    // Don't add duplicate
    if (filtersDiv.querySelector('[data-filter-key="' + key + '"]')) return;

    // Parse good_range for a sensible default
    var defaultVal = '';
    var defaultDir = 'min';
    if (info.good_range) {
        var range = info.good_range;
        var numMatch = range.match(/([\d.]+)/);
        if (numMatch) defaultVal = numMatch[1];
        if (range.includes('<') || range.toLowerCase().includes('low')) defaultDir = 'max';
    }

    var chip = createEl('div', {
        style: 'display:inline-flex;align-items:center;gap:3px;background:var(--bg-secondary);border:1px solid var(--accent);padding:2px 4px;font-size:9px;border-radius:2px;',
        dataset: { filterKey: key }
    });

    var label = createEl('span', { style: 'color:var(--accent);font-weight:bold;cursor:pointer;', textContent: info.name, title: 'Click for info' });
    label.addEventListener('click', function() { showRatioTooltip(key); });

    var dirSelect = createEl('select', {
        style: 'background:var(--bg-primary,#111);color:var(--text-primary);border:1px solid var(--border);font-family:var(--mono);font-size:8px;padding:1px;',
        dataset: { filterDir: '1' }
    });
    dirSelect.appendChild(createEl('option', { value: 'min', textContent: 'min', selected: defaultDir === 'min' }));
    dirSelect.appendChild(createEl('option', { value: 'max', textContent: 'max', selected: defaultDir === 'max' }));

    var valInput = createEl('input', {
        type: 'number',
        style: 'width:50px;background:var(--bg-primary,#111);color:var(--text-primary);border:1px solid var(--border);font-family:var(--mono);font-size:9px;padding:1px 3px;',
        value: defaultVal,
        step: 'any',
        placeholder: '#',
        dataset: { filterVal: '1' }
    });

    var removeBtn = createEl('button', {
        style: 'background:none;border:none;color:var(--red,#ff4444);cursor:pointer;font-size:11px;font-family:var(--mono);padding:0 2px;',
        textContent: '\u2715'
    });
    removeBtn.addEventListener('click', function() { chip.remove(); });

    chip.appendChild(label);
    chip.appendChild(dirSelect);
    chip.appendChild(valInput);
    chip.appendChild(removeBtn);
    filtersDiv.appendChild(chip);
}

function getActiveFilters() {
    var filtersDiv = document.getElementById('active-filters');
    if (!filtersDiv) return {};
    var rules = {};
    filtersDiv.querySelectorAll('[data-filter-key]').forEach(function(chip) {
        var key = chip.dataset.filterKey;
        var dir = chip.querySelector('[data-filter-dir]');
        var val = chip.querySelector('[data-filter-val]');
        if (dir && val && val.value !== '') {
            var ruleKey = dir.value + '_' + key;
            rules[ruleKey] = parseFloat(val.value);
        }
    });
    return rules;
}

// ============================================================
// PORTFOLIO MANAGEMENT (Multi-Portfolio)
// ============================================================

function getUserId() {
    if (!state.user_id) {
        state.user_id = 'terminal-' + Date.now();
    }
    return state.user_id;
}

async function loadPortfolios() {
    try {
        var resp = await fetch('/api/portfolios?user_id=' + encodeURIComponent(getUserId()));
        if (!resp.ok) return;
        state.portfolios = await resp.json();
        renderPortfolioSelector();
        if (state.portfolios.length > 0 && !state.activePortfolioId) {
            state.activePortfolioId = state.portfolios[0].id;
        }
        if (state.activePortfolioId) {
            renderActivePortfolio();
        }
    } catch (e) {
        console.error('Failed to load portfolios:', e);
    }
}

function renderPortfolioSelector() {
    var sel = document.getElementById('portfolio-selector');
    sel.textContent = '';
    if (state.portfolios.length === 0) {
        sel.appendChild(createEl('option', { value: '', textContent: '— No Portfolios —' }));
    } else {
        state.portfolios.forEach(function(p) {
            var opt = createEl('option', { value: p.id, textContent: p.name + ' (' + p.ticker_count + ')' });
            if (p.id === state.activePortfolioId) opt.selected = true;
            sel.appendChild(opt);
        });
    }
}

function renderActivePortfolio() {
    var display = document.getElementById('portfolio-display');
    display.textContent = '';
    var p = state.portfolios.find(function(x) { return x.id === state.activePortfolioId; });
    if (!p || !p.tickers || p.tickers.length === 0) {
        display.appendChild(createEl('div', { textContent: 'No tickers in this portfolio.', style: 'color: var(--text-secondary); font-size: 10px; padding: 8px;' }));
        return;
    }

    // Weight total indicator
    var totalBar = createEl('div', {
        id: 'weight-total-bar',
        style: 'font-size:9px;color:var(--text-secondary);padding:2px 4px;text-align:right;'
    });
    display.appendChild(totalBar);

    var table = createEl('table', { style: 'width:100%;font-size:10px;border-collapse:collapse;' }, [
        createEl('thead', {}, [
            createEl('tr', {}, [
                createEl('th', { textContent: 'Ticker', style: 'text-align:left;padding:2px 4px;border-bottom:1px solid var(--border);' }),
                createEl('th', { textContent: 'Weight %', style: 'text-align:right;padding:2px 4px;border-bottom:1px solid var(--border);' }),
                createEl('th', { textContent: '', style: 'text-align:center;padding:2px 4px;border-bottom:1px solid var(--border);width:20px;' }),
            ])
        ]),
        createEl('tbody', { id: 'portfolio-tbody' })
    ]);

    function updateTotalDisplay() {
        var inputs = table.querySelectorAll('.weight-input');
        var total = 0;
        inputs.forEach(function(inp) { total += parseFloat(inp.value) || 0; });
        var color = Math.abs(total - 100) < 0.1 ? 'var(--green,#00c853)' : 'var(--red,#ff1744)';
        totalBar.innerHTML = '';
        totalBar.appendChild(createEl('span', { textContent: 'Total: ' }));
        totalBar.appendChild(createEl('span', { style: 'color:' + color + ';font-weight:bold;', textContent: total.toFixed(1) + '%' }));
        if (Math.abs(total - 100) >= 0.1) {
            totalBar.appendChild(createEl('span', { style: 'color:var(--red);margin-left:4px;', textContent: '(must be 100%)' }));
        }
    }

    var tbody = table.querySelector('#portfolio-tbody');
    p.tickers.forEach(function(t) {
        var weightInput = createEl('input', {
            type: 'number',
            className: 'weight-input',
            value: (t.weight * 100).toFixed(1),
            step: '0.1',
            min: '0',
            max: '100',
            style: 'width:55px;background:var(--bg-primary,#111);color:var(--text-primary);border:1px solid var(--border);font-family:var(--mono);font-size:9px;padding:1px 3px;text-align:right;',
            dataset: { symbol: t.symbol }
        });
        weightInput.addEventListener('input', updateTotalDisplay);

        var removeBtn = createEl('button', {
            style: 'background:none;border:none;color:var(--red,#ff4444);cursor:pointer;font-size:10px;font-family:var(--mono);padding:0;',
            textContent: '\u2715',
            title: 'Remove ' + t.symbol
        });
        removeBtn.addEventListener('click', function() {
            removeTickerFromPortfolio(state.activePortfolioId, t.symbol);
        });

        var tr = createEl('tr', {}, [
            createEl('td', { style: 'padding:2px 4px;' }, [
                createEl('span', { textContent: t.symbol, style: 'color:var(--accent);cursor:pointer;' }),
                t.custom_weight ? createEl('span', { textContent: ' *', style: 'color:var(--yellow,#FFD700);font-size:8px;', title: 'Custom weight' }) : document.createTextNode('')
            ]),
            createEl('td', { style: 'text-align:right;padding:2px 4px;' }, [weightInput]),
            createEl('td', { style: 'text-align:center;padding:2px 4px;' }, [removeBtn]),
        ]);
        tbody.appendChild(tr);
    });
    display.appendChild(table);
    updateTotalDisplay();

    // Weight action buttons
    var btnRow = createEl('div', { style: 'display:flex;gap:4px;margin-top:4px;' });

    var saveBtn = createEl('button', {
        style: 'flex:1;background:var(--accent);color:#000;border:none;padding:4px 6px;cursor:pointer;font-family:var(--mono);font-weight:bold;font-size:9px;',
        textContent: 'SAVE WEIGHTS'
    });
    saveBtn.addEventListener('click', function() { savePortfolioWeights(); });

    var equalBtn = createEl('button', {
        style: 'background:var(--bg-secondary);color:var(--text-primary);border:1px solid var(--border);padding:4px 6px;cursor:pointer;font-family:var(--mono);font-size:9px;',
        textContent: 'EQUAL'
    });
    equalBtn.addEventListener('click', function() {
        var inputs = table.querySelectorAll('.weight-input');
        var each = (100 / inputs.length).toFixed(1);
        inputs.forEach(function(inp) { inp.value = each; });
        updateTotalDisplay();
    });

    btnRow.appendChild(saveBtn);
    btnRow.appendChild(equalBtn);
    display.appendChild(btnRow);

    // Deployed positions
    if (p.positions && p.positions.length > 0) {
        var posHead = createEl('div', { textContent: 'DEPLOYED POSITIONS', style: 'margin-top:8px;font-size:9px;color:var(--text-secondary);border-bottom:1px solid var(--border);padding-bottom:2px;' });
        display.appendChild(posHead);
        p.positions.forEach(function(pos) {
            var gl = pos.gain_loss_pct || 0;
            var cls = gl >= 0 ? 'price-up' : 'price-down';
            var row = createEl('div', { style: 'display:flex;justify-content:space-between;padding:1px 4px;font-size:9px;' }, [
                createEl('span', { textContent: pos.ticker, style: 'color:var(--accent);' }),
                createEl('span', { textContent: pos.shares.toFixed(2) + ' @ $' + pos.current_price.toFixed(2) }),
                createEl('span', { className: cls, textContent: (gl >= 0 ? '+' : '') + gl.toFixed(2) + '%' }),
            ]);
            display.appendChild(row);
        });
    }
}

async function savePortfolioWeights() {
    if (!state.activePortfolioId) return;
    var inputs = document.querySelectorAll('.weight-input');
    var total = 0;
    var weights = [];
    inputs.forEach(function(inp) {
        var pct = parseFloat(inp.value) || 0;
        total += pct;
        weights.push({ symbol: inp.dataset.symbol, weight: pct / 100 });
    });
    if (Math.abs(total - 100) >= 0.5) {
        alert('Weights must sum to 100% (currently ' + total.toFixed(1) + '%)');
        return;
    }
    // Normalize to exactly 1.0
    var sum = weights.reduce(function(s, w) { return s + w.weight; }, 0);
    if (sum > 0) {
        weights.forEach(function(w) { w.weight = w.weight / sum; });
    }
    try {
        var resp = await fetch('/api/portfolios/' + state.activePortfolioId + '/weights', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ weights: weights })
        });
        if (!resp.ok) {
            var err = await resp.json();
            alert('Failed to save weights: ' + (err.detail || 'unknown error'));
            return;
        }
        await loadPortfolios();
    } catch (e) {
        console.error('Failed to save weights:', e);
        alert('Failed to save weights: ' + e.message);
    }
}

async function removeTickerFromPortfolio(portfolioId, symbol) {
    if (!confirm('Remove ' + symbol + ' from portfolio?')) return;
    try {
        var resp = await fetch('/api/portfolios/' + portfolioId + '/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbols: [symbol] })
        });
        if (!resp.ok) {
            var err = await resp.json();
            alert('Failed to remove: ' + (err.detail || 'unknown error'));
            return;
        }
        await loadPortfolios();
    } catch (e) {
        console.error('Failed to remove ticker:', e);
    }
}

async function createNewPortfolio(name, tickers) {
    var pName = name || 'Portfolio ' + (state.portfolios.length + 1);
    try {
        var resp = await fetch('/api/portfolios', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: getUserId(), name: pName, tickers: tickers || [] })
        });
        if (!resp.ok) {
            var err = await resp.json();
            alert(err.detail || 'Failed to create portfolio');
            return null;
        }
        var data = await resp.json();
        if (data.warning) console.warn('Portfolio warning:', data.warning);
        await loadPortfolios();
        state.activePortfolioId = data.portfolio.id;
        renderPortfolioSelector();
        renderActivePortfolio();
        return data.portfolio;
    } catch (e) {
        console.error('Failed to create portfolio:', e);
        return null;
    }
}

async function deleteActivePortfolio() {
    if (!state.activePortfolioId) return;
    var p = state.portfolios.find(function(x) { return x.id === state.activePortfolioId; });
    if (!p) return;
    if (!confirm('Delete portfolio "' + p.name + '"?')) return;
    try {
        var resp = await fetch('/api/portfolios/' + state.activePortfolioId, { method: 'DELETE' });
        if (!resp.ok) return;
        state.activePortfolioId = null;
        await loadPortfolios();
    } catch (e) {
        console.error('Failed to delete portfolio:', e);
    }
}

async function deployActivePortfolio() {
    if (!state.activePortfolioId) {
        var tickerInput = document.getElementById('portfolio-tickers').value.trim();
        if (!tickerInput) { alert('Enter tickers or select a portfolio'); return; }
        var tickers = tickerInput.split(',').map(function(s) { return s.trim(); }).filter(Boolean);
        var p = await createNewPortfolio(null, tickers);
        if (!p) return;
        state.activePortfolioId = p.id;
    }
    var amount = parseFloat(document.getElementById('portfolio-capital').value) || 10000;
    var display = document.getElementById('portfolio-display');
    try {
        display.textContent = '';
        display.appendChild(createEl('div', { textContent: 'Deploying...', style: 'color: var(--yellow, #FFD700);' }));
        var resp = await fetch('/api/portfolios/' + state.activePortfolioId + '/deploy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount: amount })
        });
        if (!resp.ok) {
            var err = await resp.json();
            display.textContent = '';
            display.appendChild(createEl('div', { textContent: 'Deploy failed: ' + (err.detail || 'unknown error'), style: 'color: #ff4444;' }));
            return;
        }
        await loadPortfolios();
    } catch (e) {
        console.error('Deploy failed:', e);
        display.textContent = '';
        display.appendChild(createEl('div', { textContent: 'Deploy failed: ' + e.message, style: 'color: #ff4444;' }));
    }
}

async function addTickersToPortfolio(portfolioId, symbols) {
    try {
        var resp = await fetch('/api/portfolios/' + portfolioId + '/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbols: symbols })
        });
        if (!resp.ok) {
            var err = await resp.json();
            alert(err.detail || 'Failed to add tickers');
            return null;
        }
        var data = await resp.json();
        if (data.skipped_message) alert(data.skipped_message);
        await loadPortfolios();
        return data;
    } catch (e) {
        console.error('Failed to add tickers:', e);
        return null;
    }
}

function getSelectedScreenerTickers() {
    var checked = document.querySelectorAll('.screener-check:checked');
    return Array.from(checked).map(function(cb) { return cb.value; });
}

function showAddToPortfolioMenu(source) {
    var tickers = getSelectedScreenerTickers();
    if (tickers.length === 0) {
        alert('Select at least one stock');
        return;
    }
    var existing = document.getElementById('add-portfolio-menu');
    if (existing) existing.remove();

    var menu = createEl('div', {
        id: 'add-portfolio-menu',
        style: 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg-secondary,#1a1a3e);border:2px solid var(--accent);padding:12px;z-index:1000;min-width:250px;font-family:var(--mono);font-size:11px;'
    });
    menu.appendChild(createEl('div', { textContent: 'Add ' + tickers.length + ' stock(s) to:', style: 'margin-bottom:8px;color:var(--text-primary);font-weight:bold;' }));

    state.portfolios.forEach(function(p) {
        var btn = createEl('button', {
            style: 'display:block;width:100%;text-align:left;background:var(--bg-primary,#111125);color:var(--text-primary);border:1px solid var(--border);padding:6px 8px;margin-bottom:4px;cursor:pointer;font-family:var(--mono);font-size:10px;',
            textContent: p.name + ' (' + p.ticker_count + ' tickers)'
        });
        btn.addEventListener('click', async function() {
            menu.remove();
            var result = await addTickersToPortfolio(p.id, tickers);
            if (result) {
                state.activePortfolioId = p.id;
                renderPortfolioSelector();
                renderActivePortfolio();
            }
        });
        menu.appendChild(btn);
    });

    var newBtn = createEl('button', {
        style: 'display:block;width:100%;text-align:left;background:var(--accent);color:#000;border:none;padding:6px 8px;margin-top:6px;cursor:pointer;font-family:var(--mono);font-size:10px;font-weight:bold;',
        textContent: '+ New Portfolio'
    });
    newBtn.addEventListener('click', async function() {
        menu.remove();
        var defaultName = source === 'screener' ? 'Screen ' + new Date().toLocaleDateString() : 'Portfolio ' + (state.portfolios.length + 1);
        var name = prompt('Portfolio name:', defaultName);
        if (!name) return;
        var p = await createNewPortfolio(name, tickers);
        if (p) {
            state.activePortfolioId = p.id;
            renderPortfolioSelector();
            renderActivePortfolio();
        }
    });
    menu.appendChild(newBtn);

    var cancelBtn = createEl('button', {
        style: 'display:block;width:100%;text-align:center;background:none;color:var(--text-secondary);border:1px solid var(--border);padding:4px;margin-top:6px;cursor:pointer;font-family:var(--mono);font-size:9px;',
        textContent: 'Cancel'
    });
    cancelBtn.addEventListener('click', function() { menu.remove(); });
    menu.appendChild(cancelBtn);

    document.body.appendChild(menu);
}

// Portfolio event listeners
document.getElementById('portfolio-selector').addEventListener('change', function(e) {
    state.activePortfolioId = e.target.value || null;
    renderActivePortfolio();
});
document.getElementById('portfolio-new-btn').addEventListener('click', function() {
    var name = prompt('Portfolio name:', 'Portfolio ' + (state.portfolios.length + 1));
    if (name) createNewPortfolio(name, []);
});
document.getElementById('portfolio-delete-btn').addEventListener('click', function() {
    deleteActivePortfolio();
});
document.getElementById('portfolio-deploy-btn').addEventListener('click', function() {
    deployActivePortfolio();
});
document.getElementById('portfolio-harvest-btn').addEventListener('click', async function() {
    if (!state.user_id) { alert('No portfolio deployed yet'); return; }
    try {
        var resp = await fetch('/api/portfolio/harvest/' + encodeURIComponent(state.user_id), { method: 'POST' });
        if (!resp.ok) { alert('Harvest failed'); return; }
        var result = await resp.json();
        if (result.harvested.length === 0) {
            alert('No positions eligible for tax-loss harvesting (need >10% loss)');
        } else {
            alert('Harvested ' + result.harvested.length + ' position(s)');
            await loadPortfolios();
        }
    } catch (e) {
        console.error('Harvest failed:', e);
    }
});

// Load portfolios on startup
loadPortfolios();

// ============================================================
// DEXTER FINANCIAL DATA
// ============================================================
async function loadDexterRatios(symbol) {
    var bar = document.getElementById('dexter-ratios-bar');
    if (!bar) return;
    bar.style.display = 'none';
    bar.textContent = '';
    try {
        var resp = await fetch('/api/dexter/ratios/' + encodeURIComponent(symbol));
        if (!resp.ok) return;
        var data = await resp.json();
        bar.style.display = 'block';
        var items = [
            { label: 'P/E', value: (data.price_to_earnings_ratio || 0).toFixed(1) },
            { label: 'P/B', value: (data.price_to_book_ratio || 0).toFixed(1) },
            { label: 'EPS', value: '$' + (data.earnings_per_share || 0).toFixed(2) },
            { label: 'ROE', value: ((data.return_on_equity || 0) * 100).toFixed(1) + '%' },
            { label: 'D/E', value: (data.debt_to_equity || 0).toFixed(2) },
            { label: 'FCF/sh', value: '$' + (data.free_cash_flow_per_share || 0).toFixed(2) },
            { label: 'PEG', value: (data.peg_ratio || 0).toFixed(2) },
            { label: 'Margin', value: ((data.net_margin || 0) * 100).toFixed(1) + '%' },
            { label: 'EV/EBITDA', value: (data.enterprise_value_to_ebitda_ratio || 0).toFixed(1) },
        ];
        items.forEach(function(item) {
            bar.appendChild(createEl('span', {
                style: 'margin-right:12px;',
            }, [
                createEl('span', { style: 'color:var(--text-secondary);', textContent: item.label + ': ' }),
                createEl('span', { style: 'color:var(--accent);', textContent: item.value })
            ]));
        });
    } catch (e) {
        // Silently fail — ratios bar just stays hidden
    }
}

async function loadInsiderTrades(symbol) {
    var container = document.getElementById('insider-trades-list');
    if (!container) return;
    container.textContent = '';
    container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Loading insider trades...' }));
    try {
        var resp = await fetch('/api/dexter/insider-trades/' + encodeURIComponent(symbol) + '?limit=20');
        if (!resp.ok) {
            container.textContent = '';
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No insider trade data available.' }));
            return;
        }
        var trades = await resp.json();
        container.textContent = '';
        if (!trades || trades.length === 0) {
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No recent insider trades.' }));
            return;
        }
        trades.forEach(function(t) {
            var txShares = t.transaction_shares || 0;
            var isBuy = txShares > 0;
            var color = isBuy ? 'var(--green, #00c853)' : 'var(--red, #ff1744)';
            var arrow = isBuy ? '\u25B2 BUY' : '\u25BC SELL';
            var shares = Math.abs(txShares).toLocaleString();
            var price = t.transaction_price_per_share ? '$' + t.transaction_price_per_share.toFixed(2) : '';
            var value = t.transaction_value ? '$' + (Math.abs(t.transaction_value) / 1e6).toFixed(2) + 'M' : '';
            var who = (t.name || 'Insider') + (t.title ? ' (' + t.title + ')' : '');
            var row = createEl('div', { style: 'padding:3px 8px;border-bottom:1px solid var(--border);font-size:10px;' }, [
                createEl('div', { style: 'display:flex;justify-content:space-between;' }, [
                    createEl('span', { style: 'color:' + color + ';font-weight:bold;', textContent: arrow }),
                    createEl('span', { style: 'color:var(--text-secondary);', textContent: t.filing_date || '' }),
                ]),
                createEl('div', { style: 'color:var(--text-primary);font-size:9px;', textContent: who }),
                createEl('div', { style: 'display:flex;justify-content:space-between;color:var(--text-secondary);' }, [
                    createEl('span', { textContent: shares + ' shares ' + price }),
                    createEl('span', { style: 'color:' + color + ';', textContent: value }),
                ]),
            ]);
            container.appendChild(row);
        });
    } catch (e) {
        container.textContent = '';
        container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Insider trades unavailable.' }));
    }
}

async function loadAnalystEstimates(symbol) {
    var container = document.getElementById('analyst-estimates-list');
    if (!container) return;
    container.textContent = '';
    container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Loading analyst estimates...' }));
    try {
        var resp = await fetch('/api/dexter/analyst-estimates/' + encodeURIComponent(symbol));
        if (!resp.ok) {
            container.textContent = '';
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No analyst estimates available.' }));
            return;
        }
        var estimates = await resp.json();
        container.textContent = '';
        if (!estimates || estimates.length === 0) {
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No analyst estimates.' }));
            return;
        }
        // Table header
        var thead = createEl('div', { style: 'display:flex;padding:3px 8px;border-bottom:1px solid var(--border);font-size:9px;color:var(--text-secondary);font-weight:bold;' }, [
            createEl('span', { style: 'flex:1;', textContent: 'PERIOD' }),
            createEl('span', { style: 'flex:1;text-align:right;', textContent: 'EPS EST' }),
            createEl('span', { style: 'flex:1;text-align:right;', textContent: 'REV EST' }),
        ]);
        container.appendChild(thead);
        estimates.forEach(function(est) {
            var period = est.fiscal_period || est.period || '?';
            var epsEst = est.earnings_per_share != null ? '$' + est.earnings_per_share.toFixed(2) : '--';
            var revEst = est.revenue != null ? '$' + (est.revenue / 1e9).toFixed(2) + 'B' : '--';
            var row = createEl('div', { style: 'display:flex;padding:3px 8px;border-bottom:1px solid var(--border);font-size:10px;' }, [
                createEl('span', { style: 'flex:1;color:var(--accent);', textContent: period }),
                createEl('span', { style: 'flex:1;text-align:right;', textContent: epsEst }),
                createEl('span', { style: 'flex:1;text-align:right;', textContent: revEst }),
            ]);
            container.appendChild(row);
        });
    } catch (e) {
        container.textContent = '';
        container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Analyst estimates unavailable.' }));
    }
}

async function loadSecFilings(symbol) {
    var container = document.getElementById('sec-filings-list');
    if (!container) return;
    container.textContent = '';
    container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Loading SEC filings...' }));
    try {
        var resp = await fetch('/api/dexter/filings/' + encodeURIComponent(symbol) + '?limit=15');
        if (!resp.ok) {
            container.textContent = '';
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No SEC filings available.' }));
            return;
        }
        var filings = await resp.json();
        container.textContent = '';
        if (!filings || filings.length === 0) {
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No SEC filings found.' }));
            return;
        }
        filings.forEach(function(f) {
            var fType = f.filing_type || 'Filing';
            var typeColor = fType.includes('10-K') ? 'var(--accent)' : fType.includes('10-Q') ? 'var(--green, #00c853)' : fType.includes('8-K') ? '#FFD700' : 'var(--text-primary)';
            var row = createEl('div', { style: 'padding:3px 8px;border-bottom:1px solid var(--border);font-size:10px;cursor:pointer;' }, [
                createEl('div', { style: 'display:flex;justify-content:space-between;' }, [
                    createEl('span', { style: 'color:' + typeColor + ';font-weight:bold;', textContent: fType }),
                    createEl('span', { style: 'color:var(--text-secondary);', textContent: f.filing_date || '' }),
                ]),
                createEl('div', { style: 'color:var(--text-secondary);font-size:9px;', textContent: 'Report: ' + (f.report_date || '') + ' | CIK: ' + (f.cik || '') }),
            ]);
            if (f.url) {
                row.addEventListener('click', function() { window.open(f.url, '_blank', 'noopener'); });
            }
            container.appendChild(row);
        });
    } catch (e) {
        container.textContent = '';
        container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'SEC filings unavailable.' }));
    }
}

// ============================================================
// DCF VALUATION
// ============================================================
async function runDCF(symbol) {
    var sym = symbol || state.currentSymbol;
    var resultsEl = document.getElementById('backtest-results');
    resultsEl.classList.remove('hidden');
    resultsEl.textContent = '';
    resultsEl.appendChild(createEl('span', { style: 'color:var(--yellow,#FFD700);', textContent: 'Running DCF valuation for ' + sym + '...' }));

    try {
        var resp = await fetch('/api/dexter/dcf/' + encodeURIComponent(sym));
        if (!resp.ok) {
            var err = await resp.json();
            resultsEl.textContent = '';
            resultsEl.appendChild(createEl('span', { style: 'color:var(--red);', textContent: 'DCF error: ' + (err.detail || resp.status) }));
            return;
        }
        var dcf = await resp.json();
        resultsEl.textContent = '';

        var verdictColor = dcf.verdict === 'UNDERVALUED' ? 'var(--green, #00c853)' : dcf.verdict === 'OVERVALUED' ? 'var(--red, #ff1744)' : 'var(--accent)';
        var upsideSign = dcf.upside_pct >= 0 ? '+' : '';

        var closeBtn = createEl('span', { style: 'margin-left:12px;color:var(--text-dim);cursor:pointer;font-size:14px;', textContent: '\u2715' });
        closeBtn.addEventListener('click', function() { resultsEl.classList.add('hidden'); });

        var detailBtn = createEl('span', { style: 'margin-left:12px;color:var(--accent);cursor:pointer;text-decoration:underline;font-size:10px;', textContent: 'details' });
        detailBtn.addEventListener('click', function() { showDCFDetail(dcf); });

        resultsEl.append(
            createEl('span', { style: 'color:var(--accent);font-weight:bold;', textContent: 'DCF ' + sym + ': ' }),
            createEl('span', { style: 'color:' + verdictColor + ';font-weight:bold;', textContent: dcf.verdict }),
            createEl('span', { style: 'margin-left:12px;', textContent: 'Fair Value: $' + dcf.fair_value }),
            createEl('span', { style: 'margin-left:12px;', textContent: 'Price: $' + dcf.current_price }),
            createEl('span', { style: 'margin-left:12px;color:' + verdictColor + ';', textContent: upsideSign + dcf.upside_pct + '%' }),
            createEl('span', { style: 'margin-left:12px;color:var(--text-secondary);', textContent: 'WACC: ' + dcf.wacc + '%' }),
            createEl('span', { style: 'margin-left:12px;color:var(--text-secondary);', textContent: 'FCF Growth: ' + dcf.fcf_growth_rate + '%' }),
            detailBtn,
            closeBtn
        );
    } catch (e) {
        resultsEl.textContent = '';
        resultsEl.appendChild(createEl('span', { style: 'color:var(--red);', textContent: 'DCF error: ' + e.message }));
    }
}

function showDCFDetail(dcf) {
    var existing = document.getElementById('dcf-detail-modal');
    if (existing) existing.remove();

    var modal = createEl('div', {
        id: 'dcf-detail-modal',
        style: 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#0d0d1a;border:2px solid var(--accent);padding:16px;z-index:9999;min-width:420px;max-width:600px;max-height:80vh;overflow-y:auto;font-family:var(--mono);font-size:11px;box-shadow:0 4px 20px rgba(0,0,0,0.7);'
    });

    var verdictColor = dcf.verdict === 'UNDERVALUED' ? '#00c853' : dcf.verdict === 'OVERVALUED' ? '#ff1744' : 'var(--accent)';

    // Header
    modal.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;font-size:16px;margin-bottom:8px;', textContent: 'DCF Valuation — ' + dcf.ticker }));
    modal.appendChild(createEl('div', { style: 'color:' + verdictColor + ';font-size:14px;font-weight:bold;margin-bottom:12px;', textContent: dcf.verdict + ' | Fair Value: $' + dcf.fair_value + ' (' + (dcf.upside_pct >= 0 ? '+' : '') + dcf.upside_pct + '%)' }));

    // Key Inputs
    modal.appendChild(createEl('div', { style: 'color:var(--text-secondary);font-size:10px;margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px;', textContent: 'INPUTS' }));
    var inputs = dcf.inputs || {};
    var inputsText = 'Sector: ' + inputs.sector + ' | D/E: ' + inputs.debt_to_equity + ' | Net Margin: ' + inputs.net_margin_pct + '% | FCF History: ' + inputs.fcf_history_years + 'yr';
    modal.appendChild(createEl('div', { style: 'margin-bottom:10px;color:var(--text-primary);', textContent: inputsText }));

    // Assumptions
    modal.appendChild(createEl('div', { style: 'color:var(--text-secondary);font-size:10px;margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px;', textContent: 'ASSUMPTIONS' }));
    modal.appendChild(createEl('div', { style: 'margin-bottom:10px;', textContent: 'WACC: ' + dcf.wacc + '% | FCF Growth: ' + dcf.fcf_growth_rate + '% | Terminal Growth: 2.5% | Terminal Value: ' + dcf.terminal_value_pct + '% of EV' }));

    // Projected FCFs
    modal.appendChild(createEl('div', { style: 'color:var(--text-secondary);font-size:10px;margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px;', textContent: 'PROJECTED FREE CASH FLOWS' }));
    if (dcf.projected_fcf) {
        dcf.projected_fcf.forEach(function(p) {
            modal.appendChild(createEl('div', { style: 'display:flex;justify-content:space-between;padding:1px 0;', textContent: '' }, [
                createEl('span', { textContent: 'Year ' + p.year }),
                createEl('span', { style: 'color:var(--accent);', textContent: '$' + (p.fcf / 1e9).toFixed(2) + 'B' }),
                createEl('span', { style: 'color:var(--text-secondary);', textContent: p.growth + '% growth' }),
            ]));
        });
    }

    // Sensitivity Table
    if (dcf.sensitivity) {
        modal.appendChild(createEl('div', { style: 'color:var(--text-secondary);font-size:10px;margin-top:10px;margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:4px;', textContent: 'SENSITIVITY ANALYSIS (Fair Value)' }));
        var headerRow = createEl('div', { style: 'display:flex;gap:8px;font-size:9px;color:var(--text-secondary);padding:2px 0;' }, [
            createEl('span', { style: 'width:60px;', textContent: 'WACC' }),
            createEl('span', { style: 'width:70px;text-align:right;', textContent: 'TG 2.0%' }),
            createEl('span', { style: 'width:70px;text-align:right;', textContent: 'TG 2.5%' }),
            createEl('span', { style: 'width:70px;text-align:right;', textContent: 'TG 3.0%' }),
        ]);
        modal.appendChild(headerRow);
        dcf.sensitivity.forEach(function(row) {
            var isBase = row.wacc === dcf.wacc;
            var rowEl = createEl('div', { style: 'display:flex;gap:8px;font-size:10px;padding:2px 0;' + (isBase ? 'background:rgba(255,140,0,0.1);' : '') }, [
                createEl('span', { style: 'width:60px;color:var(--text-secondary);', textContent: row.wacc + '%' }),
                createEl('span', { style: 'width:70px;text-align:right;', textContent: row.tg_2_0 != null ? '$' + row.tg_2_0 : 'N/A' }),
                createEl('span', { style: 'width:70px;text-align:right;color:var(--accent);', textContent: row.tg_2_5 != null ? '$' + row.tg_2_5 : 'N/A' }),
                createEl('span', { style: 'width:70px;text-align:right;', textContent: row.tg_3_0 != null ? '$' + row.tg_3_0 : 'N/A' }),
            ]);
            modal.appendChild(rowEl);
        });
    }

    // Warnings
    if (dcf.warnings && dcf.warnings.length > 0) {
        modal.appendChild(createEl('div', { style: 'color:var(--yellow,#FFD700);font-size:9px;margin-top:10px;border-top:1px solid var(--border);padding-top:6px;', textContent: 'WARNINGS: ' + dcf.warnings.join(' | ') }));
    }

    // Close button
    var closeBtn = createEl('button', { style: 'position:absolute;top:6px;right:10px;background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:16px;font-family:var(--mono);', textContent: 'X' });
    closeBtn.addEventListener('click', function() { modal.remove(); });
    modal.appendChild(closeBtn);

    document.body.appendChild(modal);
}

// ============================================================
// FUNDAMENTAL SCORE
// ============================================================
async function loadFundScore(symbol) {
    var sym = symbol || state.currentSymbol;
    try {
        var resp = await fetch('/api/dexter/score/' + encodeURIComponent(sym));
        if (!resp.ok) return;
        var score = await resp.json();

        // Show score badge in chart title
        var titleEl = document.getElementById('chart-title');
        var existingBadge = document.getElementById('score-badge');
        if (existingBadge) existingBadge.remove();

        var gradeColor = score.grade === 'A' ? '#00c853' : score.grade === 'B' ? '#4488ff' : score.grade === 'C' ? '#FFD700' : '#ff1744';
        var badge = createEl('span', {
            id: 'score-badge',
            style: 'margin-left:10px;padding:1px 6px;border:1px solid ' + gradeColor + ';color:' + gradeColor + ';font-size:10px;cursor:pointer;',
            title: 'Profitability: ' + score.profitability + '/25 | Growth: ' + score.growth + '/25 | Valuation: ' + score.valuation + '/25 | Health: ' + score.financial_health + '/25',
            textContent: score.grade + ' ' + score.total_score + '/100'
        });
        badge.addEventListener('click', function() { showScoreDetail(score, sym); });
        titleEl.appendChild(badge);
    } catch (e) {
        // Silently fail
    }
}

function showScoreDetail(score, symbol) {
    var existing = document.getElementById('score-detail-modal');
    if (existing) existing.remove();

    var modal = createEl('div', {
        id: 'score-detail-modal',
        style: 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#0d0d1a;border:2px solid var(--accent);padding:16px;z-index:9999;min-width:300px;font-family:var(--mono);font-size:11px;box-shadow:0 4px 20px rgba(0,0,0,0.7);'
    });

    var gradeColor = score.grade === 'A' ? '#00c853' : score.grade === 'B' ? '#4488ff' : score.grade === 'C' ? '#FFD700' : '#ff1744';

    modal.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;font-size:14px;margin-bottom:8px;', textContent: 'Fundamental Score — ' + symbol }));
    modal.appendChild(createEl('div', { style: 'color:' + gradeColor + ';font-size:24px;font-weight:bold;margin-bottom:12px;', textContent: score.grade + ' ' + score.total_score + '/100' }));

    var categories = [
        { name: 'Profitability', score: score.profitability, max: 25, desc: 'ROE, net margin, gross margin' },
        { name: 'Growth', score: score.growth, max: 25, desc: 'Revenue growth, EPS growth, FCF growth' },
        { name: 'Valuation', score: score.valuation, max: 25, desc: 'P/E, PEG, EV/EBITDA, P/S (lower=better)' },
        { name: 'Financial Health', score: score.financial_health, max: 25, desc: 'Current ratio, D/E, quick ratio, FCF yield' },
    ];

    categories.forEach(function(cat) {
        var pct = (cat.score / cat.max * 100);
        var barColor = pct >= 70 ? '#00c853' : pct >= 40 ? '#FFD700' : '#ff1744';
        modal.appendChild(createEl('div', { style: 'margin-bottom:8px;' }, [
            createEl('div', { style: 'display:flex;justify-content:space-between;', }, [
                createEl('span', { style: 'color:var(--text-primary);', textContent: cat.name }),
                createEl('span', { style: 'color:' + barColor + ';', textContent: cat.score + '/' + cat.max }),
            ]),
            createEl('div', { style: 'height:4px;background:var(--border);margin-top:2px;' }, [
                createEl('div', { style: 'height:100%;width:' + pct + '%;background:' + barColor + ';' }),
            ]),
            createEl('div', { style: 'color:var(--text-secondary);font-size:8px;', textContent: cat.desc }),
        ]));
    });

    var dcfBtn = createEl('button', { style: 'width:100%;margin-top:8px;background:var(--accent);color:#000;border:none;padding:6px;cursor:pointer;font-family:var(--mono);font-weight:bold;font-size:10px;', textContent: 'RUN DCF VALUATION' });
    dcfBtn.addEventListener('click', function() { modal.remove(); runDCF(symbol); });
    modal.appendChild(dcfBtn);

    var closeBtn = createEl('button', { style: 'position:absolute;top:6px;right:10px;background:none;border:none;color:var(--text-secondary);cursor:pointer;font-size:16px;font-family:var(--mono);', textContent: 'X' });
    closeBtn.addEventListener('click', function() { modal.remove(); });
    modal.appendChild(closeBtn);

    document.body.appendChild(modal);
}

// ============================================================
// FINANCIAL STATEMENTS
// ============================================================
async function loadFinancials(symbol) {
    var container = document.getElementById('financials-list');
    if (!container) return;
    container.textContent = '';
    container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Loading financial statements...' }));
    try {
        var resp = await fetch('/api/dexter/financials/' + encodeURIComponent(symbol) + '?period=annual&limit=4');
        if (!resp.ok) {
            container.textContent = '';
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No financial data available.' }));
            return;
        }
        var data = await resp.json();
        container.textContent = '';

        // Sub-tabs for Income / Balance / Cash Flow
        var subTabs = createEl('div', { style: 'display:flex;gap:2px;margin-bottom:6px;padding:0 4px;' });
        ['income', 'balance', 'cashflow'].forEach(function(tabKey) {
            var labels = { income: 'INCOME', balance: 'BALANCE SHEET', cashflow: 'CASH FLOW' };
            var btn = createEl('button', {
                style: 'background:none;border:1px solid var(--border);color:var(--text-secondary);padding:2px 6px;font-family:var(--mono);font-size:8px;cursor:pointer;',
                textContent: labels[tabKey],
                dataset: { fintab: tabKey }
            });
            if (tabKey === 'income') { btn.style.color = 'var(--accent)'; btn.style.borderColor = 'var(--accent)'; }
            btn.addEventListener('click', function() {
                subTabs.querySelectorAll('button').forEach(function(b) { b.style.color = 'var(--text-secondary)'; b.style.borderColor = 'var(--border)'; });
                btn.style.color = 'var(--accent)'; btn.style.borderColor = 'var(--accent)';
                container.querySelectorAll('.fin-section').forEach(function(s) { s.style.display = 'none'; });
                var el = document.getElementById('fin-' + tabKey);
                if (el) el.style.display = '';
            });
            subTabs.appendChild(btn);
        });
        container.appendChild(subTabs);

        // Helper: render a statements array as a table
        function renderStatements(statements, fields, sectionId, visible) {
            var section = createEl('div', { id: sectionId, className: 'fin-section', style: visible ? '' : 'display:none;' });
            if (!statements || statements.length === 0) {
                section.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No data.' }));
                container.appendChild(section);
                return;
            }
            // Header row with periods
            var headerRow = createEl('div', { style: 'display:flex;font-size:8px;color:var(--text-secondary);font-weight:bold;padding:2px 4px;border-bottom:1px solid var(--border);' });
            headerRow.appendChild(createEl('span', { style: 'flex:2;', textContent: '' }));
            statements.forEach(function(s) {
                var period = (s.report_period || s.fiscal_period || '').substring(0, 7);
                headerRow.appendChild(createEl('span', { style: 'flex:1;text-align:right;', textContent: period }));
            });
            section.appendChild(headerRow);
            // Data rows
            fields.forEach(function(f) {
                var row = createEl('div', { style: 'display:flex;font-size:9px;padding:1px 4px;border-bottom:1px solid rgba(255,255,255,0.05);' });
                row.appendChild(createEl('span', { style: 'flex:2;color:var(--text-secondary);', textContent: f.label }));
                statements.forEach(function(s) {
                    var val = s[f.key];
                    var txt = val != null ? (Math.abs(val) >= 1e9 ? (val / 1e9).toFixed(1) + 'B' : Math.abs(val) >= 1e6 ? (val / 1e6).toFixed(0) + 'M' : val.toLocaleString()) : '--';
                    var color = f.highlight && val != null ? (val >= 0 ? 'var(--green,#00c853)' : 'var(--red,#ff1744)') : 'var(--text-primary)';
                    row.appendChild(createEl('span', { style: 'flex:1;text-align:right;color:' + color + ';', textContent: txt }));
                });
                section.appendChild(row);
            });
            container.appendChild(section);
        }

        // Income Statement fields
        renderStatements(data.income_statements, [
            { key: 'revenue', label: 'Revenue' },
            { key: 'cost_of_revenue', label: 'COGS' },
            { key: 'gross_profit', label: 'Gross Profit', highlight: true },
            { key: 'operating_expense', label: 'OpEx' },
            { key: 'operating_income', label: 'Operating Income', highlight: true },
            { key: 'interest_expense', label: 'Interest Exp' },
            { key: 'net_income', label: 'Net Income', highlight: true },
            { key: 'earnings_per_share', label: 'EPS' },
            { key: 'weighted_average_shares', label: 'Shares Out' },
        ], 'fin-income', true);

        // Balance Sheet fields
        renderStatements(data.balance_sheets, [
            { key: 'total_assets', label: 'Total Assets' },
            { key: 'current_assets', label: 'Current Assets' },
            { key: 'cash_and_equivalents', label: 'Cash' },
            { key: 'total_liabilities', label: 'Total Liabilities' },
            { key: 'current_liabilities', label: 'Current Liab' },
            { key: 'long_term_debt', label: 'LT Debt' },
            { key: 'shareholders_equity', label: 'Equity', highlight: true },
            { key: 'total_debt', label: 'Total Debt' },
        ], 'fin-balance', false);

        // Cash Flow fields
        renderStatements(data.cash_flow_statements, [
            { key: 'operating_cash_flow', label: 'Operating CF', highlight: true },
            { key: 'capital_expenditure', label: 'CapEx' },
            { key: 'free_cash_flow', label: 'Free Cash Flow', highlight: true },
            { key: 'dividends_paid', label: 'Dividends Paid' },
            { key: 'share_repurchase', label: 'Buybacks' },
            { key: 'net_change_in_cash', label: 'Net Cash Change', highlight: true },
        ], 'fin-cashflow', false);

    } catch (e) {
        container.textContent = '';
        container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Financial statements unavailable.' }));
    }
}

// ============================================================
// SEGMENTED REVENUES
// ============================================================
async function loadSegments(symbol) {
    var container = document.getElementById('segments-list');
    if (!container) return;
    container.textContent = '';
    container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Loading revenue segments...' }));
    try {
        var resp = await fetch('/api/dexter/segments/' + encodeURIComponent(symbol) + '?period=annual&limit=4');
        if (!resp.ok) {
            container.textContent = '';
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No segment data available.' }));
            return;
        }
        var segments = await resp.json();
        container.textContent = '';
        if (!segments || segments.length === 0) {
            container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'No segment data.' }));
            return;
        }
        // Group by period
        var byPeriod = {};
        segments.forEach(function(s) {
            var key = s.report_period || s.period || 'unknown';
            if (!byPeriod[key]) byPeriod[key] = [];
            byPeriod[key].push(s);
        });
        Object.entries(byPeriod).forEach(function(entry) {
            var period = entry[0], items = entry[1];
            container.appendChild(createEl('div', { style: 'color:var(--accent);font-weight:bold;font-size:10px;padding:4px 8px;border-bottom:1px solid var(--border);margin-top:4px;', textContent: period.substring(0, 7) }));
            var total = items.reduce(function(sum, s) { return sum + (s.revenue || 0); }, 0);
            items.sort(function(a, b) { return (b.revenue || 0) - (a.revenue || 0); });
            items.forEach(function(s) {
                var pct = total > 0 ? ((s.revenue || 0) / total * 100).toFixed(1) : '0.0';
                var revStr = s.revenue ? (s.revenue >= 1e9 ? (s.revenue / 1e9).toFixed(2) + 'B' : (s.revenue / 1e6).toFixed(0) + 'M') : '--';
                var barWidth = total > 0 ? Math.max(2, (s.revenue || 0) / total * 100) : 0;
                var row = createEl('div', { style: 'padding:2px 8px;font-size:9px;position:relative;' }, [
                    createEl('div', { style: 'position:absolute;left:0;top:0;bottom:0;width:' + barWidth + '%;background:rgba(255,140,0,0.08);' }),
                    createEl('div', { style: 'display:flex;justify-content:space-between;position:relative;' }, [
                        createEl('span', { style: 'color:var(--text-primary);', textContent: s.segment_name || s.product_name || 'Other' }),
                        createEl('span', { style: 'color:var(--accent);', textContent: '$' + revStr + ' (' + pct + '%)' }),
                    ]),
                ]);
                container.appendChild(row);
            });
        });
    } catch (e) {
        container.textContent = '';
        container.appendChild(createEl('div', { style: 'color:var(--text-secondary);padding:8px;font-size:10px;', textContent: 'Segment data unavailable.' }));
    }
}

// Intel tab switching
document.querySelectorAll('.intel-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
        var target = tab.dataset.tab;
        // Update active tab styling
        document.querySelectorAll('.intel-tab').forEach(function(t) {
            t.classList.remove('active');
            t.style.color = 'var(--text-secondary)';
        });
        tab.classList.add('active');
        tab.style.color = 'var(--accent)';
        // Update pane title
        var titles = { news: 'NEWS', insiders: 'INSIDERS', estimates: 'ESTIMATES', filings: 'FILINGS', financials: 'FINANCIALS', segments: 'SEGMENTS' };
        document.querySelector('#pane-news .pane-title').textContent = titles[target] || 'NEWS';
        // Show/hide content
        var panels = ['news-list', 'insider-trades-list', 'analyst-estimates-list', 'sec-filings-list', 'financials-list', 'segments-list'];
        var panelMap = { news: 'news-list', insiders: 'insider-trades-list', estimates: 'analyst-estimates-list', filings: 'sec-filings-list', financials: 'financials-list', segments: 'segments-list' };
        panels.forEach(function(id) { document.getElementById(id).style.display = 'none'; });
        var activePanel = panelMap[target];
        if (activePanel) document.getElementById(activePanel).style.display = '';
        // Also hide/show ratios bar based on tab
        var ratiosBar = document.getElementById('dexter-ratios-bar');
        if (ratiosBar) ratiosBar.style.display = (target === 'news') ? ratiosBar.dataset.wasVisible || 'none' : 'none';
        // Load data on tab switch
        var sym = state.currentSymbol;
        if (target === 'insiders') loadInsiderTrades(sym);
        else if (target === 'estimates') loadAnalystEstimates(sym);
        else if (target === 'filings') loadSecFilings(sym);
        else if (target === 'financials') loadFinancials(sym);
        else if (target === 'segments') loadSegments(sym);
    });
});

// --- Backtest ---
async function runBacktest() {
    const tickers = state.watchlist.length ? state.watchlist : [state.currentSymbol];
    const resultsEl = document.getElementById('backtest-results');
    resultsEl.classList.remove('hidden');
    resultsEl.textContent = 'Running backtest...';

    try {
        const resp = await fetch('/api/generate-asset/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tickers, period: '1y' })
        });
        if (!resp.ok) {
            resultsEl.textContent = '';
            resultsEl.appendChild(createEl('span', { style: 'color:var(--red);', textContent: 'Backtest failed: ' + resp.status }));
            console.error('Backtest API error:', resp.status);
            return;
        }
        const data = await resp.json();

        const m = data.metrics || {};
        const score = data.score ?? '--';
        const sharpe = m.sharpe_ratio != null ? m.sharpe_ratio.toFixed(2) : '--';
        const ret = m.total_return != null ? (m.total_return * 100).toFixed(1) + '%' : '--';
        const dd = m.max_drawdown != null ? (m.max_drawdown * 100).toFixed(1) + '%' : '--';
        const vol = m.annual_volatility != null ? (m.annual_volatility * 100).toFixed(1) + '%' : '--';

        resultsEl.textContent = '';
        const closeBtn = createEl('span', { style: 'margin-left:16px;color:var(--text-dim);cursor:pointer;', textContent: '\u2715' });
        closeBtn.addEventListener('click', () => resultsEl.classList.add('hidden'));
        resultsEl.append(
            createEl('span', { style: 'color:var(--accent);font-weight:bold;', textContent: `SCORE: ${score}/100` }),
            createEl('span', { style: 'margin-left:16px;', textContent: `Sharpe: ${sharpe}` }),
            createEl('span', { style: 'margin-left:16px;', textContent: `Return: ${ret}` }),
            createEl('span', { style: 'margin-left:16px;', textContent: `Max DD: ${dd}` }),
            createEl('span', { style: 'margin-left:16px;', textContent: `Volatility: ${vol}` }),
            closeBtn,
        );
    } catch (e) {
        resultsEl.textContent = '';
        resultsEl.appendChild(createEl('span', { style: 'color:var(--red);', textContent: 'Backtest error: ' + e.message }));
        console.error('Backtest error:', e);
    }
}

document.getElementById('backtest-btn').addEventListener('click', runBacktest);
