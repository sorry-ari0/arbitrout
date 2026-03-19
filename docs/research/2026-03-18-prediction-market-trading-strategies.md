# Prediction Market Trading Strategies — Research Compilation

**Date:** 2026-03-18
**Purpose:** Actionable strategies for improving success rates in prediction market trading (Polymarket, Kalshi, Limitless)
**Sources:** Academic papers, practitioner guides, quantitative research (2024-2026)

---

## 1. Cross-Platform Arbitrage Optimization

### Core Mechanics

When the combined cost of buying YES on Platform A and NO on Platform B totals less than $1.00, the spread is risk-free profit regardless of outcome. The invariant `YES + NO = $1.00` per platform creates deterministic P&L mechanics.

### Key Parameters

| Parameter | Value | Source |
|-----------|-------|--------|
| Typical arb window duration | 2.7 seconds average (2026) | Polymarket leaderboard analysis |
| Frequency of pure arb opportunities | 5-15 per day | AhaSignals research |
| Minimum profitable spread | >5% (below this, fees eat profit) | Cross-platform fee analysis |
| Polymarket taker fee | 0.01% ($1 per $10K) | Polymarket docs |
| Kalshi taker fee | ~1.2% ($120 per $10K) | Kalshi fee schedule |
| PredictIt fee | 10% of profits + 5% withdrawal | PredictIt terms |
| Bot dominance | 14 of top 20 most profitable wallets are bots | IMDEA Networks study |
| Total arb profits extracted | $40M from Polymarket alone (Apr 2024 - Apr 2025) | IMDEA Networks |

### Execution Techniques That Work

**1. Structural/Synthetic Arbs (minutes to days) — Best for Arbitrout**
- Target price discrepancies that persist because of structural differences between platforms (different user bases, different liquidity, different fee structures)
- These last minutes to hours, not seconds — viable without sub-100ms infrastructure
- Example: Kalshi prices "Fed rate cut in June" at 40%, Polymarket at 35%. Buy YES on Polymarket, buy NO on Kalshi. 5% locked-in spread minus fees.

**2. Logical/Combinatorial Arbs**
- Exploit pricing inconsistencies between logically related markets (e.g., "BTC > $100K by June" should be <= "BTC > $100K by December")
- **Warning:** 62% of LLM-detected logical dependencies failed to yield profits due to liquidity asymmetry and non-atomic execution
- Only pursue when both legs have >$50K daily volume

**3. Intra-Platform Arbs**
- When YES + NO on a single platform sums to != $1.00 (typically off by 1-3 cents in low-liquidity markets)
- Fastest to execute (single platform, atomic-ish)
- Polymarket: sum imbalances appear in markets with <$10K daily volume

### Critical Risk: Resolution Divergence

The 2024 government shutdown proved this is not theoretical: Polymarket resolved YES while Kalshi resolved NO for the "same" event. Polymarket's standard was "OPM issues shutdown announcement" while Kalshi required "actual shutdown exceeding 24 hours."

**Mitigation:** Before opening any cross-platform arb, programmatically compare resolution criteria. Flag divergences. This should be a pre-trade check in Arbitrout's arb scanner.

### Actionable Parameters for Arbitrout

```
MIN_ARB_SPREAD: 5%          (below this, fees eat profit)
MIN_LEG_VOLUME: $50K/day    (below this, 78% execution failure rate)
MAX_PRICE_AGE: 2 seconds    (stale prices kill arb profitability)
PREFER_MAKER_ORDERS: true   (Polymarket: 0% + rebate vs 0.01% taker)
RESOLUTION_CHECK: required  (compare resolution criteria before execution)
```

---

## 2. Synthetic Derivative Strategies

### Range Betting (Digital Spread)

Prediction markets often offer multiple strike levels for the same underlying (e.g., "BTC > $70K", "BTC > $75K", "BTC > $80K"). Combining these creates synthetic range bets.

**Construction:**
- **Bull spread:** Buy "BTC > $70K" YES at $0.65, Sell "BTC > $80K" YES at $0.30. Net cost: $0.35. Max payout: $0.70 (if BTC between $70K-$80K at expiry). Max loss: $0.35.
- **Bear spread:** Buy "BTC > $80K" NO at $0.70, Sell "BTC > $70K" NO at $0.35. Net cost: $0.35. Profits if BTC < $70K.

**Key insight:** Binary contracts are discrete — you can only construct spreads at available strike levels. The granularity of available strikes determines your range precision.

### Butterfly Spread (Binary Version)

Profit from price staying in a narrow range. Constructed with three strike levels:

```
Buy 1x "BTC > $70K" YES at $0.65
Sell 2x "BTC > $75K" YES at $0.48 (receive $0.96)
Buy 1x "BTC > $80K" YES at $0.30

Net cost: $0.65 - $0.96 + $0.30 = -$0.01 (credit)
Max profit: ~$0.50 (if BTC at exactly $75K at expiry)
Max loss: ~$0.50 (if BTC far from $75K)
```

**Practical limitation:** Most prediction markets don't have fine enough strike granularity for precise butterflies. Works best with Kalshi's BTC/ETH contracts which offer multiple strikes.

### Calendar Spread (Term Structure Trading)

Compare identical questions with different expiry dates:

- "BTC > $100K by March 31" at $0.25
- "BTC > $100K by June 30" at $0.45

If the $0.20 spread seems too wide (implying almost no chance of reaching $100K in March but reasonable chance by June), buy the near-term contract and sell the far-term contract. Risk: if BTC rockets past $100K quickly, both settle at $1.00 and you net zero minus fees.

**Best use case:** When you believe the market overestimates the time needed for an event. The near-term contract is your leveraged bet; the far-term sale partially hedges.

### Spot + Hedge (Already in Arbitrout's Design)

The derivative position manager's `spot_plus_hedge` strategy type directly implements this:
- Buy real BTC on Coinbase
- Buy "BTC > $X" NO on Polymarket as downside protection
- The NO contract acts as a put option

**Hedge ratio guidance from research:**
- 50% hedge = moderate protection (default in Arbitrout spec)
- Cost of NO contracts at far-from-money levels is cheap ($0.05-0.15), making 30-40% allocation viable
- Optimal: match hedge expiry to your intended hold period

### Actionable Parameters for Arbitrout

```
RANGE_BET_MIN_STRIKES: 3       (need at least 3 strikes for butterfly)
RANGE_BET_MAX_WIDTH: 20%       (of underlying price — wider = more theta decay)
CALENDAR_MIN_EXPIRY_GAP: 14d   (less than this, time value difference too small)
HEDGE_RATIO_DEFAULT: 0.50      (50% of spot position hedged)
HEDGE_RATIO_BOUNDS: [0.30, 0.70]  (AI-adjustable range)
```

---

## 3. News-Driven Trading

### Latency Hierarchy (What Actually Matters)

Research from 2025-2026 reveals a clear latency hierarchy for prediction market news trading:

| Speed Tier | Source | Latency to Market | Edge Duration |
|------------|--------|-------------------|---------------|
| Tier 1 | Exchange data feeds (Binance, Coinbase websockets) | <1 second | Seconds |
| Tier 2 | Wire services (Reuters, AP, Bloomberg Terminal) | 5-30 seconds | Minutes |
| Tier 3 | RSS feeds (CoinDesk, CNBC, BBC) | 2-10 minutes | 10-60 minutes |
| Tier 4 | Social media (Twitter/X, Reddit) | Variable (sometimes faster than Tier 2) | Minutes to hours |
| Tier 5 | Mainstream coverage (NYT, CNN) | 30-60 minutes | Hours (often already priced in) |

**Arbitrout's current RSS approach (Tier 3) has a 10-60 minute edge window** — viable for prediction markets where price adjustment takes hours, but requires fast LLM analysis to capitalize.

### 15-Minute Crypto Markets: The Bot Frontier

One bot turned $313 into $414,000 in a single month trading Polymarket's 15-minute BTC/ETH/SOL up/down markets by exploiting the price lag between Polymarket and Binance/Coinbase spot prices. This bot achieved a 98% win rate.

**However:** Polymarket introduced dynamic taker fees in February 2026 specifically to kill this strategy. Fees are highest at 50% probability (~3.15%), exactly where latency arbs operated. This strategy is now largely unprofitable at scale.

### NLP/Sentiment Techniques That Work

**LLM-based sentiment (best performer in 2024-2025):**
- ChatGPT/Claude zero-shot headline classification achieves up to 89.8% prediction accuracy when combined with historical price data
- Forward-looking implied sentiment captures 45-50% of variation in stock returns
- Best approach: binary classification (BULLISH/BEARISH/NEUTRAL) rather than numeric scoring

**What doesn't work well:**
- Traditional NLP (bag-of-words, VADER) — too noisy for prediction markets
- Disaggregated sentiment subscores — lack robust predictive power
- Social media sentiment alone — too much noise, requires volume thresholds

### Optimal News Scanner Parameters (Calibrated from Research)

```
RSS_POLL_INTERVAL: 120-180s     (2-3 min — matches Arbitrout spec)
HEADLINE_BATCH_SIZE: 30          (LLM context efficiency sweet spot)
CONFIDENCE_THRESHOLD_BREAKING: 8 (immediate execution)
CONFIDENCE_THRESHOLD_NORMAL: 7   (queue for auto trader)
NEWS_EDGE_DECAY_HALFLIFE: 30min  (after 30min, edge strength halves)
MAX_NEWS_TRADES_PER_DAY: 5       (already in Arbitrout spec — validated)
NEWS_COOLDOWN_PER_MARKET: 15min  (prevents chasing same story)
ARTICLE_FETCH_TIMEOUT: 5s        (httpx) / 10s (Scrapling fallback)
```

### Signal Quality Improvement

**Two-pass pipeline validation (from research):**
1. Check if headline matches a market with >$50K volume (low-volume markets can't be traded efficiently)
2. Verify the news is genuinely new (not a follow-up to already-priced-in event)
3. Cross-reference with exchange price data — if spot price already moved >2%, the edge is gone
4. Confidence calibration: track actual win rate per confidence level and adjust thresholds monthly

---

## 4. Combination/Multi-Leg Strategies

### Correlated Position Management

**Key finding:** Many prediction markets are structurally correlated. "Which candidate wins the election" directly impacts "Will there be a government shutdown" and "Will the Fed cut rates." If positions are correlated, risk multiplies — one trade going against you means several trades go against you.

**Portfolio construction rules:**
1. **Correlation mapping:** Before opening a new position, check correlation with all existing positions. If >0.6 correlation, reduce sizing by 50%.
2. **Sector limits:** Max 30% of portfolio in correlated event clusters (e.g., all crypto markets, all political markets).
3. **Hedged pairs:** Whenever opening a directional bet, identify available hedges. Even a partial hedge (30-50%) dramatically reduces portfolio variance.

### Multi-Event Portfolio Strategies

**The "Barbell" approach (most successful per QuantPedia):**
- 70-80% of capital in high-probability, low-return positions (favorites at $0.75-0.90)
- 20-30% of capital in carefully selected longshots (prices $0.05-0.20 where your model says true probability is 2-3x market price)
- Expected return: steady 5-15% from favorites, occasional 3-5x from longshots

**The "Carry" approach:**
- Systematically sell overpriced longshots (prices $0.05-0.15 where true probability is near zero)
- Small but consistent returns as contracts expire worthless
- Risk: catastrophic loss when a longshot hits — must size positions small enough to survive

**Term structure portfolios:**
- Compare same-topic markets across different dates
- Translate prices into implied probability distributions
- Buy the date that's underpriced, sell the date that's overpriced
- Hedge by matching total exposure to near-zero net delta

### KL-Divergence for Multi-Market Scanning

Kullback-Leibler divergence quantifies how much one probability distribution differs from another. Applied to prediction markets:

1. Build your own probability model for a set of related markets (e.g., "BTC price at end of each month")
2. Compare your distribution against market-implied probabilities
3. Markets where KL divergence is highest = largest mispricing
4. Construct hedged positions across the mispriced set

**This directly maps to Arbitrout's auto trader scoring system** — the conviction score is essentially a simplified KL divergence measure.

### Actionable Parameters for Arbitrout

```
MAX_CORRELATED_EXPOSURE: 30%    (of portfolio in correlated cluster)
CORRELATION_THRESHOLD: 0.6      (above this, reduce sizing 50%)
BARBELL_FAVORITE_ALLOCATION: 75%
BARBELL_LONGSHOT_ALLOCATION: 25%
LONGSHOT_MIN_EDGE: 2x           (your probability >= 2x market price)
SECTOR_CATEGORIES: [crypto, politics, economics, sports, tech]
```

---

## 5. Exit Strategy Optimization

### Time Decay in Binary Markets

Binary contracts exhibit distinct time decay patterns compared to traditional options:

**Key findings:**
- Theta decay accelerates non-linearly as expiration approaches (convex curve)
- The "binary zone" begins at 3 days to expiry (DTE) — after this, the contract is essentially a coin flip weighted by current probability
- At-the-money contracts ($0.45-0.55) have the fastest time decay
- Deep ITM contracts (>$0.85) have very little remaining time value — consider selling if the premium is minimal
- Deep OTM contracts (<$0.15) decay quickly but have "lottery ticket" convexity

**Optimal exit timing by DTE:**

| DTE | Action | Reasoning |
|-----|--------|-----------|
| >30 days | Hold with trailing stop | Plenty of time for price to move; theta is negligible |
| 14-30 days | Tighten trailing stop by 30% | Theta starts accelerating |
| 7-14 days | Begin partial exits on profitable legs | Capture 60-80% of max profit |
| 3-7 days | Exit all positions unless >85% confident in outcome | Gamma risk explodes |
| <3 days | Mandatory exit (Arbitrout trigger #10-11) | Binary outcome approaching |
| <6 hours | Force exit at market (Arbitrout trigger #11) | Price becomes binary — all or nothing |

### Trailing Stops vs. Letting Positions Expire

**Research conclusion:** Complex exits (strict stop-losses, trailing stops, profit targets) do not consistently outperform simpler time-based exits across market regimes. Simplicity improves robustness.

**Recommended hybrid approach for Arbitrout:**
1. **Primary exit:** Time-based ladder (partial exits at 14, 7, 3 DTE as above)
2. **Safety exit:** Trailing stop for catastrophic protection (15-25% from peak)
3. **Profit exit:** Sell at 50% of max profit (probability-weighted) — don't wait for 100%
4. **Never hold to expiration** unless position is >$0.95 or <$0.05 (near-certain outcome)

### The "Triple Barrier" Method (From Quantitative Research)

Sets three simultaneous conditions; whichever triggers first causes exit:
1. **Upper barrier (take profit):** Position reaches target P&L (e.g., +25%)
2. **Lower barrier (stop loss):** Position hits max acceptable loss (e.g., -15%)
3. **Time barrier:** Vertical barrier — exit after N days regardless of P&L

This is directly implementable in Arbitrout's exit engine and aligns with the existing trigger architecture.

### Actionable Parameters for Arbitrout

```
# Time-based exit ladder
PARTIAL_EXIT_14DTE: 25%      (sell 25% of position)
PARTIAL_EXIT_7DTE: 50%       (sell 50% of remaining)
PARTIAL_EXIT_3DTE: 100%      (sell all unless >85% confident)
FORCE_EXIT_6H: 100%          (already in spec as trigger #11)

# Trailing stop
TRAILING_STOP_DEFAULT: 15%   (from peak value)
TRAILING_STOP_TIGHT: 8%      (for news-driven, short-lived edge)
TRAILING_STOP_LOOSE: 25%     (for high-conviction, long-dated)

# Triple barrier (for auto trader positions)
TAKE_PROFIT_TARGET: 25%      (close position)
STOP_LOSS_LIMIT: -15%        (close position)
TIME_BARRIER_DAYS: 7         (close if no movement after 7 days)

# Profit capture
SELL_AT_PROFIT_PCT: 50%      (of max theoretical profit)
NEAR_CERTAIN_THRESHOLD: 0.95 (hold to expiry if above this)
```

---

## 6. Kelly Criterion and Position Sizing

### The Kelly Formula for Binary Prediction Markets

From the December 2024 arXiv paper (2412.14144):

**Standard Kelly fraction:**
```
f* = (Q - P) / (1 + Q)

where:
  Q = q / (1 - q)    (odds ratio from your believed probability q)
  P = p / (1 - p)    (odds ratio from market price p)
  q = your estimated true probability
  p = market price (implied probability)
```

**Simplified for binary contracts:**
```
f* = (b * p_true - (1 - p_true)) / b

where:
  b = (1 - market_price) / market_price  (net odds — what you get per dollar risked)
  p_true = your estimated true probability of YES
```

**Example:**
- Market price: $0.40 (implied 40% probability)
- Your estimate: 55% true probability
- Net odds b = (1 - 0.40) / 0.40 = 1.5
- Kelly f* = (1.5 * 0.55 - 0.45) / 1.5 = (0.825 - 0.45) / 1.5 = 0.25 = 25% of bankroll

### Why Full Kelly Is Dangerous

**Drawdown statistics for full Kelly:**
- 1/3 chance of halving bankroll before doubling it
- 1/n chance of reducing bankroll to 1/n at some point

**The paper's key insight:** Market prices in prediction markets systematically differ from mean beliefs. Prices are NOT probabilities. The gap comes from payoff asymmetry — a bet away from 50% gives one side a larger return than the other. This means your "edge" calculation may be systematically biased.

### Fractional Kelly Recommendations

| Fraction | Growth Rate | Drawdown Risk | Use Case |
|----------|-------------|---------------|----------|
| Full Kelly (1.0x) | Maximum | 33% chance of halving | Never in practice |
| Half Kelly (0.5x) | 75% of max | 11% chance of halving | Aggressive, high-confidence |
| Quarter Kelly (0.25x) | 56% of max | ~3% chance of halving | **Recommended default** |
| Tenth Kelly (0.1x) | 34% of max | <1% chance of halving | Conservative, uncertain edge |

**The 30% rule:** By betting 30% of Kelly-optimal, a trader reduces the chance of 80% drawdown from 1-in-5 to 1-in-213, while retaining 51% of Kelly-optimal growth.

**Ed Thorp's practice:** Used Kelly portfolio with ~100 simultaneous bets, produced 20% annualized returns over 28 years on $80 billion wagered. Used fractional Kelly (estimated 1/5 to 1/2 of full Kelly).

### Multi-Market Kelly (Portfolio of Simultaneous Bets)

When holding multiple positions simultaneously, individual Kelly fractions must be adjusted for:

1. **Correlations:** Correlated bets amplify variance. Use the covariance matrix of returns to compute portfolio-level Kelly weights.
2. **Simultaneous exposure:** Total exposure across all positions should not exceed full Kelly for the portfolio. If 4 independent bets each suggest 25% Kelly, total exposure = 100% — too high. Scale down.
3. **Practical rule:** Sum of all fractional Kelly allocations should not exceed 40% of bankroll at any time.

**Portfolio Kelly formula (simplified for binary):**
```
For N uncorrelated binary bets:
  f_i = (Quarter Kelly for bet i)
  Total exposure = sum(f_i) <= 0.40 * bankroll
  If exceeds: scale all f_i proportionally down
```

### Position Sizing for Arbitrout

The existing spec uses Quarter Kelly (25%) as default — this is well-supported by the research.

```
# Kelly parameters
KELLY_FRACTION: 0.25         (quarter Kelly — already in spec)
KELLY_FRACTION_BOUNDS: [0.10, 0.50]  (user-adjustable)
MAX_SINGLE_POSITION: 5%      (of total portfolio — already in spec)
MAX_TOTAL_EXPOSURE: 40%      (of bankroll across all positions)
MIN_EDGE_TO_TRADE: 5%        (your probability - market price >= 5%)
EDGE_CONFIDENCE_DECAY: 0.5   (halve edge estimate if uncertain)

# Dynamic Kelly adjustment
VOLATILITY_MULTIPLIER: true  (reduce Kelly fraction in high-volatility)
VOL_HIGH_THRESHOLD: 2x       (>2x avg stddev → halve Kelly)
VOL_LOW_THRESHOLD: 0.5x      (<0.5x avg stddev → can use up to 2x Kelly fraction)
```

---

## 7. Systematic Biases to Exploit

### Favorite-Longshot Bias (Most Documented Edge)

Both Polymarket and Kalshi display this bias. The data:

| Price Range | Actual Win Rate | Expected Win Rate | Edge |
|-------------|-----------------|-------------------|------|
| $0.01-0.10 | 3-5% | 5-10% | **Sell these (longshots lose more than implied)** |
| $0.10-0.20 | 8-12% | 10-20% | Slight sell edge |
| $0.20-0.50 | Close to implied | 20-50% | No systematic edge |
| $0.50-0.80 | Close to implied | 50-80% | No systematic edge |
| $0.80-0.90 | Higher than implied | 80-90% | Slight buy edge |
| $0.90-0.99 | Higher than implied | 90-99% | **Buy these (favorites win more than implied)** |

**Key statistic:** On Kalshi, investors who buy contracts costing less than $0.10 lose over 60% of their money. Contracts above $0.50 earn a small positive return.

**Practical strategy:** Systematically sell NO on high-probability events (>80%) and avoid buying cheap longshots (<$0.15) unless your independent model gives >2x the implied probability.

### Maker vs. Taker Edge

On Kalshi, the favorite-longshot bias is much stronger for takers (losing 32% on average) than for makers (losing ~10% on average). **Always prefer limit orders (maker) over market orders (taker).**

### Low-Volume Market Inefficiency

Markets under $100K volume exhibit severe inefficiencies — 61% accuracy vs. expected. Retail participants routinely pay $0.15 for outcomes that quantitative models price at $0.03.

**However:** Low volume means poor execution. The 78% execution failure rate in low-volume markets cancels much of the theoretical edge.

**Sweet spot:** Markets with $100K-$500K daily volume — enough liquidity for execution, but not enough institutional attention to eliminate all mispricing.

---

## 8. Platform-Specific Intelligence

### Polymarket
- **Fee advantage:** 0.01% taker, 0% maker + rebates. Best platform for frequent trading.
- **Dynamic fees on short-term markets:** Up to 3.15% on 15-min crypto markets at 50% probability. Avoid 15-min markets unless you have sub-second execution.
- **Leads price discovery:** Generally prices events before Kalshi (higher liquidity).
- **API:** CLOB with EIP-712 signing. Supports limit orders (critical for maker rebates).
- **Resolution risk:** Community-based resolution can be disputed. Check resolution criteria carefully.

### Kalshi
- **Fee:** 0% currently (promotional — may change). RSA keypair auth.
- **Regulatory advantage:** CFTC-regulated, US-legal. Less resolution risk than Polymarket.
- **Strike granularity:** Better for structured strategies (butterflies, spreads) due to more strike levels on crypto/financial markets.
- **Volume:** 62% of prediction market volume (Sep 2025). Deep liquidity on top markets.
- **Maker/taker data:** Available for analysis — makers lose 10%, takers lose 32%.

### Limitless
- **Newer platform:** Less liquidity, potentially more mispricing opportunities.
- **Best for:** Finding structural arbs against larger platforms where Limitless prices lag.

---

## Summary: Priority Strategies for Arbitrout

Ranked by expected impact and implementation feasibility:

1. **Favorite-longshot bias exploitation** (HIGH impact, EASY to implement)
   - Add a bias filter to auto trader: boost score for favorites >$0.80, penalize longshots <$0.15
   - Estimated edge: 5-15% improvement in win rate

2. **Fractional Kelly sizing** (HIGH impact, ALREADY in spec)
   - Quarter Kelly default is research-validated
   - Add portfolio-level exposure cap (40% of bankroll)

3. **Time-based exit ladder** (HIGH impact, MODERATE to implement)
   - Add DTE-based partial exit triggers to exit engine
   - Reduces gamma risk in final days before expiry

4. **Resolution criteria checking** (MEDIUM impact, MODERATE to implement)
   - Pre-trade validation of resolution terms across platforms
   - Prevents catastrophic losses from resolution divergence

5. **News scanner speed optimization** (MEDIUM impact, IN PROGRESS)
   - Current 2-3 min RSS cycle gives 10-60 min edge window — viable
   - Add exchange price cross-reference to filter stale signals

6. **Correlation-aware portfolio construction** (MEDIUM impact, HARD to implement)
   - Track correlation between open positions
   - Scale down sizing when correlated exposure exceeds 30%

7. **Term structure / calendar spread scanner** (LOW-MEDIUM impact, HARD to implement)
   - Compare same-topic markets across different dates
   - Requires building implied probability distributions

---

## Sources

### Cross-Platform Arbitrage
- [Prediction Market Arbitrage Guide: Strategies for 2026](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide)
- [Building a Prediction Market Arbitrage Bot](https://navnoorbawa.substack.com/p/building-a-prediction-market-arbitrage)
- [Prediction Market Arbitrage Strategies: Cross-Platform Trading](https://ahasignals.com/research/prediction-market-arbitrage-strategies/)
- [How Prediction Market Arbitrage Works](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works)
- [Arbitrage Opportunities in Prediction Markets](https://www.ainvest.com/news/arbitrage-opportunities-prediction-markets-smart-money-profits-price-inefficiencies-polymarket-2512/)
- [Arbitrage Bots Dominate Polymarket](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)

### Synthetic Derivatives & Structured Strategies
- [Advanced Prediction Market Trading Strategies](https://metamask.io/news/advanced-prediction-market-trading-strategies)
- [Cboe Introduces Prediction Markets Framework](https://ir.cboe.com/news/news-details/2026/Cboe-Introduces-Innovative-Prediction-Markets-Framework-Expanding-Choice-Beyond-Yes-Or-No-Outcomes/default.aspx)
- [Nasdaq Joins Binary Bets as Prediction Market Craze Hits Wall Street](https://www.coindesk.com/markets/2026/03/02/nasdaq-follows-cboe-joining-world-of-binary-bets-as-prediction-market-craze-hits-wall-street)
- [Using Butterfly Spreads for Profitable Range-Bound Trades](https://highstrike.com/butterfly-spread/)
- [Prediction Market Trading - Alpha in Academia](https://alphainacademia.substack.com/p/prediction-market-trading)

### News-Driven Trading & NLP
- [NLP in Trading: Can News and Tweets Predict Prices?](https://www.luxalgo.com/blog/nlp-in-trading-can-news-and-tweets-predict-prices/)
- [Leveraging LLMs as News Sentiment Predictors](https://link.springer.com/article/10.1007/s10791-025-09573-7)
- [Prediction Markets Are Turning Into a Bot Playground](https://www.financemagnates.com/trending/prediction-markets-are-turning-into-a-bot-playground/)
- [How Latency Impacts Polymarket Bot Performance](https://www.quantvps.com/blog/how-latency-impacts-polymarket-trading-performance)
- [Polymarket Introduces Dynamic Fees](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)

### Portfolio Construction & Multi-Leg
- [Systematic Edges in Prediction Markets — QuantPedia](https://quantpedia.com/systematic-edges-in-prediction-markets/)
- [Analyst Reveals Quant Playbook Behind Polymarket](https://beincrypto.com/quant-strategies-hedge-funds-prediction-markets/)
- [Polymarket Strategies: 2026 Guide](https://cryptonews.com/cryptocurrency/polymarket-strategies/)
- [Top 10 Polymarket Trading Strategies](https://www.datawallet.com/crypto/top-polymarket-trading-strategies)
- [CGV: 26 Predictions on Prediction Markets in 2026](https://www.cgv.fund/post/cgv-26-predictions-on-the-development-of-prediction-markets-in-2026)

### Exit Strategies & Time Decay
- [Theta Decay in Options: DTE Curves & Strategies](https://www.daystoexpiry.com/blog/theta-decay-dte-guide)
- [The Truth About 0DTE Options Time Decay](https://optionalpha.com/blog/0dte-options-time-decay)
- [Stop-Loss, Take-Profit, Triple-Barrier & Time-Exit](https://medium.com/@jpolec_72972/stop-loss-take-profit-triple-barrier-time-exit-advanced-strategies-for-backtesting-8b51836ec5a2)
- [Five Exit Strategies in Trading](https://www.quantifiedstrategies.com/trading-exit-strategies/)

### Kelly Criterion & Position Sizing
- [Application of the Kelly Criterion to Prediction Markets (arXiv:2412.14144)](https://arxiv.org/html/2412.14144v1)
- [The Math of Prediction Markets: Binary Options, Kelly Criterion](https://navnoorbawa.substack.com/p/the-math-of-prediction-markets-binary)
- [Why Fractional Kelly? Simulations](https://matthewdowney.github.io/uncertainty-kelly-criterion-optimal-bet-size.html)
- [Portfolio Optimization for Binary Options Based on Relative Entropy](https://pmc.ncbi.nlm.nih.gov/articles/PMC7517297/)
- [Kelly's Criterion in Portfolio Optimization: A Decoupled Problem](https://arxiv.org/pdf/1710.00431)
- [Best Position Sizing: Kelly Criterion for Crypto Predictions](https://www.crypticorn.com/position-sizing-on-polymarket-and-kalshi-crypto-up-down-predictions/)

### Systematic Biases
- [Are Polymarket and Kalshi Reliable? Not Quite — DL News](https://www.dlnews.com/articles/markets/polymarket-kalshi-prediction-markets-not-so-reliable-says-study/)
- [The Economics of the Kalshi Prediction Market — CEPR](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)
- [Polymarket Prediction Accuracy: Track Record & Brier Score](https://www.fensory.com/intelligence/predict/polymarket-accuracy-analysis-track-record-2026)
- [Makers and Takers: The Economics of the Kalshi Prediction Market (UCD Working Paper)](https://www.ucd.ie/economics/t4media/WP2025_19.pdf)
- [Favorite-Longshot Bias in Fixed-Odds Betting Markets](https://www.sciencedirect.com/science/article/abs/pii/S1062976916000041)
