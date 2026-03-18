import requests
import time
import hashlib
import hmac
import json

class KalshiExecutor:
    def __init__(self, access_key, secret_key):
        self.access_key = access_key
        self.secret_key = secret_key
        self.base_url = 'https://api.kalshi.io'

    def _generate_signature(self, method, path, body):
        timestamp = int(time.time() * 1000)
        message = f'{method}{path}{timestamp}{json.dumps(body)}'
        signature = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {
            'KALSHI-ACCESS-KEY': self.access_key,
            'KALSHI-SIGNATURE': signature,
            'KALSHI-TIMESTAMP': str(timestamp)
        }

    def buy_yes(self, ticker, contracts, limit_price):
        path = '/portfolio/orders'
        body = {
            'market': ticker,
            'side': 'YES',
            'type': 'LIMIT',
            'quantity': contracts,
            'price': limit_price
        }
        headers = self._generate_signature('POST', path, body)
        response = requests.post(self.base_url + path, json=body, headers=headers)
        return response.json()

    def buy_no(self, ticker, contracts, limit_price):
        path = '/portfolio/orders'
        body = {
            'market': ticker,
            'side': 'NO',
            'type': 'LIMIT',
            'quantity': contracts,
            'price': limit_price
        }
        headers = self._generate_signature('POST', path, body)
        response = requests.post(self.base_url + path, json=body, headers=headers)
        return response.json()

    def get_balance(self):
        path = '/portfolio/balances'
        headers = self._generate_signature('GET', path, {})
        response = requests.get(self.base_url + path, headers=headers)
        return response.json()

    def get_positions(self):
        path = '/portfolio/positions'
        headers = self._generate_signature('GET', path, {})
        response = requests.get(self.base_url + path, headers=headers)
        return response.json()
