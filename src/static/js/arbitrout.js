// === ARBITROUT FRONTEND ===
// Prediction market arbitrage scanner UI

let arbMode = 'lobsterminal';
let arbPollingInterval = null;
let arbScanInterval = null;
let arbWs = null;
let selectedOpp = null;
let feedItems = [];
let sortCriteria = 'Profit High-Low';

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
[_,_,_,_,_,_,_,_,_,_,O,O,O,O,O,O,O,O,N,O,O,N,O,O,O,O,O,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_,_],
[_,_,_,_,_,_,_,O,G,G,G,X,G,G,G,G,G,G,G,G,G,G,X,G,G,G,G,G,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_],
[_,_,_,_,_,_,O,G,G,G,G,G,X,G,G,X,G,G,G,X,G,G,G,G,G,X,G,G,G,G,O,O,_,_,_,_,_,_,_,_,_,_],
[_,N,N,_,_,O,G,G,X,G,G,G,G,G,G,G,G,X,G,G,G,G,G,X,G,G,G,G,G,G,G,G,O,_,_,_,_,_,_,_,_,_],
[_,N,T,N,_,O,G,G,G,G,G,X,G,G,G,G,G,G,G,G,G,X,G,G,G,G,G,G,G,G,G,Q,E,O,_,_,_,_,H,H,_,_,_],
[N,T,T,N,O,G,G,G,G,G,G,G,X,G,G,G,G,G,G,G,G,G,G,G,G,G,G,S,S,S,G,G,G,O,_,_,H,L,L,H,_,_],
[_,N,T,N,O,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,R,S,S,S,G,O,O,_,_,H,L,H,_,_],
[_,_,N,N,_,O,S,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,K,S,S,S,O,O,_,_,_,H,L,L,H,_,_],
[_,_,_,_,_,O,K,K,W,W,K,K,W,K,K,W,W,K,K,W,W,K,K,W,W,K,K,K,O,O,N,N,_,_,_,_,_,_,H,H,_,_,_],
[_,_,_,_,_,_,O,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,W,O,O,O,_,N,T,_,_,_,_,_,_,_,_,C,_,_,_],
[_,_,_,_,_,_,_,O,O,W,W,W,W,W,W,W,W,W,W,W,W,O,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_,_,C,_,_,_],
[_,_,_,_,_,_,_,_,O,O,O,O,O,O,O,O,O,O,O,O,O,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,C,_,_,_],
[D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D,D],
[_,_,I,I,I,I,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,J,J,J,J,J,J,_,_,_,_],
[_,_,I,B,B,B,B,B,B,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,H,H,C,H,H,H,J,_,_,_,_],
[_,_,I,B,B,B,B,B,B,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,H,H,C,H,H,H,J,_,_,_,_],
[_,_,I,I,I,I,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,J,J,J,J,J,J,_,_,_,_],
[_,_,_,_,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,_,_,_,_,_,_,_],
[_,_,_,I,I,I,I,I,I,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,_,J,J,J,J,_,_,_,_,_,_],
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
        [_,_,_,_,R,_,D,D,_,R,_,_,_,_,_],
        [_,_,_,R,_,_,D,D,_,_,R,_,_,_,_],
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

    var sortSelect = document.createElement('select');
    sortSelect.id = 'sort-select';
    sortSelect.style.cssText = 'margin-bottom: 10px;';
    var options = [
        { value: 'Profit High-Low', text: 'Profit High-Low' },
        { value: 'Profit Low-High', text: 'Profit Low-High' },
        { value: 'Platform A-Z', text: 'Platform A-Z' },
        { value: 'Newest First', text: 'Newest First' }
    ];
    options.forEach(function(option) {
        var opt = document.createElement('option');
        opt.value = option.value;
        opt.textContent = option.text;
        if (option.value === sortCriteria) {
            opt.selected = true;
        }
        sortSelect.appendChild(opt);
    });
    container.appendChild(sortSelect);

    sortSelect.addEventListener('change', function() {
        sortCriteria = sortSelect.value;
        renderOpportunities(opps);
    });

    var sortedOpps = opps.slice();
    switch (sortCriteria) {
        case 'Profit High-Low':
            sortedOpps.sort(function(a, b) {
                return (b.profit_pct || b.spread * 100) - (a.profit_pct || a.spread * 100);
            });
            break;
        case 'Profit Low-High':
            sortedOpps.sort(function(a, b) {
                return (a.profit_pct || a.spread * 100) - (b.profit_pct || b.spread * 100);
            });
            break;
        case 'Platform A-Z':
            sortedOpps.sort(function(a, b) {
                return a.buy_yes_platform.localeCompare(b.buy_yes_platform);
            });
            break;
        case 'Newest First':
            sortedOpps.sort(function(a, b) {
                return new Date(b.matched_event.last_updated) - new Date(a.matched_event.last_updated);
            });
            break;
    }

    sortedOpps.forEach(function(opp) {
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
    var cols = ['PLATFORM', 'YES %', 'NO %', 'ACTION', ''];
    cols.forEach(function(txt) {
        var c = document.createElement('div');
        c.textContent = txt;
        colHeader.appendChild(c);
    });
    container.appendChild(colHeader);

    var buyYesPlatform = opp.buy_yes_platform || '';
    var buyNoPlatform = opp.buy_no_platform || '';

    markets.forEach(function(m) {
        var row = document.createElement('div');
        row.className = 'platform-row';

        var nameEl = document.createElement('div');
        nameEl.className = 'platform-name';
        nameEl.textContent = m.platform;
        row.appendChild(nameEl);

        var yesEl = document.createElement('div');
        yesEl.className = 'price-yes';
        yesEl.textContent = (m.yes_price * 100).toFixed(1) + '\u00A2';
        if (m.platform === buyYesPlatform) {
            yesEl.style.fontWeight = '700';
            yesEl.classList.add('price-best');
        }
        row.appendChild(yesEl);

        var noEl = document.createElement('div');
        noEl.className = 'price-no';
        noEl.textContent = (m.no_price * 100).toFixed(1) + '\u00A2';
        if (m.platform === buyNoPlatform) {
            noEl.style.fontWeight = '700';
            noEl.classList.add('price-best');
        }
        row.appendChild(noEl);

        // Action tag
        var actionEl = document.createElement('div');
        actionEl.style.cssText = 'font-size:9px;font-weight:700;';
        if (m.platform === buyYesPlatform) {
            actionEl.style.color = 'var(--arb-green)';
            actionEl.textContent = 'BUY YES';
        } else if (m.platform === buyNoPlatform) {
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

        // Profit
        var profitLine = document.createElement('div');
        profitLine.style.cssText = 'color:var(--arb-green);font-weight:700;margin-top:6px;font-size:12px;';
        profitLine.textContent = 'GUARANTEED PROFIT: ' + profitPct.toFixed(1) + '%';
        tradeEl.appendChild(profitLine);

        // Dollar example
        var exLine = document.createElement('div');
        exLine.style.cssText = 'color:var(--arb-muted);margin-top:4px;font-size:10px;';
        var yesSpend = (100 * yesAlloc / 100);
        var noSpend = (100 * noAlloc / 100);
        var profit = (100 * profitPct / 100);
        exLine.textContent = '$100 invested = $' + yesSpend.toFixed(0) + ' YES + $' + noSpend.toFixed(0) + ' NO = $' + profit.toFixed(2) + ' profit';
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
