import json
import requests

def get_universe(exchange=None, cap_tier=None):
    try:
        with open('data/us_stock_universe.json') as f:
            universe = json.load(f)
    except FileNotFoundError:
        universe = fetch_universe()
        with open('data/us_stock_universe.json', 'w') as f:
            json.dump(universe, f)

    if exchange:
        universe = [u for u in universe if u['exchange'] == exchange]
    if cap_tier:
        universe = [u for u in universe if u['market_cap_tier'] == cap_tier]
    return universe

def fetch_universe():
    url = 'https://www.sec.gov/files/company_tickers.json'
    response = requests.get(url)
    data = response.json()
    universe = []
    for item in data['tickers']:
        ticker = item['ticker']
        company_name = item['title']
        exchange = item['exchange']
        market_cap = item.get('marketCap', 0)
        if market_cap < 300000000:
            market_cap_tier = 'small'
        elif market_cap < 2000000000:
            market_cap_tier = 'mid'
        elif market_cap < 10000000000:
            market_cap_tier = 'large'
        else:
            market_cap_tier = 'mega'
        universe.append({
            'ticker': ticker,
            'company_name': company_name,
            'exchange': exchange,
            'market_cap_tier': market_cap_tier
        })
    return universe
