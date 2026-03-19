# Hedge Fund & Quant Strategies Adapted for Prediction Market Trading

**Date:** 2026-03-18
**Purpose:** Map hedge fund / quant firm techniques (2025-2026) to concrete prediction market bot strategies for Arbitrout
**Scope:** Polymarket, Kalshi, Limitless -- ranked by impact and implementation feasibility

---

## Executive Summary

Prediction markets processed $44B+ in volume in 2025, with Kalshi at $50B annualized and Polymarket at $2.1B/week. 14 of the top 20 most profitable Polymarket wallets are bots. Over 30% of Polymarket wallets use AI agents. Only 0.51% of Polymarket traders are actually profitable. This means the bar is high, but the alpha is real for disciplined quantitative approaches.

The strategies below are distilled from hedge fund practices at Renaissance Technologies, Two Sigma, D.E. Shaw, and Man Group, combined with prediction-market-specific research from QuantPedia, IMDEA Networks, and practitioner data from 2025-2026.

---

## TIER 1: HIGH IMPACT, IMPLEMENTABLE NOW

### Strategy 1: Favorite-Longshot Bias Exploitation

**Hedge fund origin:** Documented in equity markets since Kahneman & Tversky. Sports betting firms have exploited this for decades. Now confirmed in prediction markets by CEPR/UCD research on Kalshi (2025).

**How it works in prediction markets:**
- Retail traders systematically overpay for longshots (contracts < $0.15) seeking lottery-like payoffs
- Favorites (contracts > $0.80) are systematically underpriced relative to actual outcomes
- On Kalshi, investors buying contracts under $0.10 lose over 60% of their money
- Contracts above $0.50 earn a small positive return on average

**Implementation for Arbitrout:**
- Auto trader bias filter: boost conviction score +15% for favorites > $0.80
- Penalize conviction score -30% for longshots < $0.15 unless independent model gives > 2x implied probability
- Systematically sell NO on high-probability events as a "carry" strategy

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 5-15% improvement in win rate |
| Complexity | 2/10 -- filter on existing auto trader scoring |
| Data requirements | Market prices only (already available) |
| Capital required | No additional capital |

---

### Strategy 2: LLM-Powered Mispricing Detection (Multi-Model Consensus)

**Hedge fund origin:** Sentiment analysis and NLP-based alpha generation. Two Sigma uses NLP across structured + unstructured data. Recent academic work (arXiv 2508.04975) shows transformer + LLM-generated "formulaic alpha" significantly enhances prediction.

**How it works in prediction markets:**
- Query Claude, GPT-4o, and Gemini for probability estimates on specific markets
- Compare LLM consensus probability vs. market-implied probability
- Trade when divergence exceeds fee-adjusted threshold (>5% edge post-fees)
- Multi-model consensus approach: if 3 models agree within 5%, confidence is high; if models diverge by 25%+, skip the trade

**Quantitative validation:**
- Claude AI trading bots already dominate Polymarket leaderboards
- Polystrat (autonomous AI agent) executed 4,200+ trades in one month with single-trade returns up to 376%
- AI agents show 37%+ positive P&L rate vs. 7-13% for human traders
- Multi-model approach adds 15-20 min per market but significantly improves calibration

**Implementation for Arbitrout:**
- Extend existing AI news scanner to include market-by-market probability assessment
- Pipeline: fetch market details -> query 2-3 LLMs for probability estimate -> compare vs market price -> flag if edge > 5%
- Store historical LLM estimates and track calibration accuracy over time
- Use probability ranges (e.g., "55-65%") not point estimates for more honest uncertainty quantification

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 5-15% per trade on mispriced markets |
| Complexity | 4/10 -- LLM API calls + comparison logic |
| Data requirements | Market descriptions, resolution criteria, recent news context |
| Capital required | LLM API costs (~$0.05-0.50 per market analysis) |

---

### Strategy 3: Structural Cross-Platform Arbitrage (Enhanced)

**Hedge fund origin:** Statistical arbitrage exploiting pricing inefficiencies across venues. Renaissance runs simultaneous long/short positions across correlated securities. Academic research (IMDEA Networks, 2025) documented $40M in arbitrage profits from Polymarket alone in 12 months.

**How it works in prediction markets:**
- Same event priced differently across Polymarket, Kalshi, Limitless
- Cross-platform arb windows last 2-7 seconds for pure latency arb (requires infrastructure)
- Structural arbs (different user bases, fee structures, liquidity) persist minutes to hours
- Combinatorial arbs within multi-outcome markets: if YES prices across all candidates sum != $1.00

**Key 2026 development -- Polymarket fee changes:**
- February 2026: removed 500ms taker delay, introduced dynamic taker fees up to ~1.56%
- Zero fees for maker orders + potential rebates
- Net effect: maker-only strategies now have fee advantage; taker-based latency arb is harder

**Implementation for Arbitrout (already partially built):**
- Existing arb scanner covers 9 platforms -- enhance with:
  - Resolution criteria comparison (prevent resolution divergence losses)
  - Fee-aware spread calculation (post-2026 dynamic fees on Polymarket)
  - Volume-weighted execution (only arb on legs with > $50K daily volume)
  - Maker-only order placement on Polymarket for 0% fee + rebates

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 0.5-3% per arb, $40M total extracted in 12 months across all bots |
| Complexity | 5/10 -- already built, needs fee/resolution enhancements |
| Data requirements | Real-time prices from all platforms (already have) |
| Capital required | $5K+ for meaningful returns |

---

### Strategy 4: Fractional Kelly Position Sizing with Portfolio Exposure Caps

**Hedge fund origin:** Kelly criterion is used by every serious quant fund. Ed Thorp (Princeton-Newport Partners) used fractional Kelly to produce 20% annualized returns over 28 years on $80B wagered.

**How it works in prediction markets:**
- Full Kelly maximizes long-run growth but has 33% chance of halving bankroll before doubling
- Quarter Kelly (0.25x) retains 56% of max growth rate with only ~3% chance of halving
- Portfolio-level constraint: sum of all Kelly allocations should not exceed 40% of bankroll
- Dynamic Kelly: reduce fraction in high-volatility regimes (>2x avg stddev -> halve Kelly)

**Key research finding (arXiv 2412.14144):**
- Prediction market prices are NOT probabilities -- payoff asymmetry creates systematic bias
- Bets away from 50% give one side larger return than the other
- This means your "edge" calculation may be systematically biased -- fractional Kelly compensates

**Implementation for Arbitrout:**
- Already using Quarter Kelly as default -- validated by research
- Add: portfolio-level exposure cap (40% of bankroll across all positions)
- Add: correlation-adjusted Kelly (reduce by 50% when position correlates > 0.6 with existing portfolio)
- Add: volatility multiplier (halve Kelly when market vol > 2x average)

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | Reduces ruin probability from ~33% to ~3% while retaining 56% of growth |
| Complexity | 3/10 -- math is straightforward, already partially implemented |
| Data requirements | Position data (already have), volatility estimates |
| Capital required | N/A -- sizing strategy, not capital strategy |

---

## TIER 2: MEDIUM IMPACT, MODERATE COMPLEXITY

### Strategy 5: Market Making / Liquidity Provision

**Hedge fund origin:** Core business of Citadel Securities, Virtu Financial, Jane Street. Profit from bid-ask spread capture with inventory management. Professional market makers target Sharpe 2.0-5.0.

**How it works in prediction markets:**
- Post limit orders on both YES and NO sides of a market
- Capture the spread on each round-trip (e.g., bid $0.48, offer $0.52 = $0.04 profit per round-trip)
- Critical risk: inventory accumulation -- if you end up holding only YES and the outcome is NO, you lose everything
- Polymarket: 0% maker fee + rebates make this attractive
- Professional prediction market makers target 15-30% annual returns

**Key risk factors:**
- Event risk: news can move markets 40-50 points instantly
- Binary settlement: contracts go to $0 or $1 (unlike equities which have residual value)
- Inventory management is life-or-death: must skew quotes to reduce one-sided exposure

**Implementation for Arbitrout:**
- New module: market maker engine
- Quote logic: symmetric spread around estimated fair value, skew toward reducing inventory
- Risk controls: max inventory imbalance 60/40 YES/NO, widen spreads before known events, auto-circuit breaker on sudden price moves > 10%
- Target markets: $100K-$500K daily volume (enough liquidity, not enough institutional attention to eliminate spread)

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | Sharpe 0.5-1.5, 15-30% annual returns |
| Complexity | 6/10 -- requires inventory management, quote engine, risk controls |
| Data requirements | Real-time order book depth, trade flow, event calendar |
| Capital required | $5K-$25K for meaningful returns |

---

### Strategy 6: Event-Driven News Trading (Enhanced with Tiered Latency)

**Hedge fund origin:** Event-driven is a top-performing hedge fund strategy in 2025-2026. Man Group, Citadel, and Elliott Management all run event-driven books. BNP Paribas 2026 outlook calls event-driven "gaining traction as M&A activity accelerates."

**How it works in prediction markets:**
- News breaks -> prediction market prices lag by minutes to hours
- Tier 1 (exchange data feeds): < 1 second latency, seconds of edge
- Tier 2 (wire services): 5-30 second latency, minutes of edge
- Tier 3 (RSS feeds -- Arbitrout's current approach): 2-10 minute latency, 10-60 minutes of edge
- Tier 4 (social media): variable, sometimes faster than Tier 2

**Key 2025-2026 NLP findings:**
- LLM zero-shot headline classification: up to 89.8% accuracy combined with price data
- FinBERT and GPT-based sentiment capture 45-50% of variation in short-term returns
- Binary classification (BULLISH/BEARISH/NEUTRAL) outperforms numeric scoring
- Traditional NLP (VADER, bag-of-words) is too noisy for prediction markets

**Implementation for Arbitrout (existing scanner, needs enhancement):**
- Add exchange price cross-reference: if spot price already moved > 2%, edge is gone
- Two-pass pipeline: (1) match headline to market with > $50K volume, (2) verify news is genuinely new
- Track win rate per confidence level and adjust thresholds monthly
- News edge decay half-life: 30 minutes (after 30 min, edge strength halves)

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 10-30% per news-driven trade when timing is right |
| Complexity | 5/10 -- existing scanner needs enhancement, not rebuild |
| Data requirements | RSS feeds (have), exchange price feeds (need for cross-reference) |
| Capital required | LLM API costs + existing capital |

---

### Strategy 7: Regime Detection and Strategy Switching

**Hedge fund origin:** Hidden Markov Models are the industry standard at Renaissance, Two Sigma, D.E. Shaw. 2025 research adds Wasserstein distance (optimal transport), Gaussian Mixture Models, and variational autoencoders for more robust regime detection.

**How it works in prediction markets:**
- Prediction markets exhibit regime shifts: calm periods (narrow spreads, low volume) vs. volatile periods (news-driven, wide spreads)
- Mean reversion strategies work in calm regimes; momentum/trend-following works in volatile regimes
- Market making works in calm regimes; market making is dangerous in volatile regimes
- Regime detection signals: volume > 2x 50-day average, correlation breakdowns, volatility expansion > 80% above 20-day average

**Implementation for Arbitrout:**
- Simple regime classifier (no need for HMM complexity initially):
  - CALM: volume < 1.5x average, spread < 3 cents, no pending major events
  - VOLATILE: volume > 2x average OR spread > 5 cents OR major event within 48 hours
  - CRISIS: multiple correlated markets moving > 10% in same direction
- Strategy mapping:
  - CALM -> market making + mean reversion + carry (sell overpriced longshots)
  - VOLATILE -> news trading + momentum + reduce market making exposure
  - CRISIS -> reduce all positions, widen stops, pause auto trader

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 10-20% improvement in strategy-level Sharpe by avoiding wrong-regime trades |
| Complexity | 5/10 for simple classifier, 8/10 for HMM-based |
| Data requirements | Volume, spread, event calendar data |
| Capital required | None additional |

---

### Strategy 8: Whale/Smart Money Flow Tracking (Enhanced)

**Hedge fund origin:** Institutional flow analysis. Goldman Sachs and Morgan Stanley publish flow reports. In crypto, on-chain analytics firms (Nansen, Arkham) track whale wallets.

**How it works in prediction markets:**
- Polymarket is on-chain -- every trade is publicly visible
- Track top profit leaders (not just high-volume traders -- many whales never close losing positions)
- True win rate of even top whales is typically 55-62% when counting only settled markets
- The "whale effect" can distort low-liquidity markets for weeks

**Key 2026 finding:**
- Only 0.51% of Polymarket traders are actually profitable
- Whale tracking tools: PolyScalping (real-time arbitrage detection), Polymarket JB Bot (order book depth analysis), LayerHub analytics
- Copy trading works but requires filtering: prioritize profit leaders with consistent monthly gains, not just high total P&L

**Implementation for Arbitrout (insider tracker already built):**
- Enhance existing tracker with:
  - Settlement-adjusted win rate (only count settled markets, not open positions)
  - Monthly consistency filter (positive P&L in 3+ of last 6 months)
  - Size-weighted signal (larger positions = stronger signal)
  - Inverse whale signal: when whales are wrong, they tend to lose big -- detect whale reversals

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 3-8% improvement in trade selection |
| Complexity | 4/10 -- enhancement to existing module |
| Data requirements | On-chain trade data (already collecting) |
| Capital required | None additional |

---

## TIER 3: LOWER IMPACT OR HIGHER COMPLEXITY

### Strategy 9: Decentralized AI Signal Networks (Bittensor Synth SN50)

**Hedge fund origin:** Alternative data and crowdsourced alpha. Two Sigma runs signal competitions. Renaissance uses weather data, shipping routes, TV schedules.

**How it works:**
- Bittensor Subnet 50 (Synth) runs 200+ AI miners producing Monte Carlo probabilistic price forecasts for BTC, ETH, SOL, XAU, SPY, NVDA, TSLA, AAPL, GOOGL
- Outputs are probability distributions, not point predictions
- Compare Synth probability distribution vs. Polymarket implied odds to detect 5-15%+ edges
- 4-week trial: $2K account -> $2.2K+ (~110% return), $3K -> $73K in extended testing

**Implementation for Arbitrout:**
- Integrate Synth API for crypto price contract signals on Polymarket/Kalshi
- Pipeline: fetch Synth forecast distribution -> compute implied probability for each strike -> compare vs market -> trade mispricings
- Only applicable to crypto/financial price contracts (BTC, ETH, SPY, etc.), not political/event markets

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 5-15% on crypto price contracts |
| Complexity | 6/10 -- API integration + probability distribution comparison |
| Data requirements | Synth API access (Bittensor network) |
| Capital required | $2K+ starting |

---

### Strategy 10: Synthetic Derivative Construction

**Hedge fund origin:** Options market making and volatility trading. Citadel, Susquehanna, and Jane Street construct synthetic positions from multiple legs.

**How it works in prediction markets:**
- Bull/bear spreads: buy "BTC > $70K" YES, sell "BTC > $80K" YES for range bets
- Butterfly spreads: profit from price staying in a narrow range using 3 strikes
- Calendar spreads: exploit mispricing between same-question markets with different expiry dates
- Spot + hedge: buy actual BTC + buy "BTC > $X" NO as downside protection (binary put equivalent)

**Key limitation:** Most prediction markets lack fine strike granularity. Kalshi has the best coverage for structured strategies on crypto/financial contracts.

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 3-10% from mispriced term structures and implied distributions |
| Complexity | 7/10 -- multi-leg execution, strike availability constraints |
| Data requirements | Multi-strike market data, implied probability curves |
| Capital required | $5K+ (capital tied up in multiple legs) |

---

### Strategy 11: Bayesian Real-Time Probability Updating

**Hedge fund origin:** Bayesian inference is foundational to quant finance. Used for parameter estimation, regime detection, and portfolio optimization. D.E. Shaw's AI continuously adjusts model parameters in real-time.

**How it works in prediction markets:**
- Maintain a prior probability distribution for each market based on available information
- As new data arrives (polls, economic reports, company announcements), update the posterior using Bayes' theorem
- Compare your updated posterior vs. market price -- trade when divergence exceeds threshold
- Particularly valuable for long-duration markets where information arrives gradually

**Implementation for Arbitrout:**
- For each tracked market, maintain: prior probability, evidence log, posterior probability
- Information sources: scheduled data releases, polling aggregates, economic indicators
- Update frequency: on each new data point (not on a timer)
- Advantage over LLM-only approach: creates an auditable, mathematically grounded probability trail

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 3-8% improvement in probability estimation accuracy |
| Complexity | 7/10 -- requires defining likelihood functions per market type |
| Data requirements | Structured data feeds per market category |
| Capital required | None additional |

---

### Strategy 12: Time Decay / Theta Exploitation

**Hedge fund origin:** Options theta decay strategies (iron condors, selling premium). Charles Schwab, Tastytrade, and institutional vol desks systematically sell time value.

**How it works in prediction markets:**
- Binary contracts have accelerating time decay as expiration approaches (convex curve)
- At-the-money contracts ($0.45-$0.55) have fastest time decay
- "Binary zone" begins at 3 DTE -- contract is essentially a weighted coin flip
- Deep ITM contracts (> $0.85) have minimal remaining time value
- Deep OTM contracts (< $0.15) decay quickly but have lottery-ticket convexity

**Carry strategy implementation:**
- Systematically sell overpriced longshots ($0.05-$0.15 where true probability is near zero)
- Collect small but consistent returns as contracts expire worthless
- Risk: catastrophic loss when a longshot hits -- must size tiny (< 1% of portfolio per position)
- Best for markets with short time-to-expiration (< 7 days) and clear fundamental odds

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 2-5% systematic return from theta collection |
| Complexity | 3/10 -- simple sell-side strategy |
| Data requirements | Market prices, expiration dates, fundamental probability estimates |
| Capital required | $2K+ (need diversification across many small positions) |

---

### Strategy 13: Correlation-Aware Portfolio Construction

**Hedge fund origin:** Risk parity and factor models. Bridgewater's All Weather portfolio. AQR's factor-based approach. Recent 2025 ML framework achieved Sharpe 1.38 (55% improvement over traditional risk parity).

**How it works in prediction markets:**
- Many prediction markets are structurally correlated (election outcome -> government shutdown -> Fed rates)
- Without correlation management, one adverse event can hit multiple positions simultaneously
- Target pairwise correlation < 0.6 between positions
- Sector limits: max 30% of portfolio in correlated event clusters

**Implementation for Arbitrout:**
- Correlation categories: crypto (BTC/ETH/SOL markets), politics (election/policy markets), economics (Fed/inflation markets), sports, tech
- Before opening new position: check correlation with all existing positions
- If correlation > 0.6 with existing exposure, reduce new position size by 50%
- Dynamic correlation monitoring: correlations increase during stress events (exactly when diversification is needed most)

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | 10-15% reduction in portfolio variance |
| Complexity | 6/10 -- requires correlation estimation and dynamic rebalancing |
| Data requirements | Historical price data across markets for correlation computation |
| Capital required | None additional |

---

## TIER 4: ADVANCED / INFRASTRUCTURE-HEAVY

### Strategy 14: High-Frequency Market Making with LOB Analysis

**Hedge fund origin:** Citadel Securities, Virtu Financial. Deep learning on limit order book snapshots for sub-second price prediction. VPIN (Volume-Synchronized Probability of Informed Trading) for adverse selection detection.

**How it works:**
- Requires sub-10ms latency via VPS in major data centers
- LOB features: bid/ask volumes at top 5-10 levels, spread, depth imbalance
- WebSocket streaming is critical -- Polymarket supports up to 10 instruments simultaneously
- Sub-1ms latency to Polygon RPC nodes for competitive execution

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | Sharpe 2.0-5.0 (professional HFT) |
| Complexity | 9/10 -- requires dedicated infrastructure, Rust/C++ for latency |
| Data requirements | Real-time LOB snapshots, trade-by-trade data |
| Capital required | $25K+ plus infrastructure costs |

---

### Strategy 15: Multi-Factor Quantitative Model

**Hedge fund origin:** Every major quant fund runs multi-factor models. Recent 2025 research (arXiv 2507.07107) shows ML-enhanced cross-sectional factor models with bias correction outperform traditional approaches.

**Factors applicable to prediction markets:**
1. Value: divergence between model probability and market price
2. Momentum: 7-day price trend (winning contracts tend to keep winning)
3. Liquidity: bid-ask spread as risk premium (wider spread = higher risk premium)
4. Sentiment: LLM-derived news sentiment score
5. Smart money: whale position direction and size
6. Time decay: DTE-adjusted expected return
7. Volatility: recent price variance relative to historical

**Implementation:**
- Score each market on all 7 factors, normalize to z-scores
- Combine with ML-derived weights (XGBoost or simple linear combination)
- Trade markets in top quintile of composite score
- Requires 6+ months of historical data for factor weight calibration

**Assessment:**
| Metric | Value |
|--------|-------|
| Expected edge | Sharpe 0.8-1.5 if well-calibrated |
| Complexity | 8/10 -- requires factor definition, weight estimation, continuous recalibration |
| Data requirements | 6+ months historical data per factor |
| Capital required | $10K+ for diversified factor portfolio |

---

## PRIORITY IMPLEMENTATION ROADMAP

Ranked by (expected impact x 1/complexity):

| Rank | Strategy | Impact | Complexity | Priority Score | Status in Arbitrout |
|------|----------|--------|------------|----------------|---------------------|
| 1 | Favorite-Longshot Bias Filter | HIGH | 2/10 | 5.0 | Not yet -- add to auto trader |
| 2 | Fractional Kelly + Portfolio Caps | HIGH | 3/10 | 3.3 | Partially built -- add caps |
| 3 | Time Decay / Theta Exploitation | MED | 3/10 | 2.3 | Not yet -- add carry strategy |
| 4 | LLM Mispricing Detection | HIGH | 4/10 | 2.5 | Partially -- enhance AI scanner |
| 5 | Enhanced Whale Tracking | MED | 4/10 | 1.8 | Built -- needs enhancements |
| 6 | Cross-Platform Arb (Enhanced) | HIGH | 5/10 | 2.0 | Built -- needs fee/resolution fixes |
| 7 | News Trading Enhancement | MED | 5/10 | 1.4 | Built -- needs exchange cross-ref |
| 8 | Regime Detection | MED | 5/10 | 1.4 | Not yet -- add simple classifier |
| 9 | Market Making | MED-HIGH | 6/10 | 1.3 | Not yet -- new module needed |
| 10 | Correlation Portfolio | MED | 6/10 | 1.2 | Not yet -- add to portfolio manager |
| 11 | Synth SN50 Signals | MED | 6/10 | 1.2 | Not yet -- API integration |
| 12 | Synthetic Derivatives | LOW-MED | 7/10 | 0.6 | Partially -- spec exists |
| 13 | Bayesian Updating | LOW-MED | 7/10 | 0.6 | Not yet |
| 14 | Multi-Factor Model | MED | 8/10 | 0.6 | Not yet |
| 15 | HFT Market Making | HIGH | 9/10 | 0.6 | Not yet -- infrastructure needed |

---

## KEY METRICS FROM THE RESEARCH

**Market landscape (March 2026):**
- Polymarket: $2.1B/week volume, 30%+ wallets are AI agents
- Kalshi: $2.7B/week volume (53% market share), CFTC-regulated
- Total prediction market volume 2025: $44B+
- Only 0.51% of Polymarket traders are profitable
- 14 of top 20 most profitable Polymarket wallets are bots

**Fee environment (post-February 2026):**
- Polymarket: 0% maker + rebates, 0.1-1.56% taker (dynamic)
- Kalshi: 0% currently (promotional)
- Minimum profitable arb spread: 5% (below this, fees eat profit)

**AI agent performance:**
- Polystrat: 4,200+ trades in first month, up to 376% single-trade return
- AI agents: 37%+ positive P&L rate vs. 7-13% for humans
- Multi-model LLM consensus: adds 15-20% calibration improvement
- Synth SN50: $2K -> ~110% return in 4-week trial on Polymarket

**Hedge fund quant context (2025-2026):**
- Renaissance Medallion: 30% return in 2024, Sharpe 2.0-7.5
- Two Sigma Spectrum: 10.9% return in 2024
- Systematic stock-trading hedge funds: +12% in H1 2025
- 64% of institutional allocators plan to increase hedge fund exposure in 2026

---

## DATA INFRASTRUCTURE REQUIREMENTS

For full strategy implementation, Arbitrout needs:

**Already available:**
- Real-time prices from 9 platform adapters
- On-chain Polymarket wallet tracking (insider tracker)
- RSS news feeds + AI analysis
- Paper trading execution across 10 executors

**Needed for Tier 1-2 strategies:**
- Exchange price feeds (Binance/Coinbase WebSocket) for news cross-reference
- Historical price data storage for correlation computation and factor backtesting
- LLM API access for multi-model probability estimation
- Event calendar with major scheduled releases (Fed meetings, earnings, elections)

**Needed for Tier 3-4 strategies:**
- Bittensor/Synth SN50 API for probabilistic crypto forecasts
- Real-time order book depth data (Polymarket WebSocket, Kalshi API)
- VPS with < 50ms latency to Polygon nodes (for market making)
- Factor database with 6+ months of historical data

---

## Sources

### Prediction Market Strategies
- [Systematic Edges in Prediction Markets -- QuantPedia](https://quantpedia.com/systematic-edges-in-prediction-markets/)
- [Market Making on Prediction Markets: Complete 2026 Guide](https://newyorkcityservers.com/blog/prediction-market-making-guide)
- [Prediction Market Arbitrage Guide: Strategies for 2026](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide)
- [Polymarket vs Kalshi Explained](https://www.quantvps.com/blog/polymarket-vs-kalshi-explained)
- [Prediction Markets Are Turning Into a Bot Playground](https://www.tradingview.com/news/financemagnates:7f126ddf1094b:0-prediction-markets-are-turning-into-a-bot-playground/)
- [AI Agents Are Quietly Rewriting Prediction Market Trading](https://www.coindesk.com/tech/2026/03/15/ai-agents-are-quietly-rewriting-prediction-market-trading)
- [Application of the Kelly Criterion to Prediction Markets (arXiv:2412.14144)](https://arxiv.org/html/2412.14144v1)
- [How Prediction Market Traders Make Money -- NPR](https://www.npr.org/2026/01/17/nx-s1-5672615/kalshi-polymarket-prediction-market-boom-traders-slang-glossary)
- [Polymarket AI Trading: Use ChatGPT & Claude to Win](https://vpn07.com/en/blog/2026-polymarket-ai-trading-chatgpt-claude-probability-estimation.html)
- [Bloomberg: How Prediction Markets Are Gamifying Truth](https://www.bloomberg.com/features/2026-prediction-markets-polymarket-kalshi/)

### Hedge Fund & Quant Strategies
- [The Case for Re-Evaluating Quant -- Hedge Fund Journal](https://thehedgefundjournal.com/the-case-for-re-evaluating-quant/)
- [2026 Hedge Fund Outlook -- BNP Paribas](https://globalmarkets.cib.bnpparibas/2026-hedge-fund-outlook/)
- [Hedge Fund Strategy Outlook Q1 2026 -- Man Group](https://www.man.com/insights/Q1-2026-Hedge-Fund-Strategy-Outlook)
- [JP Morgan: Hedge Fund Outlook 2026](https://am.jpmorgan.com/us/en/asset-management/per/insights/market-insights/market-updates/on-the-minds-of-investors/what-is-the-outlook-for-hedge-funds-in-2026/)
- [Top Quantitative Hedge Funds 2026 -- AlphaMaven](https://alpha-maven.com/hedge-funds/top-quantitative-funds)
- [Systematic Strategies & Quant Trading 2025 -- Gresham](https://www.greshamllc.com/media/kycp0t30/systematic-report_0525_v1b.pdf)
- [Quant Hedge Funds Capitalize on Market Swings](https://arootah.com/blog/hedge-fund-and-family-office/quant-hedge-funds-leverage-market-volatility/)

### Machine Learning & AI
- [Sentiment-Aware Stock Price Prediction with Transformer and LLM-Generated Alpha (arXiv)](https://arxiv.org/html/2508.04975v1)
- [Generating Alpha: Hybrid AI-Driven Trading System (arXiv)](https://arxiv.org/html/2601.19504v1)
- [LSTM-Transformer Hybrid for Financial Time Series](https://www.mdpi.com/2413-4155/7/1/7)
- [ML-Enhanced Multi-Factor Quantitative Trading (arXiv)](https://arxiv.org/html/2507.07107)
- [Decoding Market Regimes with Machine Learning -- SSGA](https://www.ssga.com/library-content/assets/pdf/global/pc/2025/decoding-market-regimes-with-machine-learning.pdf)

### Market Microstructure
- [Order-Flow Alpha & Microstructure](https://mas-markets.com/order-flow-alpha-microstructure-where-speed-becomes-strategy/)
- [Automated Market Makers: More Profitable Liquidity Provisioning (arXiv)](https://arxiv.org/html/2501.07828v1)
- [Polymarket CLOB Introduction](https://docs.polymarket.com/developers/CLOB/introduction)

### AI Agents & Bittensor
- [Claude AI Bots Dominate Prediction Markets](https://beincrypto.com/claude-ai-polymarket-trading-bots-millions/)
- [Synth SN50 -- Bittensor Subnet](https://subnetalpha.ai/subnet/synth/)
- [Probability Clouds Over Price Predictions: Synth SN50](https://www.synapz.org/posts/2025-06-19-synth-sn50-mandelbrot-validation)
- [Capitalizing on Prediction Markets 2026: Institutional Strategies](https://www.ainvest.com/news/capitalizing-prediction-markets-2026-institutional-grade-strategies-market-making-arbitrage-2601/)

### Portfolio Construction & Risk
- [ML Approach to Risk-Based Asset Allocation -- Nature](https://www.nature.com/articles/s41598-025-26337-x)
- [Prediction Markets at Scale: 2026 Outlook](https://insights4vc.substack.com/p/prediction-markets-at-scale-2026)
- [Awesome Prediction Market Tools (GitHub)](https://github.com/aarora4/Awesome-Prediction-Market-Tools)
