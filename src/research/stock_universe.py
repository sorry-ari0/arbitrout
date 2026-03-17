import pandas as pd
import requests

# Assuming we have obtained the list of HKEX stocks through an API
def get_hkex_universe():
    url = "https://api.example.com/hkex_stocks"  # Replace with the actual API endpoint
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception("Failed to fetch HKEX stocks")

def map_hkex_tickers_to_company_names(tickers):
    hkex_stocks = get_hkex_universe()
    company_names = {}
    for ticker, company_name in hkex_stocks.items():
        if ticker in tickers:
            company_names[ticker] = company_name
    return company_names

def get_universe(exchange=None):
    universe = []
    if exchange == "HKEX":
        universe = get_hkex_universe()
    # Add other exchanges here
    return universe
