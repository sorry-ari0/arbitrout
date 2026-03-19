# Political Synthetic Derivative Analysis — Design Spec

> **Status:** Approved design, pending implementation plan
> **Date:** 2026-03-19
> **Scope:** New module for analyzing clusters of related political prediction market contracts across platforms to find optimal multi-leg synthetic positions

---

## Goal

Add AI-driven political synthetic derivative analysis to Arbitrout. The system classifies political contracts by type using rules, detects relationships between contracts in a cluster, then uses the LLM to recommend optimal 2-4 leg positions that exploit mispriced correlations and conditional hedges.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Analysis approach | Hybrid: rules for classification, LLM for strategy | Rules are fast/cheap for structured extraction; LLM handles nuanced probability reasoning |
| Primary edge | Both mispriced correlations (priority) and conditional hedging | Mispriced correlations are closest to pure arb; hedges add higher-reward opportunities |
| Cache TTL | 15 minutes | Political markets move slowly but election news can shift prices fast |
| Platform scope | Single-platform and cross-platform equally | Maximizes opportunity count; cross-platform gets score boost from mispricing edge |
| Max legs | 4, AI decides optimal count | Full cluster exploitation; LLM determines when fewer legs are better |
| Evaluation | Log all actions taken AND not taken | Enables strategy iteration and confidence calibration |

---

## Section 1: Contract Classification

### Module: `src/political_analyzer.py`

A rule-based classifier that tags each `NormalizedEvent` in a political cluster with a contract type and extracted parameters.

### Contract Types

| Type | Pattern | Extracted Params | Example |
|------|---------|-----------------|---------|
| `candidate_win` | "{Name} wins {Race}" | candidate, race, state | "Talarico wins TX Senate" |
| `party_outcome` | "{Party} wins/holds {Race}" | party, race, state | "Democratic candidate wins TX Senate" |
| `margin_bracket` | "wins by >{N}%" or "margin" | candidate/party, threshold, direction | "Talarico wins by >5%" |
| `vote_share` | "{Party/Name} gets >{N}%" | entity, threshold | "Dem gets >48% in TX Senate" |
| `matchup` | "{Name} vs {Name}" | candidates[], race | "Talarico vs Cruz" |
| `yes_no_binary` | Generic political yes/no | topic, keywords | "Will TX have a runoff?" |

### Data Structure

```python
@dataclass
class PoliticalContractInfo:
    event: NormalizedEvent      # source event (has event_id, platform, prices)
    contract_type: str          # candidate_win, party_outcome, etc.
    candidates: list[str]       # extracted candidate names
    party: str | None           # dem, gop, etc.
    race: str | None            # "TX Senate", "President", etc.
    state: str | None           # state abbreviation
    threshold: float | None     # for margin/vote_share brackets
    direction: str | None       # "above", "below", "between"
```

### Extraction Approach

Regex patterns + keyword matching against contract titles. Political contract titles are formulaic across Polymarket/Kalshi/PredictIt. Falls back to `yes_no_binary` for anything unclassifiable.

**Tense/phrasing variations handled:** "wins", "to win", "winning", "will win" all match `candidate_win`. Case-insensitive. Example regexes:

```python
# candidate_win: captures candidate name and race
r"(?P<candidate>[A-Z][a-z]+(?: [A-Z][a-z]+)+)\s+(?:wins?|to win|winning|will win)\s+(?P<race>.+)"

# margin_bracket: captures threshold and direction
r"(?:wins?|margin)\s+(?:by\s+)?(?P<dir>[><])?\s*(?P<threshold>\d+(?:\.\d+)?)\s*%"

# party_outcome: captures party and race
r"(?P<party>Democrat(?:ic)?|Republican|GOP|Dem|Rep)\s+(?:candidate\s+)?(?:wins?|holds?|takes?)\s+(?P<race>.+)"
```

### `conditional_hedge` Detection

The `conditional_hedge` relationship type is detected structurally, not semantically:
- **Same-race, different-candidate**: Two `candidate_win` contracts for the same race but different candidates (e.g., "Talarico wins TX Senate" vs "Cruz wins TX Senate") — mutually exclusive outcomes.
- **Same-race, opposite party**: A `candidate_win` + `party_outcome` where the candidate is NOT that party.
- Falls back to LLM classification when structural rules cannot determine the relationship.

---

## Section 2: Relationship Detection & Pairing

After classification, the analyzer identifies relationships between contracts within a cluster to determine which contracts can form synthetic legs.

### Relationship Types

| Relationship | Detection Rule | Edge Source | Score Multiplier |
|---|---|---|---|
| `mispriced_correlation` | Same race + same implied outcome + price diff >3% | Price gap between platforms | 3.0x |
| `candidate_party_link` | `candidate_win` + `party_outcome` for same race, candidate is that party | Should be ~equal probability, often mispriced | 2.5x |
| `margin_decomposition` | `candidate_win` + `margin_bracket` for same candidate | Win prob must be ≥ margin prob — exploitable if violated | 2.0x |
| `conditional_hedge` | Two contracts where one winning implies the other likely loses | Covers complementary branches of the event tree | 1.5x |
| `bracket_spread` | Two `margin_bracket` or `vote_share` at different thresholds | Range play similar to crypto range synthetics | 1.5x |
| `matchup_arbitrage` | `matchup` contract + individual `candidate_win` contracts | Matchup price should equal conditional win probability | 2.0x |

### Pairing Logic

1. Iterate all pairs within a cluster (typically 3-15 contracts per political cluster)
2. Classify each pair's relationship using the rules above
3. For 3-4 leg positions: start with the highest-scored pair, then greedily add legs that introduce a new relationship type or cover an uncovered scenario branch
4. Cap at 4 legs maximum

### Constraint Checks (before sending to LLM)

- At least one pair must have a relationship score ≥ 1.5
- Combined leg prices must leave room for profit after platform-specific fees per leg:
  - Fee formula: `total_fees = sum(platform_fee_rate[leg.platform] for leg in legs)`
  - Platform rates: Polymarket 2%, Kalshi 1.5%, PredictIt 10%, Limitless 2% (round-trip)
  - Example: 3-leg position (2 Polymarket + 1 Kalshi) = 2% + 2% + 1.5% = 5.5% total fee drag
  - Reject if `expected_spread - total_fees < 1%` (must have net positive EV after all fees)
- Skip if all contracts are on the same platform AND all are `yes_no_binary` (no structural edge)

---

## Section 3: LLM Strategy Prompt & Response

When the rules phase produces candidate leg combinations, the top combinations are sent to the AI advisor for strategy analysis.

### Prompt Structure

Batched per cluster, similar to the existing exit engine's `[PKG:id]` pattern:

```
You are a political prediction market analyst. For each cluster below,
analyze the contracts and recommend optimal synthetic positions.

IMPORTANT: All expected value and P&L figures must be AFTER platform fees.
Fee rates (round-trip): Polymarket=2%, Kalshi=1.5%, PredictIt=10%, Limitless=2%.

[CLUSTER:tx-senate-2026]
Race: TX Senate 2026
Contracts:
  1. "Talarico wins TX Senate" | Polymarket | YES=$0.62 NO=$0.38
  2. "Democratic candidate wins TX Senate" | Kalshi | YES=$0.55 NO=$0.45
  3. "Talarico wins by >5%" | Polymarket | YES=$0.38 NO=$0.62
  4. "TX Senate margin <2%" | Kalshi | YES=$0.15 NO=$0.85

Pre-classified relationships:
  - (1,2): candidate_party_link — price gap 7% (mispriced correlation)
  - (1,3): margin_decomposition — win prob must be ≥ margin prob ✓
  - (3,4): bracket_spread — complementary margin ranges

For each recommended position, respond with this exact JSON structure:
{
  "strategies": [{
    "strategy_name": "human-readable name",
    "legs": [{"contract": 1, "side": "YES", "weight": 0.5}],
    "scenarios": [{"outcome": "description", "probability": 0.6, "pnl_pct": 12.5}],
    "expected_value_pct": 8.2,
    "win_probability": 0.65,
    "max_loss_pct": -45.0,
    "confidence": "high",
    "reasoning": "explanation"
  }]
}
```

### Response Data Structure

```python
@dataclass
class SyntheticLeg:
    contract_idx: int               # 1-based index from prompt (mapped to event_id during parsing)
    event_id: str                   # platform event_id (resolved after LLM response)
    side: str                       # "YES" or "NO"
    weight: float                   # allocation weight (0.0-1.0, sum to 1.0)

@dataclass
class Scenario:
    outcome: str                    # "Talarico wins by >5%"
    probability: float              # estimated probability
    pnl_pct: float                  # profit/loss percentage

@dataclass
class PoliticalSyntheticStrategy:
    cluster_id: str
    strategy_name: str              # "TX Senate Mispriced Dem Link"
    legs: list[SyntheticLeg]        # 2-4 legs
    scenarios: list[Scenario]       # 3-5 outcome scenarios
    expected_value_pct: float       # weighted avg P&L
    win_probability: float          # P(profit)
    max_loss_pct: float             # worst case
    confidence: str                 # high/medium/low
    reasoning: str                  # LLM's explanation
```

### Post-LLM Validation

- Reject if `win_probability < 0.50` (must win more often than lose)
- Reject if `max_loss_pct < -60%` (cap downside — reject losses worse than 60%)
- Reject if `expected_value_pct < 3%` (must clear fees)
- Reject if confidence is "low"
- Cross-check: leg prices must still match current market (stale price guard)

---

## Section 4: Integration with Existing Pipeline

### Strategy Type Registration

Add `"political_synthetic"` to `STRATEGY_TYPES` tuple in `src/positions/position_manager.py`. Also extend exit engine trigger checks — all `if strategy in ("cross_platform_arb", "synthetic_derivative")` conditionals in `exit_engine.py` (spread inversion, spread compression, correlation break triggers) must include `"political_synthetic"`.

### Pipeline Position

```
Arb Scanner (60s)
  └─ fetch_all() → events
  └─ match_events() → clusters
  └─ find_arbitrage() → crypto/pure arb opportunities

Political Analyzer (15-min asyncio.Task loop in server.py)
  └─ reuses latest events from arb scanner (no redundant fetch_all)
  └─ groups non-crypto NormalizedEvents into PoliticalClusters by race+state
  └─ analyze_political_clusters() → political synthetic opportunities
  └─ sets arb scanner's existing _scan_event to wake auto trader

Auto Trader (5min + event-driven via shared _scan_event)
  └─ picks up political_synthetic alongside existing opportunities
  └─ scoring: uses LLM's expected_value_pct instead of spread calc
  └─ confidence multiplier: high=1.5x, medium=1.0x
  └─ cross-platform legs get 1.5x boost
  └─ insider signal boost still applies
```

### Political Cluster Formation

The entity matcher produces `MatchedEvent` groups (same question across platforms). Political clusters are a **higher-level grouping** — multiple `MatchedEvent` objects about the same race/topic.

```python
@dataclass
class PoliticalCluster:
    cluster_id: str                          # e.g., "tx-senate-2026"
    race: str                                # extracted race (e.g., "TX Senate")
    state: str | None                        # state abbreviation
    contracts: list[PoliticalContractInfo]   # classified contracts (each has .event)
    matched_events: list[str]                # MatchedEvent IDs that were merged
```

**Clustering algorithm**: After contract classification, group by normalized `race` + `state`. Contracts with the same race string (fuzzy: "TX Senate" = "Texas Senate" = "Senate TX") are merged into one cluster. Minimum 2 contracts per cluster.

### Server Wiring

`PoliticalAnalyzer` is instantiated in `server.py` lifespan:
```python
political_analyzer = PoliticalAnalyzer(
    scanner=arb_scanner,       # access to latest events + _scan_event
    ai_advisor=ai_advisor,     # shared LLM provider chain
    decision_logger=decision_logger,  # shared JSONL logger
)
# Starts its own 15-min asyncio.Task loop
# Also starts hourly backfill task for eval log resolution checks
```

### PoliticalOpportunity Data Structure

The existing `ArbitrageOpportunity` is structurally 2-leg (buy_yes/buy_no). Rather than forcing 4-leg positions into that model, define a new `PoliticalOpportunity` dataclass:

```python
@dataclass
class PoliticalOpportunity:
    cluster_id: str
    strategy: PoliticalSyntheticStrategy  # from LLM
    legs: list[PoliticalLeg]              # 2-4 legs with full details
    total_fee_pct: float                  # sum of per-leg platform fees
    net_expected_value_pct: float         # EV after fees
    platforms: list[str]                  # unique platforms involved
    created_at: str                       # ISO timestamp

@dataclass
class PoliticalLeg:
    event: NormalizedEvent                # source contract
    contract_info: PoliticalContractInfo  # classified type
    side: str                             # "YES" or "NO"
    weight: float                         # allocation (0.0-1.0)
    platform_fee_pct: float              # round-trip fee for this platform
```

The auto trader currently works with dicts. `PoliticalOpportunity` exposes a `to_dict()` method that produces the dict format the auto trader expects, with a `"opportunity_type": "political_synthetic"` discriminator field. The auto trader checks this field to route to the political scoring path (uses `expected_value_pct` and `confidence` instead of `spread_pct` and `profit_pct`).

### Slot & Budget Allocation

Political synthetics compete on score alongside all other strategies — no reserved slots. A single political package with 4 legs at $200 total uses 1 `MAX_CONCURRENT` slot (same budget impact as a single arb package). The LLM's `expected_value_pct` is compared directly against arb `profit_pct` after applying the confidence multiplier.

### Multi-Leg Position Creation & Partial Failure Policy

- Total trade size capped at `MAX_TRADE_SIZE` ($200), split across legs by weight
- Each leg gets its own executor call (may span multiple platforms)
- **Execution order**: Legs executed sequentially, most liquid platform first (Polymarket → Kalshi → others)
- **Partial failure policy**:
  - If leg 1 fails: abort, no rollback needed
  - If legs 1-2 succeed but leg 3 fails: **keep the partial position** if the 2-leg subset is still a valid strategy (check win_probability > 0.50 with remaining legs). If not valid, rollback all executed legs.
  - If rollback fails: log to decision_log as `"partial_entry_stuck"`, exit engine monitors for manual resolution
  - Rollback incurs taker fees — logged as entry cost in trade journal

### Exit Engine Compatibility

- Political synthetics use the same 18 exit triggers
- Additional trigger #21: `political_event_resolved` (safety override, not AI-reviewed) — if any leg in the cluster resolves, evaluate all legs immediately. Requires a new resolution-check call in `evaluate_heuristics()` that queries platform APIs for contract settlement status. Added to `TRIGGERS` dict in `exit_engine.py` with category `"safety"`.
- AI advisor exit review includes the original `reasoning` from the strategy for context

### LLM Failure Handling

- If LLM returns invalid JSON: retry once with stricter prompt ("respond ONLY with valid JSON")
- If all AI providers fail: skip this cluster for this cycle, log as `"llm_unavailable"` in eval log
- If LLM returns empty strategies: log as `"no_strategy_found"`, skip cluster
- No mechanical fallback — political synthetics require LLM reasoning (unlike exit engine which has heuristic fallbacks)

### Caching

- Cache key: SHA-256 hash of sorted contract IDs in the cluster
- TTL: 15 minutes
- Invalidated early when any contract's price shifts >3% from cached value
- Max 200 cache entries, LRU eviction beyond that
- Stored in `src/data/arbitrage/political_cache.json`

---

## Section 5: API Endpoints & Dashboard

### API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/political/clusters` | GET | List active political clusters with contract classifications |
| `/api/political/strategies` | GET | List current LLM-recommended strategies with scenarios |
| `/api/political/strategies/{cluster_id}` | GET | Detailed strategy for a specific cluster |
| `/api/political/analyze` | POST | Force re-analysis of a cluster (bypasses cache). Body: `{"cluster_id": "tx-senate-2026"}` |
| `/api/political/eval` | GET | Strategy performance summary — win rate, avg P&L by action type |
| `/api/political/eval/missed` | GET | Skipped strategies that would have been profitable (hindsight) |

### Dashboard Integration

Political synthetics appear in the existing opportunities list with a "Political Synthetic" badge. Card layout:

- **Header**: Strategy name from LLM (e.g., "TX Senate Mispriced Dem Link")
- **Legs table**: Each leg with platform, contract title, side, price, allocation weight
- **Scenario table**: 3-5 outcomes with probability and P&L per scenario
- **Metrics row**: Expected value %, win probability, confidence badge, max loss
- **Action**: Same "Auto-trade" toggle and manual trade buttons as existing opportunities

No new pages required.

### Strategy Evaluation Log

Political synthetics use the unified `EvalEntry` structure defined in Section 6. Political-specific fields (`llm_reasoning`, `confidence`, `win_probability`, `legs`) are stored in the `metadata: dict` optional field on `EvalEntry`. This avoids duplicate data structures while preserving political-specific context.

### Decision Log Integration

All LLM strategy calls logged to existing `decision_log.jsonl` with type `political_synthetic_analysis`. Includes full prompt, response, validation result, and cache status. Trade journal records `strategy_type: "political_synthetic"` for P&L tracking by strategy type.

---

## Section 6: System-Wide Hindsight Analysis

The evaluation logging described above for political synthetics should apply to **all** strategy types. This section extends the eval log to cover the entire Arbitrout pipeline.

### Universal Eval Log

**File:** `src/data/arbitrage/eval_log.jsonl` (replaces the political-only log — one unified log for all strategies)

Every opportunity the system encounters is logged, whether acted on or not:

| Strategy Type | What Gets Logged |
|---|---|
| `cross_platform_arb` | Every spread ≥ 1% detected. If skipped: why (too small, no budget, duplicate, near-expiry). If entered: entry prices. Backfill P&L on close. |
| `synthetic_derivative` | Every crypto synthetic validated. If rejected by scenario analysis: which validation failed. If entered: leg prices. |
| `political_synthetic` | As described in Section 5 above. |
| `pure_prediction` | Every directional bet scored ≥ 2.0 by auto trader. If skipped: score breakdown showing which factor was too low. |
| `news_driven` | Every news signal with confidence ≥ 0.6. If skipped: why (no matching market, budget full, duplicate). |

### Unified Entry Structure

```python
@dataclass
class EvalEntry:
    timestamp: str
    strategy_type: str             # cross_platform_arb, synthetic_derivative, etc.
    opportunity_id: str            # unique ID for this opportunity
    action: str                    # "entered" | "skipped" | "rejected_validation" | ...
    action_reason: str             # machine-parseable enum
    reason_detail: str             # human-readable explanation

    # Opportunity snapshot at decision time
    markets: list[dict]            # [{event_id, platform, title, yes_price, no_price}]
    score: float | None            # auto trader score (if applicable)
    spread_pct: float | None       # for arb opportunities
    expected_value_pct: float | None  # for political/synthetic

    # Outcome (backfilled)
    actual_pnl_pct: float | None
    actual_outcome: str | None     # "win" | "loss" | "partial" | "expired" | "still_open"
    resolution_date: str | None
    prices_at_decision: dict
    prices_at_resolution: dict | None
    metadata: dict | None          # strategy-specific fields (e.g., political: llm_reasoning,
                                   # confidence, win_probability, legs; news: signal_confidence,
                                   # source_url; arb: spread_direction)
```

### Hindsight Analysis Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/eval/summary` | GET | Overall performance by strategy_type: win rate, avg P&L, total entered vs skipped |
| `/api/eval/missed` | GET | Skipped opportunities that would have been profitable. Query params: `?strategy_type=&min_hypothetical_pnl=5` |
| `/api/eval/calibration` | GET | For each action_reason, shows: how often that reason led to a correct skip vs a missed opportunity |
| `/api/eval/details/{opportunity_id}` | GET | Full entry with all fields for a specific opportunity |

### Backfill Mechanism

- **For entered positions**: P&L backfilled automatically when the position closes (already tracked by trade journal)
- **For skipped opportunities**: A background task (runs hourly) checks if skipped opportunity markets have resolved. If yes, calculates hypothetical P&L using `prices_at_decision` vs resolution outcome.
- **Hypothetical P&L formula**: `(payout - entry_price - estimated_fees) / entry_price * 100`
  - For multi-leg: sum weighted P&L across all legs the system would have entered

### Integration Points

- **Auto trader** (`auto_trader.py`): Log every opportunity scored, with action and score breakdown
- **Arb scanner** (`arbitrage_engine.py`): Log every spread ≥ 1% found, even if below MIN_SPREAD_PCT threshold
- **News scanner**: Log every signal with confidence ≥ 0.6
- **Exit engine**: Log every exit decision (hold vs exit) with trigger details — enables "did we exit too early/late?" analysis

### Dashboard Widget

Add an "Eval" tab to the arbitrout dashboard showing:
- Strategy performance comparison table (entered P&L vs hypothetical skipped P&L)
- "Missed opportunities" list with hypothetical profit
- Calibration chart: for each skip reason, % of correct vs incorrect skips

---

## Section 7: Test Plan

| Test Area | Type | What to Test |
|---|---|---|
| Contract classifier | Unit | Each contract type (candidate_win, party_outcome, margin_bracket, vote_share, matchup, yes_no_binary) with 3+ title variations per type. Tense handling ("wins", "to win", "winning"). Fallback to yes_no_binary. |
| Relationship detection | Unit | Each relationship type with known contract pairs. Score calculation. Constraint checks (fee threshold, minimum score). |
| Greedy leg extension | Unit | 2-leg, 3-leg, 4-leg combinations. Verify cap at 4. Verify new relationship type requirement for extension. |
| LLM prompt builder | Unit | Correct cluster formatting. Fee rates included. JSON schema present. |
| LLM response parser | Unit | Valid JSON parsing into `PoliticalSyntheticStrategy`. Invalid JSON handling. Missing fields. |
| Post-LLM validation | Unit | Each rejection rule (win_prob, max_loss, EV, confidence, stale prices). Edge cases at thresholds. |
| Cache | Unit | TTL expiry. Price-shift invalidation at 3%. LRU eviction at 200 entries. SHA-256 key stability. |
| Partial failure | Integration | Leg 1 fails (abort). Legs 1-2 succeed + leg 3 fails (keep valid subset). Rollback path. |
| Pipeline integration | Integration | Mock LLM returning strategies. Verify auto trader receives and scores `PoliticalOpportunity`. Verify exit engine includes trigger #21. |
| Eval logging | Integration | Verify entries logged for entered, skipped, rejected actions. Verify backfill on position close. |
| Fee calculation | Unit | Mixed-platform positions: 2 Polymarket + 1 PredictIt = 14% total fees. Verify rejection when EV < fee threshold. |
| Political clustering | Unit | "TX Senate" = "Texas Senate" = "Senate TX" grouping. Min 2 contracts. Separate clusters for different races. |
| Eval log backfill | Integration | Skipped opportunity resolves → hypothetical P&L calculated correctly. Entered position closes → actual P&L backfilled. |
| API endpoints | Integration | All 6 political + 4 eval endpoints return correct data. POST /analyze bypasses cache. |
