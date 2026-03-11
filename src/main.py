# Python Backend Code

## Module 1: The Swarm Engine (Thesis Generation & Screening)

### Intent Parser Agent
```python
def intent_parser(prompt):
    # LLM function to extract quantitative/qualitative rules into a JSON schema
    pass
```

### Swarm Evaluator
```python
def swarm_evaluator(rules, universe):
    # Function to filter mock universe of 50 stocks down to matching basket of tickers
    pass
```

## Module 2: The Backtesting Engine

### Data Fetching
```python
def fetch_historical_data(tickers):
    # Accept a list of tickers and fetch 1-year historical adjusted close prices using yfinance
    pass
```

### Risk & Return Metrics
```python
def calculate_metrics(data, benchmark):
    # Calculate equal-weighted portfolio's 1-year return, annualized volatility, and maximum drawdown
    pass
```

## Module 3: Execution & Portfolio Management (Direct Indexing)

### Deployment Endpoint
```python
def deploy_portfolio(basket, amount):
    # Takes the backtested basket and a dollar amount, calculating exact fractional share allocations for each ticker
    pass
```

### Tax-Loss Harvesting Logic
```python
def tlh_logic(portfolio):
    # Function to scan user's portfolio, log mock 'SELL' orders if any position is down more than 10% from purchase price,
    # and mock 'BUY' orders for correlated proxy tickers
    pass
```

### Automated Rebalancing Logic
```python
def rebalance_portfolio(portfolio):
    # Function to trigger quarterly, re-run user's original natural language prompt through Module 1 Swarm Engine,
    # calculate diff (buys/sells) needed to align current portfolio with new AI-generated basket
    pass
```

# API Endpoints and Core Business Logic for Each Module