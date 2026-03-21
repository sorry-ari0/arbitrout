# Crypto Synthetic Hedging — Design Spec

## Goal

Extend the existing political synthetic derivatives pipeline to discover and construct multi-leg strategies that include crypto-relevant prediction market contracts as hedge legs. When the system identifies crypto-correlated contracts (SEC rulings, ETF approvals, price targets, hacks), it builds strategies where legs offset each other's directional risk — turning isolated bets into hedged positions.

## Approach

Extend the political pipeline (Approach 1). Add `crypto_event` as a 7th classifier type, 4 new relationship types, and crypto market context to the LLM strategy prompt. No new analyzer module — crypto contracts flow through the same classify → cluster → relate → LLM → validate → execute pipeline.

## Architecture

No new modules. All changes are extensions to existing files in `src/political/`.

### Data Flow

```
AdapterRegistry.fetch_all() → NormalizedEvent[]
        ↓
PoliticalAnalyzer.analyze_clusters()
        ↓ (filter: category == "politics" OR crypto keywords match)
        ↓
classify_contract()
        ↓ (7 types: 6 political + crypto_event)
        ↓
build_clusters()
        ↓ (political: group by race+state | crypto: group by asset)
        ↓
detect_relationships()
        ↓ (10 types: 6 political + 4 crypto)
        ↓
build_leg_combinations()
        ↓ (greedy from best pair, 2-4 legs max)
        ↓
LLM strategy generation
        ↓ (existing prompt + crypto market context block for crypto clusters)
        ↓
validate_strategy() → same gates (EV≥3%, win_prob≥50%, max_loss≥-60%)
        ↓
PoliticalOpportunity → AutoTrader (strategy_type: "crypto_synthetic")
```

## Components

### 1. Classifier Extension (`classifier.py`)

Add `crypto_event` as the 7th classification type, checked at priority position 6 (before the `yes_no_binary` fallback at position 7).

**Detection patterns:**
- Crypto asset names: `bitcoin|btc|ethereum|eth|solana|sol|xrp|ripple|dogecoin|doge|crypto|cryptocurrency`
- Regulatory keywords: `sec|cftc|regulation|classify|security|securities|etf|ban|restrict`
- Price target patterns: `above|below|reach|hit|exceed` + `\$[\d,]+`
- Event keywords: `hack|exploit|upgrade|fork|halving|merge|approved|rejected`

A contract matches `crypto_event` if it contains at least one crypto asset name AND at least one keyword from any other category (regulatory, price, event). Pure mentions of "crypto" without actionable context do not match.

**Extracted fields:**
```python
{
    "contract_type": "crypto_event",
    "crypto_asset": "BTC" | "ETH" | "SOL" | "XRP" | "DOGE" | "ADA" | "AVAX" | "LINK" | "DOT" | "POL",
    "event_category": "regulatory" | "price_target" | "technical" | "market_event",
    "direction": "positive" | "negative" | "neutral",
    "threshold": float | None,  # dollar value for price_target contracts
}
```

**Asset normalization:** Map all variants to standard tickers. "Bitcoin" → "BTC", "Ethereum" / "Ether" → "ETH", "Solana" → "SOL", etc. Use the same `CRYPTO_MAP` keys from `adapters/crypto_spot.py`.

**Event category rules:**
- `regulatory`: matches SEC, CFTC, regulation, classify, security, ban, restrict, ETF (when paired with approve/reject)
- `price_target`: matches price threshold patterns ($100K, $5,000, etc.)
- `technical`: matches hack, exploit, upgrade, fork, halving, merge
- `market_event`: fallback for crypto contracts that don't fit above categories

**Direction inference:**
- `positive`: ETF approved, price above X, upgrade complete
- `negative`: SEC classifies as security, ban, hack, exploit
- `neutral`: ambiguous or no clear directional impact

### 2. Clustering Extension (`clustering.py`)

When `contract_type == "crypto_event"`, cluster by crypto asset instead of race+state.

**Normalization function:** `_normalize_crypto(crypto_asset)` → `"crypto-btc"`, `"crypto-eth"`, `"crypto-sol"`

**Cluster ID format:** `"crypto-{asset}-2026"` (parallels `"{race}-{state}-2026"`)

**Minimum cluster size:** 2 contracts (same as political). Singletons are discarded.

**Mixed clusters:** All BTC-related contracts land in `"crypto-btc"` regardless of event_category. This is intentional — the relationship detector pairs regulatory + price_target contracts within the same cluster to build hedge strategies.

**Implementation:** In `build_clusters()`, check `contract.contract_type`. If `crypto_event`, use `_normalize_crypto(contract.crypto_asset)` as the grouping key. Otherwise use existing `_normalize_race(race, state)`.

### 3. Four New Relationship Types (`relationships.py`)

Added to the existing `detect_relationships()` function. Each checks a pair of contracts within a cluster.

| Relationship | Multiplier | Condition | Example |
|---|---|---|---|
| `crypto_regulatory_hedge` | 3.0x | One is `price_target` + `direction: positive`, other is `regulatory` + `direction: negative` | "BTC above $150K" + "SEC classifies BTC as security" |
| `crypto_price_spread` | 1.5x | Both `price_target`, same `crypto_asset`, different thresholds | "ETH above $5K" + "ETH above $3K" |
| `cross_crypto_correlation` | 2.0x | Different `crypto_asset`, same `event_category` | "BTC above $150K" + "ETH above $5K" |
| `crypto_event_catalyst` | 2.5x | One is `regulatory` or `market_event`, other is `price_target`, same `crypto_asset` | "ETH ETF approved" + "ETH above $7K" |

**Minimum relationship score:** 1.5 (same as political). Relationships below this are filtered.

**Leg combination building:** Same greedy algorithm — start with highest-scored pair, extend to 3-4 legs max by adding relationships that introduce new contracts.

### 4. LLM Prompt Extension (`strategy.py`)

When a cluster contains `crypto_event` contracts, append a crypto market context section to the existing `build_cluster_prompt()` output.

**Appended block:**
```
## Crypto Market Context
Current spot prices (real-time):
- BTC: $97,450 (24h change: -2.3%)
- ETH: $3,820 (24h change: -1.8%)
[only assets relevant to this cluster]

Annualized volatility: ~60% (crypto-wide assumption)

Strategy guidance for crypto contracts:
- Regulatory events (SEC, CFTC) typically cause 10-30% drawdowns on negative resolution
- Price target contracts have implied probability based on distance from current price
- Hedge legs should offset directional risk of other legs
- Prefer strategies where at least one scenario is profitable even if crypto drops 20%
```

**Data source:** CoinGecko spot prices via `adapters/crypto_spot.py` (already fetched by adapter registry every 60s). 24h change from same API.

**No changes to response format.** The LLM returns the same JSON: strategy_name, legs[], scenarios[], expected_value_pct, win_probability, max_loss_pct, confidence, reasoning.

**No second LLM call.** Same prompt template, same provider chain (Groq → Gemini → OpenRouter).

**Handle None race/state for crypto clusters:** `build_cluster_prompt()` currently embeds `cluster.race` and `cluster.state` into the prompt string. For crypto clusters these are None. Guard with: if cluster_id starts with `"crypto-"`, replace the race/state header with the crypto asset name (e.g., "Asset: BTC" instead of "Race: TX Senate").

### 5. Analyzer Filter Extension (`analyzer.py`)

**Current:** `analyze_clusters()` filters events to `category == "politics"`.

**Extension:** Include events that are politically tagged with crypto content AND events from the crypto adapter. The filter becomes:
```python
if event.category == "politics" or event.category == "crypto" or _is_crypto_relevant(event.title):
    contracts.append(classify_contract(event))
```

Three paths into the classifier:
1. `category == "politics"` — existing political contracts (unchanged)
2. `category == "crypto"` — contracts from CryptoSpotAdapter (already tagged as crypto)
3. `_is_crypto_relevant(title)` — catches crypto contracts mis-categorized under other categories (e.g., "politics" tagged "Will Congress ban Bitcoin?")

`_is_crypto_relevant(title)` checks for the same crypto asset names used by the classifier. This is a fast string check, not an LLM call.

**Cache:** Same SHA-256 keyed cache with 15-min TTL and 3% price shift invalidation. Crypto clusters get their own cache entries.

### 6. Models Extension (`models.py`)

Add optional fields to `PoliticalContractInfo`:
```python
crypto_asset: str | None = None      # "BTC", "ETH", etc.
event_category: str | None = None    # "regulatory", "price_target", "technical", "market_event"
crypto_direction: str | None = None  # "positive", "negative", "neutral"
crypto_threshold: float | None = None # dollar value for price targets
```

These are None for political contracts, populated for crypto_event contracts.

### 7. Auto-Trader Recognition (`auto_trader.py`)

Add `"crypto_synthetic"` as a recognized strategy type (12th type). Implementation mirrors the `political_synthetic` handler — a standalone `if opportunity_type == "crypto_synthetic"` block (NOT in the general strategy branching section). This block:
- Sets same exit rules as political_synthetic: target_profit, stop_loss, trailing_stop (inline, same pattern as political_synthetic at lines 778-780)
- Does NOT set `_hold_to_resolution` (unlike cross-platform arbs)
- Sets `_use_brackets = True` (uses the bracket orders system for 0% maker exits)
- Then `continue`s before the general strategy branching, same as political_synthetic

Scoring formula unchanged: `net_EV × confidence_mult × platform_mult`. The EV already reflects hedge quality from the LLM strategy.

**`PoliticalOpportunity.to_dict()` fix:** The `to_dict()` method currently hardcodes `"opportunity_type": "political_synthetic"`. It must become type-aware: emit `"crypto_synthetic"` when the cluster_id starts with `"crypto-"`. Otherwise the auto-trader's type check will never match.

## Error Handling

- **No crypto contracts found:** Pipeline silently produces zero crypto clusters. No errors, no empty work.
- **CoinGecko price fetch fails:** LLM prompt omits the crypto context block. Strategy still generated but without spot price guidance. Log a warning.
- **Classifier ambiguity:** If a contract matches both political and crypto patterns (e.g., "Will Congress ban Bitcoin?"), crypto_event takes priority (checked first). The contract lands in a crypto cluster.
- **LLM generates invalid strategy:** Same validation gates reject it (EV < 3%, win_prob < 50%, max_loss < -60%). Logged and discarded.

## Testing

- **Classifier tests:** Verify crypto_event detection for regulatory, price_target, technical, market_event contracts. Verify non-crypto contracts don't match. Verify asset normalization.
- **Clustering tests:** Verify crypto contracts cluster by asset. Verify mixed political+crypto events produce separate clusters.
- **Relationship tests:** Verify all 4 new types detect correct pairs. Verify multipliers. Verify cross-cluster relationships don't form.
- **Integration test:** Feed a mix of political + crypto events through the full pipeline. Verify crypto clusters produce opportunities with correct strategy_type.
- **LLM prompt test:** Verify crypto context block appears for crypto clusters, absent for political clusters.

## What This Does NOT Include

- External portfolio monitoring (no wallet sync, no config file for holdings)
- Separate hedge budget or capital allocation
- New adapters or data sources
- Changes to exit engine triggers
- Portfolio-level correlation analysis
- A separate CryptoAnalyzer module
