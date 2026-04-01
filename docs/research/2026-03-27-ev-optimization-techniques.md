# EV Optimization Techniques for Prediction Market Trading

Research Date: 2026-03-27
Focus: Concrete formulas, implementable techniques, and open-source references

---

## 1. Core Expected Value Formulas

### Basic EV for Binary Contracts

For a YES contract paying $1.00 on resolution:

```
EV = p_true - market_price - fees
```

Where:
- `p_true` = your estimated true probability of the outcome
- `market_price` = current YES share price ($0.00-$1.00)
- `fees` = all-in cost (trading fees + spread impact + settlement)

For a NO contract:
```
EV_no = (1 - p_true) - (1 - market_price) - fees
     = market_price - p_true - fees
```

### Fee-Adjusted EV by Platform

**Polymarket (post-March 2026 fee changes):**
- Maker: 0% (all categories)
- Taker: 0-2% depending on category (politics/finance up to 1%)
- Fee peaks at 50% probability, declines toward 0% or 100%
- Maker rebates redistribute taker fees daily
- Use GTC limit orders (maker) to avoid taker fees entirely

```
EV_polymarket_maker = p_true - market_price    # 0% maker fee
EV_polymarket_taker = p_true - market_price - (taker_rate * market_price * (1 - market_price))
```

**Kalshi:**
```
fee = ceil(0.07 * contracts * price * (1 - price))
```
Ranges from ~0.6% for tail events to 1.75% at $0.50.

### Minimum Edge Threshold

Given Polymarket's fee structure requires 2.5-3% spreads for taker arbitrage to be viable:
- **Maker trades**: minimum edge ~0% (just need positive EV)
- **Taker trades**: minimum edge ~2-3% depending on price level
- **Cross-platform arb**: minimum edge ~3-5% after both platforms' fees

---

## 2. Kelly Criterion for Prediction Markets

### Full Kelly Formula

```
f* = (b * p - q) / b

where:
  p = true probability (your estimate)
  q = 1 - p
  b = net odds = (1 - market_price) / market_price
```

Equivalent simplified form for binary contracts:
```
f* = (p_true - market_price) / (1 - market_price)
```

### Worked Example

Market prices a Fed rate cut at $0.60. Your model says 75% probability.

```
b = (1 - 0.60) / 0.60 = 0.667
f* = (0.667 * 0.75 - 0.25) / 0.667 = 0.375
```

Kelly recommends 37.5% of bankroll in YES contracts. Edge = $0.15 per share (25% ROI if correct).

### Fractional Kelly (CRITICAL for Real Trading)

Full Kelly has a 33% probability of ruin before doubling capital. Use fractional Kelly:

| Alpha | Growth Rate (vs Full) | Variance (vs Full) | Ruin Probability |
|-------|----------------------|--------------------|----|
| 1.0 (Full) | 100% | 100% | 33% |
| 0.5 (Half) | ~75% | 25% | 11% |
| 0.25 (Quarter) | ~50% | 6.25% | <3% |

**Industry standard: Quarter Kelly (alpha = 0.25)**

```
actual_position = alpha * f* * bankroll
```

### Brier-Tiered Adaptive Alpha

The most sophisticated approach ties Kelly fraction to model calibration:

```python
def get_alpha(brier_score):
    """Map model accuracy to Kelly fraction"""
    if brier_score < 0.10:    # Excellent calibration
        return 0.40
    elif brier_score < 0.15:  # Good
        return 0.30
    elif brier_score < 0.20:  # Decent
        return 0.20
    elif brier_score < 0.30:  # Poor
        return 0.15
    else:                     # Unreliable
        return 0.10
```

Never exceed 0.40 (40% of full Kelly) even at peak accuracy.

### Kelly with Bankroll-Relative Sizing

```python
def kelly_position_size(p_true, market_price, bankroll, alpha=0.25, max_pct=0.10):
    """
    Calculate fee-adjusted Kelly position size.
    max_pct: hard cap as fraction of bankroll (e.g., 0.10 = 10%)
    """
    if p_true <= market_price:
        return 0  # No edge

    edge = p_true - market_price
    f_star = edge / (1 - market_price)
    fractional = alpha * f_star

    position = min(fractional, max_pct) * bankroll
    return position
```

---

## 3. Market Microstructure

### Order Book Imbalance (OBI)

```
OBI = (Q_bid - Q_ask) / (Q_bid + Q_ask)
```

Where Q_bid = total quantity at best bid, Q_ask = total quantity at best ask.

**Empirical findings:**
- OBI explains ~65% of short-interval price variance (R^2 = 0.65)
- Trade imbalance alone: R^2 = 0.32
- OBI subsumes trade information entirely

### Volume-Adjusted Mid Price (VAMP)

Better than simple midpoint for thin order books:

```
VAMP = (P_bid * Q_ask + P_ask * Q_bid) / (Q_bid + Q_ask)
```

### OBI-Based Momentum Strategy

```python
def obi_signal(order_book):
    q_bid = sum(level.qty for level in order_book.bids[:3])
    q_ask = sum(level.qty for level in order_book.asks[:3])
    obi = (q_bid - q_ask) / (q_bid + q_ask)

    if obi > 0.65:
        return "BUY"   # Strong bid pressure
    elif obi < -0.65:
        return "SELL"   # Strong ask pressure
    return "HOLD"
```

Prediction accuracy: 58% vs 50% random baseline over 15-30 minute windows.

### Bid-Ask Spread Impact on EV

```
effective_cost = market_price + (spread / 2)     # for market buy
effective_cost = market_price - (spread / 2)     # for market sell

adjusted_EV = p_true - effective_cost - fees
```

Polymarket average spread: ~1.2% (2025), down from ~4.5% (2023).

### Time-to-Expiry Effects

**Information incorporation speed by category:**
- Sports (in-game): <5 minutes
- Politics: 15-60 minutes
- Economics: 30-180 minutes

**Time-decay position adjustment (theta analog):**
```
position_size(t) = initial_position * sqrt(T_remaining / T_initial)
```

This reduces exposure as binary gamma increases near expiration:
- 30 days out: $10,000 (full)
- 7 days out: $4,830 (48%)
- 1 day out: $1,826 (18%)

### Calendar Spread Strategy

When the same event has multiple expiry dates:
```
Sell near-term (faster theta decay) + Buy far-term (slower decay)
Net position harvests the theta differential
```

Near-term contracts are systematically overpriced relative to back months when the probability differential is small.

---

## 4. Calibration Techniques

### Brier Score

```
Brier = (1/N) * sum((p_forecast - outcome)^2)
```

Where outcome = 1 if event occurred, 0 otherwise. Lower = better. Perfect = 0.0, random = 0.25.

### Logarithmic Scoring Rule

```
LogScore = x * ln(p) + (1 - x) * ln(1 - p)
```

Where x = 1 if event occurred, 0 otherwise. More sensitive to confident wrong predictions.

### Calibration Check

For a well-calibrated model, events predicted at probability p should occur p% of the time:

```python
def calibration_check(predictions, outcomes, n_bins=10):
    """
    predictions: list of probability estimates
    outcomes: list of 0/1 actual outcomes
    """
    bins = np.linspace(0, 1, n_bins + 1)
    calibration = []
    for i in range(n_bins):
        mask = (predictions >= bins[i]) & (predictions < bins[i+1])
        if mask.sum() > 0:
            predicted_avg = predictions[mask].mean()
            actual_avg = outcomes[mask].mean()
            calibration.append({
                'bin': f'{bins[i]:.1f}-{bins[i+1]:.1f}',
                'predicted': predicted_avg,
                'actual': actual_avg,
                'gap': abs(predicted_avg - actual_avg),
                'count': mask.sum()
            })
    return calibration
```

### Bayesian Model Aggregation

When combining multiple probability sources:

```
P_posterior = (w1*P_polls + w2*P_fundamentals + w3*P_market) / sum(w_i)
```

Optimize weights via Brier score minimization on historical data.

### Multi-Model Ensemble (ilovecircle approach)

The $2.2M Polymarket trader used:
- GPT-4o: 40% weight
- Claude 3.5 Sonnet: 35% weight
- Gemini 1.5 Pro: 25% weight

Aggregation: trimmed mean, median, or weighted average. Track per-model Brier scores to shift weights toward best-performing model per category.

### Empirical Market Calibration

Research findings:
- Prediction markets are ~90% accurate 30 days before events
- ~94% accurate hours before resolution
- Slight overestimation bias: 2-3%
- Calibration degrades for extreme probabilities (>90% or <10%)
- Short time-to-expiry = better calibration; long-dated = significant bias

---

## 5. Favorite-Longshot Bias and Vig Extraction

### The Bias

High-probability outcomes (favorites) are systematically underpriced. Low-probability outcomes (longshots) are systematically overpriced. This creates a persistent, exploitable edge.

**Causes:**
1. Probability misperception: humans cannot distinguish well between small and tiny probabilities
2. Risk-love at small stakes: people treat tiny probabilities like lottery tickets
3. Limited arbitrage: small position limits prevent full correction

**Trading implication:** Systematically bet favorites, fade longshots.

### Devigging Methods

To extract true probabilities from market prices that include vig:

**Additive (Proportional) Method:**
```
p_true_i = p_implied_i / sum(p_implied_all)
```

Simple, works well for 2-way markets.

**Shin Method (best for 3+ outcomes):**
```
Iterative algorithm that corrects for favorite-longshot bias
by modeling insider trading effects.
```

The Shin method produces better calibration than additive/multiplicative methods because it explicitly accounts for information asymmetry. For 2-way markets, Shin = Additive. For 3+ way markets, Shin adjusts longshots down more and favorites up more.

**Power Method:**
```
p_true_i = p_implied_i^k / sum(p_implied_j^k for all j)
```

Where k is solved such that sum(p_true) = 1.0. Always keeps probabilities in [0,1] range.

### Practical Vig Calculation

```python
def calculate_vig(prices):
    """
    prices: list of YES prices for all outcomes in a market
    returns: vig as percentage overround
    """
    total = sum(prices)
    vig = total - 1.0
    return vig  # e.g., 0.05 = 5% overround

def devig_additive(prices):
    """Remove vig using proportional method"""
    total = sum(prices)
    return [p / total for p in prices]
```

---

## 6. Multi-Leg / Synthetic Derivative EV Calculation

### No-Arbitrage Constraint

For mutually exclusive outcomes:
```
sum(P_i) = 1.00
```

If sum < 1.00: buy all outcomes = guaranteed profit.
If sum > 1.00: sell (buy NO on) all outcomes = guaranteed profit (overround exploitation).

### Market Rebalancing Arbitrage

Within a single market where outcome prices don't sum to $1.00:

```python
def find_rebalancing_arb(yes_prices):
    """
    yes_prices: dict of {outcome: price} for all outcomes in a market
    """
    total = sum(yes_prices.values())
    if total < 1.0:
        cost = total
        guaranteed_payout = 1.0
        profit = 1.0 - total
        roi = profit / cost
        return {'type': 'buy_all', 'cost': cost, 'profit': profit, 'roi': roi}
    return None
```

Empirical: same-market arb on Polymarket averaged 0.5-2% returns, windows closing within 200ms.

### Combinatorial (Cross-Market) Arbitrage

Exploit logical dependencies between related markets:

```
If P(Trump wins) = 0.55 and P(Republican wins) = 0.50
Then logical inconsistency: P(Trump) > P(Republican) is impossible
since Trump winning implies Republican winning.

Trade: Buy YES Republican (0.50), Sell YES Trump (0.55)
Edge: $0.05 guaranteed if Trump wins, $0.50 if non-Trump Republican wins
```

### Synthetic Derivative Construction

For correlated events A and B:

```
P(A and B) = P(A) * P(B|A)
P(A or B) = P(A) + P(B) - P(A and B)

Synthetic long correlation = buy YES on both A and B
Synthetic short correlation = buy YES on A, buy NO on B
```

Fee-adjusted synthetic EV:
```
EV_synthetic = sum(leg_i.ev) - sum(leg_i.fees) - correlation_uncertainty_penalty
```

The correlation_uncertainty_penalty accounts for the fact that inter-market correlation is often unknown and must be estimated.

### Portfolio NO Strategy

For multi-outcome events (n >= 3) where sum(YES prices) > 1.0:

```python
def portfolio_no_ev(yes_prices, exclude_top_n=1):
    """
    Buy NO on all outcomes except the top N favorites.
    Profit comes from overround (sum > 1.0).
    """
    sorted_outcomes = sorted(yes_prices.items(), key=lambda x: -x[1])

    # Exclude top N favorites
    targets = sorted_outcomes[exclude_top_n:]

    # Cost to buy NO = (1 - YES_price) for each
    total_cost = sum(1 - price for _, price in targets)

    # Guaranteed payout: (n - exclude_top_n - 1) * $1.00
    # (all NOs pay $1 except the one that wins)
    n_targets = len(targets)
    guaranteed_min = (n_targets - 1) * 1.0  # worst case: one target wins

    profit = guaranteed_min - total_cost
    return {'cost': total_cost, 'min_payout': guaranteed_min, 'profit': profit}
```

### Research Findings

$40M+ in arbitrage profits were extracted from Polymarket between April 2024 and April 2025 across 86 million bets:
- 60% from market rebalancing (intra-market)
- 40% from combinatorial arbitrage (cross-market)

---

## 7. Open-Source Trading Bots with EV Code

### 1. Polymarket Official Agents
- **Repo:** github.com/Polymarket/agents
- **Features:** LLM-based probability estimation, Polymarket API integration, trade execution
- **Language:** Python

### 2. Fully-Autonomous Polymarket AI Trading Bot (dylanpersonguy)
- **Repo:** github.com/dylanpersonguy/Fully-Autonomous-Polymarket-AI-Trading-Bot
- **Features:** Multi-model ensemble (GPT-4o 40%, Claude 35%, Gemini 25%), 15+ risk checks, fractional Kelly sizing, whale tracking, 9-tab dashboard
- **Language:** TypeScript
- **Modes:** Paper and live trading

### 3. Polymarket Bot (joicodev)
- **Docs:** mintlify.com/joicodev/polymarket-bot/risk/kelly-criterion
- **Features:** Brier-tiered adaptive alpha (0.10-0.40), Kelly criterion with dynamic confidence adjustment
- **Key detail:** Maps model accuracy to Kelly fraction, never exceeds 40% of full Kelly

### 4. OctoBot Prediction Market
- **Repo:** github.com/Drakkar-Software/OctoBot-Prediction-Market
- **Features:** Copy trading, arbitrage automation
- **Language:** Python, built on OctoBot framework

### 5. Poly-Maker (Market Making Bot)
- **Repo:** github.com/warproxxx/poly-maker
- **Features:** Dual-sided liquidity, configurable spread parameters via Google Sheets
- **Language:** Python

### 6. Polymarket Trading Bot (dylanpersonguy, 53K+ lines)
- **Repo:** github.com/dylanpersonguy/Polymarket-Trading-Bot
- **Features:** 7 concurrent strategies (arbitrage, convergence, market making, momentum, AI forecast), whale tracker with copy-trade simulator
- **Language:** TypeScript

### 7. Weather Trading Bot (suislanchez)
- **Repo:** github.com/suislanchez/polymarket-kalshi-weather-bot
- **Features:** GFS 31-member ensemble forecasts + BTC microstructure signals, Kelly criterion sizing, signal calibration
- **Highest profits:** $1.8K reported

### 8. Prediction Market Arbitrage Bot (realfishsam)
- **Repo:** github.com/realfishsam/prediction-market-arbitrage-bot
- **Features:** Cross-platform arb between Polymarket and Kalshi, auto buy-low/sell-high

### 9. NavnoorBawa Polymarket Prediction System
- **Repo:** github.com/NavnoorBawa/polymarket-prediction-system
- **Features:** Quantitative prediction system with Kelly criterion and edge detection

---

## 8. Academic Papers on Prediction Market Pricing Efficiency

### Foundational Papers

1. **Wolfers & Zitzewitz (2006)** - "Interpreting Prediction Market Prices as Probabilities" (NBER Working Paper 12200)
   - Under risk-neutrality, equilibrium price = quantile of belief distribution
   - Under reasonable risk-aversion, prices approximate wealth-weighted mean belief
   - URL: nber.org/papers/w12200

2. **Page & Clemen (2013)** - "Do Prediction Markets Produce Well-Calibrated Probability Forecasts?"
   - Markets are well-calibrated when time-to-expiry is short
   - Significant bias for long-dated events
   - Political markets show strong longshot bias
   - URL: people.duke.edu/~clemen/bio/Published%20Papers/45.PredictionMarkets-Page&Clemen-EJ-2013.pdf

3. **Manski (2006)** - "Interpreting the Predictions of Prediction Markets"
   - Challenged the assumption that market prices equal probabilities
   - Showed prices can diverge from mean beliefs under heterogeneous risk preferences

### Favorite-Longshot Bias

4. **NBER Working Paper 15923** - "Explaining the Favorite-Longshot Bias"
   - Favorite-longshot bias documented across prediction and betting markets
   - URL: nber.org/system/files/working_papers/w15923/w15923.pdf

5. **Whelan (2024)** - "Risk Aversion and Favourite-Longshot Bias in a Competitive Fixed-Odds Betting Market" (Economica)
   - Bias due to misperception of probabilities rather than risk-love
   - URL: karlwhelan.com/Papers/EconomicaFinal.pdf

6. **Management Science (2023)** - "The Longshot Bias Is a Context Effect"
   - Bias varies depending on context and market structure
   - URL: pubsonline.informs.org/doi/10.1287/mnsc.2023.4684

### Market Microstructure

7. **IMDEA Networks (2025)** - "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets"
   - Documented $40M+ arbitrage profits on Polymarket (Apr 2024-Apr 2025)
   - Two types: market rebalancing (60%) and combinatorial (40%)
   - URL: arxiv.org/abs/2508.03474

8. **Clinton & Huang (2025)** - Semantic Non-Fungibility and Law of One Price Violations
   - Identical contracts diverge significantly across platforms
   - URL: arxiv.org/html/2601.01706v1

### Options Pricing for Prediction Markets

9. **"Toward Black-Scholes for Prediction Markets" (2025)** - Unified kernel for event-linked derivatives
   - Introduces belief-volatility, jump intensity, correlation swaps
   - Martingale property in probability space
   - Future direction: multi-event joint calibration
   - URL: arxiv.org/pdf/2510.15205

10. **"The Anatomy of Polymarket: Evidence from the 2024 Presidential Election" (2026)**
    - Kyle's lambda declined by >10x as volume grew (improved efficiency)
    - Wash trading estimated at 20-60% of volume in some periods
    - URL: arxiv.org/html/2603.03136v1

---

## 9. Systematic Return Expectations (Benchmarks)

From professional prediction market quant trading:

| Metric | Target Range |
|--------|-------------|
| Annual Return | 15-25% |
| Sharpe Ratio | 2.0-2.8 |
| Win Rate | 52-58% |
| Average Edge per Trade | 2-4% |
| Maximum Drawdown | 12-18% |
| Capital Utilization | 40-60% |

### Revenue Composition
- Probability estimation edge: 60%
- Arbitrage capture: 25%
- Microstructure/momentum: 15%

### Portfolio Allocation by Category
- Politics: 30%
- Sports: 30%
- Economics: 25%
- Entertainment: 15%

---

## 10. Implementation Priorities for Arbitrout

Based on this research, the highest-impact improvements to Arbitrout's EV calculations:

1. **Replace flat edge threshold with fee-adjusted EV**: Currently uses MIN_SPREAD 8%. Should use `EV = p_true - market_price - fee_rate * market_price * (1 - market_price)` with platform-specific fee schedules.

2. **Implement adaptive fractional Kelly**: Replace fixed position sizing with Brier-tiered alpha (0.10-0.40) that adjusts based on model accuracy per category.

3. **Add OBI (Order Book Imbalance) signal**: Use `(Q_bid - Q_ask) / (Q_bid + Q_ask)` as a directional multiplier. R^2 = 0.65 for short-term price prediction. Already have CLOB WebSocket -- just need to compute OBI from it.

4. **Time-decay position reduction**: Scale down position size as `sqrt(T_remaining / T_initial)` to manage increasing gamma near expiration.

5. **Devig multi-outcome markets**: Apply Shin method instead of raw price sums for portfolio NO strategy and multi-outcome arb.

6. **Track Brier score per strategy/source**: Enable closed-loop calibration. Weight insider signals, news scanner, and arb scanner by their historical accuracy.

7. **Calendar spread strategy**: For Polymarket series markets with multiple expiry dates, harvest theta differential between near and far contracts.

8. **Combinatorial arbitrage detection**: Beyond simple sum-to-one checks, detect logical dependencies between related markets (e.g., candidate vs party markets) where conditional probability constraints are violated.
