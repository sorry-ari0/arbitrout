// === ARBITROUT FRONTEND ===
// Prediction market arbitrage scanner UI

let arbMode = localStorage.getItem('arbMode') || 'lobsterminal';
let arbPollingInterval = null;
let arbScanInterval = null;
let arbWs = null;
let selectedOpp = null;
let feedItems = [];

let lastLoadedOpportunities = []; // To hold opportunities for restoring selected
let lastSelectedOppId = localStorage.getItem('arbSelectedOppId') || null; // For restoring selected opportunity

// NEW: For hedge packages, saved markets, news, and insider signals
let hedgePackages = [];
let savedMarkets = [];
let newsItems = [];
let insiderSignals = { wallets_count: 0, recent_movements: [], active_signals: [], convergence_alerts: [] }; // NEW: Insider data structure
let whaleSignals = { scan_count: 0, active_signals: 0, signals: {} }; // Kalshi whale data
let arbHedgePollingInterval = null; // New interval for hedge packages
let arbNewsPollingInterval = null; // NEW interval for news scanner
let arbInsiderPollingInterval = null; // NEW interval for insider signals
let arbWhalePollingInterval = null; // Kalshi whale signals interval
let forexPollingInterval = null; // NEW: Forex rates polling interval
let currentForexRates = {}; // NEW: Store current forex rates

// === PIXEL ART (CSS grid of div cells) ===
function createPixelGrid(colorMap, scale) {
    // colorMap: 2D array of hex colors (null = transparent)
    var rows = colorMap.length;
    var cols = colorMap[0] ? colorMap[0].length : rows;
    var container = document.createElement('div');
    container.style.display = 'grid';
    container.style.gridTemplateColumns = 'repeat(' + cols + ', ' + scale + 'px)';
    container.style.gridTemplateRows = 'repeat(' + rows + ', ' + scale + 'px)';
    container.style.imageRendering = 'pixelated';

    for (var y = 0; y < rows; y++) {
        for (var x = 0; x < cols; x++) {
            var cell = document.createElement('div');
            var color = colorMap[y][x];
            if (color) {
                cell.style.backgroundColor = color;
            }
            container.appendChild(cell);
        }
    }
    return container;
}

function getTroutPixelArt() {
    // 44x20: elongated rainbow trout on grey wire phone at desk
    var O = '#3d5c3a'; // dark olive back
    var G = '#5a7a4e'; // green upper body
    var S = '#8aaa7e'; // silver-green
    var R = '#d44868'; // pink-red lateral stripe
    var K = '#b8c8b8'; // silver sides
    var W = '#dde4dd'; // white belly
    var E = '#111122'; // eye pupil
    var Q = '#ffffff'; // eye white
    var X = '#2d3d2d'; // dark spots
    var N = '#d49050'; // orange fin
    var T = '#8a6830'; // fin edge
    var H = '#78909c'; // grey phone handset
    var L = '#546e7a'; // dark grey phone
    var C = '#90a4ae'; // grey wire cord
    var I = '#2a2a3a'; // monitor frame
    var B = '#00bcd4'; // screen glow
    var D = '#3a3a4a'; // desk
    var J = '#455a64'; // phone base
    var _ = null;
    var map = [
[_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,N,N,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
[_,_,_,_,_,_,_,_,_,_,O,O,O,O,O,O,O,O,N,O,O,N,O,O,O,O,O,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
[_,_,_,_,_,_,_,_,O,G,G,G,X,G,G,G,G,G,G,G,G,G,G,X,G,G,G,G,G,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_],
[_,_,_,_,_,_,_,O,G,G,G,G,G,X,G,G,X,G,G,G,X,G,G,G,G,G,X,G,G,G,G,O,O,_,_,_,_,_,_,_,_,_,_,_],
[_,_,N,N,_,_,O,G,G,X,G,G,G,G,G,G,G,G,X,G,G,G,G,G,X,G,G,G,G,G,G,G,G,O,_,_,_,_,_,_,_,_,_,_],
[_,N,T,N,_,O,G,G,G,G,G,X,G,G,G,G,G,G,G,G,G,X,G,G,G,G,G,G,G,G,G,G,Q,E,O,_,_,_,_,H,H,_,_,_],
[N,T,T,N,O,G,G,G,G,G,G,G,G,X,G,G,G,G,G,G,G,G,G,G,G,G,G,G,G,S,S,S,G,G,G,O,_,_,H,L,L,H,_,_],
[_,N,T,N,O,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,S,S,S,G,O,O,_,_,H,L,H,_,_],
[_,_,N,N,_,O,S,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,S,S,S,O,O,_,_,_,H,L,L,H,_,_],
[_,_,_,_,_,_,O,K,K,W,W,K,K,W,K,K,W,W,K,K,W,W,K,K,W,W,K,K,K,O,O,N,N,_,_,_,_,_,_,H,H,_,_,_],
[_,_,_,_,_,_,_,O,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,O,O,O,_,N,T,_,_,_,_,_,_,_,_,_,_,_,C,_,_,_],
[_,_,_,_,_,_,_,_,O,O,W,W,W,W,W,W,W,W,W,W,W,W,W,O,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_,_,C,_,_,_],
[_,_,_,_,_,_,_,_,_,_,O,O,O,O,O,O,O,O,O,O,O,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,C,_,_,_],
[D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D],
[_,_,I,I,I,I,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,J,J,J,J,J,J,_,_,_,_],
[_,_,I,B,B,B,B,B,B,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,H,H,C,H,H,H,J,_,_,_,_],
[_,_,I,B,B,B,B,B,B,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,H,H,C,H,H,H,J,_,_,_,_],
[_,_,I,I,I,I,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,J,J,J,J,J,J,_,_,_,_],
[_,_,_,_,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,_,_,_,_,_,_,_],
[_,_,_,I,I,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,J,J,_,_,_,_,_,_],
    ];
    return createPixelGrid(map, 3);
}

function getLobsterPixelArt() {
    var R = '#ff8c00'; // orange body
    var D = '#cc5500'; // dark orange
    var E = '#1a1a2e'; // eye
    var C = '#ff6600'; // claw
    var _ = null;
    var map = [
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,C,_,_,_,_,_,_,_,_,_,_,C,_,_],
        [_,C,C,_,_,_,_,_,_,_,_,_,_,C,C,_],
        [_,C,_,_,_,_,R,R,R,R,_,_,_,_,C,_],
        [_,_,_,_,_,R,R,R,R,R,R,_,_,_,_,_],
        [_,_,_,_,R,R,E,R,R,E,R,R,_,_,_,_],
        [_,_,_,_,R,R,R,R,R,R,R,R,_,_,_,_],
        [_,_,_,_,_,D,R,R,R,R,D,_,_,_,_,_],
        [_,_,_,_,_,D,R,R,R,R,D,_,_,_,_,_],
        [_,_,_,_,_,D,R,R,R,R,D,_,_,_,_,_],
        [_,_,_,_,_,_,D,R,R,D,_,_,_,_,_,_],
        [_,_,_,_,_,R,_,D,D,_,R,_,_,_,_,_],
        [_,_,_,_,R,_,_,D,D,_,_,R,_,_,_,_],
        [_,_,_,R,_,_,_,_,_,_,_,_,R,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_]
    ];
    return createPixelGrid(map, 4);
}

// === SPLASH SCREEN ===
function showSplash(mode) {
    var overlay = document.createElement('div');
    overlay.className = 'splash-overlay';

    var artContainer = document.createElement('div');
    artContainer.className = 'pixel-art-container';
    if (mode === 'arbitrout') {
        artContainer.appendChild(getTroutPixelArt());
    } else {
        var img = document.createElement('img');
        img.src = '/static/img/lobster.svg';
        img.style.width = '96px';
        img.style.height = '96px';
        artContainer.appendChild(img);
    }
    overlay.appendChild(artContainer);

    var title = document.createElement('div');
    title.className = 'splash-title ' + (mode === 'arbitrout' ? 'teal' : 'orange');
    title.textContent = mode === 'arbitrout' ? 'ARBITROUT' : 'LOBSTERMINAL';
    overlay.appendChild(title);

    document.body.appendChild(overlay);

    setTimeout(function() {
        overlay.classList.add('fade-out');
        setTimeout(function() {
            if (overlay.parentNode) {
                overlay.parentNode.removeChild(overlay);
            }
        }, 500);
    }, 1200);
}

// === TAB SWITCHING ===
function switchMode(mode, isInit) {
    if (mode === arbMode && !isInit) return;
    arbMode = mode;
    localStorage.setItem('arbMode', arbMode); // Save current mode

    if (!isInit) showSplash(mode);

    var lobster = document.getElementById('lobsterminal-container');
    var arb = document.getElementById('arbitrout-container');
    var tabLob = document.getElementById('tab-lobsterminal');
    var tabArb = document.getElementById('tab-arbitrout');

    if (mode === 'arbitrout') {
        if (lobster) lobster.style.display = 'none';
        if (arb) arb.classList.add('active');
        if (tabLob) { tabLob.classList.remove('active-lobster'); }
        if (tabArb) { tabArb.classList.add('active-trout'); }
        startArbPolling();
    } else {
        if (lobster) lobster.style.display = '';
        if (arb) arb.classList.remove('active');
        if (tabLob) { tabLob.classList.add('active-lobster'); }
        if (tabArb) { tabArb.classList.remove('active-trout'); }
        stopArbPolling();
    }
}

// === POLLING + AUTO-SCAN ===
function startArbPolling() {
    triggerScan();
    loadOpportunities();
    loadSavedMarkets();
    loadHedgePackages(); // NEW: Load hedge packages
    loadNews(); // NEW: Load news
    loadInsiderSignals(); // NEW: Load insider signals
    loadWhaleSignals(); // Load Kalshi whale signals
    loadForexRates(); // NEW: Load forex rates

    arbPollingInterval = setInterval(loadOpportunities, 15000);
    arbScanInterval = setInterval(triggerScan, 60000);
    arbHedgePollingInterval = setInterval(loadHedgePackages, 30000); // NEW: Poll hedge packages every 30s
    arbNewsPollingInterval = setInterval(loadNews, 60000); // NEW: Poll news every 60s
    arbInsiderPollingInterval = setInterval(loadInsiderSignals, 30000); // NEW: Poll insider signals every 30s
    arbWhalePollingInterval = setInterval(loadWhaleSignals, 30000); // Poll Kalshi whale signals every 30s
    forexPollingInterval = setInterval(loadForexRates, 60000); // NEW: Poll forex rates every 60s
    connectArbWs();
}

function stopArbPolling() {
    if (arbPollingInterval) {
        clearInterval(arbPollingInterval);
        arbPollingInterval = null;
    }
    if (arbScanInterval) {
        clearInterval(arbScanInterval);
        arbScanInterval = null;
    }
    if (arbHedgePollingInterval) { // NEW: Clear hedge polling interval
        clearInterval(arbHedgePollingInterval);
        arbHedgePollingInterval = null;
    }
    if (arbNewsPollingInterval) { // NEW: Clear news polling interval
        clearInterval(arbNewsPollingInterval);
        arbNewsPollingInterval = null;
    }
    if (arbInsiderPollingInterval) { // NEW: Clear insider polling interval
        clearInterval(arbInsiderPollingInterval);
        arbInsiderPollingInterval = null;
    }
    if (arbWhalePollingInterval) { // Clear Kalshi whale polling interval
        clearInterval(arbWhalePollingInterval);
        arbWhalePollingInterval = null;
    }
    if (forexPollingInterval) { // NEW: Clear forex polling interval
        clearInterval(forexPollingInterval);
        forexPollingInterval = null;
    }
    if (arbWs) {
        arbWs.close();
        arbWs = null;
    }
}

function triggerScan() {
    fetch('/api/arbitrage/scan', {method: 'POST'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var countEl = document.getElementById('opp-count');
            if (countEl) {
                countEl.textContent = data.opportunities_count || 0;
            }
            loadOpportunities();
        })
        .catch(function(err) { console.error('Scan error:', err); });
}

// === WEBSOCKET ===
function connectArbWs() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    arbWs = new WebSocket(proto + '//' + location.host + '/api/arbitrage/ws');

    arbWs.onmessage = function(e) {
        var data = JSON.parse(e.data);
        if (data.type === 'init') {
            // Initial state on connect
            if (data.opportunities) renderOpportunities(data.opportunities);
            if (data.feed) data.feed.forEach(function(item) { addFeedItem(item); });
        } else if (data.type === 'scan_result') {
            if (data.opportunities) renderOpportunities(data.opportunities);
            if (data.feed) data.feed.forEach(function(item) { addFeedItem(item); });
        } else if (data.type === 'opportunities') {
            renderOpportunities(data.data);
        } else if (data.type === 'feed') {
            addFeedItem(data.data);
        } else if (data.type === 'news_alert') { // NEW
            addFeedItem({
                type: 'news_alert', // NEW: Type for news alerts
                time: new Date().toLocaleTimeString(),
                platform: 'NEWS ALERT',
                title: data.data.headline,
                direction: 'alert'
            });
            loadNews(); // Reload news to show new headlines
        }
    };

    arbWs.onclose = function() {
        if (arbMode === 'arbitrout') {
            setTimeout(connectArbWs, 3000);
        }
    };
}

// === OPPORTUNITIES ===
function loadOpportunities() {
    fetch('/api/arbitrage/opportunities')
        .then(function(r) { return r.json(); })
        .then(function(data) { renderOpportunities(data); })
        .catch(function(err) { console.error('Arb fetch error:', err); });
}

var currentSort = localStorage.getItem('arbCurrentSort') || 'profit-high';

function sortOpportunities(opps) {
    var sorted = opps.slice();
    switch (currentSort) {
        case 'profit-high': sorted.sort(function(a, b) { return (b.net_profit_pct || b.profit_pct || 0) - (a.net_profit_pct || a.profit_pct || 0); }); break;
        case 'profit-low': sorted.sort(function(a, b) { return (a.net_profit_pct || a.profit_pct || 0) - (b.net_profit_pct || b.profit_pct || 0); }); break;
        case 'platform-az': sorted.sort(function(a, b) { return (a.buy_yes_platform || '').localeCompare(b.buy_yes_platform || ''); }); break;
        case 'newest': sorted.sort(function(a, b) { return (b.last_updated || 0) - (a.last_updated || 0); }); break;
        case 'volume-high': sorted.sort(function(a, b) { return (b.volume || 0) - (a.volume || 0); }); break;
    }
    return sorted;
}

function createSortDropdown() {
    var select = document.createElement('select');
    select.id = 'arb-sort';
    select.style.cssText = 'background:#1a1a2e;color:#0f0;border:1px solid #333;padding:4px 8px;font-family:monospace;font-size:12px;margin-bottom:8px;width:100%;';
    var options = [
        ['profit-high', 'Profit: High → Low'],
        ['profit-low', 'Profit: Low → High'],
        ['platform-az', 'Platform: A → Z'],
        ['newest', 'Newest First'],
        ['volume-high', 'Volume: High → Low']
    ];
    options.forEach(function(opt) {
        var o = document.createElement('option');
        o.value = opt[0]; o.textContent = opt[1];
        if (opt[0] === currentSort) o.selected = true;
        select.appendChild(o);
    });
    select.addEventListener('change', function() {
        currentSort = this.value;
        localStorage.setItem('arbCurrentSort', this.value); // Save sort preference
        loadOpportunities();
    });
    return select;
}

function renderOpportunities(opps) {
    var container = document.getElementById('opp-list');
    if (!container) return;

    lastLoadedOpportunities = opps; // Store for potential re-selection

    // Clear existing
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    // Add sort dropdown
    container.appendChild(createSortDropdown());

    // NEW: Add CSV Export Button
    var exportBtn = document.createElement('button');
    exportBtn.textContent = 'EXPORT CSV';
    exportBtn.style.cssText = 'background:var(--arb-card); color:var(--arb-accent); border:1px solid var(--arb-border); padding:4px 8px; font-family:monospace; font-size:12px; margin-bottom:8px; width:100%; cursor:pointer;';
    exportBtn.addEventListener('click', function() {
        window.open('/api/arbitrage/opportunities/export/csv', '_blank');
    });
    container.appendChild(exportBtn);


    // Apply sort
    opps = sortOpportunities(opps);

    if (!opps || opps.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Scanning for opportunities...';
        container.appendChild(empty);
        return;
    }

    opps.forEach(function(opp) {
        var row = document.createElement('div');
        row.className = 'opp-row';
        row.dataset.matchId = opp.match_id || (opp.matched_event ? opp.matched_event.match_id : null); // Add match ID for selection
        if (row.dataset.matchId === lastSelectedOppId) {
            row.classList.add('selected');
        }
        row.addEventListener('click', function() { showEventDetail(opp); });

        var titleEl = document.createElement('div');
        titleEl.className = 'opp-title';
        titleEl.textContent = opp.canonical_title || opp.matched_event.canonical_title;
        row.appendChild(titleEl);

        var spreadEl = document.createElement('div');
        spreadEl.className = 'opp-spread positive';
        spreadEl.style.display = 'flex';
        spreadEl.style.alignItems = 'center';
        spreadEl.style.gap = '4px';
        // Show net profit (after fees) as primary, gross in parentheses
        var netPct = opp.net_profit_pct != null ? opp.net_profit_pct : (opp.profit_pct || opp.spread * 100);
        var grossPct = opp.profit_pct || opp.spread * 100;
        var pctText = '+' + netPct.toFixed(1) + '%';
        if (Math.abs(netPct - grossPct) > 0.5) {
            pctText += ' (gross ' + grossPct.toFixed(1) + '%)';
        }
        if (opp.is_synthetic) {
            spreadEl.style.color = '#e040fb';
            pctText += ' \u2726';  // synthetic indicator
        }
        var pctSpan = document.createElement('span');
        pctSpan.textContent = pctText;
        spreadEl.appendChild(pctSpan);
        // Confidence badge
        if (opp.confidence && opp.confidence !== 'high') {
            var badge = document.createElement('span');
            badge.style.cssText = 'font-size:9px;padding:1px 4px;border-radius:3px;font-weight:700;';
            if (opp.confidence === 'very_low') {
                badge.style.background = '#d32f2f';
                badge.style.color = '#fff';
                badge.textContent = 'LIKELY FALSE';
            } else if (opp.confidence === 'low') {
                badge.style.background = '#f57c00';
                badge.style.color = '#fff';
                badge.textContent = 'LOW CONF';
            } else {
                badge.style.background = '#ffd54f';
                badge.style.color = '#333';
                badge.textContent = 'MED';
            }
            spreadEl.appendChild(badge);
        }
        row.appendChild(spreadEl);

        // Quick buy signal line
        var signalEl = document.createElement('div');
        signalEl.style.cssText = 'font-size:10px;font-family:monospace;padding:2px 0;display:flex;gap:8px;';
        var yesTag = document.createElement('span');
        yesTag.style.color = 'var(--arb-green)';
        yesTag.textContent = 'YES ' + (opp.buy_yes_price * 100).toFixed(0) + '\u00A2 ' + (opp.buy_yes_platform || '');
        signalEl.appendChild(yesTag);
        var noTag = document.createElement('span');
        noTag.style.color = '#ff9800';
        noTag.textContent = 'NO ' + (opp.buy_no_price * 100).toFixed(0) + '\u00A2 ' + (opp.buy_no_platform || '');
        signalEl.appendChild(noTag);
        var volTag = document.createElement('span');
        volTag.style.color = 'var(--arb-muted)';
        volTag.textContent = '$' + ((opp.combined_volume || 0) / 1000).toFixed(0) + 'K vol';
        signalEl.appendChild(volTag);
        row.appendChild(signalEl);

        container.appendChild(row);
    });

    restoreSelectedOpportunityView(); // Attempt to restore the detailed view
}

// === EVENT DETAIL ===
async function showEventDetail(opp) { // NEW: Make function async
    selectedOpp = opp;
    lastSelectedOppId = opp.match_id || (opp.matched_event ? opp.matched_event.match_id : null);
    localStorage.setItem('arbSelectedOppId', lastSelectedOppId); // Save selected opportunity ID

    // Update selected class on opportunity rows
    document.querySelectorAll('.opp-row').forEach(function(rowElement) {
        rowElement.classList.remove('selected');
        if (rowElement.dataset.matchId === lastSelectedOppId) {
            rowElement.classList.add('selected');
        }
    });

    var container = document.getElementById('event-detail');
    if (!container) return;

    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    var event = opp.matched_event || opp;
    var markets = event.markets || [];

    var headerEl = document.createElement('div');
    headerEl.style.padding = '8px';
    headerEl.style.borderBottom = '1px solid var(--arb-border)';

    var titleEl = document.createElement('div');
    titleEl.style.fontFamily = "'Courier New', monospace";
    titleEl.style.fontSize = '13px';
    titleEl.style.fontWeight = '700';
    titleEl.style.color = 'var(--arb-text)';
    titleEl.textContent = event.canonical_title || opp.canonical_title || '';
    headerEl.appendChild(titleEl);

    var metaEl = document.createElement('div');
    metaEl.style.fontFamily = "'Courier New', monospace";
    metaEl.style.fontSize = '10px';
    metaEl.style.color = 'var(--arb-muted)';
    metaEl.style.marginTop = '4px';
    metaEl.textContent = (event.category || '') + ' | Expires: ' + (event.expiry || 'ongoing');
    headerEl.appendChild(metaEl);

    container.appendChild(headerEl);

    // Column headers
    var colHeader = document.createElement('div');
    colHeader.className = 'platform-row';
    colHeader.style.color = 'var(--arb-muted)';
    colHeader.style.fontSize = '10px';
    var cols = ['PLATFORM', 'YES %', 'NO %', 'ACTION', ''];
    cols.forEach(function(txt) {
        var c = document.createElement('div');
        c.textContent = txt;
        colHeader.appendChild(c);
    });
    container.appendChild(colHeader);

    var buyYesPlatform = opp.buy_yes_platform || '';
    var buyNoPlatform = opp.buy_no_platform || '';
    var buyYesEventId = opp.buy_yes_event_id || '';
    var buyNoEventId = opp.buy_no_event_id || '';

    markets.forEach(function(m) {
        var row = document.createElement('div');
        row.className = 'platform-row';

        var nameEl = document.createElement('div');
        nameEl.className = 'platform-name';
        nameEl.textContent = m.platform;
        row.appendChild(nameEl);

        // Match by event_id (unique) instead of platform name (can have duplicates)
        var isYesMatch = buyYesEventId ? (m.event_id === buyYesEventId) : (m.platform === buyYesPlatform);
        var isNoMatch = buyNoEventId ? (m.event_id === buyNoEventId) : (m.platform === buyNoPlatform);

        var yesEl = document.createElement('div');
        yesEl.className = 'price-yes';
        yesEl.textContent = (m.yes_price * 100).toFixed(1) + '\u00A2';
        if (isYesMatch) {
            yesEl.style.fontWeight = '700';
            yesEl.classList.add('price-best');
        }
        row.appendChild(yesEl);

        var noEl = document.createElement('div');
        noEl.className = 'price-no';
        noEl.textContent = (m.no_price * 100).toFixed(1) + '\u00A2';
        if (isNoMatch) {
            noEl.style.fontWeight = '700';
            noEl.classList.add('price-best');
        }
        row.appendChild(noEl);

        // Action tag — match by event_id to avoid all-same-platform markets getting same action
        var actionEl = document.createElement('div');
        actionEl.style.cssText = 'font-size:9px;font-weight:700;';
        if (isYesMatch) {
            actionEl.style.color = 'var(--arb-green)';
            actionEl.textContent = 'BUY YES';
        } else if (isNoMatch) {
            actionEl.style.color = '#ff9800';
            actionEl.textContent = 'BUY NO';
        }
        row.appendChild(actionEl);

        var linkEl = document.createElement('a');
        linkEl.href = m.url || '#';
        linkEl.target = '_blank';
        linkEl.rel = 'noopener';
        linkEl.textContent = '\u2197';
        linkEl.style.color = 'var(--arb-accent)';
        linkEl.style.textDecoration = 'none';
        row.appendChild(linkEl);

        container.appendChild(row);
    });

    // Arbitrage trade instructions
    if (opp.buy_yes_price !== undefined && opp.buy_no_price !== undefined) {
        var yesAlloc = parseFloat(opp.yes_allocation_pct) || 50.0;
        var noAlloc = parseFloat(opp.no_allocation_pct) || 50.0;
        var profitPct = opp.profit_pct || (opp.spread * 100);
        var yesCost = opp.buy_yes_price;
        var noCost = opp.buy_no_price;

        var tradeEl = document.createElement('div');
        tradeEl.style.cssText = 'padding:10px 8px;border-top:2px solid var(--arb-accent);font-family:monospace;font-size:11px;background:rgba(0,200,200,0.05);';

        var tradeTitle = document.createElement('div');
        tradeTitle.style.cssText = 'color:var(--arb-accent);font-weight:700;font-size:12px;margin-bottom:6px;';
        tradeTitle.textContent = 'HOW TO TRADE';
        tradeEl.appendChild(tradeTitle);

        // Step 1: BUY YES
        var step1 = document.createElement('div');
        step1.style.cssText = 'padding:4px 0;';
        var s1a = document.createElement('span');
        s1a.style.cssText = 'color:var(--arb-green);font-weight:700;';
        s1a.textContent = 'BUY YES';
        step1.appendChild(s1a);
        step1.appendChild(document.createTextNode(' on '));
        var s1b = document.createElement('span');
        s1b.style.cssText = 'color:var(--arb-text);font-weight:700;';
        s1b.textContent = (opp.buy_yes_platform || '?').toUpperCase();
        step1.appendChild(s1b);
        step1.appendChild(document.createTextNode(' @ ' + (yesCost * 100).toFixed(1) + '\u00A2 (' + yesAlloc.toFixed(1) + '% of capital)'));
        tradeEl.appendChild(step1);

        // Step 2: BUY NO
        var step2 = document.createElement('div');
        step2.style.cssText = 'padding:4px 0;';
        var s2a = document.createElement('span');
        s2a.style.cssText = 'color:#ff9800;font-weight:700;';
        s2a.textContent = 'BUY NO';
        step2.appendChild(s2a);
        step2.appendChild(document.createTextNode(' on '));
        var s2b = document.createElement('span');
        s2b.style.cssText = 'color:var(--arb-text);font-weight:700;';
        s2b.textContent = (opp.buy_no_platform || '?').toUpperCase();
        step2.appendChild(s2b);
        step2.appendChild(document.createTextNode(' @ ' + (noCost * 100).toFixed(1) + '\u00A2 (' + noAlloc.toFixed(1) + '% of capital)'));
        tradeEl.appendChild(step2);

        var isSynthetic = opp.is_synthetic || false;
        var synthInfo = opp.synthetic_info || {};

        // Profit header — show net (after fees) as primary
        var netPctDetail = opp.net_profit_pct != null ? opp.net_profit_pct : profitPct;
        var profitLine = document.createElement('div');
        profitLine.style.cssText = 'font-weight:700;margin-top:6px;font-size:12px;';
        if (isSynthetic) {
            profitLine.style.color = '#e040fb';
            profitLine.textContent = 'SYNTHETIC DERIVATIVE: +' + netPctDetail.toFixed(1) + '% net (wins 2/3 scenarios)';
        } else {
            profitLine.style.color = 'var(--arb-green)';
            profitLine.textContent = 'GUARANTEED PROFIT: ' + netPctDetail.toFixed(1) + '% (after fees)';
        }
        tradeEl.appendChild(profitLine);

        // Fee breakdown line
        if (Math.abs(netPctDetail - profitPct) > 0.5) {
            var feeLine = document.createElement('div');
            feeLine.style.cssText = 'font-size:10px;color:var(--arb-muted);margin-top:2px;';
            feeLine.textContent = 'Gross spread: ' + profitPct.toFixed(1) + '% | Fee drag: -' + (profitPct - netPctDetail).toFixed(1) + '%';
            tradeEl.appendChild(feeLine);
        }

        // Confidence warning
        if (opp.confidence && opp.confidence !== 'high') {
            var warnEl = document.createElement('div');
            warnEl.style.cssText = 'margin-top:4px;padding:4px 6px;border-radius:3px;font-size:10px;font-weight:700;';
            if (opp.confidence === 'very_low') {
                warnEl.style.background = 'rgba(211,47,47,0.15)';
                warnEl.style.color = '#ef5350';
                warnEl.textContent = '\u26A0 LIKELY FALSE MATCH \u2014 platforms disagree by >50% on implied probability. Verify markets are the same event before trading.';
            } else if (opp.confidence === 'low') {
                warnEl.style.background = 'rgba(245,124,0,0.15)';
                warnEl.style.color = '#ff9800';
                warnEl.textContent = '\u26A0 LOW CONFIDENCE \u2014 large price discrepancy suggests possible market mismatch or stale data.';
            } else {
                warnEl.style.background = 'rgba(255,213,79,0.1)';
                warnEl.style.color = '#ffd54f';
                warnEl.textContent = '\u26A0 MEDIUM CONFIDENCE \u2014 verify liquidity and market matching before trading.';
            }
            tradeEl.appendChild(warnEl);
        }

        // Synthetic scenario breakdown
        if (isSynthetic && synthInfo.scenarios) {
            var scenDiv = document.createElement('div');
            scenDiv.style.cssText = 'margin-top:6px;padding:6px;background:rgba(224,64,251,0.08);border:1px solid rgba(224,64,251,0.2);border-radius:3px;font-size:10px;';
            var scenTitle = document.createElement('div');
            scenTitle.style.cssText = 'color:#e040fb;font-weight:700;margin-bottom:4px;';
            scenTitle.textContent = 'SCENARIO ANALYSIS';
            scenDiv.appendChild(scenTitle);

            var scenarios = synthInfo.scenarios;
            // Iterate all scenario keys dynamically (handles crypto, threshold, range types)
            Object.keys(scenarios).forEach(function(key) {
                var s = scenarios[key];
                if (!s) return;
                var line = document.createElement('div');
                line.style.cssText = 'padding:2px 0;color:var(--arb-text);';
                var icon = s.net > 0 ? '\u2705' : '\u274C';
                var color = s.net > 0 ? 'var(--arb-green)' : '#ff5252';
                var iconSpan = document.createElement('span');
                iconSpan.textContent = icon + ' ' + s.condition + ': ';
                line.appendChild(iconSpan);
                var pctSpan = document.createElement('span');
                pctSpan.style.cssText = 'color:' + color + ';font-weight:700;';
                pctSpan.textContent = (s.net > 0 ? '+' : '') + (s.return_pct).toFixed(1) + '%';
                line.appendChild(pctSpan);
                scenDiv.appendChild(line);
            });

            if (synthInfo.high_strike && synthInfo.low_strike) {
                var range = document.createElement('div');
                range.style.cssText = 'margin-top:4px;color:var(--arb-muted);font-style:italic;';
                var isCrypto = synthInfo.type === 'range_synthetic' || synthInfo.type === 'range_vs_directional';
                var prefix = isCrypto ? '$' : '';
                range.textContent = 'Thresholds: ' + prefix + synthInfo.low_strike.toLocaleString() + ' - ' + prefix + synthInfo.high_strike.toLocaleString();
                scenDiv.appendChild(range);
            }
            tradeEl.appendChild(scenDiv);
        }

        // Dollar example
        var exLine = document.createElement('div');
        exLine.style.cssText = 'color:var(--arb-muted);margin-top:4px;font-size:10px;';
        var yesSpend = (100 * yesAlloc / 100);
        var noSpend = (100 * noAlloc / 100);
        var netProfit = (100 * netPctDetail / 100);
        exLine.textContent = '$100 invested = $' + yesSpend.toFixed(0) + ' YES + $' + noSpend.toFixed(0) + ' NO = $' + netProfit.toFixed(2) + ' net' + (isSynthetic ? ' expected' : ' profit');
        tradeEl.appendChild(exLine);

        // Cost per $1 payout
        var costLine = document.createElement('div');
        costLine.style.cssText = 'color:var(--arb-muted);margin-top:2px;font-size:10px;';
        costLine.textContent = 'Cost: ' + ((yesCost + noCost) * 100).toFixed(1) + '\u00A2 per $1 payout (' + (100 - (yesCost + noCost) * 100).toFixed(1) + '\u00A2 spread)';
        tradeEl.appendChild(costLine);

        container.appendChild(tradeEl);
    }

    // Save button
    var saveBtn = document.createElement('button');
    saveBtn.style.cssText = 'margin:8px;padding:6px 12px;background:var(--arb-accent);color:var(--arb-bg);border:none;border-radius:3px;cursor:pointer;font-family:monospace;font-size:11px;font-weight:700;';
    saveBtn.textContent = 'BOOKMARK';
    saveBtn.addEventListener('click', function() {
        saveMarket(event.match_id || '', event.canonical_title || opp.canonical_title || '', event.category || '');
    });
    container.appendChild(saveBtn);

    // NEW: Display Historical Profit for this opportunity
    if (lastSelectedOppId) {
        try {
            const response = await fetch('/api/arbitrage/history/' + encodeURIComponent(lastSelectedOppId));
            const historyData = await response.json();
            if (historyData && historyData.length > 0) {
                var historyDiv = document.createElement('div');
                historyDiv.style.cssText = 'padding:10px 8px;border-top:1px solid var(--arb-border);font-family:monospace;font-size:11px;margin-top:8px;';
                var historyTitle = document.createElement('div');
                historyTitle.style.cssText = 'color:var(--arb-accent);font-weight:700;font-size:12px;margin-bottom:6px;';
                historyTitle.textContent = 'HISTORICAL PROFIT (%)';
                historyDiv.appendChild(historyTitle);

                historyData.forEach(entry => {
                    var historyEntry = document.createElement('div');
                    historyEntry.style.cssText = 'padding:2px 0;display:flex;justify-content:space-between;';
                    var timestamp = new Date(entry.timestamp * 1000).toLocaleString();
                    var profit = entry.net_profit_pct.toFixed(1) + '%';
                    historyEntry.innerHTML = `<span>${timestamp}</span> <span style="color: ${entry.net_profit_pct > 0 ? 'var(--arb-green)' : 'var(--arb-red)'}; font-weight:700;">${profit}</span>`;
                    historyDiv.appendChild(historyEntry);
                });
                container.appendChild(historyDiv);
            }
        } catch (error) {
            console.error('Failed to load historical opportunity data:', error);
        }
    }
}

// Function to restore the selected opportunity's detailed view
function restoreSelectedOpportunityView() {
    var savedOppId = localStorage.getItem('arbSelectedOppId');
    if (savedOppId && lastLoadedOpportunities.length > 0) {
        var oppToSelect = lastLoadedOpportunities.find(function(opp) {
            return (opp.match_id || (opp.matched_event ? opp.matched_event.match_id : null)) === savedOppId;
        });
        if (oppToSelect) {
            showEventDetail(oppToSelect);
        }
    }
}


// === FEED ===
function addFeedItem(item) {
    feedItems.unshift(item);
    if (feedItems.length > 100) feedItems = feedItems.slice(0, 100);
    renderFeed();
}

function renderFeed() {
    var container = document.getElementById('feed-list');
    if (!container) return;

    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (feedItems.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Waiting for price updates...';
        container.appendChild(empty);
        return;
    }

    feedItems.slice(0, 50).forEach(function(item) {
        var el = document.createElement('div');
        el.className = 'feed-item';

        var time = document.createElement('span');
        time.className = 'feed-time';
        time.textContent = (item.time || new Date(item.timestamp * 1000).toLocaleTimeString()) + ' ';
        el.appendChild(time);

        var plat = document.createElement('span');
        plat.className = 'feed-platform';
        plat.textContent = '[' + (item.platform || '?') + '] ';
        el.appendChild(plat);

        var text = document.createElement('span');
        text.textContent = (item.title || '') + ' ';
        el.appendChild(text);

        // NEW: Handle different feed item types
        if (item.type === 'opportunity_alert') {
            el.style.backgroundColor = 'rgba(0, 229, 204, 0.08)'; // Highlight alerts
            el.style.borderLeft = '3px solid var(--arb-accent)';
            el.style.paddingLeft = '5px';
            text.style.color = 'var(--arb-accent)';
            text.style.fontWeight = '700';
            if (item.match_id) {
                el.style.cursor = 'pointer';
                el.title = 'View opportunity details';
                el.addEventListener('click', function() {
                    var opp = lastLoadedOpportunities.find(o => (o.match_id || (o.matched_event ? o.matched_event.match_id : null)) === item.match_id);
                    if (opp) showEventDetail(opp);
                });
            }
        } else if (item.type === 'price_change' && item.change) {
            var arrow = document.createElement('span');
            arrow.className = item.change > 0 ? 'feed-price-up' : 'feed-price-down';
            arrow.textContent = (item.change > 0 ? '\u25B2' : '\u25BC') + ' ' + (item.yes_price * 100).toFixed(1) + '\u00A2';
            el.appendChild(arrow);
        } else if (item.type === 'news_alert') {
            el.style.backgroundColor = 'rgba(255, 140, 0, 0.08)';
            el.style.borderLeft = '3px solid #ffa726';
            el.style.paddingLeft = '5px';
            text.style.color = '#ffa726';
            text.style.fontWeight = '700';
        }

        container.appendChild(el);
    });
}

// === STACKED PANELS (NEWS, SAVED, HEDGE, INSIDER, WHALE, FOREX) ===
// This function renders all three sections in the bottom-right panel
function renderAllStackedPanels() {
    // Target the bottom-right arb-panel, assuming arbitrout-container is a 2x2 grid
    var stackedPanelContainer = document.querySelector('.arbitrout-container > .arb-panel:nth-child(4)');

    // If the element doesn't have an ID, give it one for easier access in CSS/JS
    if (stackedPanelContainer && !stackedPanelContainer.id) {
        stackedPanelContainer.id = 'arb-bottom-right-panel';
    }

    if (!stackedPanelContainer) {
        console.error("Could not find the target container for stacked panels (bottom-right arb-panel).");
        return;
    }

    // Clear existing content in the panel body
    var panelBody = stackedPanelContainer.querySelector('.arb-panel-body');
    if (!panelBody) {
        panelBody = document.createElement('div');
        panelBody.className = 'arb-panel-body';
        stackedPanelContainer.appendChild(panelBody);
    } else {
        while (panelBody.firstChild) {
            panelBody.removeChild(panelBody.firstChild);
        }
    }

    // --- Render Forex Rates Section --- (NEW)
    var forexHeader = document.createElement('div');
    forexHeader.className = 'arb-section-header';
    forexHeader.textContent = 'FOREX RATES';
    panelBody.appendChild(forexHeader);
    var forexList = document.createElement('div');
    forexList.id = 'forex-rates-list';
    forexList.className = 'arb-section-content';
    renderForexRatesContent(forexList, currentForexRates);
    panelBody.appendChild(forexList);

    // --- Render Insider Signals Section ---
    var insiderHeader = document.createElement('div');
    insiderHeader.className = 'arb-section-header';
    insiderHeader.textContent = 'INSIDER SIGNALS';
    panelBody.appendChild(insiderHeader);
    var insiderList = document.createElement('div');
    insiderList.id = 'insider-signals-list';
    insiderList.className = 'arb-section-content';
    renderInsiderSignalsContent(insiderList, insiderSignals);
    panelBody.appendChild(insiderList);

    // --- Render Kalshi Whale Signals Section ---
    var whaleHeader = document.createElement('div');
    whaleHeader.className = 'arb-section-header';
    whaleHeader.textContent = 'KALSHI WHALE SIGNALS';
    panelBody.appendChild(whaleHeader);
    var whaleList = document.createElement('div');
    whaleList.id = 'whale-signals-list';
    whaleList.className = 'arb-section-content';
    renderWhaleSignalsContent(whaleList, whaleSignals);
    panelBody.appendChild(whaleList);

    // --- Render News Scanner Section ---
    var newsHeader = document.createElement('div');
    newsHeader.className = 'arb-section-header';
    newsHeader.textContent = 'NEWS SCANNER';
    panelBody.appendChild(newsHeader);
    var newsList = document.createElement('div');
    newsList.id = 'news-list';
    newsList.className = 'arb-section-content'; // Use a specific class for content areas
    renderNewsContent(newsList, newsItems);
    panelBody.appendChild(newsList);

    // --- Render Saved Markets Section ---
    var savedHeader = document.createElement('div');
    savedHeader.className = 'arb-section-header';
    savedHeader.textContent = 'BOOKMARKED MARKETS';
    panelBody.appendChild(savedHeader);
    var savedList = document.createElement('div');
    savedList.id = 'saved-list';
    savedList.className = 'arb-section-content';
    renderSavedContent(savedList, savedMarkets);
    panelBody.appendChild(savedList);

    // --- Render Hedge Packages Section ---
    var hedgeHeader = document.createElement('div');
    hedgeHeader.className = 'arb-section-header';
    hedgeHeader.textContent = 'HEDGE PACKAGES';
    panelBody.appendChild(hedgeHeader);
    var hedgeList = document.createElement('div');
    hedgeList.id = 'hedge-packages-list';
    hedgeList.className = 'arb-section-content';
    renderHedgePackagesContent(hedgeList, hedgePackages);
    panelBody.appendChild(hedgeList);
}


// === SAVED MARKETS ===
function loadSavedMarkets() {
    fetch('/api/arbitrage/saved')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            savedMarkets = data; // Store data globally
            renderAllStackedPanels(); // Render all sections in the stacked container
        })
        .catch(function() {});
}

// Refactored to populate a given container
function renderSavedContent(container, items) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!items || items.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'No bookmarked markets';
        container.appendChild(empty);
        return;
    }

    items.forEach(function(item) {
        var row = document.createElement('div');
        row.className = 'saved-row';

        var titleEl = document.createElement('div');
        titleEl.className = 'saved-title';
        titleEl.textContent = item.canonical_title || item.title || item;
        row.appendChild(titleEl);

        var removeBtn = document.createElement('button');
        removeBtn.className = 'saved-remove';
        removeBtn.textContent = '\u00D7';
        removeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            removeSaved(item.match_id || '');
        });
        row.appendChild(removeBtn);

        container.appendChild(row);
    });
}

function saveMarket(matchId, title, category) {
    fetch('/api/arbitrage/saved', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({match_id: matchId, canonical_title: title, category: category})
    }).then(function() { loadSavedMarkets(); });
}

function removeSaved(matchId) {
    fetch('/api/arbitrage/saved/' + encodeURIComponent(matchId), {
        method: 'DELETE'
    }).then(function() { loadSavedMarkets(); });
}


// === HEDGE PACKAGES ===
function loadHedgePackages() {
    fetch('/api/arbitrage/hedge-packages')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            hedgePackages = data;
            renderAllStackedPanels(); // Render all sections in the stacked container
        })
        .catch(function(err) { console.error('Hedge packages fetch error:', err); });
}

// Refactored to populate a given container
function renderHedgePackagesContent(container, packages) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!packages || packages.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Scanning for hedge opportunities...';
        container.appendChild(empty);
        return;
    }

    packages.forEach(function(pkg) {
        var card = document.createElement('div');
        card.className = 'hedge-package-card arb-card'; // Add arb-card for general styling

        var header = document.createElement('div');
        header.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding-bottom:4px;border-bottom:1px solid var(--arb-border);margin-bottom:8px;';

        var title = document.createElement('div');
        title.style.cssText = 'font-weight:700;color:var(--arb-accent);';
        title.textContent = pkg.crypto_symbol + ' vs ' + pkg.pm_platform.toUpperCase() + ' (Strike: $' + pkg.strike_price.toLocaleString() + ')';
        header.appendChild(title);

        var profitIndicator = document.createElement('span');
        profitIndicator.style.cssText = 'font-size:11px;font-weight:700;padding:2px 6px;border-radius:3px;';
        if (pkg.overall_profit_type === 'guaranteed') {
            profitIndicator.textContent = 'GUARANTEED PROFIT';
            profitIndicator.style.backgroundColor = 'rgba(0, 255, 0, 0.1)'; // var(--arb-green-bg)
            profitIndicator.style.color = '#0f0'; // var(--arb-green)
        } else if (pkg.overall_profit_type === 'conditional') {
            profitIndicator.textContent = 'CONDITIONAL PROFIT';
            profitIndicator.style.backgroundColor = 'rgba(255, 255, 0, 0.1)'; // var(--arb-yellow-bg)
            profitIndicator.style.color = '#ff0'; // var(--arb-yellow)
        } else {
            profitIndicator.textContent = 'P&L SCENARIOS';
            profitIndicator.style.backgroundColor = 'rgba(100, 100, 100, 0.1)'; // var(--arb-muted-bg)
            profitIndicator.style.color = '#888'; // var(--arb-muted)
        }
        header.appendChild(profitIndicator);
        card.appendChild(header);

        var metrics = document.createElement('div');
        metrics.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:11px;margin-bottom:8px;';

        var addMetric = function(label, value, color) {
            var item = document.createElement('div');
            item.textContent = label + ': ';
            var valSpan = document.createElement('span');
            valSpan.style.color = color || 'var(--arb-text)';
            valSpan.textContent = value;
            item.appendChild(valSpan);
            metrics.appendChild(item);
        };

        addMetric('Spot Price', '$' + pkg.spot_price.toFixed(2));
        addMetric('PM NO Price', (pkg.pm_buy_no_price * 100).toFixed(1) + '\u00A2', '#ff9800');
        addMetric('Max Profit', '$' + pkg.max_profit.toFixed(2), pkg.max_profit >= 0 ? 'var(--arb-green)' : 'var(--arb-red)');
        addMetric('Max Loss', '$' + pkg.max_loss.toFixed(2), pkg.max_loss <= 0 ? 'var(--arb-red)' : 'var(--arb-green)');
        addMetric('Breakeven', '$' + pkg.breakeven_price.toFixed(2));
        card.appendChild(metrics);

        var scenariosTitle = document.createElement('div');
        scenariosTitle.style.cssText = 'font-size:11px;font-weight:700;margin-top:8px;color:var(--arb-accent);border-top:1px dashed var(--arb-border);padding-top:8px;';
        scenariosTitle.textContent = 'P&L Scenarios:';
        card.appendChild(scenariosTitle);

        var scenariosList = document.createElement('div');
        scenariosList.style.cssText = 'font-size:10px;margin-bottom:8px;';

        for (var key in pkg.scenarios) {
            var s = pkg.scenarios[key];
            var scenarioEl = document.createElement('div');
            scenarioEl.style.padding = '2px 0';
            var icon = s.net_profit > 0 ? '\u2705' : (s.net_profit < 0 ? '\u274C' : '\u2796');
            var color = s.net_profit > 0 ? 'var(--arb-green)' : (s.net_profit < 0 ? 'var(--arb-red)' : 'var(--arb-text)');
            scenarioEl.textContent = icon + ' ' + s.condition + ': ';
            var profitSpan = document.createElement('span');
            profitSpan.style.color = color;
            profitSpan.style.fontWeight = '700';
            profitSpan.textContent = '$' + s.net_profit.toFixed(2) + ' (' + s.return_pct.toFixed(1) + '%)';
            scenarioEl.appendChild(profitSpan);
            scenariosList.appendChild(scenarioEl);
        }
        card.appendChild(scenariosList);

        container.appendChild(card);
    });
}

// === NEWS SCANNER (NEW SECTION) ===
function loadNews() {
    fetch('/api/arbitrage/news')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            newsItems = data || []; // Ensure newsItems is an array
            renderAllStackedPanels(); // Render all sections in the stacked container
        })
        .catch(function(err) { console.error('News fetch error:', err); });
}

function renderNewsContent(container, newsData) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!newsData || newsData.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Scanning for breaking news...';
        container.appendChild(empty);
        return;
    }

    newsData.slice(0, 20).forEach(function(item) { // Limit to 20 recent news items
        var newsEntry = document.createElement('div');
        newsEntry.className = 'news-entry';
        if (item.is_breaking) {
            newsEntry.classList.add('news-breaking');
        }

        var header = document.createElement('div');
        header.className = 'news-header';
        var timeSpan = document.createElement('span');
        timeSpan.className = 'news-time';
        timeSpan.textContent = new Date(item.timestamp).toLocaleTimeString() + ' ';
        header.appendChild(timeSpan);
        var headlineLink = document.createElement('a');
        headlineLink.className = 'news-headline';
        headlineLink.textContent = item.headline;
        headlineLink.href = item.url || '#';
        headlineLink.target = '_blank';
        headlineLink.rel = 'noopener';
        header.appendChild(headlineLink);
        newsEntry.appendChild(header);

        if (item.matched_markets && item.matched_markets.length > 0) {
            var marketsDiv = document.createElement('div');
            marketsDiv.className = 'news-markets';
            var marketPrefix = document.createElement('span');
            marketPrefix.style.color = 'var(--arb-muted)';
            marketPrefix.textContent = 'Markets: ';
            marketsDiv.appendChild(marketPrefix);
            item.matched_markets.forEach(function(market, idx) {
                var marketSpan = document.createElement('span');
                marketSpan.className = 'news-market-tag';
                marketSpan.textContent = market.title;
                if (market.match_id) {
                     marketSpan.style.cursor = 'pointer';
                     marketSpan.title = 'View details';
                     marketSpan.addEventListener('click', function() {
                         var opp = lastLoadedOpportunities.find(o => (o.match_id || (o.matched_event ? o.matched_event.match_id : null)) === market.match_id);
                         if (opp) showEventDetail(opp);
                     });
                }
                marketsDiv.appendChild(marketSpan);
                if (idx < item.matched_markets.length - 1) {
                    marketsDiv.appendChild(document.createTextNode(', '));
                }
            });
            newsEntry.appendChild(marketsDiv);
        }

        if (item.trade_decisions && item.trade_decisions.length > 0) {
            var tradesDiv = document.createElement('div');
            tradesDiv.className = 'news-trades';
            tradesDiv.textContent = 'Trades: ';
            item.trade_decisions.forEach(function(decision, idx) {
                var tradeSpan = document.createElement('span');
                tradeSpan.className = 'news-trade-tag';
                tradeSpan.style.background = 'rgba(0, 230, 118, 0.1)';
                tradeSpan.style.color = 'var(--arb-green)';
                tradeSpan.textContent = decision.type.toUpperCase() + (decision.platform ? ' on ' + decision.platform : '') + (decision.price ? ' @ ' + (decision.price * 100).toFixed(0) + '\u00A2' : '');
                tradesDiv.appendChild(tradeSpan);
                if (idx < item.trade_decisions.length - 1) {
                    tradesDiv.appendChild(document.createTextNode(', '));
                }
            });
            newsEntry.appendChild(tradesDiv);
        }
        container.appendChild(newsEntry);
    });
}

// === INSIDER SIGNALS (NEW SECTION) ===
function loadInsiderSignals() {
    fetch('/api/arbitrage/insider-signals')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            insiderSignals = data || { wallets_count: 0, recent_movements: [], active_signals: [], convergence_alerts: [] };
            renderAllStackedPanels();
        })
        .catch(function(err) { console.error('Insider signals fetch error:', err); });
}

function renderInsiderSignalsContent(container, data) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!data || (data.wallets_count === 0 && data.recent_movements.length === 0 && data.active_signals.length === 0 && data.convergence_alerts.length === 0)) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Scanning for insider activity...';
        container.appendChild(empty);
        return;
    }

    // Tracked Wallets Count
    if (data.wallets_count > 0) {
        var walletsCountEl = document.createElement('div');
        walletsCountEl.className = 'insider-stat-row';
        walletsCountEl.innerHTML = '<span class="insider-label">TRACKED WALLETS:</span> <span class="insider-value">' + data.wallets_count + '</span>';
        container.appendChild(walletsCountEl);
    }

    // Recent Movements
    if (data.recent_movements && data.recent_movements.length > 0) {
        var movementsHeader = document.createElement('div');
        movementsHeader.className = 'insider-subsection-header';
        movementsHeader.textContent = 'RECENT MOVEMENTS';
        container.appendChild(movementsHeader);

        data.recent_movements.slice(0, 10).forEach(function(movement) { // Limit to 10
            var movementEl = document.createElement('div');
            movementEl.className = 'insider-movement-entry';
            
            var timestampSpan = document.createElement('span');
            timestampSpan.className = 'insider-time';
            timestampSpan.textContent = new Date(movement.timestamp).toLocaleTimeString() + ' ';
            movementEl.appendChild(timestampSpan);

            var typeSpan = document.createElement('span');
            typeSpan.className = 'insider-movement-type ' + (movement.type === 'entry' ? 'entry' : 'exit');
            typeSpan.textContent = movement.type.toUpperCase();
            movementEl.appendChild(typeSpan);
            
            movementEl.appendChild(document.createTextNode(' ' + (movement.size_usd ? '$' + movement.size_usd.toFixed(0) : '') + ' on '));

            var marketLink = document.createElement('a');
            marketLink.className = 'insider-market-link';
            marketLink.textContent = movement.market_title || 'Unknown Market';
            marketLink.href = movement.market_url || '#';
            marketLink.target = '_blank';
            marketLink.rel = 'noopener';
            movementEl.appendChild(marketLink);

            movementEl.appendChild(document.createTextNode(' by ' + (movement.wallet_label || movement.wallet_address.substring(0,6) + '...')));
            
            container.appendChild(movementEl);
        });
    }

    // Active Signals
    if (data.active_signals && data.active_signals.length > 0) {
        var signalsHeader = document.createElement('div');
        signalsHeader.className = 'insider-subsection-header';
        signalsHeader.textContent = 'ACTIVE SIGNALS';
        container.appendChild(signalsHeader);

        data.active_signals.slice(0, 5).forEach(function(signal) { // Limit to 5
            var signalEl = document.createElement('div');
            signalEl.className = 'insider-signal-entry';
            
            var type = signal.signal_type || 'UNKNOWN';
            var typeSpan = document.createElement('span');
            typeSpan.className = 'insider-signal-type';
            if (type.includes('BUY')) {
                typeSpan.classList.add('buy-signal');
            } else if (type.includes('SELL')) {
                typeSpan.classList.add('sell-signal');
            }
            typeSpan.textContent = type;
            signalEl.appendChild(typeSpan);

            var marketLink = document.createElement('a');
            marketLink.className = 'insider-market-link';
            marketLink.textContent = signal.market_title || 'Unknown Market';
            marketLink.href = signal.market_url || '#';
            marketLink.target = '_blank';
            marketLink.rel = 'noopener';
            signalEl.appendChild(marketLink);

            var priceSpan = document.createElement('span');
            priceSpan.className = 'insider-signal-price';
            priceSpan.textContent = ' @ ' + (signal.price * 100).toFixed(0) + '\u00A2';
            signalEl.appendChild(priceSpan);

            container.appendChild(signalEl);
        });
    }

    // Convergence Alerts
    if (data.convergence_alerts && data.convergence_alerts.length > 0) {
        var convHeader = document.createElement('div');
        convHeader.className = 'insider-subsection-header';
        convHeader.textContent = 'CONVERGENCE ALERTS';
        container.appendChild(convHeader);

        data.convergence_alerts.slice(0, 3).forEach(function(alert) { // Limit to 3
            var alertEl = document.createElement('div');
            alertEl.className = 'insider-convergence-alert';
            
            var timeSpan = document.createElement('span');
            timeSpan.className = 'insider-time';
            timeSpan.textContent = new Date(alert.timestamp).toLocaleTimeString() + ' ';
            alertEl.appendChild(timeSpan);

            var alertText = document.createElement('span');
            alertText.className = 'insider-alert-text';
            alertText.textContent = alert.message;
            alertEl.appendChild(alertText);

            if (alert.market_url) {
                var marketLink = document.createElement('a');
                marketLink.className = 'insider-market-link';
                marketLink.textContent = ' \u2197'; // External link icon
                marketLink.href = alert.market_url;
                marketLink.target = '_blank';
                marketLink.rel = 'noopener';
                alertEl.appendChild(marketLink);
            }

            container.appendChild(alertEl);
        });
    }
}

// === KALSHI WHALE SIGNALS ===
function loadWhaleSignals() {
    fetch('/api/derivatives/whales')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            whaleSignals = data || { scan_count: 0, active_signals: 0, signals: {} };
            renderAllStackedPanels();
        })
        .catch(function(err) { console.error('Whale signals fetch error:', err); });
}

function renderWhaleSignalsContent(container, data) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    var signals = data.signals || {};
    var signalKeys = Object.keys(signals);

    if (signalKeys.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = data.scan_count > 0 ? 'No active whale signals' : 'Scanning for whale activity...';
        container.appendChild(empty);
        return;
    }

    // Summary stats row
    var statsRow = document.createElement('div');
    statsRow.className = 'whale-stat-row';
    var lbl1 = document.createElement('span');
    lbl1.className = 'whale-label';
    lbl1.textContent = 'ACTIVE SIGNALS:';
    statsRow.appendChild(lbl1);
    var val1 = document.createElement('span');
    val1.className = 'whale-value';
    val1.textContent = ' ' + signalKeys.length;
    statsRow.appendChild(val1);
    var lbl2 = document.createElement('span');
    lbl2.className = 'whale-label';
    lbl2.style.marginLeft = '12px';
    lbl2.textContent = 'SCANS:';
    statsRow.appendChild(lbl2);
    var val2 = document.createElement('span');
    val2.className = 'whale-value';
    val2.textContent = ' ' + (data.scan_count || 0);
    statsRow.appendChild(val2);
    var lbl3 = document.createElement('span');
    lbl3.className = 'whale-label';
    lbl3.style.marginLeft = '12px';
    lbl3.textContent = 'TRACKED:';
    statsRow.appendChild(lbl3);
    var val3 = document.createElement('span');
    val3.className = 'whale-value';
    val3.textContent = ' ' + (data.watched_tickers || 0);
    statsRow.appendChild(val3);
    container.appendChild(statsRow);

    // Sort by signal strength descending
    var sortedKeys = signalKeys.sort(function(a, b) {
        return (signals[b].signal_strength || 0) - (signals[a].signal_strength || 0);
    });

    sortedKeys.slice(0, 10).forEach(function(ticker) {
        var sig = signals[ticker];
        var entry = document.createElement('div');
        entry.className = 'whale-signal-entry';

        // Direction badge
        var dirBadge = document.createElement('span');
        dirBadge.className = 'whale-direction';
        var dir = sig.net_direction || 'NONE';
        if (dir === 'YES') {
            dirBadge.classList.add('dir-yes');
        } else if (dir === 'NO') {
            dirBadge.classList.add('dir-no');
        } else if (dir === 'MIXED') {
            dirBadge.classList.add('dir-mixed');
        }
        dirBadge.textContent = dir;
        entry.appendChild(dirBadge);

        // Ticker
        var tickerSpan = document.createElement('span');
        tickerSpan.className = 'whale-ticker';
        tickerSpan.textContent = ticker;
        entry.appendChild(tickerSpan);

        // Signal strength bar
        var strengthWrap = document.createElement('span');
        strengthWrap.className = 'whale-strength-wrap';
        var strengthBar = document.createElement('span');
        strengthBar.className = 'whale-strength-bar';
        var pct = Math.round((sig.signal_strength || 0) * 100);
        strengthBar.style.width = pct + '%';
        if (pct >= 70) {
            strengthBar.classList.add('strength-high');
        } else if (pct >= 40) {
            strengthBar.classList.add('strength-med');
        } else {
            strengthBar.classList.add('strength-low');
        }
        strengthWrap.appendChild(strengthBar);
        var strengthLabel = document.createElement('span');
        strengthLabel.className = 'whale-strength-label';
        strengthLabel.textContent = pct + '%';
        strengthWrap.appendChild(strengthLabel);
        entry.appendChild(strengthWrap);

        // Details row
        var details = document.createElement('div');
        details.className = 'whale-details';

        var tradeCount = sig.large_trade_count || 0;
        var tradeVol = sig.large_trade_volume || 0;
        var spikeRatio = sig.volume_spike_ratio || 0;
        var tilt = sig.orderbook_tilt || 0;

        var d1 = document.createElement('span');
        d1.className = 'whale-detail-item';
        d1.textContent = tradeCount + ' trade' + (tradeCount !== 1 ? 's' : '');
        details.appendChild(d1);

        var d2 = document.createElement('span');
        d2.className = 'whale-detail-item';
        d2.textContent = '$' + (tradeVol >= 1000 ? (tradeVol / 1000).toFixed(1) + 'K' : tradeVol.toFixed(0)) + ' vol';
        details.appendChild(d2);

        if (spikeRatio > 0) {
            var d3 = document.createElement('span');
            d3.className = 'whale-detail-item whale-spike';
            d3.textContent = spikeRatio.toFixed(1) + 'x spike';
            details.appendChild(d3);
        }

        var d4 = document.createElement('span');
        d4.className = 'whale-detail-item whale-tilt';
        if (tilt > 0.2) {
            d4.classList.add('tilt-yes');
        } else if (tilt < -0.2) {
            d4.classList.add('tilt-no');
        }
        d4.textContent = 'tilt ' + (tilt >= 0 ? '+' : '') + tilt.toFixed(2);
        details.appendChild(d4);

        entry.appendChild(details);
        container.appendChild(entry);
    });
}

// === FOREX RATES (NEW SECTION) ===
function loadForexRates() {
    fetch('/api/forex/rates')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            currentForexRates = data;
            renderAllStackedPanels();
        })
        .catch(function(err) { console.error('Forex rates fetch error:', err); });
}

function renderForexRatesContent(container, ratesData) {
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

    if (!ratesData || Object.keys(ratesData).length === 0) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'Fetching forex rates...';
        container.appendChild(empty);
        return;
    }

    // Example rates to display
    const interestingRates = ['EUR', 'GBP', 'JPY', 'CAD', 'AUD'];

    interestingRates.forEach(function(currency) {
        if (ratesData[currency]) {
            var rateEntry = document.createElement('div');
            rateEntry.style.cssText = 'display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px dashed var(--arb-border);font-family:monospace;font-size:11px;color:var(--arb-text);';
            var currencyPair = document.createElement('span');
            currencyPair.style.color = 'var(--arb-muted)';
            currencyPair.textContent = 'USD/' + currency;
            var rateValue = document.createElement('span');
            rateValue.style.color = 'var(--arb-accent)';
            rateValue.style.fontWeight = '700';
            rateValue.textContent = ratesData[currency].toFixed(4); // Format to 4 decimal places
            rateEntry.appendChild(currencyPair);
            rateEntry.appendChild(rateValue);
            container.appendChild(rateEntry);
        }
    });

    var timeUpdated = ratesData.time_last_update_unix ? new Date(ratesData.time_last_update_unix * 1000).toLocaleTimeString() : 'N/A';
    var updateTime = document.createElement('div');
    updateTime.style.cssText = 'font-size:10px;color:var(--arb-muted);margin-top:8px;text-align:right;';
    updateTime.textContent = 'Last updated: ' + timeUpdated;
    container.appendChild(updateTime);
}

// === POSITIONS DASHBOARD ===
var positionsData = { packages: [], dashboard: {}, alerts: [], config: {} };
var posWs = null;

function loadPositionsConfig() {
    fetch('/api/derivatives/config').then(function(r) { return r.json(); }).then(function(data) {
        positionsData.config = data;
        var banner = document.getElementById('pos-paper-banner');
        if (banner) {
            banner.style.display = data.paper_mode ? 'block' : 'none';
        }
    }).catch(function() {});
}

function loadPositionsDashboard() {
    fetch('/api/derivatives/dashboard').then(function(r) { return r.json(); }).then(function(data) {
        positionsData.dashboard = data;
        renderPortfolioBar(data);
    }).catch(function() {});
}

function loadPositionsPackages() {
    fetch('/api/derivatives/packages').then(function(r) { return r.json(); }).then(function(data) {
        positionsData.packages = data.packages || [];
        renderPackages(positionsData.packages);
    }).catch(function() {});
}

function loadPositionsAlerts() {
    fetch('/api/derivatives/dashboard/alerts').then(function(r) { return r.json(); }).then(function(data) {
        positionsData.alerts = data.alerts || [];
        renderAlerts(positionsData.alerts);
    }).catch(function() {});
}

function renderPortfolioBar(stats) {
    var bar = document.getElementById('pos-portfolio-stats');
    if (!bar) return;
    while (bar.firstChild) bar.removeChild(bar.firstChild);

    var items = [
        { label: 'OPEN', value: stats.open_packages || 0 },
        { label: 'INVESTED', value: '$' + (stats.total_invested || 0).toFixed(2) },
        { label: 'P&L', value: '$' + (stats.unrealized_pnl || 0).toFixed(2), cls: (stats.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative' },
        { label: 'WIN RATE', value: ((stats.win_rate || 0) * 100).toFixed(0) + '%' }
    ];

    items.forEach(function(item) {
        var stat = document.createElement('div');
        stat.className = 'pos-stat';
        var label = document.createElement('div');
        label.className = 'pos-stat-label';
        label.textContent = item.label;
        var val = document.createElement('div');
        val.className = 'pos-stat-value' + (item.cls ? ' ' + item.cls : '');
        val.textContent = item.value;
        stat.appendChild(label);
        stat.appendChild(val);
        bar.appendChild(stat);
    });

    // Add per-platform executor status
    if (stats.executor_details) {
        var executorSection = document.createElement('div');
        executorSection.className = 'pos-executor-section';
        executorSection.style.cssText = 'margin-top: 10px; border-top: 1px solid var(--arb-border); padding-top: 10px;';
        
        var sectionHeader = document.createElement('div');
        sectionHeader.className = 'pos-section-header';
        sectionHeader.style.cssText = 'font-weight: 700; color: var(--arb-accent); margin-bottom: 5px; font-size: 11px;';
        sectionHeader.textContent = 'EXECUTORS';
        executorSection.appendChild(sectionHeader);

        for (var name in stats.executor_details) {
            var exec = stats.executor_details[name];
            var execRow = document.createElement('div');
            execRow.className = 'pos-executor-row';
            execRow.style.cssText = 'display: flex; justify-content: space-between; align-items: center; font-size: 10px; padding: 2px 0;';

            var nameGroup = document.createElement('span');
            nameGroup.style.cssText = 'display: flex; align-items: center; gap: 5px;';

            var execName = document.createElement('span');
            execName.className = 'pos-executor-name';
            execName.style.fontWeight = '700';
            execName.textContent = name.toUpperCase();
            if (!exec.active) {
                execName.style.color = 'var(--arb-red)';
                execName.title = exec.error || 'Inactive';
            }
            nameGroup.appendChild(execName);

            if (exec.active) {
                var statusBadge = document.createElement('span');
                statusBadge.className = 'pos-badge ' + (exec.is_paper_trading ? 'paper' : 'live');
                statusBadge.textContent = exec.is_paper_trading ? 'PAPER' : 'LIVE';
                statusBadge.style.cssText = 'font-size: 8px; padding: 1px 4px; border-radius: 3px; background: rgba(0, 200, 255, 0.1); color: var(--arb-accent);';
                if (!exec.is_paper_trading) { // For live, make it slightly different
                    statusBadge.style.background = 'rgba(0, 255, 0, 0.1)';
                    statusBadge.style.color = 'var(--arb-green)';
                }
                nameGroup.appendChild(statusBadge);

                execRow.appendChild(nameGroup);

                var balanceAndTrades = document.createElement('span');
                balanceAndTrades.style.cssText = 'color: var(--arb-text);';
                balanceAndTrades.textContent = '$' + (exec.total_balance || 0).toFixed(2) + ' (' + (exec.trade_count || 0) + ' trades)';
                execRow.appendChild(balanceAndTrades);

            } else {
                var errorSpan = document.createElement('span');
                errorSpan.className = 'pos-executor-error';
                errorSpan.style.color = 'var(--arb-red)';
                errorSpan.textContent = exec.error ? 'Error: ' + exec.error : 'Inactive';
                nameGroup.appendChild(errorSpan);
                execRow.appendChild(nameGroup);
            }
            executorSection.appendChild(execRow);
        }
        bar.appendChild(executorSection);
    }
}

function renderPackages(packages) {
    var container = document.getElementById('pos-packages-list');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);

    if (!packages.length) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'No active positions';
        container.appendChild(empty);
        return;
    }

    packages.forEach(function(pkg) {
        var card = document.createElement('div');
        card.className = 'pos-package';

        // Header
        var header = document.createElement('div');
        header.className = 'pos-pkg-header';
        var name = document.createElement('span');
        name.className = 'pos-pkg-name';
        name.textContent = pkg.name || pkg.id;
        var pnl = document.createElement('span');
        pnl.className = 'pos-pkg-pnl';
        var pnlVal = pkg.unrealized_pnl || 0;
        pnl.textContent = (pnlVal >= 0 ? '+' : '') + '$' + pnlVal.toFixed(2);
        pnl.style.color = pnlVal >= 0 ? 'var(--arb-green)' : 'var(--arb-red)';
        var badge = document.createElement('span');
        badge.className = 'pos-badge ' + (pkg.itm_status || 'atm').toLowerCase();
        badge.textContent = pkg.itm_status || 'ATM';
        header.appendChild(name);
        header.appendChild(badge);
        header.appendChild(pnl);
        card.appendChild(header);

        // Legs
        (pkg.legs || []).forEach(function(leg) {
            var row = document.createElement('div');
            row.className = 'pos-leg-row';
            var platform = document.createElement('span');
            platform.className = 'pos-leg-platform';
            platform.textContent = leg.platform;
            var label = document.createElement('span');
            label.className = 'pos-leg-label';
            label.textContent = leg.asset_label || leg.asset_id;
            var entry = document.createElement('span');
            entry.textContent = '$' + (leg.entry_price || 0).toFixed(4);
            var current = document.createElement('span');
            current.textContent = '$' + (leg.current_price || 0).toFixed(4);
            current.style.color = (leg.current_price || 0) >= (leg.entry_price || 0) ? 'var(--arb-green)' : 'var(--arb-red)';
            var status = document.createElement('span');
            status.className = 'pos-badge ' + (leg.leg_status || 'atm').toLowerCase();
            status.textContent = leg.leg_status || 'ATM';
            row.appendChild(platform);
            row.appendChild(label);
            row.appendChild(entry);
            row.appendChild(current);
            row.appendChild(status);
            card.appendChild(row);
        });

        // Actions
        var actions = document.createElement('div');
        actions.className = 'pos-pkg-actions';
        var exitBtn = document.createElement('button');
        exitBtn.className = 'pos-btn danger';
        exitBtn.textContent = 'EXIT ALL';
        exitBtn.addEventListener('click', function() { exitPackage(pkg.id); });
        actions.appendChild(exitBtn);
        card.appendChild(actions);

        container.appendChild(card);
    });
}

function renderAlerts(alerts) {
    var container = document.getElementById('pos-alerts-list');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);

    if (!alerts.length) {
        var empty = document.createElement('div');
        empty.className = 'arb-empty';
        empty.textContent = 'No pending alerts';
        container.appendChild(empty);
        return;
    }

    alerts.forEach(function(alert) {
        var row = document.createElement('div');
        row.className = 'pos-alert';
        var text = document.createElement('span');
        text.className = 'pos-alert-text';
        text.textContent = alert.trigger_name + ': ' + (alert.details ? (alert.details.details || '') : '');
        var btns = document.createElement('div');
        btns.className = 'pos-alert-actions';
        var approveBtn = document.createElement('button');
        approveBtn.className = 'pos-btn';
        approveBtn.textContent = 'APPROVE';
        approveBtn.addEventListener('click', function() { approveAlert(alert.id); });
        var rejectBtn = document.createElement('button');
        rejectBtn.className = 'pos-btn danger';
        rejectBtn.textContent = 'REJECT';
        rejectBtn.addEventListener('click', function() { rejectAlert(alert.id); });
        btns.appendChild(approveBtn);
        btns.appendChild(rejectBtn);
        row.appendChild(text);
        row.appendChild(btns);
        container.appendChild(row);
    });
}

function exitPackage(pkgId) {
    fetch('/api/derivatives/packages/' + pkgId + '/exit', { method: 'POST' })
        .then(function() { loadPositionsPackages(); loadPositionsDashboard(); });
}

function approveAlert(alertId) {
    fetch('/api/derivatives/dashboard/alerts/' + alertId + '/approve', { method: 'POST' })
        .then(function() { loadPositionsAlerts(); loadPositionsPackages(); });
}

function rejectAlert(alertId) {
    fetch('/api/derivatives/dashboard/alerts/' + alertId + '/reject', { method: 'POST' })
        .then(function() { loadPositionsAlerts(); });
}

function connectPositionsWs() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    posWs = new WebSocket(proto + '//' + location.host + '/api/derivatives/ws');
    posWs.onmessage = function(e) {
        try {
            var data = JSON.parse(e.data);
            if (data.event === 'position_update' || data.event === 'package_created' || data.event === 'package_closed') {
                loadPositionsPackages();
                loadPositionsDashboard();
            }
            if (data.event === 'escalation') {
                loadPositionsAlerts();
            }
        } catch (err) {}
    };
    posWs.onclose = function() { setTimeout(connectPositionsWs, 5000); };
}

function initPositionsDashboard() {
    loadPositionsConfig();
    loadPositionsDashboard();
    loadPositionsPackages();
    loadPositionsAlerts();
    connectPositionsWs();
    setInterval(function() {
        loadPositionsDashboard();
        loadPositionsPackages();
    }, 30000);
}

// === INIT ===
document.addEventListener('DOMContentLoaded', function() {
    var tabLob = document.getElementById('tab-lobsterminal');
    var tabArb = document.getElementById('tab-arbitrout');

    if (tabLob) {
        tabLob.addEventListener('click', function() { switchMode('lobsterminal'); });
    }
    if (tabArb) {
        tabArb.addEventListener('click', function() { switchMode('arbitrout'); });
    }

    // Initialize mode based on saved preference, then load opportunities if in arbitrout mode
    switchMode(arbMode, true);

    // Init positions dashboard if container exists
    if (document.getElementById('pos-packages-list')) {
        initPositionsDashboard();
    }
});
