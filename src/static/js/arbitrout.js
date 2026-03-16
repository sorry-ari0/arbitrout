// === ARBITROUT FRONTEND ===
// Prediction market arbitrage scanner UI

let arbMode = 'lobsterminal';
let arbPollingInterval = null;
let arbScanInterval = null;
let arbWs = null;
let selectedOpp = null;
let feedItems = [];

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
[_,_,_,_,_,_,_,O,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,O,O,O,_,N,T,_,_,_,_,_,_,_,_,C,_,_,_],
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

// === POLLING + AUTO-SCAN ===
function startArbPolling() {
    triggerScan();
    loadSavedMarkets();
    arbPollingInterval = setInterval(loadOpportunities, 15000);
    arbScanInterval = setInterval(triggerScan, 60000);
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

    // Arbitrage trade ratio
    if (opp.buy_yes_price !== undefined && opp.buy_no_price !== undefined) {
        var yesAlloc = opp.yes_allocation_pct || '50.0';
        var noAlloc = opp.no_allocation_pct || '50.0';

        var tradeEl = document.createElement('div');
        tradeEl.style.cssText = 'padding:8px;border-top:1px solid var(--arb-border);font-family:monospace;font-size:11px;';

        var tradeTitle = document.createElement('div');
        tradeTitle.style.cssText = 'color:var(--arb-accent);font-weight:700;margin-bottom:4px;';
        tradeTitle.textContent = 'TRADE RATIO';
        tradeEl.appendChild(tradeTitle);

        var yesLine = document.createElement('div');
        yesLine.style.color = 'var(--arb-green)';
        yesLine.textContent = 'BUY YES on ' + (opp.buy_yes_platform || '?') + ': ' + (opp.buy_yes_price * 100).toFixed(1) + '\u00A2 (' + yesAlloc + '% of capital)';
        tradeEl.appendChild(yesLine);

        var noLine = document.createElement('div');
        noLine.style.color = '#ff9800';
        noLine.textContent = 'BUY NO on ' + (opp.buy_no_platform || '?') + ': ' + (opp.buy_no_price * 100).toFixed(1) + '\u00A2 (' + noAlloc + '% of capital)';
        tradeEl.appendChild(noLine);

        var profitLine = document.createElement('div');
        profitLine.style.cssText = 'color:var(--arb-green);font-weight:700;margin-top:4px;';
        profitLine.textContent = 'PROFIT: ' + (opp.profit_pct || (opp.spread * 100)).toFixed(2) + '% per $1 invested';
        tradeEl.appendChild(profitLine);

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
