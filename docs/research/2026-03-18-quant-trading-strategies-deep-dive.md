# Quantitative Trading Strategies Deep Dive: Hedge Fund & Quant Firm Techniques (2024-2026)

**Date:** 2026-03-18
**Purpose:** Concrete, implementable strategies from hedge funds, quant firms, and academic research
**Scope:** Statistical arbitrage, microstructure, ML, news trading, crypto, risk management, prediction markets

---

## 1. Statistical Arbitrage / Pairs Trading

### How Top Firms Approach Stat Arb

**Renaissance Technologies (Medallion Fund):**
- Operates at timeframes from seconds to days (not months)
- Two-phase system: "scoring" ranks every available stock by investment desirability; "risk reduction" combines high-scored stocks into a portfolio that minimizes aggregate risk
- Opens simultaneous long and short positions to exploit inefficient pricing in correlated securities
- Mean-reversion component: buys futures that opened unusually low vs. previous close, sells those that opened unusually high
- Performance: Sharpe ratio of 2.0-7.5 (peaked at 7.5 in 2004), arithmetic mean return of 66.1%, standard deviation of 31.7%
- Philosophy: Identify statistically significant patterns repeated thousands of times. Never try to explain *why* a pattern exists -- only that it is reliable.

**Two Sigma:**
- Runs >100,000 simulations on market data daily
- Toolkit ranges from simple ridge regressions to NLP
- Collects structured + unstructured data: market prices, economic indicators, social media, satellite imagery, credit card flows
- Known for crowdsourcing -- holds competitions to discover new trading signals

**D.E. Shaw:**
- Research-intensive computational finance + AI
- Built on statistical arbitrage as core business
- AI continuously analyzes strategy performance and adjusts parameters in real-time
- ML models predict which parameter adjustments lead to better performance using historical + live data

### Cointegration Techniques (State of the Art)

**1. Engle-Granger Two-Step (Classic)**
- Step 1: Regress Y on X, get residuals
- Step 2: Test residuals for stationarity (ADF test)
- If residual is stationary, the pair is cointegrated
- Limitation: Only tests one cointegrating relationship

**2. Johansen Test (Multivariate)**
- Tests for multiple cointegrating relationships among N assets simultaneously
- Recent papers show multivariate cointegration (3-5 assets) produces more stable spreads than pairs
- Allows portfolio construction from N assets where the linear combination is mean-reverting

**3. Kalman Filter (Dynamic Hedge Ratio) -- Most Used in Practice**
- Treats the hedge ratio as a hidden state variable, estimated dynamically
- Adapts to changing relationships in real time (unlike static OLS)
- Implementation: `pykalman` library in Python
- The Kalman Filter updates the cointegration relationship and feeds a mean-reversion strategy
- Advantage over OLS: captures time-varying betas that OLS misses entirely

**4. Ornstein-Uhlenbeck Process (Spread Modeling)**
- Continuous-time analogue of discrete AR(1) process
- Fit the spread to an OU process to extract: mean-reversion speed (theta), long-run mean (mu), volatility (sigma)
- Half-life of mean reversion = ln(2) / theta -- determines holding period
- Typical half-lives for tradeable pairs: 5-20 days (equity pairs), 1-5 days (ETF pairs)
- Trade when spread deviates >2 sigma from mean; exit when it crosses the mean

### Pair Selection: What Works Now (2024-2026)

**ETF pairs dominate** for retail/small fund implementation:
- Same-sector ETFs (XLE/XOP, GDX/GDXJ, SPY/IVV) have highest cointegration stability
- Cross-sector pairs fail more often due to regime changes
- 2024 research: Lowering entry threshold increases trades and boosts Sharpe, but also raises drawdowns and volatility
- Key finding: Cointegration stability is the #1 predictor of pairs trading success

**ML-enhanced pair selection:**
- Use clustering (DBSCAN, hierarchical) on return features to find candidate pairs
- Filter candidates through cointegration tests
- Random forests for feature importance in pair stability prediction

### Implementation Specifics

```
ENTRY_THRESHOLD: 2.0 sigma (standard deviation from mean spread)
EXIT_THRESHOLD: 0.0 sigma (mean reversion to center)
STOP_LOSS: 4.0 sigma (pair has broken down)
LOOKBACK_WINDOW: 60 days (for cointegration test)
RETEST_FREQUENCY: weekly (re-run cointegration test)
HALF_LIFE_MIN: 3 days (shorter = too noisy)
HALF_LIFE_MAX: 30 days (longer = capital tied up too long)
KALMAN_OBSERVATION_COVARIANCE: auto-calibrate (pykalman)
MIN_HURST_EXPONENT: < 0.4 (confirms mean-reverting spread)
```

| Metric | Typical Value |
|--------|--------------|
| Expected Sharpe Ratio | 1.0-2.5 (ETF pairs), 0.8-1.5 (equity pairs) |
| Win Rate | 55-65% |
| Average Trade Duration | 5-15 days |
| Max Drawdown | 10-25% |
| Capital Required | $50K+ (need multiple pairs for diversification) |
| Complexity to Implement | 6/10 |
| Applicable to Prediction Markets? | Partially -- correlated prediction contracts (e.g., "BTC > 70K" vs "BTC > 80K") can be treated as synthetic pairs |

---

## 2. Market Microstructure Exploitation

### Order Flow Analysis

**VPIN (Volume-Synchronized Probability of Informed Trading):**
- Measures probability that informed traders are active by looking at order flow imbalance across fixed-volume buckets
- Synchronizes to volume time (not calendar time) so high-activity and low-activity periods are weighted equally
- Sudden jumps from baseline indicate regime change, often preceding large price moves
- The 2010 Flash Crash was preceded by elevated VPIN readings
- **Caution:** Academic debate ongoing -- some papers show VPIN is a poor predictor of short-run volatility and reached all-time high *after* the Flash Crash, not before
- Implementation: Open source at `github.com/yt-feng/VPIN`

**Order Flow Imbalance (OFI):**
- Computed as the net imbalance between buy-initiated and sell-initiated volume at each price level
- Persistent OFI in one direction predicts short-term price movement (1-5 minutes)
- Best used as a confirmation signal alongside other indicators, not standalone

**Limit Order Book (LOB) Analysis:**
- Deep learning (CNNs, LSTMs) on LOB snapshots for mid-price direction prediction
- Features: bid/ask volumes at top 5-10 levels, spread, depth imbalance, trade imbalance
- Stock microstructural characteristics influence deep learning efficacy -- works better on liquid, high-tick-size stocks
- Timeframe: microseconds to minutes

### Bid-Ask Spread Capture (Market Making)

**How it works:**
- Continuously post limit orders on both sides of the book
- Capture the spread on each round-trip (buy at bid, sell at ask)
- Key risk: adverse selection (informed traders pick you off)
- Key metric: Fill rate vs. adverse selection rate

**Queue Position Strategies:**
- Price-time priority: earlier orders at the same price execute first
- Being first in queue at a price level is enormously valuable
- Strategies: "penny jumping" (posting 1 tick ahead), rapid cancel-replace cycles
- On prediction markets (Polymarket CLOB): queue position matters, cancel-replace interval of 500ms matches taker delay

**Applicable to Prediction Markets:**
- Polymarket uses a CLOB -- standard microstructure strategies apply
- Spreads: 2-5 cents on Polymarket, 3-8 cents on Kalshi
- Market making capital requirement: $5,000-$25,000 for meaningful returns
- Key difference from equity MM: contracts settle at 0 or 1 (total inventory loss risk)
- Inventory management is critical -- can't hold losing side to expiry

### Implementation Specifics

```
SPREAD_CAPTURE_MIN_SPREAD: 3 cents (below this, fees eat profit)
CANCEL_REPLACE_INTERVAL: 500ms
MAX_INVENTORY_IMBALANCE: 60/40 (YES/NO ratio)
ADVERSE_SELECTION_MONITOR: track fill rate on each side
QUEUE_PRIORITY: always use limit orders (0% maker fee + rebates on Polymarket)
LOB_SNAPSHOT_DEPTH: top 10 levels
```

| Metric | Typical Value |
|--------|--------------|
| Expected Sharpe Ratio | 2.0-5.0 (professional HFT), 0.5-1.5 (prediction market MM) |
| Win Rate | 52-55% per trade (high volume compensates) |
| Capital Required | $5K-$25K (prediction markets), $1M+ (equity HFT) |
| Complexity to Implement | 8/10 (equity), 5/10 (prediction market MM) |
| Applicable to Prediction Markets? | YES -- directly. Polymarket CLOB and Kalshi both support limit order market making |

---

## 3. Momentum / Mean Reversion

### Timeframes by Strategy Type

**Momentum works at:**
- Cross-sectional (12-1 month): Classic Jegadeesh-Titman. Buy past 12-month winners, sell losers, skip most recent month. Sharpe ~0.5-0.8 historically.
- Time-series (1-12 months): Go long assets with positive past returns, short those with negative. Works across asset classes (stocks, bonds, currencies, commodities).
- Short-term (1-5 days): Intraday momentum driven by order flow persistence. Requires high-frequency data.
- **NOT at:** <1 hour (noise dominates) or >18 months (reversal dominates)

**Mean reversion works at:**
- Intraday (minutes to hours): Best edge, fastest decay. RSI(2) < 10 on SPY: 75% win rate, Sharpe ~2.85 in backtests
- Daily (1-10 days): Bollinger Band touches + RSI confirmation. Sharpe ~2.1, 69% win rate over 25 years on S&P
- Weekly (5-20 days): Pairs trading sweet spot. OU half-life determines optimal timeframe.
- **NOT at:** >1 month (trends dominate)

### Regime Change Detection (Critical for Both)

**Hidden Markov Models (HMM) -- Industry Standard:**
- Model market as transitioning between 2-3 hidden states (bull, bear, neutral)
- Each state has its own return distribution (mean, variance)
- HMM estimates transition probabilities between states
- Use volatility clustering as primary feature (works well due to vol clustering phenomenon)
- Recent advance (2025): Multi-model ensemble HMM voting framework combines bagging and boosting with HMM for state identification

**Regime Change Detection Signals:**
1. Volume profile shifts exceeding 2 standard deviations from 50-day average
2. Correlation breakdowns: historically linked assets dropping below 0.3 correlation
3. Volatility expansion: >80% above 20-day average
4. Mean reversion win rate drops 45% in first 20 trading days of a regime change

**Wasserstein Distance (Cutting Edge -- 2025):**
- Uses optimal transport theory instead of parametric HMM
- More robust, data-driven alternative to HMMs
- Computes distance between current return distribution and historical regime distributions
- No need to pre-specify number of states

**Online Changepoint Detection (CPD) + Deep Learning:**
- Insert CPD module into deep momentum network pipeline
- LSTM architecture simultaneously learns trend estimation and position sizing
- Addresses momentum strategies' vulnerability at turning points
- "Slow Momentum with Fast Reversion" (PM-Research, 2024): Combines long-term momentum signal with fast mean-reversion exit when CPD detects regime break

### Indicators Beyond RSI/MACD

| Indicator | What It Does | Edge |
|-----------|-------------|------|
| Hurst Exponent | Measures degree of mean reversion (H<0.5) or trending (H>0.5) | Determines regime before trading |
| Z-Score of spread | Standard deviations from rolling mean | Entry/exit for mean reversion |
| ATR (Average True Range) | Volatility-adjusted range | Dynamic stop-loss sizing (2x ATR stops, 3x targets) |
| VWAP deviation | Distance from volume-weighted average price | Intraday mean-reversion signal |
| IBS (Internal Bar Strength) | (Close - Low) / (High - Low) | IBS < 0.2 = strong mean-reversion buy signal |
| Keltner Channels | ATR-based bands (vs. Bollinger's stddev) | More stable in trending markets |
| OBV (On-Balance Volume) | Cumulative volume flow | Confirms momentum direction |
| Put/Call Ratio | Options market sentiment | Contrarian mean-reversion signal at extremes |

### Concrete Mean Reversion Strategy (Backtested)

**RSI(2) Strategy on SPY/QQQ:**
```
Entry: RSI(2) < 10 AND price below lower Bollinger Band (20,2)
Exit: RSI(2) > 50 OR after 7 trading days (whichever first)
Stop: 2x ATR below entry
Position size: Quarter Kelly based on historical win rate
```
Backtest results (25 years, S&P 500):
- Win rate: ~75%
- Sharpe ratio: ~2.85
- Profit factor: ~3
- Max drawdown: ~19.5%

| Metric | Momentum | Mean Reversion |
|--------|----------|----------------|
| Expected Sharpe Ratio | 0.5-1.0 (long-term), 1.0-2.0 (short-term) | 1.5-3.0 (intraday/daily) |
| Win Rate | 40-55% (wins are large) | 65-80% (wins are small) |
| Capital Required | $25K+ | $10K+ |
| Complexity to Implement | 5/10 (basic), 8/10 (with regime detection) | 4/10 (basic), 7/10 (adaptive) |
| Applicable to Prediction Markets? | YES -- momentum in contract prices exists (trending toward 0 or 1 as info resolves); mean reversion works on short-term overreactions |

---

## 4. News/Event-Driven Alpha

### How Citadel, Point72, Two Sigma Use NLP

**Point72 (Cubist division):**
- NLP-powered sentiment analysis on earnings calls
- Automatically incorporates insights into options trading strategies
- Uses alternative data + quant strategies as core differentiator
- 2024 return: 19% | 2025 return: 17.5%

**Citadel:**
- Employs reinforcement learning to optimize trading strategies
- AI learns best policies to maximize returns while minimizing risks
- Wellington fund: 15.1% return in 2024

**Two Sigma:**
- NLP across all text data: news, social media, regulatory filings, earnings transcripts
- Generates sentiment scores, event detection signals, market impact indicators for >12 million entities
- Crowdsources signal discovery through competitions

### Latency Requirements and Edge Decay

| Source Tier | Latency to Market | Edge Duration | Suitable For |
|-------------|-------------------|---------------|-------------|
| Exchange data feeds | <1 second | Seconds | HFT only |
| Wire services (Reuters, Bloomberg) | 5-30 seconds | Minutes | Institutional |
| RSS feeds (CoinDesk, CNBC) | 2-10 minutes | 10-60 minutes | Retail/small fund |
| Social media (X, Reddit) | Variable | Minutes to hours | Sentiment analysis |
| Mainstream (NYT, CNN) | 30-60 minutes | Hours (often priced in) | Background context only |

**Critical finding:** Low-latency trading activity is significantly more pronounced during announcement windows and quickly declines to normal within days. The half-life of news alpha is approximately 30 minutes for prediction markets and 2-4 hours for equity markets.

### Earnings Announcement Drift (PEAD) -- Longest-Studied Anomaly

- Signal starts at full strength immediately after earnings announcement
- Steadily declines over following days
- Plateaus around Day 9 (the "exit zone")
- Entry: Buy if earnings surprise > +5% and volume spikes above average
- Hold period: 5-20 trading days depending on backtested decay
- NLP enhancement: Process earnings call transcripts in real-time, detect cautious language patterns that signal hidden concerns
- Edge: NLP analysis catches language shifts before written reports reflect them

### LLM-Based Sentiment (Best Performer in 2024-2025)

**Model performance comparison:**
| Model | Sharpe Ratio (Long-Short) | Accuracy |
|-------|--------------------------|----------|
| OPT (GPT-3 based) | 3.05 | 74.4% |
| BERT | 2.11 | ~68% |
| FinBERT | 2.07 | ~67% |
| Traditional NLP (VADER) | <1.0 | ~55% |

**Implementation approach:**
1. Binary classification (BULLISH/BEARISH/NEUTRAL) outperforms numeric scoring
2. Forward-looking implied sentiment captures 45-50% of variation in stock returns
3. Chain-of-Thought prompting on generative LLMs (Llama 3.1, Mistral) improves over zero-shot
4. Combine LLM sentiment with technical signals for entry confirmation

### Fed Decisions and Macro Events

- Events create "compressed risk" -- multiple macro events on same day amplify volatility
- Pre-announcement: Implied vol rises 48-72 hours before scheduled Fed meetings
- Post-announcement: Dot plot and forward guidance matter more than the rate decision itself
- Trading strategy: Enter positions based on the *deviation from consensus*, not the event itself
- Prediction market edge: Kalshi economic data markets (Fed, CPI, NFP) consistently produce cross-platform discrepancies vs. Polymarket

| Metric | Typical Value |
|--------|--------------|
| Expected Sharpe Ratio | 1.5-3.0 (LLM sentiment), 0.8-1.5 (traditional NLP) |
| Edge Decay Half-Life | 30 min (prediction markets), 2-4 hours (equities) |
| Capital Required | $10K+ (prediction markets), $100K+ (equities) |
| Complexity to Implement | 5/10 (RSS + LLM), 8/10 (real-time wire + ML pipeline) |
| Applicable to Prediction Markets? | YES -- directly. News moves prediction market prices with slower adjustment than equities |

---

## 5. Machine Learning Approaches (What's Actually Used)

### Model Hierarchy (Real Production Use)

**Tier 1: Gradient Boosted Trees (XGBoost/LightGBM) -- Workhorse**
- Most widely used ML model in production quant systems
- Why: handles missing data, feature interactions, non-linear relationships, provides feature importance
- Typical config: 1000-3000 estimators, learning rate 0.05-0.1
- SHAP values for interpretability (required by risk teams)
- Recent result: Multi-factor framework with XGBoost achieved ~20% annualized returns, Sharpe >2.0 (2021-2024)
- Best features: "distance from 52-week high" often dominates; technical indicators provide additional context for price movements

**Tier 2: LSTM / Transformer Networks -- Time Series**
- LSTMs: Standard for sequential financial data. Proven in hedging (Buehler et al. 2019 framework)
- Transformers: Attention mechanism captures long-range dependencies better than LSTM
- Temporal Fusion Transformer: Specialized variant for multi-horizon forecasting
- "Galformer" (2024): Generative decoding + hybrid loss function for multi-step stock index prediction
- Hybrid LSTM-CNN + XGBoost: Combines sequence learning with ensemble robustness
- Key limitation: Transformers struggle with non-stationarity in financial data

**Tier 3: Reinforcement Learning -- Execution & Portfolio Optimization**
- Citadel uses RL to optimize trading strategies (learns policies for max returns, min risk)
- DQN: Most reliable for optimal execution (outperforms TRPO, PPO, actor-critic in comparisons)
- PPO: Best for cryptocurrency trading across market conditions
- DDPG: Strong for continuous action spaces (market making)
- Hybrid adoption: LSTM-DQN (15.4% gain in portfolio optimization), CNN-PPO (17.9% in crypto), Attention-DDPG (16.3% in market making)
- Hybrid approach adoption: 15% in 2020 -> 42% in 2025

**Tier 4: Random Forests -- Feature Selection & Ensembles**
- Used more for feature importance ranking than primary prediction
- Insider trading detection: XGBoost trees approach identifies unlawful transactions
- Combine with boosted trees in ensemble for robustness

### Features That Actually Matter

**Most important features (ranked by SHAP importance across studies):**
1. Distance from 52-week high/low
2. Volatility (realized vs. implied, ratio)
3. Volume profile (vs. 20-day, 50-day averages)
4. Momentum signals (1-day, 5-day, 20-day returns)
5. Order flow imbalance (buy vs. sell volume)
6. Spread (bid-ask) dynamics
7. Sector relative strength
8. Sentiment scores (NLP-derived)
9. Cross-asset correlations
10. Macro regime indicators (VIX level, yield curve slope)

**Feature engineering pipeline (production systems):**
- Alpha101-style factors: 500-1000 factors from price, volume, fundamental data
- Rolling calculations: 5-day, 20-day, 60-day windows
- Cross-sectional: rank within sector, rank within universe
- Interaction features: momentum * volatility, sentiment * volume
- Bias correction: systematic factor engineering with stability assessment

### What Doesn't Work (Despite Hype)

- Pure LSTM price prediction (without ensemble/hybrid): High variance, poor out-of-sample
- Single neural network for everything: Overfits to training regime
- Unstructured deep learning on raw price data: Features matter more than architecture
- AutoML without domain knowledge: Produces spurious correlations

| Metric | Gradient Boosted Trees | LSTM/Transformer | RL |
|--------|----------------------|------------------|-----|
| Expected Sharpe | 1.5-2.5 | 1.0-2.0 | Varies by task |
| Primary Use | Cross-sectional prediction | Time-series forecasting | Execution optimization |
| Data Required | 5+ years daily | 2+ years (higher frequency better) | 1+ year for training |
| Capital Required | $100K+ (needs diversified portfolio) | $50K+ | N/A (execution layer) |
| Complexity | 6/10 | 8/10 | 9/10 |
| Prediction Markets? | YES -- features adapt to binary contracts | YES -- contract price time series | YES -- order execution |

---

## 6. Options/Derivatives Strategies for Binary Outcomes

### How Market Makers Hedge Binary Options

**Traditional binary option hedging:**
- Market makers use dynamic delta hedging with the underlying asset
- As binary approaches expiry, gamma explodes (delta changes from 0 to 1 in a tiny price range)
- Near expiry, hedging becomes extremely difficult -- this is where market makers make OR lose the most money
- Solution: widen spreads dramatically near expiry (evident in prediction market spread widening near resolution)

**Prediction market-specific hedging:**
- Market makers on Polymarket/Kalshi maintain positions on both YES and NO sides
- Ideal: balanced inventory (50/50 YES/NO exposure captures pure spread)
- Reality: inventory drifts as informed traders push one side
- Risk: contracts settle at 0 or 1 -- unbalanced inventory at resolution = total loss on one side

### Volatility Trading Applied to Prediction Markets

**Gamma scalping (adapted):**
- In traditional markets: buy options (long gamma), delta-hedge with underlying, profit from realized vol > implied vol
- In prediction markets: buy both YES and NO when combined cost < $1.00 (market-wide gamma equivalent)
- Rebalance as prices move to maintain near-neutral exposure
- Citadel and Millennium deploy structured gamma scalping as core multi-strategy components
- Performance: Citadel Wellington 15.1% (2024), Millennium 15% (2024)

**Dispersion trading (adapted):**
- In traditional markets: sell index options, buy component stock options (exploit correlation premium)
- In prediction markets: sell "broad event" contracts, buy specific sub-event contracts
- Example: Sell "recession in 2026" YES, buy specific economic indicators (CPI, unemployment, GDP) -- if specific indicators move independently, you profit from low correlation
- Assenagon Alpha Premium: Best performing dispersion fund 2024

### Portfolio of Binary Bets -- Optimal Construction

**Kelly Criterion for Binary Contracts:**
```
f* = (b * p_true - (1 - p_true)) / b

where:
  b = (1 - market_price) / market_price  (net odds)
  p_true = your estimated true probability
```

**Multi-position portfolio rules:**
1. Individual position: Quarter Kelly (0.25x) of Kelly-optimal fraction
2. Maximum single position: 5% of portfolio
3. Maximum total exposure: 40% of bankroll across all positions
4. Correlated positions: If correlation >0.6, reduce combined sizing by 50%
5. Sum of Kelly allocations should never exceed 40% of bankroll

**The "Barbell" portfolio (QuantPedia):**
- 70-80%: High-probability favorites ($0.75-$0.90) for steady 5-15% returns
- 20-30%: Carefully selected longshots (where your model says true prob is 2-3x market price) for occasional 3-5x returns

**The "Carry" portfolio:**
- Systematically sell overpriced longshots ($0.05-$0.15 where true prob near zero)
- Small, consistent returns as contracts expire worthless
- Must size small enough to survive the inevitable longshot hitting

| Metric | Typical Value |
|--------|--------------|
| Expected Sharpe Ratio | 0.8-1.5 (barbell), 1.0-2.0 (carry), 2.0-5.0 (market making) |
| Win Rate | 55-65% (barbell), 80-90% (carry, small wins), 52-55% (MM) |
| Capital Required | $5K-$25K (prediction market strategies) |
| Max Drawdown | 15-30% (barbell), 10-20% (carry, but tail risk), 5-15% (MM) |
| Complexity to Implement | 4/10 (barbell), 3/10 (carry), 7/10 (market making) |
| Applicable to Prediction Markets? | YES -- these ARE prediction market strategies |

---

## 7. Crypto-Specific Strategies

### Funding Rate Arbitrage (Most Reliable)

**How it works:**
- Perpetual futures use funding rates to keep price close to spot
- If funding is positive (longs pay shorts), go: LONG spot + SHORT perp
- Collect funding payments every 4-8 hours (hourly on some DEXs)
- Position is delta-neutral (price movement doesn't matter)

**Concrete numbers (2025-2026):**
- Average funding rate: 0.01-0.03% per 8-hour period (normal)
- Bullish periods: 0.05-0.2% per 8-hour period
- Annualized yield: 10-33% APY (varies with market conditions)
- Cross-exchange discrepancy: BTC funding on Hyperliquid vs Binance averaged 11.4% spread
- Cross-exchange APR: 6.42% average (2.1-2.6x premium over staking yields)
- Pendle fixed-yield: Can lock in funding rate yields via yield tokenization

**Implementation:**
```python
# Simplified funding rate arb
# 1. Monitor funding rates across exchanges
# 2. When positive funding > threshold:
#    - Buy spot BTC on Exchange A
#    - Short BTC perpetual on Exchange B (or same exchange)
# 3. Collect funding every 8 hours
# 4. Exit when funding turns negative or spread compresses

FUNDING_RATE_THRESHOLD: 0.02%  # minimum per 8h period
EXCHANGES: [Binance, Hyperliquid, Bybit, OKX]
POSITION_SIZE: equal spot and perp (delta neutral)
CHECK_INTERVAL: 1 hour (adjust between funding periods)
EXIT_TRIGGER: funding < 0.005% for 3 consecutive periods
```

**Risks:**
- Funding rate can flip negative (you start paying)
- Exchange insolvency/outage risk
- Liquidation risk if margin not maintained during sharp swings
- Basis risk between spot and perp prices

### Cross-Exchange Arbitrage

**Current state (2025-2026):**
- Price discrepancies are smaller and shorter-lived as markets mature
- Easy 3-5% opportunities replaced by razor-thin margins
- Cash-and-carry (spot vs futures basis): 2-5% monthly in bull markets
- Flash loan arbitrage: Borrow, swap across DEXs, repay in single transaction. Reverts if no profit.
- Automation is mandatory -- opportunities close in milliseconds

**Capital required:** High capital + advanced bots needed. Not viable for small traders without infrastructure.

### DeFi Yield Strategies (2025-2026)

**Liquid Staking + Restaking (EigenLayer):**
- EigenLayer: $19B TVL. Restake ETH to secure multiple protocols simultaneously
- Liquid Restaking Tokens (LRTs): Tradeable claim on restaked ETH + yield
- Layers: ETH staking (~3-4%) + restaking rewards (~2-5%) + liquidity mining
- Risk: Smart contract risk, slashing risk, economic risk from AVS tokenomics

**Pendle Yield Tokenization:**
- TVL: $8.27B (August 2025), >50% of DeFi yield sector
- Separates principal from yield -- trade future returns independently
- Lock in fixed yields by selling yield tokens
- Speculate on yield direction by buying yield tokens
- Expanding to Solana and TON

**Yield-Bearing Stablecoins:**
- Combine stability + yield in single asset
- Becoming core collateral type in DeFi
- Expected to be emerging cash alternative for DAOs and institutions

### MEV (Maximal Extractable Value)

**Strategy types and profit distribution:**
| Strategy | Share of MEV | Monthly Profit (Ethereum) |
|----------|-------------|--------------------------|
| Arbitrage | 35% (~$2.5B total) | Largest category |
| Sandwich attacks | 30% (~$2.2B total) | Front-run + back-run user trades |
| Liquidations | 25% (~$1.8B total) | Liquidate undercollateralized positions |
| Other | 10% | Various |

**Total MEV extracted:** ~$24M in 30 days (Dec 2025 - Jan 2026) on Ethereum alone
**Invisible tax on users:** 0.5-2% per transaction; large trades (>$50K) face 1-3% extraction

**Flashbots and MEV-Boost:**
- MEV-Boost implements Proposer-Builder Separation (PBS)
- Validators auction blockspace to builders, increasing staking rewards by up to 60%
- Top builders: Flashbots (30-40%), BloXroute (15-20%), Titan (10-15%)
- 2026: Enshrinement via ePBS (Glamsterdam upgrade) brings auction into protocol

**Searcher strategies:**
- Require deep Ethereum/EVM expertise
- Most profit captured by <50 sophisticated searcher operations
- Entry barrier: Very high (compete against well-funded, experienced searchers)

| Strategy | Expected Return | Capital Req | Complexity | Pred Market Applicable? |
|----------|----------------|-------------|------------|------------------------|
| Funding Rate Arb | 10-33% APY | $10K+ | 5/10 | NO (crypto-specific) |
| Cross-Exchange Arb | 5-15% annual | $50K+ | 7/10 | YES (cross-platform arb) |
| DeFi Yield | 8-20% APY | $5K+ | 6/10 | NO |
| MEV | Highly variable | $50K+ | 10/10 | NO |

---

## 8. Risk Management

### How Top Firms Manage Drawdowns

**Multi-Strategy Pod Model (Citadel, Millennium, Point72):**
- Each "pod" (team of 3-8 traders) has independent risk limits
- Hard stop: Pod shut down if drawdown exceeds 3-5% of allocated capital
- Capital is reallocated from underperforming pods to outperforming ones
- Result: Firm-level drawdown is dramatically lower than any individual pod
- Millennium: 15% return in 2024, Point72: 19% -- both with max drawdowns <5%

**Tail Risk Hedging Approaches:**

| Approach | Cost | Effectiveness | When to Use |
|----------|------|--------------|-------------|
| Direct hedging (put options) | 0.5-1% of NAV/year | Strong in crashes | Always-on insurance |
| Trend-following overlay | Minimal (self-financing) | Strong in slow bear markets | Long-term overlay |
| VIX-based position scaling | None (reduces exposure) | Good in vol spikes | Dynamic sizing |
| Hybrid (Kelly-VIX) | None | Best overall | **Recommended** |

**Key finding (2025):** Combining trend-following + tail risk hedging overlays onto equity portfolios significantly enhances risk-adjusted performance. Trend-following protects in slow bears; put hedging protects in crashes.

### Position Sizing Beyond Kelly

**Fractional Kelly (Industry Standard):**
| Fraction | Growth Rate (% of max) | Drawdown Risk | Use |
|----------|----------------------|---------------|-----|
| Full Kelly (1.0x) | 100% | 33% chance of halving | Never |
| Half Kelly (0.5x) | 75% | 11% chance of halving | Aggressive |
| Quarter Kelly (0.25x) | 56% | ~3% chance of halving | **Default** |
| Tenth Kelly (0.1x) | 34% | <1% chance of halving | Conservative |

**Ed Thorp's track record:** 1/5 to 1/2 Kelly on ~100 simultaneous bets, 20% annualized over 28 years.

**The 30% Rule:** Betting 30% of Kelly-optimal reduces chance of 80% drawdown from 1-in-5 to 1-in-213 while retaining 51% of optimal growth.

**VIX-Hybrid Position Sizing (2024 Research):**
- Base: Quarter Kelly
- VIX < 15 (low vol): Scale up to Half Kelly
- VIX 15-25 (normal): Quarter Kelly
- VIX > 25 (high vol): Reduce to Tenth Kelly
- VIX > 35 (crisis): Reduce to minimum sizing or flat
- 2024 paper: Hybrid sizing consistently balanced return generation with robust drawdown control

### Correlation Risk Management

**For a portfolio of prediction market bets:**
1. Map correlations between all open positions
2. Cluster positions by event type (crypto, politics, economics, sports)
3. Maximum 30% of portfolio in any single correlated cluster
4. When opening a new position with >0.6 correlation to existing positions, reduce sizing by 50%
5. Stress test: What happens if ALL correlated positions move against you simultaneously?

**Multi-strategy diversification:**
- Minimum 3 uncorrelated strategy types running simultaneously
- Rebalance allocation based on trailing Sharpe of each strategy
- Never let one strategy exceed 40% of total capital

### Drawdown-Based Position Scaling

```
Current Drawdown    Position Size Multiplier
0-5%               1.0x (normal)
5-10%              0.75x
10-15%             0.50x
15-20%             0.25x
>20%               0.0x (stop trading, reassess)
```

**Institutional standard:** Many institutions dedicate 0.5-1% of portfolio NAV annually to hedging costs. Hedging the first 10-15% of drawdown secures most of the compounding benefit at a fraction of full coverage cost.

| Metric | Typical Value |
|--------|--------------|
| Max Single Position | 5% of portfolio |
| Max Correlated Cluster | 30% of portfolio |
| Max Total Exposure | 40% of bankroll |
| Drawdown Halt Level | 20% (reassess everything) |
| Hedging Budget | 0.5-1% of NAV annually |
| Complexity to Implement | 5/10 (rules-based), 8/10 (dynamic/ML-based) |

---

## 9. Alternative Data Signals

### What's Generating Alpha (2024-2026)

**Market size:** $14-18B in 2025, projected $33.7B by 2033. 67% of investment managers now use alternative data. Hedge funds will spend $15.4B on alt data in 2025 alone.

**J.P. Morgan finding (2024):** Hedge funds using alternative data achieved 3% higher annual returns vs. those using only traditional data.

### Data Sources Ranked by Signal Quality

**Tier 1: High Alpha, Proven**

| Source | Application | Edge | Latency |
|--------|------------|------|---------|
| Satellite imagery (parking lots) | Retail earnings prediction | 85% accuracy predicting earnings beats/misses (MIT Sloan) | Days before earnings |
| Credit card / transaction data | Revenue estimation | Direct consumer spending signal | Weekly updates |
| Web traffic / app downloads | Tech company performance | Leading indicator of user growth | Daily |
| Earnings call NLP | Sentiment extraction | Detects hidden concerns in language | Real-time |

**Tier 2: Moderate Alpha, Growing**

| Source | Application | Edge | Latency |
|--------|------------|------|---------|
| Satellite (oil storage tanks) | Crude oil supply | Shadow analysis estimates tank fill levels | Weekly |
| Satellite (crop imagery) | Agricultural commodities | Spectral analysis differentiates crop types, predicts yield before USDA reports | Seasonal |
| Job postings / hiring velocity | Business expansion/contraction | Early signal 2-4 months ahead | Weekly |
| Social media sentiment | Market-wide sentiment | Volume-weighted; requires noise filtering | Real-time |
| Shipping / AIS data | Trade flow, supply chain | Track vessel movements globally | Daily |

**Tier 3: Emerging/Niche**

| Source | Application | Edge | Latency |
|--------|------------|------|---------|
| On-chain data (crypto) | Whale tracking, DeFi flows | "Smart money" wallet labels, 500M+ addresses | Real-time |
| Government lobbying data | Regulatory prediction | Signals upcoming policy changes | Days-weeks |
| Weather data | Energy, agriculture | Combined with satellite for crop/demand prediction | Hours |
| IoT sensor data | Industrial output | Factory activity proxies | Daily |

### On-Chain Data for Crypto/Prediction Markets

**Key platforms:**
- Nansen: 20+ chains, 500M+ labeled addresses, "smart money" flow tracking
- Arkham Intelligence: 800M+ labels across 450K+ entity pages, ties wallets to real identities
- Glassnode: Deep on-chain metrics for BTC/ETH market cycle analysis

**Signal methodology:**
- Individual transactions = noise; sustained directional flow = signal
- Track accumulation/distribution patterns of known smart money wallets
- AI identifies behavioral patterns (accumulation, distribution, DeFi exits) behind transactions
- Alpha: Position ahead of narrative shifts, sector rotations, liquidity events

### Applying Alt Data to Prediction Markets

| Data Source | Prediction Market Application |
|-------------|------------------------------|
| Satellite / foot traffic | "Will Walmart beat earnings?" contracts on Kalshi |
| Social media sentiment | Political prediction markets (election, approval ratings) |
| Weather data | Kalshi weather contracts (temperature, snowfall) |
| Economic indicators (leading) | Fed rate decision contracts |
| On-chain whale flows | Crypto price prediction contracts on Polymarket |
| Job postings data | Recession prediction contracts |
| Web traffic | "Will company X IPO?" or tech adoption contracts |

| Metric | Typical Value |
|--------|--------------|
| Alpha Improvement | +3% annual returns (J.P. Morgan study), up to 25% predictive accuracy improvement |
| Capital Required | $1K+ (free data), $10K/year (premium data feeds) |
| Complexity to Implement | 4/10 (social sentiment), 7/10 (satellite), 9/10 (multi-source fusion) |
| Applicable to Prediction Markets? | YES -- directly maps to specific contract types |

---

## 10. Prediction Market-Specific Strategies

### Are Funds Trading Prediction Markets?

**Scale of institutional activity (2025-2026):**
- Polymarket: $9B traded in 2024, sustained >$2B weekly in 2025
- Kalshi: $50B annualized volume in 2025 (up from $300M in 2024), 60% global market share
- Kalshi: $1B funding round at $11B valuation (Dec 2025)
- 14 of 20 most profitable Polymarket wallets are automated bots
- Arbitrage bots extracted ~$40M from Polymarket alone (Apr 2024 - Apr 2025)
- Nasdaq and Cboe entering binary contracts market (2026)

### Strategies Unique to Binary Outcome Markets

**1. Favorite-Longshot Bias Exploitation**
- Contracts >$0.80: actual win rate HIGHER than implied (systematic buy signal)
- Contracts <$0.15: actual win rate LOWER than implied (systematic sell signal)
- On Kalshi: buyers of contracts <$0.10 lose >60% of money
- Implementation: Weight portfolio toward favorites, against longshots
- Edge: 5-15% improvement in win rate
- Complexity: 2/10

**2. Cross-Platform Arbitrage**
- Price differences between Polymarket and Kalshi last 2-7 seconds (fast arb) or minutes-hours (structural arb)
- Example (Feb 2026): LA Mayoral election -- 58c YES on Kalshi + 35c NO on Polymarket = 93c cost, guaranteed $1 payout = 7.53% return
- Fee advantage: Polymarket 0.01% vs Kalshi ~1.2%. Polymarket preferred for high-frequency leg.
- Economic data markets (Fed, CPI, NFP) produce most consistent cross-platform discrepancies
- Systematic annual returns: 10-20% for sophisticated traders
- Complexity: 6/10

**3. Combinatorial/Logical Arbitrage**
- Exploit logical inconsistencies: "Trump wins" at 55% should be <= "Republican wins" at 50% -- impossible, therefore arbitrage
- Multi-leg positions across related contracts
- Warning: 62% of detected logical dependencies fail to profit due to liquidity asymmetry
- Only pursue when both legs have >$50K daily volume
- Complexity: 7/10

**4. Time Decay Harvesting**
- Binary contracts exhibit accelerating theta decay near expiry
- "Binary zone" begins at 3 DTE -- contracts become weighted coin flips
- Strategy: Sell at-the-money contracts ($0.45-$0.55) 3-7 days before expiry to harvest theta
- Risk: Gamma explosion -- small information can flip contract from 0.50 to 0.90 overnight
- Complexity: 4/10

**5. News-Information Edge**
- Prediction markets adjust slower than equity markets to news
- RSS-based news scanner (2-3 min polling) gives 10-60 minute edge window
- LLM analysis of headlines for market impact
- OPT model sentiment: Sharpe 3.05 on equity long-short (transferable to prediction markets)
- Complexity: 5/10

**6. Market Making (Spread Capture)**
- Place simultaneous YES and NO limit orders
- Capture 2-5 cent spreads on Polymarket, 3-8 cents on Kalshi
- Maintain near-neutral inventory (60/40 max imbalance)
- Cancel-replace every 500ms to maintain queue position
- Capital: $5K-$25K for meaningful returns
- Key risk: Inventory on wrong side at resolution (binary payoff = total loss)
- Complexity: 7/10

**7. Event Clustering**
- Multiple related events create correlated pricing opportunities
- Example: Fed meeting day -- trade the deviation from consensus across rate decision, dot plot interpretation, press conference tone
- Build portfolio of positions across the event cluster
- Hedge correlated legs against each other
- Complexity: 6/10

### How Market Makers on Polymarket/Kalshi Operate

**Infrastructure:**
- Polymarket: CLOB with EIP-712 signing, REST + WebSocket API, 0% maker + rebates
- Kalshi: CFTC-regulated, REST + WebSocket + FIX protocol, 0% current (promotional)

**Bot architecture:**
```
1. Market data ingestion (WebSocket stream + order book snapshots)
2. Fair value estimation (proprietary model or external signals)
3. Quote generation (fair value +/- half spread)
4. Inventory management (track YES/NO balance, skew quotes toward balanced)
5. Risk controls (max position size, max inventory imbalance, circuit breakers)
6. Cancel-replace cycle (500ms interval)
7. P&L tracking and position management
```

**Key parameters:**
```
MIN_SPREAD: 3 cents (Polymarket), 5 cents (Kalshi)
QUOTE_SIZE: $50-$500 per level
MAX_INVENTORY: $5,000 per market
INVENTORY_SKEW: 1 cent per $500 inventory imbalance
CANCEL_REPLACE_MS: 500
MAX_MARKETS_SIMULTANEOUS: 10-50
CIRCUIT_BREAKER: pause if P&L drops >$500 in 1 hour
```

**Revenue model:** Professional Polymarket market makers target $50-$500/day across 10-50 markets with $10K-$25K capital deployed.

---

## Appendix: Strategy Comparison Matrix

| # | Strategy | Sharpe | Capital | Complexity | Pred Market? |
|---|----------|--------|---------|------------|-------------|
| 1 | Pairs Trading (Kalman Filter) | 1.0-2.5 | $50K+ | 6/10 | Partial |
| 2 | Prediction Market Making | 0.5-1.5 | $5-25K | 7/10 | YES |
| 3 | Mean Reversion (RSI2 + BB) | 1.5-3.0 | $10K+ | 4/10 | YES |
| 4 | LLM News Sentiment | 1.5-3.0 | $10K+ | 5/10 | YES |
| 5 | Favorite-Longshot Bias | 0.8-1.5 | $5K+ | 2/10 | YES |
| 6 | Cross-Platform Arbitrage | N/A (risk-free) | $10K+ | 6/10 | YES |
| 7 | Funding Rate Arb (Crypto) | N/A (yield) | $10K+ | 5/10 | NO |
| 8 | XGBoost Factor Model | 1.5-2.5 | $100K+ | 6/10 | Partial |
| 9 | Gamma Scalping / Vol Arb | 2.0-5.0 | $1M+ | 9/10 | NO |
| 10 | Barbell Portfolio (Pred Mkt) | 0.8-1.5 | $5K+ | 4/10 | YES |
| 11 | HMM Regime Detection | Improves base strategy by 0.3-0.5 | N/A | 7/10 | YES |
| 12 | DeFi Yield Stacking | N/A (8-20% APY) | $5K+ | 6/10 | NO |
| 13 | On-Chain Whale Tracking | Variable | $5K+ | 5/10 | Partial |
| 14 | Satellite / Alt Data | +3% annual alpha | $10K/yr data | 7/10 | Partial |
| 15 | RL Execution Optimization | Improves execution 5-15% | N/A | 9/10 | YES |

---

## Sources

### Statistical Arbitrage & Pairs Trading
- [7 Innovative Pairs Trading Strategies for 2025](https://chartswatcher.com/pages/blog/7-innovative-pairs-trading-strategies-for-2025)
- [Cointegration-based Pairs Trading: ETFs (Springer, 2025)](https://link.springer.com/article/10.1057/s41260-025-00416-0)
- [Multivariate Cointegration in Statistical Arbitrage (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4906546)
- [Advanced Statistical Arbitrage with Reinforcement Learning (arXiv)](https://arxiv.org/html/2403.12180v1)
- [Pairs Trading — Hudson & Thames](https://hudsonthames.org/definitive-guide-to-pairs-trading/)
- [Examining Pairs Trading Profitability (Yale, 2024)](https://economics.yale.edu/sites/default/files/2024-05/Zhu_Pairs_Trading.pdf)
- [Kalman Filter for Pairs Trading](https://kalman-filter.com/pairs-trading/)
- [Dynamic Hedge Ratio Using Kalman Filter (QuantStart)](https://www.quantstart.com/articles/Dynamic-Hedge-Ratio-Between-ETF-Pairs-Using-the-Kalman-Filter/)
- [Renaissance Technologies: $100B on Statistical Arbitrage](https://navnoorbawa.substack.com/p/renaissance-technologies-the-100)
- [How Jim Simons Achieved 66% Annual Returns](https://www.quantifiedstrategies.com/jim-simons/)
- [Medallion Fund: The Ultimate Counterexample (Cornell Capital)](https://www.cornell-capital.com/blog/2020/02/medallion-fund-the-ultimate-counterexample.html)

### Market Microstructure
- [Deep Limit Order Book Forecasting (arXiv)](https://arxiv.org/html/2403.09267v4)
- [Market Microstructure: Order Flow and Level 2 Analysis](https://pocketoption.com/blog/en/knowledge-base/learning/market-microstructure/)
- [VPIN: Volume-Synchronized Probability of Informed Trading](https://www.quantresearch.org/VPIN.pdf)
- [From PIN to VPIN: Order Flow Toxicity](https://www.quantresearch.org/From%20PIN%20to%20VPIN.pdf)
- [Retail Limit Orders (Microstructure Exchange, 2025)](https://microstructure.exchange/papers/Retail%20Limit%20Orders%2004082025.pdf)

### Momentum & Mean Reversion
- [Mean-Reversion and Momentum Regime Switching (Price Action Lab)](https://www.priceactionlab.com/Blog/2024/01/mean-reversion-and-momentum-regime-switching/)
- [Slow Momentum with Fast Reversion (PM-Research)](https://www.pm-research.com/content/iijjfds/4/1/111)
- [Mean Reversion Technique Comparison for S&P 500 and Nasdaq](https://canomi.com/mean-reversion-technique-comparison-for-sp-500-and-nasdaq/)
- [Market Regime Detection Using HMMs (QuantStart)](https://www.quantstart.com/articles/market-regime-detection-using-hidden-markov-models-in-qstrader/)
- [Multi-Model Ensemble HMM Voting Framework (2025)](https://www.aimspress.com/article/id/69045d2fba35de34708adb5d)
- [Regime Detection: From HMMs to Wasserstein Clustering](https://medium.com/hikmah-techstack/market-regime-detection-from-hidden-markov-models-to-wasserstein-clustering-6ba0a09559dc)

### News/Event-Driven Alpha
- [Point72's Multi-Pod Engine: 19% Returns](https://medium.com/@navnoorbawa/point72s-multi-pod-engine-how-41-5b-delivers-19-returns-through-systematic-diversification-c174e4e25404)
- [How Hedge Funds Use ML for Derivatives Pricing](https://navnoorbawa.substack.com/p/how-hedge-funds-use-machine-learning)
- [NLP and Sentiments Analysis Based Trading Strategy (GitHub)](https://github.com/tatsath/fin-ml/blob/master/Chapter%2010%20-%20Natural%20Language%20Processing/Case%20Study%201%20-%20NLP%20and%20Sentiments%20Analysis%20based%20Trading%20Strategy/NLPandSentimentAnalysisBasedTradingStrategy.ipynb)
- [Sentiment Trading with LLMs (arXiv:2412.19245)](https://arxiv.org/abs/2412.19245)
- [The New Quant: Survey of LLMs in Financial Prediction](https://arxiv.org/html/2510.05533v1)
- [LLMs in Equity Markets: Applications and Insights (Frontiers, 2025)](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1608365/full)
- [News Alpha: Predicting Post-Earnings Drift (LSEG)](https://www.lseg.com/en/insights/data-analytics/news-alpha-predicting-post-earnings-drift-us-markets)
- [How to Improve PEAD with NLP Analysis (QuantPedia)](https://quantpedia.com/how-to-improve-post-earnings-announcement-drift-with-nlp-analysis/)

### Machine Learning
- [ML Enhanced Multi-Factor Quantitative Trading (arXiv, 2025)](https://arxiv.org/html/2507.07107)
- [Insider Purchase Signals: Gradient Boosting Detection (arXiv)](https://arxiv.org/html/2602.06198)
- [Boosting for Trading (Machine Learning for Trading)](https://stefan-jansen.github.io/machine-learning-for-trading/12_gradient_boosting_machines/)
- [Predicting EPS Using Feature-Engineered XGBoost (Springer)](https://link.springer.com/article/10.1007/s41870-023-01450-0)
- [Transformer Based Time-Series Forecasting for Stock (arXiv, 2025)](https://arxiv.org/html/2502.09625v1)
- [Galformer: Transformer with Generative Decoding (Nature, 2024)](https://www.nature.com/articles/s41598-024-72045-3)
- [RL in Financial Decision Making: Systematic Review (arXiv)](https://arxiv.org/html/2512.10913v1)
- [Optimal Execution with RL in Multi-Agent Market (arXiv, 2024)](https://arxiv.org/html/2411.06389v2)

### Options/Derivatives & Binary Outcomes
- [Analytical Modeling of Binary Options Strategies (MDPI)](https://www.mdpi.com/1999-5903/14/7/208)
- [Application of Kelly Criterion to Prediction Markets (arXiv:2412.14144)](https://arxiv.org/html/2412.14144v1)
- [The Math of Prediction Markets: Binary Options, Kelly (Substack)](https://navnoorbawa.substack.com/p/the-math-of-prediction-markets-binary)
- [Gamma Scalping: How Institutional Traders Exploit Expiration](https://navnoorbawa.substack.com/p/how-institutional-traders-exploit)
- [Dispersion Trading: Assenagon Strategy (Hedge Fund Journal)](https://thehedgefundjournal.com/assenagon-long-short-volatility-strategy-equity/)
- [7 Advanced Volatility Trading Strategies for 2025](https://chartswatcher.com/pages/blog/7-advanced-volatility-trading-strategies-for-2025)

### Crypto Strategies
- [Cross-Exchange Funding Rate Arbitrage via Pendle Boros](https://medium.com/boros-fi/cross-exchange-funding-rate-arbitrage-a-fixed-yield-strategy-through-boros-c9e828b61215)
- [What is Funding Rate Arbitrage? (CoinGlass)](https://www.coinglass.com/learn/what-is-funding-rate-arbitrage)
- [Ultimate Guide to Funding Rate Arbitrage (Amberdata)](https://blog.amberdata.io/the-ultimate-guide-to-funding-rate-arbitrage-amberdata)
- [Restaking Revolution: EigenLayer and DeFi Yields 2025 (QuickNode)](https://blog.quicknode.com/restaking-revolution-eigenlayer-defi-yields-2025/)
- [Best DeFi Staking Platforms 2026 (Coin Bureau)](https://coinbureau.com/analysis/best-defi-staking-platforms)
- [MEV: Maximal Extractable Value Guide (Arkham)](https://info.arkm.com/research/beginners-guide-to-mev)
- [MEV: Ethereum.org](https://ethereum.org/developers/docs/mev)
- [SoK: Market Microstructure for Decentralized Prediction Markets (arXiv)](https://arxiv.org/pdf/2510.15612)

### Risk Management
- [Sizing the Risk: Kelly, VIX, and Hybrid Approaches (arXiv, 2025)](https://arxiv.org/html/2508.16598v1)
- [Risk-Constrained Kelly Criterion (QuantInsti)](https://blog.quantinsti.com/risk-constrained-kelly-criterion/)
- [Position Sizing Strategies for Algo-Traders](https://medium.com/@jpolec_72972/position-sizing-strategies-for-algo-traders-a-comprehensive-guide-c9a8fc2443c8)
- [Tail Risk Hedging Toolkit (Goldman Sachs, 2024)](https://am.gs.com/en-us/institutions/insights/article/2024/tail-risk-hedging-toolkit)
- [Trend-Following and Tail Risk Hedging Overlays (2025)](https://www.tandfonline.com/doi/full/10.1080/10293523.2025.2553254)
- [The Power of Tail Risk (Hedge Fund Journal)](https://thehedgefundjournal.com/the-power-of-tail-risk/)
- [LMR Partners: 15 Years of Multi-Strategy Alpha (HFJ)](https://thehedgefundjournal.com/lmr-partners-differentiated-multi-strategy-alpha-hedge-fund/)

### Alternative Data
- [Alternative Data Industry Growth (Integrity Research)](https://www.integrity-research.com/the-explosive-growth-of-the-alternative-data-industry-trends-drivers-and-revenue-forecasts-through-2028/)
- [5 Best Alt Data Sources for Hedge Funds (ExtractAlpha)](https://extractalpha.com/2025/07/07/5-best-alternative-data-sources-for-hedge-funds/)
- [Reimagining Alpha with Data and AI (BlackRock)](https://www.blackrock.com/us/financial-professionals/insights/data-driven-investing)
- [Satellite Imagery in Quantitative Trading (BlueChipAlgos)](https://bluechipalgos.com/blog/satellite-imagery-and-its-applications-in-quantitative-trading/)
- [Institutional Trading and Satellite Data (ScienceDirect)](https://www.sciencedirect.com/science/article/pii/S1544612324013709)
- [Advanced On-Chain Analytics (Amberdata)](https://blog.amberdata.io/advanced-on-chain-analytics-for-crypto-trading)
- [Top Crypto Analytics Platforms 2025 (Nansen)](https://www.nansen.ai/post/top-crypto-analytics-platforms-2025)

### Prediction Market Specific
- [Systematic Edges in Prediction Markets (QuantPedia)](https://quantpedia.com/systematic-edges-in-prediction-markets/)
- [How Kalshi and Polymarket Traders Make Money (NPR)](https://www.npr.org/2026/01/17/nx-s1-5672615/kalshi-polymarket-prediction-market-boom-traders-slang-glossary)
- [Market Making on Prediction Markets: 2026 Guide](https://newyorkcityservers.com/blog/prediction-market-making-guide)
- [4 Polymarket Strategies Bots Actually Profit From (2026)](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)
- [Prediction Market Arbitrage Guide 2026](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide)
- [Polymarket & Kalshi Arbitrage Opportunities](https://laikalabs.ai/prediction-markets/polymarket-kalshi-arbitrage-guide)
- [Polymarket vs Kalshi: Liquidity, Regulation, Strategies](https://www.quantvps.com/blog/polymarket-vs-kalshi-explained)
- [Prediction Markets Explode in 2025 (The Block)](https://www.theblock.co/post/383733/prediction-markets-kalshi-polymarket-duopoly-2025)
- [Semantic Trading: Agentic AI for Prediction Markets (arXiv)](https://arxiv.org/html/2512.02436v1)
- [Automated Market Making on Polymarket (Official Blog)](https://news.polymarket.com/p/automated-market-making-on-polymarket)
- [Kalshi API Documentation](https://docs.kalshi.com/welcome)
