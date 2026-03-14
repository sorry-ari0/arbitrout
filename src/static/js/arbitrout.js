// === ARBITROUT FRONTEND ===
// Prediction market arbitrage scanner UI

let arbMode = 'lobsterminal';
let arbPollingInterval = null;
let arbWs = null;
let selectedOpp = null;
let feedItems = [];

// === PIXEL ART (CSS grid of div cells) ===
function createPixelGrid(colorMap, scale) {
    // colorMap: 2D array of hex colors (null = transparent)
    var size = colorMap.length;
    var container = document.createElement('div');
    container.style.display = 'grid';
    container.style.gridTemplateColumns = 'repeat(' + size + ', ' + scale + 'px)';
    container.style.gridTemplateRows = 'repeat(' + size + ', ' + scale + 'px)';
    container.style.imageRendering = 'pixelated';

    for (var y = 0; y < size; y++) {
        for (var x = 0; x < size; x++) {
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
    // 16x16 pixel art trout, rendered at 4x scale = 64x64 visual
    var T = '#00e5cc'; // teal body
    var D = '#009688'; // dark teal
    var E = '#004d40'; // eye
    var W = '#e0f7fa'; // white belly
    var F = '#ff8a65'; // fin/tail accent
    var _ = null;
    var map = [
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,T,T,T,T,_,_,_,_,_,_],
        [_,_,_,_,_,T,T,T,T,T,T,_,_,_,_,_],
        [_,_,_,_,T,T,T,E,T,T,T,T,_,_,_,_],
        [_,_,_,T,T,T,T,T,T,T,T,T,T,F,_,_],
        [_,_,T,D,T,T,T,T,T,T,T,T,T,F,F,_],
        [_,_,T,D,W,W,T,T,T,T,T,T,F,F,_,_],
        [_,_,T,D,W,W,T,T,T,T,T,T,F,F,_,_],
        [_,_,T,D,T,T,T,T,T,T,T,T,T,F,F,_],
        [_,_,_,T,T,T,T,T,T,T,T,T,T,F,_,_],
        [_,_,_,_,T,T,T,T,T,T,T,T,_,_,_,_],
        [_,_,_,_,_,T,T,T,T,T,T,_,_,_,_,_],
        [_,_,_,_,_,_,T,T,T,T,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
        [_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_]
    ];
    return createPixelGrid(map, 4);
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
        artContainer.appendChild(getLobsterPixelArt());
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
function switchMode(mode) {
    if (mode === arbMode) return;
    arbMode = mode;

    showSplash(mode);

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

// === POLLING ===
function startArbPolling() {
    loadOpportunities();
    loadSavedMarkets();
    arbPollingInterval = setInterval(loadOpportunities, 15000);
    connectArbWs();
}

function stopArbPolling() {
    if (arbPollingInterval) {
        clearInterval(arbPollingInterval);
        arbPollingInterval = null;
    }
    if (arbWs) {
        arbWs.close();
        arbWs = null;
    }
}

// === WEBSOCKET ===
function connectArbWs() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    arbWs = new WebSocket(proto + '//' + location.host + '/ws/arbitrage');

    arbWs.onmessage = function(e) {
        var data = JSON.parse(e.data);
        if (data.type === 'opportunities') {
            renderOpportunities(data.data);
        } else if (data.type === 'feed') {
            addFeedItem(data.data);
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

function renderOpportunities(opps) {
    var container = document.getElementById('opp-list');
    if (!container) return;

    // Clear existing
    while (container.firstChild) {
        container.removeChild(container.firstChild);
    }

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
        row.addEventListener('click', function() { showEventDetail(opp); });

        var titleEl = document.createElement('div');
        titleEl.className = 'opp-title';
        titleEl.textContent = opp.canonical_title || opp.matched_event.canonical_title;
        row.appendChild(titleEl);

        var spreadEl = document.createElement('div');
        spreadEl.className = 'opp-spread positive';
        spreadEl.textContent = '+' + (opp.profit_pct || opp.spread * 100).toFixed(1) + '%';
        row.appendChild(spreadEl);

        var platEl = document.createElement('div');
        platEl.className = 'opp-platforms';
        platEl.textContent = (opp.buy_yes_platform || '') + ' / ' + (opp.buy_no_platform || '');
        row.appendChild(platEl);

        var volEl = document.createElement('div');
        volEl.className = 'opp-volume';
        volEl.textContent = '$' + ((opp.combined_volume || 0) / 1000).toFixed(0) + 'K';
        row.appendChild(volEl);

        container.appendChild(row);
    });
}

// === EVENT DETAIL ===
function showEventDetail(opp) {
    selectedOpp = opp;
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
    var cols = ['PLATFORM', 'YES', 'NO', 'LINK'];
    cols.forEach(function(txt) {
        var c = document.createElement('div');
        c.textContent = txt;
        colHeader.appendChild(c);
    });
    container.appendChild(colHeader);

    // Find best prices
    var bestYes = 1, bestNo = 1;
    markets.forEach(function(m) {
        if (m.yes_price < bestYes) bestYes = m.yes_price;
        if (m.no_price < bestNo) bestNo = m.no_price;
    });

    markets.forEach(function(m) {
        var row = document.createElement('div');
        row.className = 'platform-row';

        var nameEl = document.createElement('div');
        nameEl.className = 'platform-name';
        nameEl.textContent = m.platform;
        row.appendChild(nameEl);

        var yesEl = document.createElement('div');
        yesEl.className = 'price-yes' + (m.yes_price === bestYes ? ' price-best' : '');
        yesEl.textContent = (m.yes_price * 100).toFixed(1) + '\u00A2';
        row.appendChild(yesEl);

        var noEl = document.createElement('div');
        noEl.className = 'price-no' + (m.no_price === bestNo ? ' price-best' : '');
        noEl.textContent = (m.no_price * 100).toFixed(1) + '\u00A2';
        row.appendChild(noEl);

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

    // Save button
    var saveBtn = document.createElement('button');
    saveBtn.style.cssText = 'margin:8px;padding:6px 12px;background:var(--arb-accent);color:var(--arb-bg);border:none;border-radius:3px;cursor:pointer;font-family:monospace;font-size:11px;font-weight:700;';
    saveBtn.textContent = 'BOOKMARK';
    saveBtn.addEventListener('click', function() {
        saveMarket(event.canonical_title || opp.canonical_title);
    });
    container.appendChild(saveBtn);
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
        time.textContent = (item.time || new Date().toLocaleTimeString()) + ' ';
        el.appendChild(time);

        var plat = document.createElement('span');
        plat.className = 'feed-platform';
        plat.textContent = '[' + (item.platform || '?') + '] ';
        el.appendChild(plat);

        var text = document.createElement('span');
        text.textContent = (item.title || '') + ' ';
        el.appendChild(text);

        if (item.direction) {
            var arrow = document.createElement('span');
            arrow.className = item.direction === 'up' ? 'feed-price-up' : 'feed-price-down';
            arrow.textContent = item.direction === 'up' ? '\u25B2' : '\u25BC';
            if (item.price) arrow.textContent += ' ' + item.price;
            el.appendChild(arrow);
        }

        container.appendChild(el);
    });
}

// === SAVED MARKETS ===
function loadSavedMarkets() {
    fetch('/api/arbitrage/saved')
        .then(function(r) { return r.json(); })
        .then(function(data) { renderSaved(data); })
        .catch(function() {});
}

function renderSaved(items) {
    var container = document.getElementById('saved-list');
    if (!container) return;

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
        titleEl.textContent = item.title || item;
        row.appendChild(titleEl);

        var removeBtn = document.createElement('button');
        removeBtn.className = 'saved-remove';
        removeBtn.textContent = '\u00D7';
        removeBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            removeSaved(item.title || item);
        });
        row.appendChild(removeBtn);

        container.appendChild(row);
    });
}

function saveMarket(title) {
    fetch('/api/arbitrage/saved', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title})
    }).then(function() { loadSavedMarkets(); });
}

function removeSaved(title) {
    fetch('/api/arbitrage/saved', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title})
    }).then(function() { loadSavedMarkets(); });
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
});
