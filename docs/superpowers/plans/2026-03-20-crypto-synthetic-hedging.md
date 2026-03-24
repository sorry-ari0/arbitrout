# Crypto Synthetic Hedging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the political synthetic derivatives pipeline to discover and construct multi-leg strategies that include crypto-relevant prediction market contracts as hedge legs.

**Architecture:** Add `crypto_event` as a 7th classifier type, 4 new relationship types, and crypto market context to the LLM strategy prompt. No new modules — all changes extend existing files in `src/political/`.

**Tech Stack:** Python 3.14, pytest, asyncio, regex, CoinGecko API (via existing `adapters/crypto_spot.py`)

**Spec:** `docs/superpowers/specs/2026-03-20-crypto-synthetic-hedging-design.md`

---

### Task 1: Add Crypto Fields to Models

**Files:**
- Modify: `src/political/models.py:17-28`
- Test: `tests/test_political_classifier.py` (existing test file, add new tests)

- [ ] **Step 1: Write the failing test**

Add to the bottom of `tests/test_political_classifier.py`:

```python
class TestCryptoModelFields:
    """Tests for crypto fields on PoliticalContractInfo."""

    def test_crypto_fields_default_none(self):
        """Crypto fields default to None for political contracts."""
        event = _make_event("Talarico wins TX Senate")
        info = PoliticalContractInfo(
            event=event, contract_type="candidate_win",
            candidates=["Talarico"], race="TX Senate", state="TX",
        )
        assert info.crypto_asset is None
        assert info.event_category is None
        assert info.crypto_direction is None
        assert info.crypto_threshold is None

    def test_crypto_fields_populated(self):
        """Crypto fields can be set for crypto_event contracts."""
        event = _make_event("Bitcoin above $150K by end of 2026", platform="polymarket")
        info = PoliticalContractInfo(
            event=event, contract_type="crypto_event",
            crypto_asset="BTC", event_category="price_target",
            crypto_direction="positive", crypto_threshold=150000.0,
        )
        assert info.crypto_asset == "BTC"
        assert info.event_category == "price_target"
        assert info.crypto_direction == "positive"
        assert info.crypto_threshold == 150000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestCryptoModelFields -v`
Expected: FAIL — `PoliticalContractInfo` has no `crypto_asset` field.

- [ ] **Step 3: Add crypto fields to PoliticalContractInfo**

In `src/political/models.py`, add 4 optional fields after `direction` (line 27):

```python
@dataclass
class PoliticalContractInfo:
    """Classification result for a single political contract."""
    event: NormalizedEvent          # source event (has event_id, platform, prices)
    contract_type: str              # candidate_win, party_outcome, margin_bracket,
                                    # vote_share, matchup, yes_no_binary, crypto_event
    candidates: list[str] = field(default_factory=list)  # extracted candidate names
    party: Optional[str] = None     # "dem", "gop", or None
    race: Optional[str] = None      # "TX Senate", "President", etc.
    state: Optional[str] = None     # two-letter state abbreviation
    threshold: Optional[float] = None   # for margin/vote_share brackets
    direction: Optional[str] = None     # "above", "below", "between"
    # Crypto extension fields (None for political contracts)
    crypto_asset: Optional[str] = None       # "BTC", "ETH", "SOL", etc.
    event_category: Optional[str] = None     # "regulatory", "price_target", "technical", "market_event"
    crypto_direction: Optional[str] = None   # "positive", "negative", "neutral"
    crypto_threshold: Optional[float] = None # dollar value for price_target contracts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestCryptoModelFields -v`
Expected: PASS

- [ ] **Step 5: Run all existing model tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestPoliticalModels -v`
Expected: All existing tests PASS (new fields are Optional with None defaults).

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/models.py tests/test_political_classifier.py
git commit -m "feat: add crypto fields to PoliticalContractInfo model"
```

---

### Task 2: Add crypto_event Classifier Type

**Files:**
- Modify: `src/political/classifier.py`
- Test: `tests/test_political_classifier.py`

This task adds `crypto_event` as the 7th classification type, checked at priority position 6 (before the `yes_no_binary` fallback). Includes regex patterns, asset normalization, event category detection, and direction inference.

- [ ] **Step 1: Write the failing tests**

Add to the bottom of `tests/test_political_classifier.py`:

```python
class TestCryptoEventClassifier:
    """Tests for crypto_event classification type."""

    def test_regulatory_btc(self):
        """SEC + Bitcoin → crypto_event / regulatory."""
        event = _make_event("Will SEC classify Bitcoin as a security?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "BTC"
        assert info.event_category == "regulatory"
        assert info.crypto_direction == "negative"

    def test_price_target_btc(self):
        """BTC above $150K → crypto_event / price_target."""
        event = _make_event("Will Bitcoin be above $150,000 by end of 2026?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "BTC"
        assert info.event_category == "price_target"
        assert info.crypto_direction == "positive"
        assert info.crypto_threshold == 150000.0

    def test_price_target_eth(self):
        """ETH above $5,000 → crypto_event / price_target."""
        event = _make_event("Will Ethereum reach $5,000?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "ETH"
        assert info.event_category == "price_target"
        assert info.crypto_direction == "positive"
        assert info.crypto_threshold == 5000.0

    def test_technical_event(self):
        """Ethereum upgrade → crypto_event / technical."""
        event = _make_event("Will Ethereum complete the Pectra upgrade?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "ETH"
        assert info.event_category == "technical"

    def test_etf_approval(self):
        """ETH ETF approved → crypto_event / regulatory / positive."""
        event = _make_event("Will Ethereum ETF be approved by SEC?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "ETH"
        assert info.event_category == "regulatory"
        assert info.crypto_direction == "positive"

    def test_hack_event(self):
        """Solana hack → crypto_event / technical / negative."""
        event = _make_event("Will Solana suffer a major exploit in 2026?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "SOL"
        assert info.event_category == "technical"
        assert info.crypto_direction == "negative"

    def test_halving_is_technical(self):
        """Bitcoin halving → crypto_event / technical."""
        event = _make_event("Will Bitcoin halving happen before May 2028?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "BTC"
        assert info.event_category == "technical"

    def test_non_crypto_not_matched(self):
        """Non-crypto political contract must NOT match crypto_event."""
        event = _make_event("Talarico wins TX Senate")
        info = classify_contract(event)
        assert info.contract_type != "crypto_event"

    def test_pure_crypto_mention_no_match(self):
        """Pure mention of 'crypto' without actionable context → NOT crypto_event."""
        event = _make_event("Will crypto be discussed at the debate?")
        info = classify_contract(event)
        # Should NOT match crypto_event — no asset name + actionable keyword
        assert info.contract_type == "yes_no_binary"

    def test_congress_ban_bitcoin(self):
        """Political + crypto overlap: 'Will Congress ban Bitcoin?' → crypto_event (priority)."""
        event = _make_event("Will Congress ban Bitcoin?")
        info = classify_contract(event)
        assert info.contract_type == "crypto_event"
        assert info.crypto_asset == "BTC"
        assert info.event_category == "regulatory"
        assert info.crypto_direction == "negative"

    def test_asset_normalization(self):
        """Various asset name forms normalize to standard tickers."""
        cases = [
            ("Will BTC reach $200,000?", "BTC"),
            ("Will Ether exceed $10,000?", "ETH"),
            ("Will SOL hit $500?", "SOL"),
            ("Will Ripple be above $5?", "XRP"),
            ("Will Dogecoin reach $1?", "DOGE"),
        ]
        for title, expected_asset in cases:
            event = _make_event(title)
            info = classify_contract(event)
            assert info.contract_type == "crypto_event", f"Failed for: {title}"
            assert info.crypto_asset == expected_asset, f"Expected {expected_asset} for: {title}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestCryptoEventClassifier -v`
Expected: FAIL — no crypto_event classification logic exists yet.

- [ ] **Step 3: Add crypto classifier implementation**

In `src/political/classifier.py`, add the following after the existing regex patterns (after line 90, before the helper functions section):

```python
# ============================================================
# CRYPTO EVENT DETECTION
# ============================================================

# Asset name → standard ticker
_CRYPTO_ASSET_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|ether|solana|sol|xrp|ripple|"
    r"dogecoin|doge|cardano|ada|avalanche|avax|chainlink|link|"
    r"polkadot|dot|polygon|pol|crypto(?:currency)?)\b",
    re.IGNORECASE,
)

_CRYPTO_TICKER_MAP: dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH", "ether": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "dogecoin": "DOGE", "doge": "DOGE",
    "cardano": "ADA", "ada": "ADA",
    "avalanche": "AVAX", "avax": "AVAX",
    "chainlink": "LINK", "link": "LINK",
    "polkadot": "DOT", "dot": "DOT",
    "polygon": "POL", "pol": "POL",
}

# Regulatory keywords
_CRYPTO_REGULATORY_RE = re.compile(
    r"\b(sec|cftc|regulat\w*|classify|classifies|security|securities|"
    r"etf|ban|bans|banned|restrict\w*)\b",
    re.IGNORECASE,
)

# Price target pattern: above/below/reach/hit/exceed + $amount
_CRYPTO_PRICE_RE = re.compile(
    r"\b(above|below|reach|reaches|hit|hits|exceed|exceeds)\b.*?"
    r"\$[\d,]+",
    re.IGNORECASE,
)

# Extract dollar amount
_DOLLAR_AMOUNT_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")

# Technical/event keywords
_CRYPTO_TECHNICAL_RE = re.compile(
    r"\b(hack|hacked|exploit|exploited|upgrade|fork|forked|"
    r"halving|merge|merged)\b",
    re.IGNORECASE,
)

# ETF + approve/reject (special: regulatory + positive/negative)
_ETF_APPROVAL_RE = re.compile(
    r"\betf\b.*?\b(approv\w*|reject\w*|denied|deny)\b",
    re.IGNORECASE,
)


def _classify_crypto(title: str) -> dict | None:
    """Check if title is a crypto_event. Returns extracted fields or None.

    A contract matches crypto_event if it contains at least one specific
    crypto asset name AND at least one actionable keyword (regulatory,
    price target, technical/event). Pure 'crypto' without a specific asset
    or actionable context does not match.
    """
    # Find crypto asset mentions
    asset_matches = _CRYPTO_ASSET_RE.findall(title)
    if not asset_matches:
        return None

    # Resolve to ticker — skip generic "crypto"/"cryptocurrency"
    ticker = None
    for match in asset_matches:
        t = _CRYPTO_TICKER_MAP.get(match.lower())
        if t:
            ticker = t
            break

    if not ticker:
        # Only generic "crypto" mentioned, no specific asset
        return None

    # Check for actionable keywords (must match at least one category)
    is_regulatory = bool(_CRYPTO_REGULATORY_RE.search(title))
    is_price = bool(_CRYPTO_PRICE_RE.search(title))
    is_technical = bool(_CRYPTO_TECHNICAL_RE.search(title))

    if not (is_regulatory or is_price or is_technical):
        return None

    # Determine event_category (priority: price_target > regulatory > technical)
    if is_price:
        event_category = "price_target"
    elif is_regulatory:
        event_category = "regulatory"
    else:
        event_category = "technical"

    # Extract price threshold
    threshold = None
    if is_price:
        dollar_match = _DOLLAR_AMOUNT_RE.search(title)
        if dollar_match:
            threshold = float(dollar_match.group(1).replace(",", ""))

    # Infer direction
    direction = "neutral"
    title_lower = title.lower()
    etf_match = _ETF_APPROVAL_RE.search(title)
    if etf_match:
        verb = etf_match.group(1).lower()
        if verb.startswith("approv"):
            direction = "positive"
        else:
            direction = "negative"
    elif event_category == "price_target":
        # Price targets with "above"/"reach"/"hit"/"exceed" → positive
        if re.search(r"\b(above|reach|reaches|hit|hits|exceed|exceeds)\b", title_lower):
            direction = "positive"
        elif re.search(r"\b(below|drop|drops|fall|falls)\b", title_lower):
            direction = "negative"
    elif event_category == "regulatory":
        # Classify/ban/restrict → negative; approve → positive
        if re.search(r"\b(ban|bans|banned|restrict|classif(?:y|ies))\b", title_lower):
            direction = "negative"
        elif re.search(r"\b(approv\w*)\b", title_lower):
            direction = "positive"
    elif event_category == "technical":
        if re.search(r"\b(hack|hacked|exploit|exploited)\b", title_lower):
            direction = "negative"
        elif re.search(r"\b(upgrade|merge|merged|halving)\b", title_lower):
            direction = "positive"

    return {
        "crypto_asset": ticker,
        "event_category": event_category,
        "crypto_direction": direction,
        "crypto_threshold": threshold,
    }
```

Then modify `classify_contract()` to check crypto at position 6 (before the yes_no_binary fallback). Insert BEFORE the `# 6. yes_no_binary — fallback` block (before line 298):

```python
    # 6. crypto_event — crypto asset + actionable keyword
    crypto = _classify_crypto(title)
    if crypto:
        return PoliticalContractInfo(
            event=event,
            contract_type="crypto_event",
            candidates=[],
            party=party,
            race=None,
            state=state,
            threshold=crypto["crypto_threshold"],
            direction=crypto.get("crypto_direction"),
            crypto_asset=crypto["crypto_asset"],
            event_category=crypto["event_category"],
            crypto_direction=crypto["crypto_direction"],
            crypto_threshold=crypto["crypto_threshold"],
        )

    # 7. yes_no_binary — fallback
```

Update the docstring classification order comment from "6 contract types" to "7 contract types", and renumber `yes_no_binary` from 6 to 7.

- [ ] **Step 4: Run crypto classifier tests to verify they pass**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestCryptoEventClassifier -v`
Expected: All PASS

- [ ] **Step 5: Run ALL classifier tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py -v`
Expected: All existing tests PASS (crypto check is BEFORE yes_no_binary but AFTER all political checks, so political contracts still classify correctly).

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/classifier.py tests/test_political_classifier.py
git commit -m "feat: add crypto_event as 7th classifier type with regex detection"
```

---

### Task 3: Extend Clustering for Crypto Contracts

**Files:**
- Modify: `src/political/clustering.py`
- Test: `tests/test_political_clustering.py`

Crypto contracts cluster by asset (e.g., `"crypto-btc-2026"`) instead of by race+state. The `build_clusters()` function needs a branch for `contract_type == "crypto_event"`.

- [ ] **Step 1: Write the failing tests**

Add to the bottom of `tests/test_political_clustering.py`:

```python
class TestCryptoClustering:
    """Tests for crypto contract clustering by asset."""

    def _make_crypto_contract(self, title, crypto_asset, event_category="regulatory",
                              platform="polymarket", event_id=None):
        """Helper: create a crypto_event PoliticalContractInfo."""
        event = _make_event(title, platform=platform)
        if event_id:
            event = NormalizedEvent(
                platform=platform, event_id=event_id, title=title,
                category="crypto", yes_price=0.55, no_price=0.45,
                volume=5000, expiry="2026-12-31",
                url=f"https://{platform}.com/test",
            )
        return PoliticalContractInfo(
            event=event, contract_type="crypto_event",
            crypto_asset=crypto_asset, event_category=event_category,
            crypto_direction="positive",
        )

    def test_btc_contracts_cluster_together(self):
        """Two BTC contracts → one crypto-btc cluster."""
        c1 = self._make_crypto_contract("BTC above $150K", "BTC", "price_target", event_id="e1")
        c2 = self._make_crypto_contract("SEC classifies BTC as security", "BTC", "regulatory", event_id="e2")
        clusters = build_clusters([c1, c2])
        assert len(clusters) == 1
        assert clusters[0].cluster_id == "crypto-btc-2026"
        assert len(clusters[0].contracts) == 2

    def test_different_assets_separate_clusters(self):
        """BTC and ETH contracts → separate clusters."""
        c1 = self._make_crypto_contract("BTC above $150K", "BTC", "price_target", event_id="e1")
        c2 = self._make_crypto_contract("BTC above $100K", "BTC", "price_target", event_id="e2")
        c3 = self._make_crypto_contract("ETH above $5K", "ETH", "price_target", event_id="e3")
        c4 = self._make_crypto_contract("ETH ETF approved", "ETH", "regulatory", event_id="e4")
        clusters = build_clusters([c1, c2, c3, c4])
        cluster_ids = {c.cluster_id for c in clusters}
        assert "crypto-btc-2026" in cluster_ids
        assert "crypto-eth-2026" in cluster_ids

    def test_singleton_crypto_filtered(self):
        """Single crypto contract → no cluster (minimum 2)."""
        c1 = self._make_crypto_contract("BTC above $150K", "BTC", "price_target", event_id="e1")
        clusters = build_clusters([c1])
        assert len(clusters) == 0

    def test_mixed_political_and_crypto(self):
        """Political + crypto events produce separate clusters."""
        pol1 = _make_contract("Talarico wins TX Senate", "TX Senate", "TX")
        pol2 = _make_contract("Cruz wins TX Senate", "TX Senate", "TX")
        cry1 = self._make_crypto_contract("BTC above $150K", "BTC", "price_target", event_id="c1")
        cry2 = self._make_crypto_contract("SEC bans BTC", "BTC", "regulatory", event_id="c2")
        clusters = build_clusters([pol1, pol2, cry1, cry2])
        cluster_ids = {c.cluster_id for c in clusters}
        assert any("crypto-btc" in cid for cid in cluster_ids)
        assert any("senate" in cid.lower() or "tx" in cid.lower() for cid in cluster_ids)

    def test_crypto_cluster_race_state_none(self):
        """Crypto clusters have race=None and state=None."""
        c1 = self._make_crypto_contract("BTC above $150K", "BTC", "price_target", event_id="e1")
        c2 = self._make_crypto_contract("SEC bans BTC", "BTC", "regulatory", event_id="e2")
        clusters = build_clusters([c1, c2])
        assert clusters[0].race is None
        assert clusters[0].state is None
```

Also update `_make_contract` helper if it doesn't accept `event_id` — check and adapt. The existing helper at line 34-48 uses `hash(title)` for event_id, which is fine for uniqueness. However the `_make_crypto_contract` helper above creates its own events with explicit event_ids to guarantee uniqueness.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_clustering.py::TestCryptoClustering -v`
Expected: FAIL — `build_clusters` only handles political contracts via `_normalize_race`.

- [ ] **Step 3: Implement crypto clustering**

In `src/political/clustering.py`, add a normalization function after `_FILLER_WORDS` (after line 37):

```python
def _normalize_crypto(crypto_asset: str) -> str:
    """Normalize crypto asset to a clustering key.

    Args:
        crypto_asset: Standard ticker (e.g. "BTC", "ETH").

    Returns:
        Normalized key string like "crypto-btc".
    """
    return f"crypto-{crypto_asset.lower()}"
```

Then modify `build_clusters()` to branch on contract_type. Replace lines 103-110 (the grouping loop):

```python
    # Group contracts by normalized key
    groups: dict[str, list[PoliticalContractInfo]] = defaultdict(list)

    for contract in contracts:
        if contract.contract_type == "crypto_event" and contract.crypto_asset:
            key = _normalize_crypto(contract.crypto_asset)
        elif contract.race is not None:
            key = _normalize_race(contract.race, contract.state)
        else:
            continue
        groups[key].append(contract)
```

And in the cluster-building loop, handle the case where `representative.race` is None for crypto clusters (the existing code already uses `representative.race` which will be None — this is correct for crypto clusters):

No change needed to lines 128-138 — the existing code already uses `representative.race` and `representative.state` which are None for crypto contracts.

The cluster_id format for crypto is `"crypto-btc-2026"` which comes from the key `"crypto-btc"` + `"-2026"` suffix at line 132.

- [ ] **Step 4: Run crypto clustering tests to verify they pass**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_clustering.py::TestCryptoClustering -v`
Expected: All PASS

- [ ] **Step 5: Run ALL clustering tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_clustering.py -v`
Expected: All existing tests PASS (political contracts still have `race != None` and go through `_normalize_race`).

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/clustering.py tests/test_political_clustering.py
git commit -m "feat: cluster crypto_event contracts by asset"
```

---

### Task 4: Add 4 Crypto Relationship Types

**Files:**
- Modify: `src/political/relationships.py`
- Test: `tests/test_political_relationships.py`

Add 4 new relationship types to `_classify_pair()`: `crypto_regulatory_hedge` (3.0x), `crypto_price_spread` (1.5x), `cross_crypto_correlation` (2.0x), `crypto_event_catalyst` (2.5x).

- [ ] **Step 1: Write the failing tests**

Add to the bottom of `tests/test_political_relationships.py`:

```python
def _make_crypto_info(title, crypto_asset, event_category, crypto_direction="positive",
                      crypto_threshold=None, platform="polymarket", event_id="ev1",
                      yes=0.50, no=0.50):
    """Helper for creating crypto_event PoliticalContractInfo."""
    ev = NormalizedEvent(
        platform=platform, event_id=event_id, title=title,
        category="crypto", yes_price=yes, no_price=no,
        volume=1000, expiry="2026-12-31", url=f"https://{platform}.com/{event_id}",
    )
    return PoliticalContractInfo(
        event=ev, contract_type="crypto_event",
        candidates=[], party=None, race=None, state=None,
        threshold=crypto_threshold, direction=None,
        crypto_asset=crypto_asset, event_category=event_category,
        crypto_direction=crypto_direction, crypto_threshold=crypto_threshold,
    )


class TestCryptoRelationships:
    """Tests for crypto-specific relationship types."""

    def test_crypto_regulatory_hedge(self):
        """price_target positive + regulatory negative → crypto_regulatory_hedge (3.0x)."""
        c1 = _make_crypto_info("BTC above $150K", "BTC", "price_target",
                               crypto_direction="positive", crypto_threshold=150000, event_id="e1")
        c2 = _make_crypto_info("SEC classifies BTC as security", "BTC", "regulatory",
                               crypto_direction="negative", event_id="e2")
        rels = detect_relationships([c1, c2])
        hedge_rels = [r for r in rels if r["type"] == "crypto_regulatory_hedge"]
        assert len(hedge_rels) == 1
        assert hedge_rels[0]["score"] == 3.0

    def test_crypto_price_spread(self):
        """Two price_target, same asset, different thresholds → crypto_price_spread (1.5x)."""
        c1 = _make_crypto_info("ETH above $5K", "ETH", "price_target",
                               crypto_threshold=5000, event_id="e1")
        c2 = _make_crypto_info("ETH above $3K", "ETH", "price_target",
                               crypto_threshold=3000, event_id="e2")
        rels = detect_relationships([c1, c2])
        spread_rels = [r for r in rels if r["type"] == "crypto_price_spread"]
        assert len(spread_rels) == 1
        assert spread_rels[0]["score"] == 1.5

    def test_cross_crypto_correlation(self):
        """Different assets, same event_category → cross_crypto_correlation (2.0x)."""
        c1 = _make_crypto_info("BTC above $150K", "BTC", "price_target",
                               crypto_threshold=150000, event_id="e1")
        c2 = _make_crypto_info("ETH above $5K", "ETH", "price_target",
                               crypto_threshold=5000, event_id="e2")
        rels = detect_relationships([c1, c2])
        cross_rels = [r for r in rels if r["type"] == "cross_crypto_correlation"]
        assert len(cross_rels) == 1
        assert cross_rels[0]["score"] == 2.0

    def test_crypto_event_catalyst(self):
        """regulatory/market_event + price_target, same asset → crypto_event_catalyst (2.5x)."""
        c1 = _make_crypto_info("ETH ETF approved", "ETH", "regulatory",
                               crypto_direction="positive", event_id="e1")
        c2 = _make_crypto_info("ETH above $7K", "ETH", "price_target",
                               crypto_threshold=7000, event_id="e2")
        rels = detect_relationships([c1, c2])
        cat_rels = [r for r in rels if r["type"] == "crypto_event_catalyst"]
        assert len(cat_rels) == 1
        assert cat_rels[0]["score"] == 2.5

    def test_same_threshold_no_price_spread(self):
        """Two price_target with identical thresholds → NO crypto_price_spread."""
        c1 = _make_crypto_info("BTC above $100K", "BTC", "price_target",
                               crypto_threshold=100000, event_id="e1")
        c2 = _make_crypto_info("BTC exceeds $100K", "BTC", "price_target",
                               crypto_threshold=100000, event_id="e2")
        rels = detect_relationships([c1, c2])
        spread_rels = [r for r in rels if r["type"] == "crypto_price_spread"]
        assert len(spread_rels) == 0

    def test_crypto_rels_dont_trigger_on_political(self):
        """Political contracts must NOT match crypto relationship types."""
        c1 = _make_info("A wins TX Senate", "candidate_win", candidates=["A"],
                        platform="polymarket", event_id="e1", yes=0.62)
        c2 = _make_info("B wins TX Senate", "candidate_win", candidates=["B"],
                        platform="polymarket", event_id="e2", yes=0.55)
        rels = detect_relationships([c1, c2])
        crypto_types = {"crypto_regulatory_hedge", "crypto_price_spread",
                        "cross_crypto_correlation", "crypto_event_catalyst"}
        for r in rels:
            assert r["type"] not in crypto_types
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_relationships.py::TestCryptoRelationships -v`
Expected: FAIL — no crypto relationship types in `_classify_pair`.

- [ ] **Step 3: Implement crypto relationship detection**

In `src/political/relationships.py`, add the 4 new types to `RELATIONSHIP_SCORES` (after line 12):

```python
RELATIONSHIP_SCORES = {
    "mispriced_correlation": 3.0,
    "candidate_party_link": 2.5,
    "margin_decomposition": 2.0,
    "conditional_hedge": 1.5,
    "bracket_spread": 1.5,
    "matchup_arbitrage": 2.0,
    # Crypto relationship types
    "crypto_regulatory_hedge": 3.0,
    "crypto_event_catalyst": 2.5,
    "cross_crypto_correlation": 2.0,
    "crypto_price_spread": 1.5,
}
```

Then add crypto checks to `_classify_pair()`, AFTER the existing 6 political checks (after the matchup_arbitrage block, before `return None` at line 121):

```python
    # --- Crypto relationship types ---
    # Only apply when both contracts are crypto_event
    if a.contract_type == "crypto_event" and b.contract_type == "crypto_event":

        # 7. crypto_regulatory_hedge: price_target+positive vs regulatory+negative, same asset
        if (a.crypto_asset and a.crypto_asset == b.crypto_asset):
            a_cat, b_cat = a.event_category, b.event_category

            # Check both orderings
            if ({a_cat, b_cat} == {"price_target", "regulatory"}):
                pt = a if a_cat == "price_target" else b
                rg = b if a_cat == "price_target" else a
                if pt.crypto_direction == "positive" and rg.crypto_direction == "negative":
                    return {
                        "type": "crypto_regulatory_hedge",
                        "pair": (idx_a, idx_b),
                        "score": RELATIONSHIP_SCORES["crypto_regulatory_hedge"],
                        "details": f"{a.crypto_asset} price target vs regulatory risk",
                    }

            # 8. crypto_event_catalyst: regulatory/technical + price_target, same asset
            if ("price_target" in {a_cat, b_cat}
                    and {a_cat, b_cat} & {"regulatory", "technical"}
                    and a_cat != b_cat):
                return {
                    "type": "crypto_event_catalyst",
                    "pair": (idx_a, idx_b),
                    "score": RELATIONSHIP_SCORES["crypto_event_catalyst"],
                    "details": f"{a.crypto_asset} event catalyst → price impact",
                }

            # 9. crypto_price_spread: both price_target, same asset, different thresholds
            if (a_cat == "price_target" and b_cat == "price_target"
                    and a.crypto_threshold is not None and b.crypto_threshold is not None
                    and a.crypto_threshold != b.crypto_threshold):
                return {
                    "type": "crypto_price_spread",
                    "pair": (idx_a, idx_b),
                    "score": RELATIONSHIP_SCORES["crypto_price_spread"],
                    "details": f"{a.crypto_asset} spread: ${a.crypto_threshold:,.0f} vs ${b.crypto_threshold:,.0f}",
                }

        # 10. cross_crypto_correlation: different assets, same event_category
        if (a.crypto_asset and b.crypto_asset
                and a.crypto_asset != b.crypto_asset
                and a.event_category == b.event_category):
            return {
                "type": "cross_crypto_correlation",
                "pair": (idx_a, idx_b),
                "score": RELATIONSHIP_SCORES["cross_crypto_correlation"],
                "details": f"{a.crypto_asset}/{b.crypto_asset} correlated {a.event_category}",
            }

    return None
```

**Important:** The crypto checks must be inside an `if a.contract_type == "crypto_event" and b.contract_type == "crypto_event"` guard so political contracts never match crypto types. The `return None` at the end stays as the final fallback.

- [ ] **Step 4: Run crypto relationship tests to verify they pass**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_relationships.py::TestCryptoRelationships -v`
Expected: All PASS

- [ ] **Step 5: Run ALL relationship tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_relationships.py -v`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/relationships.py tests/test_political_relationships.py
git commit -m "feat: add 4 crypto relationship types for synthetic hedging"
```

---

### Task 5: Extend LLM Prompt with Crypto Market Context

**Files:**
- Modify: `src/political/strategy.py`
- Test: `tests/test_political_strategy.py`

When a cluster contains `crypto_event` contracts, append a crypto market context section. Also guard `build_cluster_prompt()` against None race/state (crypto clusters have `race=None`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_political_strategy.py`:

```python
class TestCryptoPromptExtension:
    """Tests for crypto market context in LLM prompt."""

    def _make_crypto_cluster(self, contracts=None):
        """Build a crypto cluster with 2 BTC contracts."""
        from political.models import PoliticalCluster, PoliticalContractInfo
        from adapters.models import NormalizedEvent

        ev1 = NormalizedEvent(
            platform="polymarket", event_id="btc-150k", title="BTC above $150K",
            category="crypto", yes_price=0.35, no_price=0.65,
            volume=50000, expiry="2026-12-31", url="https://polymarket.com/btc150k",
        )
        ev2 = NormalizedEvent(
            platform="polymarket", event_id="btc-sec", title="SEC classifies BTC as security",
            category="crypto", yes_price=0.15, no_price=0.85,
            volume=30000, expiry="2026-12-31", url="https://polymarket.com/btcsec",
        )
        c1 = PoliticalContractInfo(
            event=ev1, contract_type="crypto_event",
            crypto_asset="BTC", event_category="price_target",
            crypto_direction="positive", crypto_threshold=150000.0,
        )
        c2 = PoliticalContractInfo(
            event=ev2, contract_type="crypto_event",
            crypto_asset="BTC", event_category="regulatory",
            crypto_direction="negative",
        )
        return PoliticalCluster(
            cluster_id="crypto-btc-2026", race=None, state=None,
            contracts=[c1, c2], matched_events=[ev1, ev2],
        )

    def _make_political_cluster(self):
        """Build a political cluster (for comparison)."""
        from political.models import PoliticalCluster, PoliticalContractInfo
        from adapters.models import NormalizedEvent

        ev1 = NormalizedEvent(
            platform="polymarket", event_id="tx-1", title="Talarico wins TX Senate",
            category="politics", yes_price=0.55, no_price=0.45,
            volume=10000, expiry="2026-11-03", url="https://polymarket.com/tx1",
        )
        c1 = PoliticalContractInfo(
            event=ev1, contract_type="candidate_win",
            candidates=["Talarico"], race="TX Senate", state="TX",
        )
        ev2 = NormalizedEvent(
            platform="polymarket", event_id="tx-2", title="Cruz wins TX Senate",
            category="politics", yes_price=0.40, no_price=0.60,
            volume=8000, expiry="2026-11-03", url="https://polymarket.com/tx2",
        )
        c2 = PoliticalContractInfo(
            event=ev2, contract_type="candidate_win",
            candidates=["Cruz"], race="TX Senate", state="TX",
        )
        return PoliticalCluster(
            cluster_id="senate-tx-2026", race="TX Senate", state="TX",
            contracts=[c1, c2], matched_events=[ev1, ev2],
        )

    def test_crypto_cluster_prompt_has_asset_header(self):
        """Crypto cluster prompt uses 'Asset: BTC' instead of 'Race: ...'."""
        cluster = self._make_crypto_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Asset: BTC" in prompt
        assert "Race:" not in prompt

    def test_crypto_cluster_prompt_has_context_block(self):
        """Crypto cluster prompt includes crypto market context section."""
        cluster = self._make_crypto_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Crypto Market Context" in prompt
        assert "Annualized volatility" in prompt
        assert "Regulatory events" in prompt

    def test_political_cluster_no_crypto_context(self):
        """Political cluster prompt does NOT include crypto context."""
        cluster = self._make_political_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Crypto Market Context" not in prompt
        assert "Race: TX Senate" in prompt

    def test_political_cluster_prompt_unchanged(self):
        """Political cluster prompt still works correctly."""
        cluster = self._make_political_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "CLUSTER:senate-tx-2026" in prompt
        assert "Race: TX Senate" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_strategy.py::TestCryptoPromptExtension -v`
Expected: FAIL — prompt crashes or doesn't include crypto context.

- [ ] **Step 3: Implement prompt extension**

In `src/political/strategy.py`, modify `build_cluster_prompt()`. **Before replacing**, diff the prompt template text against the current function to ensure no recent changes are lost. The only structural changes should be: (1) assign to `prompt` variable instead of returning directly, (2) add `is_crypto` header logic, (3) append `_build_crypto_context()` call:

```python
def build_cluster_prompt(cluster: PoliticalCluster, relationships: list[dict],
                         spot_prices: dict[str, float] | None = None) -> str:
    """Build LLM prompt for a single cluster (political or crypto)."""
    contracts_text = []
    for i, c in enumerate(cluster.contracts, 1):
        contracts_text.append(
            f'  {i}. "{c.event.title}" | {c.event.platform} | '
            f'YES=${c.event.yes_price:.2f} NO=${c.event.no_price:.2f}'
        )

    rels_text = []
    for r in relationships:
        a, b = r["pair"]
        rels_text.append(f"  - ({a+1},{b+1}): {r['type']} — {r['details']}")

    # Determine header: crypto uses asset name, political uses race/state
    is_crypto = cluster.cluster_id.startswith("crypto-")
    if is_crypto:
        # Extract asset from cluster_id (e.g., "crypto-btc-2026" → "BTC")
        asset = cluster.cluster_id.split("-")[1].upper() if "-" in cluster.cluster_id else "CRYPTO"
        header_line = f"Asset: {asset}"
    else:
        header_line = f"Race: {cluster.race} {cluster.state or ''}"

    prompt = f"""You are a political prediction market analyst. For each cluster below,
analyze the contracts and recommend optimal synthetic positions.

IMPORTANT: All expected value and P&L figures must be AFTER platform fees.
Fee rates (round-trip): Polymarket=2%, Kalshi=1.5%, PredictIt=10%, Limitless=2%.

[CLUSTER:{cluster.cluster_id}]
{header_line}
Contracts:
{chr(10).join(contracts_text)}

Pre-classified relationships:
{chr(10).join(rels_text) if rels_text else '  (none detected)'}

For each recommended position, respond with this exact JSON structure:
{{
  "strategies": [{{
    "strategy_name": "human-readable name",
    "legs": [{{"contract": 1, "side": "YES", "weight": 0.5}}],
    "scenarios": [{{"outcome": "description", "probability": 0.6, "pnl_pct": 12.5}}],
    "expected_value_pct": 8.2,
    "win_probability": 0.65,
    "max_loss_pct": -45.0,
    "confidence": "high",
    "reasoning": "explanation"
  }}]
}}

Respond with ONLY valid JSON. No preamble, no explanation outside the JSON."""

    # Append crypto market context for crypto clusters
    if is_crypto:
        prompt += _build_crypto_context(cluster, spot_prices=spot_prices)

    return prompt


def _build_crypto_context(cluster: PoliticalCluster,
                          spot_prices: dict[str, float] | None = None) -> str:
    """Build crypto market context block for the LLM prompt.

    Appends spot price data and strategy guidance for crypto clusters.
    Falls back gracefully if price data unavailable.

    Args:
        cluster: The crypto cluster being analyzed.
        spot_prices: Optional dict of {ticker: price} from the adapter cache.
    """
    # Collect relevant assets from cluster contracts
    assets = set()
    for c in cluster.contracts:
        if c.crypto_asset:
            assets.add(c.crypto_asset)

    # Build price lines from spot_prices if available
    price_lines = []
    for asset in sorted(assets):
        if spot_prices and asset in spot_prices:
            price_lines.append(f"- {asset}: ${spot_prices[asset]:,.2f}")
        else:
            price_lines.append(f"- {asset}: (price unavailable)")

    return f"""

## Crypto Market Context
Current spot prices:
{chr(10).join(price_lines)}

Annualized volatility: ~60% (crypto-wide assumption)

Strategy guidance for crypto contracts:
- Regulatory events (SEC, CFTC) typically cause 10-30% drawdowns on negative resolution
- Price target contracts have implied probability based on distance from current price
- Hedge legs should offset directional risk of other legs
- Prefer strategies where at least one scenario is profitable even if crypto drops 20%"""
```

- [ ] **Step 4: Run crypto prompt tests to verify they pass**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_strategy.py::TestCryptoPromptExtension -v`
Expected: All PASS

- [ ] **Step 5: Run ALL strategy tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_strategy.py -v`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/strategy.py tests/test_political_strategy.py
git commit -m "feat: add crypto market context to LLM prompt for crypto clusters"
```

---

### Task 6: Extend Analyzer Filter for Crypto Events

**Files:**
- Modify: `src/political/analyzer.py`

The analyzer currently filters events to `category == "politics"`. Extend to also include `category == "crypto"` and events where the title matches crypto keywords (via `_is_crypto_relevant()`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_political_analyzer_crypto.py`:

```python
"""Tests for crypto event filtering in PoliticalAnalyzer."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent
from political.classifier import classify_contract
from political.analyzer import _is_crypto_relevant


class TestCryptoRelevanceFilter:
    """Tests for _is_crypto_relevant helper."""

    def test_bitcoin_relevant(self):
        assert _is_crypto_relevant("Will Bitcoin reach $100K?") is True

    def test_btc_relevant(self):
        assert _is_crypto_relevant("BTC above $150,000") is True

    def test_ethereum_etf_relevant(self):
        assert _is_crypto_relevant("SEC approves Ethereum ETF") is True

    def test_pure_political_not_relevant(self):
        assert _is_crypto_relevant("Talarico wins TX Senate") is False

    def test_generic_crypto_not_relevant(self):
        """Generic 'crypto' without specific asset is NOT relevant."""
        assert _is_crypto_relevant("Will crypto be discussed?") is False

    def test_congress_ban_bitcoin_relevant(self):
        """Cross-category: political title with crypto content."""
        assert _is_crypto_relevant("Will Congress ban Bitcoin?") is True


class TestAnalyzerCryptoFiltering:
    """Tests that crypto events flow through the classifier."""

    def test_crypto_category_event_classified(self):
        """Events with category='crypto' get classified."""
        ev = NormalizedEvent(
            platform="polymarket", event_id="btc-test",
            title="Will Bitcoin be above $150,000 by 2026?",
            category="crypto", yes_price=0.35, no_price=0.65,
            volume=50000, expiry="2026-12-31", url="https://polymarket.com/test",
        )
        info = classify_contract(ev)
        assert info.contract_type == "crypto_event"

    def test_politics_category_crypto_content_classified(self):
        """Political event with crypto content gets classified as crypto_event."""
        ev = NormalizedEvent(
            platform="polymarket", event_id="congress-btc",
            title="Will Congress ban Bitcoin?",
            category="politics", yes_price=0.10, no_price=0.90,
            volume=20000, expiry="2026-12-31", url="https://polymarket.com/test",
        )
        info = classify_contract(ev)
        assert info.contract_type == "crypto_event"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_analyzer_crypto.py -v`
Expected: FAIL — `_is_crypto_relevant` doesn't exist yet.

- [ ] **Step 3: Implement analyzer filter extension**

In `src/political/analyzer.py`, add the `_is_crypto_relevant` function near the top (after imports, before the class):

```python
import re

# Fast crypto relevance check — matches specific crypto asset names in titles
_CRYPTO_RELEVANCE_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|ether|solana|sol|xrp|ripple|"
    r"dogecoin|doge|cardano|ada|avalanche|avax|chainlink|link|"
    r"polkadot|dot|polygon|pol)\b",
    re.IGNORECASE,
)


def _is_crypto_relevant(title: str) -> bool:
    """Fast check if a title contains specific crypto asset names.

    Returns True only for specific asset names (BTC, ETH, etc.),
    NOT for generic 'crypto'/'cryptocurrency'.
    """
    return bool(_CRYPTO_RELEVANCE_RE.search(title))
```

Then modify `_analyze_cycle()` to widen the event filter. Replace the filter block (lines 78-94):

```python
        # Filter to political and crypto-relevant events
        from adapters.models import NormalizedEvent
        filtered_events = []
        for ev_dict in all_events_raw:
            markets = ev_dict.get("markets", [])
            for m in markets:
                category = m.get("category", "")
                title = m.get("title", "")
                if category == "politics" or category == "crypto" or _is_crypto_relevant(title):
                    try:
                        ne = NormalizedEvent(
                            platform=m["platform"], event_id=m["event_id"],
                            title=title, category=category,
                            yes_price=m["yes_price"], no_price=m["no_price"],
                            volume=m.get("volume", 0),
                            expiry=m.get("expiry", "ongoing"),
                            url=m.get("url", ""),
                            last_updated=m.get("last_updated", ""),
                        )
                        filtered_events.append(ne)
                    except (KeyError, TypeError):
                        continue

        if not filtered_events:
            logger.debug("Political analyzer: no political/crypto events found")
            return
```

Also rename `political_events` → `filtered_events` in the rest of the method. Exact changes:
- Line 96: `if not political_events:` → `if not filtered_events:`
- Line 97: `logger.debug("Political analyzer: no political events found")` → `logger.debug("Political analyzer: no political/crypto events found")`
- Line 101: `classified = [classify_contract(ev) for ev in political_events]` → `classified = [classify_contract(ev) for ev in filtered_events]`
- Line 107: `logger.debug("Political analyzer: no clusters formed from %d events", len(political_events))` → `logger.debug("Political analyzer: no clusters formed from %d events", len(filtered_events))`
- Line 110-111: `logger.info("Political analyzer: %d political events → %d clusters", len(political_events), len(clusters))` → `logger.info("Political analyzer: %d political/crypto events → %d clusters", len(filtered_events), len(clusters))`

- [ ] **Step 4: Run crypto analyzer tests to verify they pass**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_analyzer_crypto.py -v`
Expected: All PASS

- [ ] **Step 5: Run ALL political tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/ -k "political" -v --timeout=30`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/analyzer.py tests/test_political_analyzer_crypto.py
git commit -m "feat: widen analyzer filter to include crypto events"
```

---

### Task 7: Add crypto_synthetic Auto-Trader Handler and Fix to_dict()

**Files:**
- Modify: `src/political/models.py:168-169`
- Modify: `src/positions/auto_trader.py` (add handler after political_synthetic block)
- Test: `tests/test_political_classifier.py` (add to_dict test)

Two changes: (1) `PoliticalOpportunity.to_dict()` must emit `"crypto_synthetic"` when cluster_id starts with `"crypto-"`. (2) Auto-trader needs a `crypto_synthetic` handler block mirroring `political_synthetic`.

- [ ] **Step 1: Write the failing test for to_dict**

Add to `tests/test_political_classifier.py`:

```python
class TestCryptoOpportunityType:
    """Tests for PoliticalOpportunity.to_dict() type awareness."""

    def test_political_opportunity_type(self):
        """Political cluster → opportunity_type 'political_synthetic'."""
        from political.models import (
            PoliticalOpportunity, PoliticalSyntheticStrategy,
            PoliticalLeg, SyntheticLeg, Scenario,
        )
        event = _make_event("Talarico wins TX Senate")
        info = PoliticalContractInfo(
            event=event, contract_type="candidate_win",
            candidates=["Talarico"], race="TX Senate", state="TX",
        )
        strategy = PoliticalSyntheticStrategy(
            cluster_id="senate-tx-2026", strategy_name="Test",
            legs=[SyntheticLeg(contract_idx=1, event_id=event.event_id, side="yes", weight=1.0)],
            scenarios=[Scenario(outcome="Win", probability=0.6, pnl_pct=10.0)],
            expected_value_pct=6.0, win_probability=0.6,
            max_loss_pct=-40.0, confidence=0.7,
        )
        leg = PoliticalLeg(event=event, contract_info=info, side="yes", weight=1.0, platform_fee_pct=2.0)
        opp = PoliticalOpportunity(
            cluster_id="senate-tx-2026", strategy=strategy,
            legs=[leg], total_fee_pct=2.0, net_expected_value_pct=4.0,
            platforms=["polymarket"],
        )
        d = opp.to_dict()
        assert d["opportunity_type"] == "political_synthetic"

    def test_crypto_opportunity_type(self):
        """Crypto cluster → opportunity_type 'crypto_synthetic'."""
        from political.models import (
            PoliticalOpportunity, PoliticalSyntheticStrategy,
            PoliticalLeg, SyntheticLeg, Scenario,
        )
        event = _make_event("BTC above $150K", platform="polymarket")
        info = PoliticalContractInfo(
            event=event, contract_type="crypto_event",
            crypto_asset="BTC", event_category="price_target",
            crypto_direction="positive", crypto_threshold=150000.0,
        )
        strategy = PoliticalSyntheticStrategy(
            cluster_id="crypto-btc-2026", strategy_name="BTC Hedge",
            legs=[SyntheticLeg(contract_idx=1, event_id=event.event_id, side="yes", weight=1.0)],
            scenarios=[Scenario(outcome="BTC up", probability=0.5, pnl_pct=15.0)],
            expected_value_pct=7.0, win_probability=0.5,
            max_loss_pct=-45.0, confidence=0.65,
        )
        leg = PoliticalLeg(event=event, contract_info=info, side="yes", weight=1.0, platform_fee_pct=2.0)
        opp = PoliticalOpportunity(
            cluster_id="crypto-btc-2026", strategy=strategy,
            legs=[leg], total_fee_pct=2.0, net_expected_value_pct=5.0,
            platforms=["polymarket"],
        )
        d = opp.to_dict()
        assert d["opportunity_type"] == "crypto_synthetic"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestCryptoOpportunityType -v`
Expected: FAIL — `to_dict()` always returns `"political_synthetic"`.

- [ ] **Step 3: Fix PoliticalOpportunity.to_dict()**

In `src/political/models.py`, replace line 169:

Old:
```python
            "opportunity_type": "political_synthetic",
```

New:
```python
            "opportunity_type": "crypto_synthetic" if self.cluster_id.startswith("crypto-") else "political_synthetic",
```

- [ ] **Step 4: Run to_dict tests to verify they pass**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_political_classifier.py::TestCryptoOpportunityType -v`
Expected: PASS

- [ ] **Step 5: Add crypto_synthetic handler to auto_trader.py**

In `src/positions/auto_trader.py`, add a `crypto_synthetic` handler block immediately after the `political_synthetic` block (after the `continue` on line 820). This is a standalone `if` block, NOT an `elif`:

```python
            # Crypto synthetic: same structure as political_synthetic, different exit rules
            if opp.get("opportunity_type") == "crypto_synthetic":
                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "crypto_synthetic")
                except ValueError:
                    continue

                opp_legs = opp.get("legs", [])
                if not opp_legs:
                    continue

                for opp_leg in opp_legs:
                    leg_cost = round(trade_size * opp_leg.get("weight", 1.0 / len(opp_legs)), 2)
                    leg_cost = max(MIN_TRADE_SIZE, leg_cost)
                    side = opp_leg.get("side", "YES")
                    leg_type = "prediction_yes" if side == "YES" else "prediction_no"
                    price = opp_leg.get("yes_price", 0.5) if side == "YES" else opp_leg.get("no_price", 0.5)
                    pkg["legs"].append(create_leg(
                        platform=opp_leg.get("platform", "polymarket"),
                        leg_type=leg_type,
                        asset_id=f"{opp_leg['event_id']}:{side}",
                        asset_label=f"{side} @ {opp_leg.get('platform', '?')}: {opp_leg.get('title', '?')[:40]}",
                        entry_price=price if price > 0 else 0.5,
                        cost=leg_cost,
                        expiry=opp.get("expiry", "2026-12-31")[:10],
                    ))

                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 50}))
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -40}))
                pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"current": 35, "bound_min": 15, "bound_max": 50}))
                pkg["_use_brackets"] = True

                if not pkg["legs"]:
                    self._trades_skipped += 1
                    continue

                pkg["_use_limit_orders"] = True
                pkg_name = pkg.get("name", opp_title)
                bet_side = "CRYPTO"
                bet_conviction = 0.0
                entry_price = 0.5
                try:
                    result = await self.pm.execute_package(pkg)
                    if result.get("success"):
                        trades_this_cycle += 1
                        self._trades_opened += 1
                        self._daily_trade_count += 1
                        remaining_budget -= trade_size
                        for leg in pkg.get("legs", []):
                            cid = leg.get("asset_id", "").split(":")[0]
                            if cid:
                                open_market_ids.add(cid)
                        logger.info("Auto trader OPENED crypto synthetic: %s (ev=%.1f%%, size=$%.2f)",
                                    pkg_name, spread_pct, trade_size)
                        if self.dlog:
                            self.dlog.log_trade_opened(
                                pkg_id=pkg.get("id", ""), title=pkg_name,
                                strategy="crypto_synthetic",
                                side=bet_side, price=entry_price,
                                size=trade_size, score=score, spread_pct=spread_pct,
                                conviction=bet_conviction,
                                days_to_expiry=days_to_expiry, volume=opp.get("volume", 0))
                except Exception as e:
                    logger.warning("Auto trader: crypto trade failed: %s", e)
                    if self.dlog:
                        self.dlog.log_trade_failed(opp_title, str(e))
                continue
```

Key differences from political_synthetic:
- `pkg["_use_brackets"] = True` — uses bracket orders for 0% maker exits
- Does NOT set `_hold_to_resolution`
- Strategy type is `"crypto_synthetic"` in logging
- `bet_side = "CRYPTO"` instead of `"POLITICAL"`

- [ ] **Step 6: Run ALL tests to confirm no regression**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/ -v --timeout=30`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add src/political/models.py src/positions/auto_trader.py tests/test_political_classifier.py
git commit -m "feat: add crypto_synthetic auto-trader handler and type-aware to_dict()"
```

---

### Task 8: Integration Test — Full Pipeline

**Files:**
- Create: `tests/test_crypto_synthetic_integration.py`

End-to-end test: feed a mix of political + crypto events through classify → cluster → relationships → prompt. Verify crypto clusters produce correct structure.

- [ ] **Step 1: Write the integration test**

Create `tests/test_crypto_synthetic_integration.py`:

```python
"""Integration test for the crypto synthetic hedging pipeline."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent
from political.classifier import classify_contract
from political.clustering import build_clusters
from political.relationships import detect_relationships, build_leg_combinations
from political.strategy import build_cluster_prompt


def _ev(title, platform="polymarket", event_id=None, category="crypto",
        yes=0.50, no=0.50):
    return NormalizedEvent(
        platform=platform, event_id=event_id or f"ev-{hash(title) % 10000}",
        title=title, category=category, yes_price=yes, no_price=no,
        volume=10000, expiry="2026-12-31",
        url=f"https://{platform}.com/test",
    )


class TestCryptoSyntheticPipeline:
    """Full pipeline integration test."""

    def test_classify_cluster_relate_prompt(self):
        """Crypto events flow through classify → cluster → relate → prompt."""
        events = [
            _ev("Will Bitcoin be above $150,000 by end of 2026?", event_id="btc-150k", yes=0.35),
            _ev("Will SEC classify Bitcoin as a security?", event_id="btc-sec", yes=0.15),
            _ev("Will Ethereum reach $5,000?", event_id="eth-5k", yes=0.55),
            _ev("Will Ethereum ETF be approved by SEC?", event_id="eth-etf", yes=0.65),
        ]

        # Classify
        classified = [classify_contract(ev) for ev in events]
        assert all(c.contract_type == "crypto_event" for c in classified)

        # Cluster
        clusters = build_clusters(classified)
        cluster_ids = {c.cluster_id for c in clusters}
        assert "crypto-btc-2026" in cluster_ids
        assert "crypto-eth-2026" in cluster_ids

        # Relationships within BTC cluster
        btc_cluster = next(c for c in clusters if c.cluster_id == "crypto-btc-2026")
        btc_rels = detect_relationships(btc_cluster.contracts)
        assert len(btc_rels) > 0
        btc_rel_types = {r["type"] for r in btc_rels}
        assert "crypto_regulatory_hedge" in btc_rel_types

        # Relationships within ETH cluster
        eth_cluster = next(c for c in clusters if c.cluster_id == "crypto-eth-2026")
        eth_rels = detect_relationships(eth_cluster.contracts)
        assert len(eth_rels) > 0
        eth_rel_types = {r["type"] for r in eth_rels}
        assert "crypto_event_catalyst" in eth_rel_types

        # Build prompt for BTC cluster
        btc_combos = build_leg_combinations(btc_cluster.contracts, btc_rels)
        prompt = build_cluster_prompt(btc_cluster, btc_rels)
        assert "Asset: BTC" in prompt
        assert "Crypto Market Context" in prompt
        assert "crypto_regulatory_hedge" in prompt

    def test_mixed_political_crypto(self):
        """Political and crypto events produce separate cluster types."""
        events = [
            _ev("Talarico wins TX Senate", category="politics", event_id="pol1", yes=0.55),
            _ev("Cruz wins TX Senate", category="politics", event_id="pol2", yes=0.40),
            _ev("Will Bitcoin be above $150,000?", event_id="btc1", yes=0.35),
            _ev("Will SEC classify Bitcoin as a security?", event_id="btc2", yes=0.15),
        ]

        classified = [classify_contract(ev) for ev in events]

        # Political events should NOT be classified as crypto
        assert classified[0].contract_type in ("candidate_win", "yes_no_binary")
        assert classified[1].contract_type in ("candidate_win", "yes_no_binary")
        # Crypto events should be classified as crypto_event
        assert classified[2].contract_type == "crypto_event"
        assert classified[3].contract_type == "crypto_event"

        clusters = build_clusters(classified)
        crypto_clusters = [c for c in clusters if c.cluster_id.startswith("crypto-")]
        political_clusters = [c for c in clusters if not c.cluster_id.startswith("crypto-")]

        assert len(crypto_clusters) >= 1
        # Political may or may not cluster depending on normalization

    def test_opportunity_dict_type(self):
        """Crypto opportunity to_dict() returns crypto_synthetic type."""
        from political.models import (
            PoliticalOpportunity, PoliticalSyntheticStrategy,
            PoliticalLeg, PoliticalContractInfo, SyntheticLeg, Scenario,
        )
        ev = _ev("BTC above $150K", event_id="btc-150k", yes=0.35)
        info = PoliticalContractInfo(
            event=ev, contract_type="crypto_event",
            crypto_asset="BTC", event_category="price_target",
            crypto_direction="positive", crypto_threshold=150000.0,
        )
        strategy = PoliticalSyntheticStrategy(
            cluster_id="crypto-btc-2026", strategy_name="BTC Regulatory Hedge",
            legs=[SyntheticLeg(contract_idx=1, event_id="btc-150k", side="yes", weight=1.0)],
            scenarios=[Scenario(outcome="BTC rallies", probability=0.5, pnl_pct=20.0)],
            expected_value_pct=8.0, win_probability=0.55,
            max_loss_pct=-40.0, confidence=0.7,
        )
        leg = PoliticalLeg(event=ev, contract_info=info, side="yes", weight=1.0, platform_fee_pct=2.0)
        opp = PoliticalOpportunity(
            cluster_id="crypto-btc-2026", strategy=strategy,
            legs=[leg], total_fee_pct=2.0, net_expected_value_pct=6.0,
            platforms=["polymarket"],
        )
        d = opp.to_dict()
        assert d["opportunity_type"] == "crypto_synthetic"
        assert d["is_synthetic"] is True
        assert d["cluster_id"] == "crypto-btc-2026"
```

- [ ] **Step 2: Run integration tests**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/test_crypto_synthetic_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite**

Run: `cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout" && python -m pytest tests/ -v --timeout=30`
Expected: All tests PASS across all test files.

- [ ] **Step 4: Commit**

```bash
cd "C:/Users/afoma/.openclaw/workspace/projects/arbitrout"
git add tests/test_crypto_synthetic_integration.py
git commit -m "test: add integration tests for crypto synthetic hedging pipeline"
```

---

## File Summary

| File | Action | What Changes |
|---|---|---|
| `src/political/models.py` | Modify | Add 4 optional crypto fields to `PoliticalContractInfo`, fix `to_dict()` type |
| `src/political/classifier.py` | Modify | Add `crypto_event` as 7th type with regex detection |
| `src/political/clustering.py` | Modify | Add `_normalize_crypto()`, branch in `build_clusters()` |
| `src/political/relationships.py` | Modify | Add 4 crypto relationship types to scores dict and `_classify_pair()` |
| `src/political/strategy.py` | Modify | Guard None race/state, append crypto context block |
| `src/political/analyzer.py` | Modify | Widen filter to `politics OR crypto OR _is_crypto_relevant()` |
| `src/positions/auto_trader.py` | Modify | Add `crypto_synthetic` handler block (standalone, mirrors political) |
| `tests/test_political_classifier.py` | Modify | Add crypto model, classifier, and to_dict tests |
| `tests/test_political_clustering.py` | Modify | Add crypto clustering tests |
| `tests/test_political_relationships.py` | Modify | Add crypto relationship type tests |
| `tests/test_political_strategy.py` | Modify | Add crypto prompt tests |
| `tests/test_political_analyzer_crypto.py` | Create | Analyzer crypto filter tests |
| `tests/test_crypto_synthetic_integration.py` | Create | Full pipeline integration test |
